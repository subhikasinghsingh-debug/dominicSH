import sqlite3
import tempfile
import unittest
from pathlib import Path

from demo_data import connect, reset_demo, seed_demo
from app import verify_database


class DemoDataTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "school.sqlite3"
        self.db = connect(self.database)
        self.credentials = seed_demo(self.db)

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def count(self, table, where="is_demo=1"):
        return self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]

    def test_seed_meets_demo_minimums_and_links_records(self):
        self.assertEqual(self.count("students"), 240)
        self.assertEqual(self.count("employees", "is_demo=1 AND category='teacher'"), 30)
        self.assertEqual(self.count("employees", "is_demo=1 AND category='staff'"), 18)
        self.assertEqual(self.count("class_sections"), 30)
        self.assertEqual(self.count("attendance"), 240 * 30)
        self.assertEqual(self.count("fees"), 240)
        self.assertEqual(self.count("buses"), 8)
        self.assertEqual(self.count("smart_boards"), 65)
        self.assertGreaterEqual(self.count("technical_issues"), 10)
        self.assertGreaterEqual(self.count("announcements"), 10)
        self.assertGreater(self.count("examinations"), 1000)
        self.assertTrue(
            {"Paid", "Partially Paid", "Pending", "Overdue"}.issubset({
                row[0] for row in self.db.execute(
                    "SELECT DISTINCT payment_status FROM fees WHERE is_demo=1"
                )
            })
        )
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(len(self.credentials), 6)

    def test_seed_is_idempotent_and_payment_totals_reconcile(self):
        counts = (self.count("students"), self.count("attendance"), self.count("fees"))
        self.assertEqual(seed_demo(self.db), [])
        self.assertEqual(counts, (self.count("students"), self.count("attendance"), self.count("fees")))
        self.assertEqual(
            self.db.execute(
                "SELECT SUM(paid_amount) FROM fees WHERE is_demo=1"
            ).fetchone()[0],
            self.db.execute(
                "SELECT SUM(amount) FROM fee_payments WHERE is_demo=1"
            ).fetchone()[0],
        )

    def test_demo_records_have_unique_identifiers_and_full_fee_coverage(self):
        for table, field in (
            ("students", "student_id"), ("students", "admission_number"),
            ("employees", "employee_id"), ("smart_boards", "board_id"),
        ):
            duplicate = self.db.execute(
                f"SELECT {field} FROM {table} WHERE is_demo=1 "
                f"GROUP BY {field} HAVING COUNT(*) > 1"
            ).fetchone()
            self.assertIsNone(duplicate, f"duplicate {table}.{field}")
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM students s LEFT JOIN fees f "
                "ON f.student_id=s.student_id AND f.is_demo=1 "
                "WHERE s.is_demo=1 AND f.fee_id IS NULL"
            ).fetchone()[0],
            0,
        )

    def test_reset_preserves_real_records_and_demo_logins(self):
        self.db.execute(
            "INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("real-employee-1", "teacher", "Real Teacher", "Teacher", "Science",
             "teacher@real.example", "+91-99999-00001", "2020-06-01", "Active", "Science", "", 0),
        )
        self.db.execute(
            "INSERT INTO class_sections VALUES (?,?,?,?,?,?)",
            ("real-section-1", "Class I", "A", "2026-27", "real-employee-1", 0),
        )
        self.db.execute(
            "INSERT INTO students VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("real-student-1", "REAL-001", "Actual Student", "2019-01-01", "Female",
             "real-section-1", 1, "2026-27", "Actual Guardian", "+91-99999-00002",
             "guardian@real.example", "Actual address", "2026-04-01", None, "Active", 0),
        )
        self.db.execute(
            "INSERT INTO app_sessions VALUES (?,?,?,?)",
            ("test-session", "demo-user-director", "test-csrf", "2099-01-01T00:00:00+00:00"),
        )
        self.db.execute(
            "INSERT INTO buses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("real-bus-1", "BUS-01", "RJ14-DEMO-0001", "Actual Driver",
             "+91-99999-00003", "Real Route", 40, 26.9, 75.8, "SIMULATED", "Active", 0),
        )
        self.db.execute(
            "INSERT INTO classrooms VALUES (?,?,?)", ("real-room-1", "101", 0)
        )
        login_hashes = self.db.execute(
            "SELECT user_id,password_hash FROM demo_users ORDER BY user_id"
        ).fetchall()

        reset_demo(self.db)
        reset_demo(self.db)

        self.assertEqual(self.count("students", "is_demo=0"), 1)
        self.assertEqual(self.count("employees", "is_demo=0"), 1)
        self.assertEqual(self.count("students"), 240)
        self.assertEqual(self.count("buses", "is_demo=0"), 1)
        self.assertEqual(self.count("classrooms", "is_demo=0"), 1)
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM app_sessions WHERE session_hash='test-session'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            [tuple(row) for row in self.db.execute(
                "SELECT user_id,password_hash FROM demo_users ORDER BY user_id"
            )],
            [tuple(row) for row in login_hashes],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT student_id FROM demo_users WHERE user_id='demo-user-parent'"
            ).fetchone()[0],
            "demo-student-0001",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT employee_id FROM demo_users WHERE user_id='demo-user-teacher'"
            ).fetchone()[0],
            "demo-teacher-001",
        )
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_verifier_accepts_seed_and_detects_missing_attendance(self):
        self.assertEqual(verify_database(self.database), [])
        self.db.execute("DELETE FROM attendance WHERE student_id='demo-student-0240'")
        self.db.commit()
        self.assertTrue(any("students" in error for error in verify_database(self.database)))


if __name__ == "__main__":
    unittest.main()
