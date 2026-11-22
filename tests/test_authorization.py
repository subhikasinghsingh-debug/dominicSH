import os
import re
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

import app
from app import NAV_ITEMS, SchoolHandler
from demo_data import connect, seed_demo


class AuthorizationTests(unittest.TestCase):
    def test_server_host_defaults_for_codespaces_and_local_development(self):
        for codespaces, expected_host in (
            ("true", "0.0.0.0"),
            ("false", "127.0.0.1"),
        ):
            with self.subTest(codespaces=codespaces):
                with (
                    patch.dict(os.environ, {"CODESPACES": codespaces}),
                    patch("app.startup"),
                    patch("app.ThreadingHTTPServer") as server_class,
                    patch.object(SchoolHandler, "database", self.database),
                    patch("sys.argv", ["app.py"]),
                ):
                    server = server_class.return_value
                    server.server_address = (expected_host, 8000)
                    server.serve_forever.side_effect = KeyboardInterrupt

                    app.main()

                server_class.assert_called_once_with(
                    (expected_host, 8000), SchoolHandler
                )

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

    def test_login_page_has_school_branding_accessible_fields_and_generic_error(self):
        anonymous = build_opener()
        page = anonymous.open(self.base_url + "/login", timeout=10).read().decode()
        self.assertIn('<main class="login-page"', page)
        self.assertIn(
            'background-image:url("/static/images/school-logo.jpg")', page
        )
        self.assertIn('inset:0;pointer-events:none;background-image:url("/static/images/school-logo.jpg")', page)
        self.assertIn("background-position:center 56%", page)
        self.assertIn("background-size:min(100vw,700px) auto", page)
        self.assertIn(
            "background-size:min(100vw,700px) auto;opacity:.2;filter:saturate(.72)",
            page,
        )
        self.assertIn(
            '<h1 id="login-school-name">St Dominic Savio\'s High School</h1>', page
        )
        self.assertIn('<p class="login-motto"><span>Thorough</span></p>', page)
        self.assertIn("<h2 id=\"login-heading\">Welcome Back</h2>", page)
        self.assertIn("Sign in to access your dashboard", page)
        self.assertIn("--glass-blur:blur(28px) saturate(180%)", page)
        self.assertIn(
            ".login-card{padding:clamp(24px,5vw,36px);background:rgba(255,255,255,.4);border:1px solid rgba(255,255,255,.78)",
            page,
        )
        self.assertIn(
            ".login-input-wrap input:focus{border-color:rgba(48,104,69,.65)",
            page,
        )
        self.assertIn(
            'id="email" name="email" type="email" required autocomplete="username"',
            page,
        )
        self.assertIn(
            'id="password" name="password" type="password" required autocomplete="current-password"',
            page,
        )
        self.assertIn('id="password-toggle" type="button" aria-label="Show password"', page)
        self.assertIn('id="login-spinner" aria-hidden="true" hidden', page)
        self.assertIn('identifier.type = identifierValue.includes("@") ? "email" : "text";', page)
        self.assertIn("Use your email address or configured username.", page)
        self.assertIn("Secure School Access", page)
        self.assertNotIn("School Management Portal", page)

        invalid_login = urlencode({
            "email": "not-an-account@example.test",
            "password": "invalid-password",
        }).encode()
        with self.assertRaises(HTTPError) as response:
            anonymous.open(
                Request(self.base_url + "/login", data=invalid_login), timeout=10
            )
        self.assertEqual(response.exception.code, 401)
        error_page = response.exception.read().decode()
        self.assertIn("The email address or password was not accepted.", error_page)
        self.assertIn("Welcome Back", error_page)
        self.assertNotIn("Sign in failed", error_page)

    def test_shared_glass_system_is_used_across_application_modules(self):
        for path, module in (
            ("/", "dashboard"),
            ("/?page=students", "students"),
            ("/?page=fees", "fees"),
            ("/?page=issues", "issues"),
        ):
            with self.subTest(module=module):
                status, page = self.request("Director", path)
                self.assertEqual(status, 200)
                self.assertIn(".top{position:relative;margin:16px", page)
                self.assertIn(
                    ".card,.panel{position:relative;overflow:hidden;background:rgba(255,255,255,.39)",
                    page,
                )
                self.assertIn(
                    ".table-wrap{max-width:100%;overflow:auto;background:rgba(255,255,255,.27)",
                    page,
                )
                self.assertIn(
                    "dialog::backdrop{background:rgba(31,47,37,.32)",
                    page,
                )
                self.assertIn(
                    "nav{flex-direction:row;overflow-x:auto;overscroll-behavior-x:contain",
                    page,
                )
                self.assertIn(
                    "input[type=text],input[type=email],input[type=password],input[type=number],input[type=date],select,textarea{width:100%;max-width:100%",
                    page,
                )
                self.assertIn(
                    "button,.button{background:linear-gradient(135deg,#467c5e,#245541)",
                    page,
                )
                self.assertIn(
                    ".grid{grid-template-columns:minmax(0,1fr);gap:12px}", page
                )

    def test_static_school_logo_is_served_as_image_bytes(self):
        response = build_opener().open(
            self.base_url + "/static/images/school-logo.jpg", timeout=10
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "image/jpeg")
        self.assertEqual(
            response.read(),
            (app.ROOT / "static" / "images" / "school-logo.jpg").read_bytes(),
        )

    def test_static_files_cannot_escape_the_static_directory(self):
        with self.assertRaises(HTTPError) as response:
            build_opener().open(
                self.base_url + "/static/images/%2e%2e/%2e%2e/app.py",
                timeout=10,
            )

        self.assertEqual(response.exception.code, 404)

    def test_only_administrators_can_manage_hashed_account_credentials(self):
        status, page = self.request("Director", "/credentials")
        self.assertEqual(status, 200)
        for role in (
            "Director", "Principal", "Class Teacher", "Subject Teacher",
            "Parent", "Technical Team",
        ):
            self.assertIn(role, page)

        status, _ = self.request("Parent", "/credentials")
        self.assertEqual(status, 403)
        original = self.credentials["Director"]
        new_username = "director.custom"
        new_password = "custom-password-123"
        values = {
            f"username_{user_id}": self.db_value(
                "SELECT email FROM demo_users WHERE user_id=?", (user_id,)
            )
            for user_id in app.DEMO_ACCOUNT_IDS
        }
        status, _ = self.request("Parent", "/credentials/update", values)
        self.assertEqual(status, 403)

        values["username_demo-user-subject"] = values["username_demo-user-director"].upper()
        status, _ = self.request("Director", "/credentials/update", values)
        self.assertEqual(status, 400)
        values["username_demo-user-subject"] = self.db_value(
            "SELECT email FROM demo_users WHERE user_id='demo-user-subject'"
        )

        values["username_demo-user-director"] = new_username
        values["password_demo-user-director"] = "short"
        status, _ = self.request("Director", "/credentials/update", values)
        self.assertEqual(status, 400)
        self.assertEqual(
            self.db_value("SELECT email FROM demo_users WHERE user_id='demo-user-director'"),
            original["email"],
        )

        values["password_demo-user-director"] = new_password
        status, _ = self.request("Director", "/credentials/update", values)
        self.assertEqual(status, 200)
        db = connect(self.database)
        try:
            updated = db.execute(
                "SELECT * FROM demo_users WHERE user_id='demo-user-director'"
            ).fetchone()
            self.assertEqual(updated["email"], new_username)
            self.assertNotEqual(updated["password_hash"], new_password)
            self.assertNotIn(
                new_password,
                " ".join(str(value) for value in tuple(updated)),
            )
            self.assertTrue(app.verify_password(new_password, updated))
        finally:
            db.close()

        fresh_client = build_opener(HTTPCookieProcessor(CookieJar()))
        login = urlencode({
            "username": new_username,
            "password": new_password,
        }).encode()
        fresh_client.open(
            Request(self.base_url + "/login", data=login), timeout=10
        )
        self.assertIn("Director", fresh_client.open(self.base_url + "/", timeout=10).read().decode())

        restore_values = {
            **values,
            "username_demo-user-director": original["email"],
            "password_demo-user-director": original["password"],
        }
        status, _ = self.request("Director", "/credentials/update", restore_values)
        self.assertEqual(status, 200)

    def test_director_and_principal_can_open_every_module(self):
        for role in ("Director", "Principal"):
            for page, _ in NAV_ITEMS:
                status, module = self.request(role, f"/?page={page}")
                self.assertEqual(status, 200, f"{role} denied {page}")
                if page != "dashboard":
                    self.assertIn('href="/"', module, f"{role} page lacks dashboard navigation: {page}")
            self.assertEqual(self.request(role, "/?page=examinations")[0], 200)
            self.assertEqual(
                self.request(role, "/?page=student&id=demo-student-0009")[0], 200
            )

    def test_dashboard_cards_and_module_links_open_and_refresh(self):
        status, dashboard = self.request("Director", "/")
        self.assertEqual(status, 200)
        hrefs = re.findall(r'<a class="card card-link[^"]*" href="([^"]+)"', dashboard)
        self.assertGreaterEqual(len(hrefs), 20)
        for href in hrefs:
            target = href.replace("&amp;", "&")
            with self.subTest(target=target):
                self.assertEqual(self.request("Director", target)[0], 200)
                self.assertEqual(self.request("Director", target)[0], 200)
        for page, _ in NAV_ITEMS:
            self.assertIn(f'href="/?page={page}"', dashboard)

    def test_page_navigation_includes_accessible_transition_support(self):
        status, page = self.request("Director", "/")
        self.assertEqual(status, 200)
        self.assertIn('id="page-transition-indicator" role="progressbar"', page)
        self.assertIn("@view-transition{navigation:auto}", page)
        self.assertIn("view-transition-name:app-sidebar", page)
        self.assertIn("prefers-reduced-motion:reduce", page)
        self.assertIn("document.startViewTransition", page)
        self.assertIn('addEventListener("popstate"', page)

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
        self.assertEqual(self.request("Class Teacher", "/?page=classes")[0], 200)
        self.assertEqual(self.request("Class Teacher", "/?page=examinations")[0], 200)

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
        self.assertEqual(self.request("Subject Teacher", "/?page=classes")[0], 200)
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
        self.assertIn("Aarav Sharma", buses)
        self.assertNotIn("BUS-01", buses)
        db = connect(self.database)
        other_bus_student = db.execute(
            "SELECT full_name FROM students WHERE bus_id='demo-bus-02' "
            "AND student_id<>'demo-student-0001' LIMIT 1"
        ).fetchone()
        db.close()
        if other_bus_student:
            self.assertNotIn(other_bus_student["full_name"], buses)
        status, classes = self.request("Parent", "/?page=classes")
        self.assertEqual(status, 200)
        self.assertIn("Nursery", classes)
        self.assertNotIn("LKG", classes)
        for page in ("staff", "reports", "boards", "issues"):
            self.assertEqual(self.request("Parent", f"/?page={page}")[0], 403, page)
        status, dashboard = self.request("Parent", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("Fees collected", dashboard)
        self.assertNotIn("Non-teaching staff", dashboard)

    def test_student_profile_edit_payment_and_adjustment_workflows(self):
        db = connect(self.database)
        student = db.execute(
            "SELECT s.*,c.section_id,c.class_name,c.section_name "
            "FROM students s JOIN class_sections c ON c.section_id=s.section_id "
            "WHERE s.student_id='demo-student-0001'"
        ).fetchone()
        fee = db.execute(
            "SELECT f.fee_id,f.student_id,f.pending_amount,f.paid_amount,f.annual_total "
            "FROM fees f WHERE f.pending_amount>0 ORDER BY f.student_id LIMIT 1"
        ).fetchone()
        db.close()

        status, profile = self.request(
            "Director", "/?page=student&id=demo-student-0001"
        )
        self.assertEqual(status, 200)
        self.assertIn("Aarav Sharma", profile)
        self.assertIn("Basic information", profile)
        self.assertIn("Parent / guardian information", profile)
        self.assertIn("Record payment", profile)
        self.assertIn("Adjust fee due", profile)

        status, _ = self.request("Director", "/students/update", {
            "student_id": student["student_id"],
            "full_name": student["full_name"],
            "admission_number": student["admission_number"],
            "section_id": student["section_id"],
            "roll_number": str(student["roll_number"]),
            "date_of_birth": student["date_of_birth"],
            "gender": student["gender"],
            "guardian_name": "Updated Guardian",
            "guardian_phone": student["guardian_phone"],
            "guardian_email": student["guardian_email"],
            "address": "Updated address",
            "father_name": "Updated Father",
            "mother_name": "Updated Mother",
            "emergency_contact": "+91-99999-00001",
            "admission_date": student["admission_date"],
            "bus_id": student["bus_id"] or "",
            "status": "Active",
        })
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value(
                "SELECT guardian_name FROM students WHERE student_id=?",
                (student["student_id"],),
            ),
            "Updated Guardian",
        )

        payment_amount = min(1, fee["pending_amount"])
        status, _ = self.request("Director", "/students/payments/add", {
            "student_id": fee["student_id"],
            "fee_id": fee["fee_id"],
            "amount": str(payment_amount),
            "payment_date": "2026-11-21",
            "method": "Cash",
            "receipt_number": "PROFILE-TEST-001",
            "remarks": "Profile payment test",
        })
        self.assertEqual(status, 200)
        db = connect(self.database)
        try:
            updated_fee = db.execute(
                "SELECT paid_amount,pending_amount FROM fees WHERE fee_id=?",
                (fee["fee_id"],),
            ).fetchone()
            payment_count = db.execute(
                "SELECT COUNT(*) FROM fee_payments p "
                "JOIN fee_payment_remarks r ON r.payment_id=p.payment_id "
                "WHERE p.receipt_number='PROFILE-TEST-001'"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(updated_fee["paid_amount"], fee["paid_amount"] + payment_amount)
        self.assertEqual(updated_fee["pending_amount"], fee["pending_amount"] - payment_amount)
        self.assertEqual(payment_count, 1)

        status, _ = self.request("Director", "/students/fees/adjust", {
            "student_id": fee["student_id"],
            "fee_id": fee["fee_id"],
            "amount": "-1",
            "adjustment_date": "2026-11-21",
            "reason": "Approved fee correction",
        })
        self.assertEqual(status, 200)
        db = connect(self.database)
        try:
            adjustment_count = db.execute(
                "SELECT COUNT(*) FROM fee_adjustments WHERE fee_id=?",
                (fee["fee_id"],),
            ).fetchone()[0]
            adjusted_fee = db.execute(
                "SELECT annual_total,pending_amount FROM fees WHERE fee_id=?",
                (fee["fee_id"],),
            ).fetchone()
            preserved_payment = db.execute(
                "SELECT COUNT(*) FROM fee_payments WHERE receipt_number='PROFILE-TEST-001'"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(adjustment_count, 1)
        self.assertEqual(adjusted_fee["annual_total"], fee["annual_total"] - 1)
        self.assertEqual(preserved_payment, 1)

    def test_technical_team_only_sees_technical_modules(self):
        for page in ("boards", "issues"):
            self.assertEqual(self.request("Technical Team", f"/?page={page}")[0], 200)
        for page in ("students", "staff", "classes", "attendance", "fees", "academics", "examinations", "reports"):
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

    def test_student_crud_duplicate_prevention_and_persistence(self):
        status, add_page = self.request("Director", "/?page=students&action=add")
        self.assertEqual(status, 200)
        self.assertIn("Add student", add_page)

        values = {
            "admission_number": "SC-PHASE1-001",
            "full_name": "Phase One Student",
            "section_id": "demo-section-04-A",
            "roll_number": "77",
            "date_of_birth": "2017-05-10",
            "gender": "Female",
            "guardian_name": "Test Guardian",
            "guardian_phone": "+91-90000-11111",
            "guardian_email": "guardian.phase1@example.test",
            "address": "Phase One Test Address",
            "bus_id": "demo-bus-02",
        }
        invalid_values = {**values, "guardian_email": "not-an-email"}
        self.assertEqual(
            self.request("Director", "/students/add", invalid_values)[0], 400
        )
        self.assertEqual(
            self.request(
                "Director", "/students/add",
                {**values, "date_of_birth": "2999-01-01"},
            )[0],
            400,
        )
        self.assertEqual(
            self.db_value(
                "SELECT COUNT(*) FROM students WHERE admission_number='SC-PHASE1-001'"
            ),
            0,
        )
        status, _ = self.request("Director", "/students/add", values)
        self.assertEqual(status, 200)
        db = connect(self.database)
        try:
            student = db.execute(
                "SELECT * FROM students WHERE admission_number=?",
                ("SC-PHASE1-001",),
            ).fetchone()
            self.assertIsNotNone(student)
            student_id = student["student_id"]
            self.assertEqual(student["is_demo"], 0)
            db.execute(
                "INSERT INTO attendance VALUES (?,?,?,?,0)",
                ("phase1-attendance-1", student_id, "2026-11-19", "Present"),
            )
            db.commit()
        finally:
            db.close()

        edit_values = {**values, "student_id": student_id, "full_name": "Phase One Edited"}
        status, _ = self.request("Director", "/students/update", edit_values)
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value(
                "SELECT full_name FROM students WHERE student_id=?",
                (student_id,),
            ),
            "Phase One Edited",
        )
        status, filtered_students = self.request(
            "Director",
            "/?page=students&q=Phase+One&class_name=Class+I&status=Active",
        )
        self.assertEqual(status, 200)
        self.assertIn("Phase One Edited", filtered_students)
        self.assertIn(
            "Phase One Edited",
            self.request("Director", "/?page=students&q=Phase+One")[1],
        )

        duplicate_values = {
            **values,
            "admission_number": "sc-phase1-001",
            "full_name": "Duplicate Student",
            "roll_number": "78",
        }
        status, _ = self.request("Director", "/students/add", duplicate_values)
        self.assertEqual(status, 400)

        status, profile = self.request(
            "Director", "/?page=student&id=" + student_id
        )
        self.assertEqual(status, 200)
        self.assertIn("Phase One Edited", profile)
        self.assertIn("Attendance history", profile)
        self.assertIn("Academic information", profile)
        self.assertIn("Fee information", profile)

        app.ensure_database(self.database)
        self.assertEqual(
            self.db_value(
                "SELECT full_name FROM students WHERE student_id=?",
                (student_id,),
            ),
            "Phase One Edited",
        )
        self.assertEqual(
            self.request("Parent", f"/?page=student&id={student_id}")[0], 403
        )
        status, parent_students = self.request(
            "Parent", "/?page=students&q=Phase+One"
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Phase One Edited", parent_students)

        status, _ = self.request(
            "Director", "/students/deactivate",
            {"student_id": student_id, "confirm": "DEACTIVATE"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value("SELECT status FROM students WHERE student_id=?", (student_id,),
                          ),
            "Inactive",
        )
        self.assertEqual(
            self.db_value(
                "SELECT COUNT(*) FROM attendance WHERE attendance_id='phase1-attendance-1'"
            ),
            1,
        )

        status, _ = self.request(
            "Parent", "/students/add",
            values,
        )
        self.assertEqual(status, 403)
        status, _ = self.request(
            "Parent", "/students/update",
            edit_values,
        )
        self.assertEqual(status, 403)

    def test_existing_student_profile_edits_fees_transport_and_payment_history(self):
        db = connect(self.database)
        student = db.execute(
            "SELECT * FROM students WHERE student_id='demo-student-0001'"
        ).fetchone()
        fee = db.execute(
            "SELECT * FROM fees WHERE student_id=?",
            (student["student_id"],),
        ).fetchone()
        payments_before = [
            tuple(row) for row in db.execute(
                "SELECT payment_id,fee_id,payment_date,amount,method,receipt_number,is_demo "
                "FROM fee_payments WHERE fee_id=? ORDER BY payment_id",
                (fee["fee_id"],),
            )
        ]
        db.close()

        edit_url = f"/?page=students&action=edit&id={student['student_id']}"
        status, edit_page = self.request("Director", edit_url)
        self.assertEqual(status, 200)
        self.assertIn('value="Aarav Sharma"', edit_page)
        self.assertIn('name="status"', edit_page)

        edit_values = {
            "student_id": student["student_id"],
            "full_name": "Aarav Sharma Updated",
            "admission_number": "SC-EDIT-0001",
            "section_id": "demo-section-02-A",
            "roll_number": "99",
            "date_of_birth": student["date_of_birth"],
            "gender": student["gender"],
            "guardian_name": "Updated Guardian",
            "guardian_phone": student["guardian_phone"],
            "guardian_email": student["guardian_email"],
            "address": "Updated address",
            "status": "Inactive",
            "bus_id": student["bus_id"],
        }
        status, _ = self.request("Director", "/students/update", edit_values)
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value(
                "SELECT full_name FROM students WHERE student_id=?",
                (student["student_id"],),
            ),
            "Aarav Sharma Updated",
        )
        self.assertEqual(
            self.db_value(
                "SELECT section_id FROM students WHERE student_id=?",
                (student["student_id"],),
            ),
            "demo-section-02-A",
        )
        self.assertEqual(
            self.db_value(
                "SELECT status FROM students WHERE student_id=?",
                (student["student_id"],),
            ),
            "Inactive",
        )
        status, profile = self.request(
            "Director", f"/?page=student&id={student['student_id']}"
        )
        self.assertEqual(status, 200)
        self.assertIn("Aarav Sharma Updated", profile)

        original_student_values = {
            "student_id": student["student_id"],
            "full_name": student["full_name"],
            "admission_number": student["admission_number"],
            "section_id": student["section_id"],
            "roll_number": student["roll_number"],
            "date_of_birth": student["date_of_birth"],
            "gender": student["gender"],
            "guardian_name": student["guardian_name"],
            "guardian_phone": student["guardian_phone"],
            "guardian_email": student["guardian_email"],
            "address": student["address"],
            "status": student["status"],
            "bus_id": student["bus_id"] or "",
        }
        status, _ = self.request(
            "Director", "/students/update",
            {**edit_values, "status": "Suspended"},
        )
        self.assertEqual(status, 400)
        status, _ = self.request(
            "Director", "/students/update", original_student_values
        )
        self.assertEqual(status, 200)

        new_total = fee["annual_total"] + 1000
        fee_values = {
            "student_id": student["student_id"],
            "fee_id": fee["fee_id"],
            "fee_category": "Adjusted tuition",
            "annual_total": str(new_total),
            "due_date": "2099-12-31",
        }
        status, updated_profile = self.request(
            "Director", "/students/fees/update", fee_values
        )
        self.assertEqual(status, 200)
        self.assertIn("Fee information saved", updated_profile)
        self.assertEqual(
            self.db_value("SELECT paid_amount FROM fees WHERE fee_id=?", (fee["fee_id"],)),
            fee["paid_amount"],
        )
        self.assertEqual(
            self.db_value("SELECT pending_amount FROM fees WHERE fee_id=?", (fee["fee_id"],)),
            new_total - fee["paid_amount"],
        )
        self.assertEqual(
            self.db_value("SELECT payment_status FROM fees WHERE fee_id=?", (fee["fee_id"],)),
            "Partially Paid",
        )
        db = connect(self.database)
        try:
            payments_after = [
                tuple(row) for row in db.execute(
                "SELECT payment_id,fee_id,payment_date,amount,method,receipt_number,is_demo "
                "FROM fee_payments WHERE fee_id=? ORDER BY payment_id",
                (fee["fee_id"],),
                )
            ]
        finally:
            db.close()
        self.assertEqual(payments_after, payments_before)
        status, _ = self.request(
            "Director", "/students/fees/update",
            {**fee_values, "annual_total": str(fee["paid_amount"] - 1)},
        )
        self.assertEqual(status, 400)
        status, _ = self.request(
            "Director", "/students/fees/update",
            {
                **fee_values,
                "fee_category": fee["fee_category"],
                "annual_total": str(fee["annual_total"]),
                "due_date": fee["due_date"],
            },
        )
        self.assertEqual(status, 200)

        transport_values = {
            "student_id": student["student_id"],
            "bus_id": "demo-bus-01",
        }
        status, transport_profile = self.request(
            "Director", "/students/transport", transport_values
        )
        self.assertEqual(status, 200)
        self.assertIn("Transport assignment updated", transport_profile)
        self.assertEqual(
            self.db_value(
                "SELECT bus_id FROM students WHERE student_id=?",
                (student["student_id"],),
            ),
            "demo-bus-01",
        )
        self.assertIn(
            "BUS-01",
            self.request("Director", f"/?page=student&id={student['student_id']}")[1],
        )
        self.assertIn(
            "Aarav Sharma",
            self.request("Director", "/?page=buses")[1],
        )
        self.assertEqual(
            self.request(
                "Director", "/students/transport",
                {**transport_values, "bus_id": "not-a-bus"},
            )[0],
            400,
        )
        self.assertEqual(
            self.request(
                "Director", "/students/transport",
                {**transport_values, "bus_id": ""},
            )[0],
            200,
        )
        self.assertIsNone(
            self.db_value(
                "SELECT bus_id FROM students WHERE student_id=?",
                (student["student_id"],),
            )
        )
        self.request(
            "Director", "/students/transport",
            {**transport_values, "bus_id": student["bus_id"]},
        )

        for endpoint, values in (
            ("/students/fees/update", fee_values),
            ("/students/transport", transport_values),
        ):
            self.assertEqual(self.request("Parent", endpoint, values)[0], 403)
            self.assertEqual(self.request("Class Teacher", endpoint, values)[0], 403)

    def test_search_and_combined_filters_use_database_records(self):
        status, students = self.request(
            "Director",
            "/?page=students&q=Aarav&class_name=Nursery&status=Active",
        )
        self.assertEqual(status, 200)
        self.assertIn("Aarav", students)

        status, no_match = self.request(
            "Director",
            "/?page=students&q=NoSuchStudent&class_name=Nursery",
        )
        self.assertEqual(status, 200)
        self.assertIn("No records available", no_match)

        status, attendance = self.request(
            "Director",
            "/?page=attendance&attendance_date=2026-11-18&class_name=Nursery",
        )
        self.assertEqual(status, 200)
        self.assertIn("Nursery", attendance)

        status, issues = self.request(
            "Technical Team",
            "/?page=issues&room=1&status=Reported&priority=High",
        )
        self.assertEqual(status, 200)
        self.assertIn("Reported", issues)
        self.assertIn("High", issues)

        db = connect(self.database)
        try:
            staff = db.execute(
                "SELECT name,employee_id,designation FROM employees "
                "WHERE is_demo=1 ORDER BY employee_id LIMIT 1"
            ).fetchone()
            exam = db.execute(
                "SELECT e.examination_name,e.subject,c.class_name "
                "FROM examinations e JOIN class_sections c ON c.section_id=e.section_id "
                "WHERE e.is_demo=1 ORDER BY e.exam_id LIMIT 1"
            ).fetchone()
            fee = db.execute(
                "SELECT s.full_name,c.class_name,f.payment_status "
                "FROM fees f JOIN students s ON s.student_id=f.student_id "
                "JOIN class_sections c ON c.section_id=s.section_id "
                "WHERE f.is_demo=1 ORDER BY f.fee_id LIMIT 1"
            ).fetchone()
            assigned_student = db.execute(
                "SELECT s.full_name,b.bus_number,b.route FROM students s "
                "JOIN buses b ON b.bus_id=s.bus_id WHERE s.is_demo=1 "
                "ORDER BY s.student_id LIMIT 1"
            ).fetchone()
            announcement = db.execute(
                "SELECT title,audience,substr(published_at,1,10) AS published_date "
                "FROM announcements WHERE is_demo=1 ORDER BY announcement_id LIMIT 1"
            ).fetchone()
        finally:
            db.close()

        status, staff_results = self.request(
            "Director",
            "/?page=staff&q=" + staff["employee_id"][:5] +
            "&designation=" + staff["designation"].replace(" ", "+"),
        )
        self.assertEqual(status, 200)
        self.assertIn(staff["name"], staff_results)

        status, academic_results = self.request(
            "Director",
            "/?page=academics&class_name=" + exam["class_name"].replace(" ", "+") +
            "&subject=" + exam["subject"][:4] +
            "&examination=" + exam["examination_name"][:5].replace(" ", "+"),
        )
        self.assertEqual(status, 200)
        self.assertIn(exam["subject"], academic_results)

        status, fee_results = self.request(
            "Director",
            "/?page=fees&q=" + fee["full_name"].split()[0] +
            "&class_name=" + fee["class_name"].replace(" ", "+") +
            "&payment_status=" + fee["payment_status"].replace(" ", "+"),
        )
        self.assertEqual(status, 200)
        self.assertIn(fee["full_name"], fee_results)

        status, transport_results = self.request(
            "Director",
            "/?page=buses&q=" + assigned_student["bus_number"] +
            "&route=" + assigned_student["route"].replace(" ", "+") +
            "&student=" + assigned_student["full_name"].split()[0],
        )
        self.assertEqual(status, 200)
        self.assertIn(assigned_student["bus_number"], transport_results)

        status, announcement_results = self.request(
            "Director",
            "/?page=announcements&q=" + announcement["title"][:5].replace(" ", "+") +
            "&category=" + announcement["audience"].replace(" ", "+") +
            "&date=" + announcement["published_date"],
        )
        self.assertEqual(status, 200)
        self.assertIn(announcement["title"], announcement_results)

    def test_payment_board_and_announcement_actions_persist(self):
        db = connect(self.database)
        fee = db.execute(
            "SELECT fee_id,paid_amount,pending_amount FROM fees "
            "WHERE pending_amount>0 ORDER BY fee_id LIMIT 1"
        ).fetchone()
        board = db.execute(
            "SELECT board_id,status FROM smart_boards WHERE is_demo=1 LIMIT 1"
        ).fetchone()
        db.close()

        status, _ = self.request(
            "Director", "/payments/add",
            {
                "fee_id": fee["fee_id"],
                "receipt_number": "SC-PHASE2-001",
                "payment_date": "2026-11-21",
                "amount": "1",
                "method": "Cash",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value("SELECT paid_amount FROM fees WHERE fee_id=?", (fee["fee_id"],)),
            fee["paid_amount"] + 1,
        )
        self.assertEqual(
            self.db_value("SELECT pending_amount FROM fees WHERE fee_id=?", (fee["fee_id"],)),
            fee["pending_amount"] - 1,
        )
        status, _ = self.request(
            "Director", "/payments/add",
            {
                "fee_id": fee["fee_id"],
                "receipt_number": "sc-phase2-001",
                "payment_date": "2026-11-21",
                "amount": "1",
                "method": "Cash",
            },
        )
        self.assertEqual(status, 400)

        status, _ = self.request(
            "Technical Team", "/boards/update",
            {"board_id": board["board_id"], "status": "Under Maintenance"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value("SELECT status FROM smart_boards WHERE board_id=?", (board["board_id"],)),
            "Under Maintenance",
        )
        db = connect(self.database)
        db.execute(
            "UPDATE smart_boards SET status=?,last_update=? WHERE board_id=?",
            (board["status"], "2026-11-21T00:00:00+00:00", board["board_id"]),
        )
        db.commit()
        db.close()

        status, _ = self.request(
            "Director", "/announcements/create",
            {
                "title": "Phase 2 announcement",
                "description": "Created by the Phase 2 test.",
                "audience": "All School",
                "priority": "Important",
            },
        )
        self.assertEqual(status, 200)
        announcement_id = self.db_value(
            "SELECT announcement_id FROM announcements "
            "WHERE title='Phase 2 announcement'"
        )
        status, _ = self.request(
            "Technical Team", "/announcements/read",
            {"announcement_id": announcement_id},
        )
        self.assertEqual(status, 403)

    def test_attendance_can_insert_and_export_routes_are_authorized(self):
        values = {
            "admission_number": "SC-PHASE2-STUDENT",
            "full_name": "Phase Two Student",
            "section_id": "demo-section-04-A",
            "roll_number": "88",
            "date_of_birth": "2017-05-10",
            "gender": "Female",
            "guardian_name": "Phase Two Guardian",
            "guardian_phone": "+91-90000-22222",
            "guardian_email": "phase2@example.test",
            "address": "Phase Two Address",
            "bus_id": "demo-bus-02",
        }
        status, _ = self.request("Director", "/students/add", values)
        self.assertEqual(status, 200)
        student_id = self.db_value(
            "SELECT student_id FROM students WHERE admission_number=?",
            ("SC-PHASE2-STUDENT",),
        )
        status, _ = self.request(
            "Director", "/attendance/update",
            {
                "student_id": student_id,
                "attendance_date": "2026-11-21",
                "status": "Present",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.db_value(
                "SELECT status FROM attendance WHERE student_id=? AND attendance_date=?",
                (student_id, "2026-11-21"),
            ),
            "Present",
        )
        status, csv_page = self.request("Director", "/export/payments")
        self.assertEqual(status, 200)
        self.assertIn("Receipt", csv_page)
        status, _ = self.request("Technical Team", "/export/payments")
        self.assertEqual(status, 403)

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
