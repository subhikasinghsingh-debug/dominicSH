import csv
import io
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from app import SchoolHandler
from demo_data import connect, seed_demo


class AttendanceReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp_dir.name) / "school.sqlite3"
        db = connect(cls.database)
        credentials = seed_demo(db)
        db.close()
        cls.credentials = {item["role"]: item for item in credentials}
        SchoolHandler.database = cls.database
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SchoolHandler)
        cls.server.daemon_threads = True
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        )
        cls.server_thread.start()
        cls.clients = {}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        cls.temp_dir.cleanup()

    def client(self, role):
        if role not in self.clients:
            credential = self.credentials[role]
            opener = build_opener(
                ProxyHandler({}), HTTPCookieProcessor(CookieJar())
            )
            login = urlencode({
                "email": credential["email"],
                "password": credential["password"],
            }).encode()
            opener.open(Request(self.base_url + "/login", data=login), timeout=10)
            self.clients[role] = opener
        return self.clients[role]

    def request(self, role, path):
        opener = self.client(role)
        try:
            response = opener.open(self.base_url + path, timeout=15)
            return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def test_student_report_counts_only_recorded_days_and_csv_escapes_values(self):
        student_id = "demo-student-0001"
        db = connect(self.database)
        original_name = db.execute(
            "SELECT full_name FROM students WHERE student_id=?", (student_id,),
        ).fetchone()[0]
        db.execute(
            "UPDATE students SET full_name=? WHERE student_id=?",
            ('Aarav, "Test"', student_id),
        )
        sample_dates = (
            ("2099-01-10", "Present"),
            ("2099-01-11", "Absent"),
            ("2099-01-12", "Leave"),
            ("2099-01-13", "Late"),
        )
        db.executemany(
            "INSERT INTO attendance VALUES (?,?,?,?,0)",
            [
                (f"report-test-{index}", student_id, attendance_date, status)
                for index, (attendance_date, status) in enumerate(sample_dates)
            ],
        )
        db.commit()
        db.close()
        try:
            query = urlencode({
                "q": "Test",
                "start_date": "2099-01-10",
                "end_date": "2099-01-13",
            })
            status, content, headers = self.request(
                "Director", "/export/attendance/student-wise?" + query,
            )
            self.assertEqual(status, 200)
            self.assertIn("attendance-student-wise.csv", headers["Content-Disposition"])
            records = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
            self.assertEqual(records[0][:6], [
                "Student", "Admission number", "Class", "Section", "Recorded days",
                "Present days",
            ])
            self.assertEqual(records[1][0], 'Aarav, "Test"')
            self.assertEqual(records[1][4:10], ["4", "1", "1", "1", "1", "25.0"])

            page_status, page, _ = self.request(
                "Director", "/?page=attendance_reports&" + query,
            )
            self.assertEqual(page_status, 200)
            self.assertIn(b"Attendance Reports &amp; Analytics", page)
            self.assertIn(b"Date-wise attendance summary", page)
        finally:
            db = connect(self.database)
            db.execute("DELETE FROM attendance WHERE attendance_id LIKE 'report-test-%'")
            db.execute(
                "UPDATE students SET full_name=? WHERE student_id=?",
                (original_name, student_id),
            )
            db.commit()
            db.close()

    def test_class_teacher_and_parent_scopes_apply_to_pages_and_exports(self):
        db = connect(self.database)
        assigned_section = db.execute(
            "SELECT section_id,class_name,section_name FROM class_sections "
            "WHERE homeroom_teacher_id='demo-teacher-001' LIMIT 1"
        ).fetchone()
        denied_section = db.execute(
            "SELECT section_id FROM class_sections "
            "WHERE homeroom_teacher_id<>'demo-teacher-001' LIMIT 1"
        ).fetchone()[0]
        db.close()

        self.assertEqual(
            self.request(
                "Class Teacher",
                "/?page=attendance_reports&section_id=" + denied_section,
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "Class Teacher",
                "/export/attendance/class-wise?section_id=" + denied_section,
            )[0],
            403,
        )
        allowed_status, allowed_csv, _ = self.request(
            "Class Teacher",
            "/export/attendance/class-wise?section_id=" + assigned_section["section_id"],
        )
        self.assertEqual(allowed_status, 200)
        self.assertIn(assigned_section["class_name"].encode(), allowed_csv)
        self.assertIn(assigned_section["section_name"].encode(), allowed_csv)

        parent_status, parent_page, _ = self.request(
            "Parent", "/?page=attendance_reports",
        )
        self.assertEqual(parent_status, 200)
        self.assertIn(b"Aarav Sharma", parent_page)
        self.assertNotIn(b"Attendance Reports &amp; Analytics", parent_page)
        self.assertNotIn(b"Export", parent_page)
        self.assertEqual(
            self.request(
                "Parent", "/export/attendance/student-wise",
            )[0],
            403,
        )

    def test_subject_teacher_and_technical_team_have_no_attendance_reports(self):
        for role in ("Subject Teacher", "Technical Team"):
            with self.subTest(role=role):
                self.assertEqual(
                    self.request(role, "/?page=attendance_reports")[0], 403,
                )
                self.assertEqual(
                    self.request(
                        role, "/export/attendance/student-wise",
                    )[0],
                    403,
                )

    def test_monthly_export_uses_selected_month_and_low_attendance_threshold(self):
        db = connect(self.database)
        attendance_month = db.execute(
            "SELECT substr(MIN(attendance_date),1,7) FROM attendance"
        ).fetchone()[0]
        db.close()
        year, month = attendance_month.split("-")
        status, content, headers = self.request(
            "Principal",
            "/export/attendance/monthly?" +
            urlencode({"month": month, "year": year}),
        )
        self.assertEqual(status, 200)
        self.assertIn(f"attendance-monthly-{year}-{month}.csv", headers["Content-Disposition"])
        monthly_rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(monthly_rows[0][0:2], ["Month", "Year"])
        self.assertTrue(all(row[0:2] == [str(int(month)), year] for row in monthly_rows[1:]))
        self.assertEqual(
            self.request(
                "Principal",
                "/export/attendance/low-attendance?threshold=101",
            )[0],
            400,
        )

    def test_low_attendance_alerts_respect_date_section_filters_and_threshold(self):
        student_id = "demo-student-0001"
        db = connect(self.database)
        student = db.execute(
            "SELECT s.full_name,c.section_id FROM students s "
            "JOIN class_sections c ON c.section_id=s.section_id "
            "WHERE s.student_id=?",
            (student_id,),
        ).fetchone()
        db.executemany(
            "INSERT INTO attendance VALUES (?,?,?,?,0)",
            [
                (f"alert-report-{index}", student_id, f"2099-02-{10 + index:02d}", status)
                for index, status in enumerate(
                    ("Present", "Absent", "Absent", "Absent")
                )
            ],
        )
        db.commit()
        db.close()
        query = urlencode({
            "section_id": student["section_id"],
            "start_date": "2099-02-10",
            "end_date": "2099-02-13",
            "threshold": "50",
        })
        try:
            status, content, _ = self.request(
                "Director", "/export/attendance/low-attendance?" + query,
            )
            self.assertEqual(status, 200)
            rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
            matching_rows = [row for row in rows[1:] if row[0] == student["full_name"]]
            self.assertEqual(len(matching_rows), 1)
            self.assertEqual(matching_rows[0][4:], ["25.0", "3"])

            boundary_query = urlencode({
                "section_id": student["section_id"],
                "start_date": "2099-02-10",
                "end_date": "2099-02-13",
                "threshold": "25",
            })
            status, content, _ = self.request(
                "Director", "/export/attendance/low-attendance?" + boundary_query,
            )
            self.assertEqual(status, 200)
            boundary_rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
            self.assertFalse(
                any(row[0] == student["full_name"] for row in boundary_rows[1:])
            )
        finally:
            db = connect(self.database)
            db.execute(
                "DELETE FROM attendance WHERE attendance_id LIKE 'alert-report-%'"
            )
            db.commit()
            db.close()


if __name__ == "__main__":
    unittest.main()
