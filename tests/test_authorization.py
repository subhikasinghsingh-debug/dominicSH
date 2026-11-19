import re
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.server import ThreadingHTTPServer

from app import NAV_ITEMS, SchoolHandler
from demo_data import connect, seed_demo


class AuthorizationTests(unittest.TestCase):
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
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
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
            opener = build_opener(HTTPCookieProcessor(CookieJar()))
            login = urlencode({
                "email": credential["email"],
                "password": credential["password"],
            }).encode()
            opener.open(Request(self.base_url + "/login", data=login), timeout=10)
            response = opener.open(self.base_url + "/", timeout=10)
            page = response.read().decode()
            csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
            self.clients[role] = (opener, csrf)
        return self.clients[role]

    def request(self, role, path, values=None):
        opener, csrf = self.client(role)
        data = None
        headers = {}
        if values is not None:
            values = {**values, "csrf": csrf}
            data = urlencode(values).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = opener.open(
                Request(self.base_url + path, data=data, headers=headers), timeout=15
            )
            return response.status, response.read().decode()
        except HTTPError as error:
            return error.code, error.read().decode()

    def db_value(self, query, parameters=()):
        db = connect(self.database)
        try:
            return db.execute(query, parameters).fetchone()[0]
        finally:
            db.close()

    def test_director_and_principal_can_open_every_module(self):
        for role in ("Director", "Principal"):
            for page, _ in NAV_ITEMS:
                status, _ = self.request(role, f"/?page={page}")
                self.assertEqual(status, 200, f"{role} denied {page}")
            self.assertEqual(
                self.request(role, "/?page=student&id=demo-student-0009")[0], 200
            )

    def test_class_teacher_is_limited_to_homeroom_and_nonfinancial_modules(self):
        status, _ = self.request("Class Teacher", "/?page=student&id=demo-student-0001")
        self.assertEqual(status, 200)
        status, _ = self.request("Class Teacher", "/?page=student&id=demo-student-0009")
        self.assertEqual(status, 403)
        for page in ("fees", "payments", "staff", "buses", "reports", "boards", "issues"):
            self.assertEqual(
                self.request("Class Teacher", f"/?page={page}")[0], 403, page
            )
        status, dashboard = self.request("Class Teacher", "/")
        self.assertEqual(status, 200)
        self.assertIn("Assigned students", dashboard)
        self.assertNotIn("Fees collected", dashboard)
        self.assertNotIn('href="/?page=fees"', dashboard)

    def test_subject_teacher_is_limited_to_assigned_classes_and_subjects(self):
        self.assertEqual(
            self.request("Subject Teacher", "/?page=student&id=demo-student-0009")[0], 200
        )
        self.assertEqual(
            self.request("Subject Teacher", "/?page=student&id=demo-student-0001")[0], 403
        )
        status, html = self.request("Subject Teacher", "/?page=academics")
        self.assertEqual(status, 200)
        self.assertNotIn(">Mathematics<", html)
        for page in ("attendance", "fees", "payments", "staff", "reports"):
            self.assertEqual(
                self.request("Subject Teacher", f"/?page={page}")[0], 403, page
            )
        db = connect(self.database)
        allowed_exam = db.execute(
            "SELECT e.exam_id,e.marks_obtained,e.grade,e.maximum_marks FROM examinations e "
            "JOIN teacher_assignments ta ON ta.section_id=e.section_id "
            "AND ta.subject=e.subject WHERE ta.employee_id='demo-teacher-002' LIMIT 1"
        ).fetchone()
        denied_exam = db.execute(
            "SELECT e.exam_id,e.marks_obtained FROM examinations e "
            "JOIN teacher_assignments ta ON ta.section_id=e.section_id "
            "WHERE ta.employee_id='demo-teacher-002' AND ta.subject<>e.subject LIMIT 1"
        ).fetchone()
        db.close()
        self.assertIsNotNone(denied_exam)
        updated_mark = allowed_exam["marks_obtained"] + 1
        self.assertEqual(
            self.request(
                "Subject Teacher", "/academics/update",
                {"exam_id": allowed_exam["exam_id"], "marks": str(updated_mark)},
            )[0],
            200,
        )
        status, _ = self.request(
            "Subject Teacher", "/academics/update",
            {"exam_id": denied_exam["exam_id"], "marks": "1"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(
            self.db_value(
                "SELECT marks_obtained FROM examinations WHERE exam_id=?",
                (denied_exam["exam_id"],),
            ),
            denied_exam["marks_obtained"],
        )
        db = connect(self.database)
        db.execute(
            "UPDATE examinations SET marks_obtained=?,grade=? WHERE exam_id=?",
            (allowed_exam["marks_obtained"], allowed_exam["grade"], allowed_exam["exam_id"]),
        )
        db.commit()
        db.close()

    def test_parent_can_only_open_linked_child_and_sees_scoped_data(self):
        status, own_profile = self.request("Parent", "/?page=student&id=demo-student-0001")
        self.assertEqual(status, 200)
        self.assertIn("Aarav Sharma", own_profile)
        self.assertEqual(
            self.request("Parent", "/?page=student&id=demo-student-0002")[0], 403
        )
        status, fees = self.request("Parent", "/?page=fees")
        self.assertEqual(status, 200)
        self.assertIn("Aarav Sharma", fees)
        self.assertNotIn("Aditi Patel", fees)
        status, payments = self.request("Parent", "/?page=payments")
        self.assertEqual(status, 200)
        self.assertIn("Aarav Sharma", payments)
        self.assertNotIn("Aditi Patel", payments)
        status, buses = self.request("Parent", "/?page=buses")
        self.assertEqual(status, 200)
        self.assertIn("BUS-02", buses)
        self.assertNotIn("BUS-01", buses)
        for page in ("staff", "reports", "boards", "issues"):
            self.assertEqual(self.request("Parent", f"/?page={page}")[0], 403, page)
        status, dashboard = self.request("Parent", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("Fees collected", dashboard)
        self.assertNotIn("Non-teaching staff", dashboard)

    def test_technical_team_only_sees_technical_modules(self):
        for page in ("boards", "issues"):
            self.assertEqual(self.request("Technical Team", f"/?page={page}")[0], 200)
        for page in ("students", "staff", "attendance", "fees", "academics", "reports"):
            self.assertEqual(
                self.request("Technical Team", f"/?page={page}")[0], 403, page
            )
        self.assertEqual(
            self.request("Technical Team", "/?page=student&id=demo-student-0001")[0], 403
        )
        status, dashboard = self.request("Technical Team", "/")
        self.assertEqual(status, 200)
        self.assertIn("Pending repairs", dashboard)
        self.assertNotIn("Fees collected", dashboard)
        self.assertNotIn("Assigned students", dashboard)
        self.assertNotIn('href="/?page=students"', dashboard)

    def test_unauthorized_attendance_and_exam_writes_are_rejected(self):
        db = connect(self.database)
        attendance = db.execute(
            "SELECT attendance_date,status FROM attendance WHERE student_id='demo-student-0009' "
            "ORDER BY attendance_date LIMIT 1"
        ).fetchone()
        exam_id = db.execute(
            "SELECT exam_id FROM examinations WHERE student_id='demo-student-0001' LIMIT 1"
        ).fetchone()[0]
        original_mark = db.execute(
            "SELECT marks_obtained FROM examinations WHERE exam_id=?", (exam_id,)
        ).fetchone()[0]
        db.close()
        status, _ = self.request(
            "Class Teacher", "/attendance/update",
            {"student_id": "demo-student-0009", "attendance_date": attendance["attendance_date"],
             "status": "Absent"},
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            "Subject Teacher", "/academics/update",
            {"exam_id": exam_id, "marks": "1"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(
            self.db_value(
                "SELECT status FROM attendance WHERE student_id='demo-student-0009' "
                "AND attendance_date=?", (attendance["attendance_date"],)
            ),
            attendance["status"],
        )
        self.assertEqual(
            self.db_value("SELECT marks_obtained FROM examinations WHERE exam_id=?", (exam_id,)),
            original_mark,
        )

    def test_authorized_class_teacher_update_is_scoped(self):
        db = connect(self.database)
        record = db.execute(
            "SELECT attendance_date,status FROM attendance WHERE student_id='demo-student-0001' "
            "ORDER BY attendance_date LIMIT 1"
        ).fetchone()
        db.close()
        new_status = "Absent" if record["status"] != "Absent" else "Present"
        status, _ = self.request(
            "Class Teacher", "/attendance/update",
            {"student_id": "demo-student-0001", "attendance_date": record["attendance_date"],
             "status": new_status},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value(
                "SELECT status FROM attendance WHERE student_id='demo-student-0001' "
                "AND attendance_date=?", (record["attendance_date"],)
            ),
            new_status,
        )
        db = connect(self.database)
        db.execute(
            "UPDATE attendance SET status=? WHERE student_id='demo-student-0001' "
            "AND attendance_date=?",
            (record["status"], record["attendance_date"]),
        )
        db.commit()
        db.close()

    def test_technical_workflow_and_cross_module_write_denials(self):
        db = connect(self.database)
        issue_id = db.execute(
            "SELECT issue_id FROM technical_issues WHERE is_demo=1 LIMIT 1"
        ).fetchone()[0]
        count_before = db.execute(
            "SELECT COUNT(*) FROM technical_issues WHERE is_demo=1"
        ).fetchone()[0]
        db.close()
        status, _ = self.request(
            "Technical Team", "/issues/update",
            {"issue_id": issue_id, "status": "In Progress", "repair_notes": "Diagnostic check"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value("SELECT status FROM technical_issues WHERE issue_id=?", (issue_id,)),
            "In Progress",
        )
        self.assertGreater(
            self.db_value("SELECT COUNT(*) FROM board_maintenance WHERE is_demo=1"), 65
        )
        status, _ = self.request(
            "Technical Team", "/issues/report",
            {"board_id": "demo-board-001", "category": "Connectivity",
             "description": "Reported by authorization test", "priority": "Low"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value("SELECT COUNT(*) FROM technical_issues WHERE is_demo=1"),
            count_before + 1,
        )
        status, _ = self.request(
            "Parent", "/issues/update",
            {"issue_id": issue_id, "status": "Resolved", "repair_notes": "Unauthorized"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(
            self.db_value("SELECT status FROM technical_issues WHERE issue_id=?", (issue_id,)),
            "In Progress",
        )
        self.assertEqual(
            self.request("Technical Team", "/attendance/update",
                         {"student_id": "demo-student-0001",
                          "attendance_date": "2026-11-18", "status": "Absent"})[0],
            403,
        )


if __name__ == "__main__":
    unittest.main()
