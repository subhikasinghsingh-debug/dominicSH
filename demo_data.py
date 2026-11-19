"""SQLite schema and deterministic, fictional SCHOOL COMMAND demo data."""

from __future__ import annotations

import hashlib
import os
import random
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ACADEMIC_SESSION = "2026-27"
PASSWORD_ITERATIONS = 310_000
DEMO_DATA_TABLES = (
    "employees", "buses", "class_sections", "students", "teacher_assignments",
    "attendance", "fees", "fee_payments", "classrooms", "smart_boards",
    "board_maintenance", "technical_issues", "announcements",
    "announcement_reads", "examinations",
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY,
    category TEXT NOT NULL CHECK (category IN ('teacher', 'staff')),
    name TEXT NOT NULL,
    designation TEXT NOT NULL,
    department TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    joining_date TEXT NOT NULL,
    status TEXT NOT NULL,
    subjects TEXT NOT NULL DEFAULT '',
    assigned_classes TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS buses (
    bus_id TEXT PRIMARY KEY,
    bus_number TEXT NOT NULL,
    registration_number TEXT NOT NULL,
    driver_name TEXT NOT NULL,
    driver_phone TEXT NOT NULL,
    route TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    location_label TEXT NOT NULL CHECK (location_label = 'SIMULATED'),
    status TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS class_sections (
    section_id TEXT PRIMARY KEY,
    class_name TEXT NOT NULL,
    section_name TEXT NOT NULL,
    academic_session TEXT NOT NULL,
    homeroom_teacher_id TEXT NOT NULL REFERENCES employees(employee_id),
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    UNIQUE (class_name, section_name, academic_session, is_demo)
);
CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    admission_number TEXT NOT NULL,
    full_name TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,
    gender TEXT NOT NULL,
    section_id TEXT NOT NULL REFERENCES class_sections(section_id),
    roll_number INTEGER NOT NULL,
    academic_session TEXT NOT NULL,
    guardian_name TEXT NOT NULL,
    guardian_phone TEXT NOT NULL,
    guardian_email TEXT NOT NULL,
    address TEXT NOT NULL,
    admission_date TEXT NOT NULL,
    bus_id TEXT REFERENCES buses(bus_id),
    status TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    UNIQUE (section_id, roll_number)
);
CREATE TABLE IF NOT EXISTS teacher_assignments (
    assignment_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    section_id TEXT NOT NULL REFERENCES class_sections(section_id),
    subject TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    UNIQUE (employee_id, section_id, subject)
);
CREATE TABLE IF NOT EXISTS attendance (
    attendance_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    attendance_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('Present', 'Absent', 'Late', 'Leave')),
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    UNIQUE (student_id, attendance_date)
);
CREATE TABLE IF NOT EXISTS fees (
    fee_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    academic_session TEXT NOT NULL,
    fee_category TEXT NOT NULL,
    annual_total INTEGER NOT NULL,
    paid_amount INTEGER NOT NULL,
    pending_amount INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    payment_status TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    CHECK (annual_total = paid_amount + pending_amount),
    UNIQUE (student_id, academic_session)
);
CREATE TABLE IF NOT EXISTS fee_payments (
    payment_id TEXT PRIMARY KEY,
    fee_id TEXT NOT NULL REFERENCES fees(fee_id),
    payment_date TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    method TEXT NOT NULL,
    receipt_number TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS classrooms (
    classroom_id TEXT PRIMARY KEY,
    room_number TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS smart_boards (
    board_id TEXT PRIMARY KEY,
    classroom_id TEXT NOT NULL UNIQUE REFERENCES classrooms(classroom_id),
    brand TEXT NOT NULL CHECK (brand = 'BenQ'),
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('Online', 'Offline', 'Error', 'Under Maintenance')),
    last_update TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS board_maintenance (
    maintenance_id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES smart_boards(board_id),
    maintenance_date TEXT NOT NULL,
    description TEXT NOT NULL,
    technician_id TEXT NOT NULL REFERENCES employees(employee_id),
    status TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS technical_issues (
    issue_id TEXT PRIMARY KEY,
    classroom_id TEXT NOT NULL REFERENCES classrooms(classroom_id),
    board_id TEXT NOT NULL REFERENCES smart_boards(board_id),
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    priority TEXT NOT NULL,
    reporting_user TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    technician_id TEXT NOT NULL REFERENCES employees(employee_id),
    status TEXT NOT NULL CHECK (status IN ('Reported', 'Acknowledged', 'In Progress', 'Resolved')),
    repair_notes TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS announcements (
    announcement_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    audience TEXT NOT NULL,
    author TEXT NOT NULL,
    published_at TEXT NOT NULL,
    priority TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS demo_users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_iterations INTEGER NOT NULL,
    student_id TEXT REFERENCES students(student_id),
    employee_id TEXT REFERENCES employees(employee_id),
    is_demo INTEGER NOT NULL DEFAULT 1 CHECK (is_demo = 1)
);
CREATE TABLE IF NOT EXISTS parent_children (
    user_id TEXT NOT NULL REFERENCES demo_users(user_id),
    student_id TEXT NOT NULL REFERENCES students(student_id),
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    PRIMARY KEY (user_id, student_id)
);
CREATE TABLE IF NOT EXISTS announcement_reads (
    announcement_id TEXT NOT NULL REFERENCES announcements(announcement_id),
    user_id TEXT NOT NULL REFERENCES demo_users(user_id),
    read_status TEXT NOT NULL CHECK (read_status IN ('Read', 'Unread')),
    is_demo INTEGER NOT NULL DEFAULT 1 CHECK (is_demo = 1),
    PRIMARY KEY (announcement_id, user_id)
);
CREATE TABLE IF NOT EXISTS examinations (
    exam_id TEXT PRIMARY KEY,
    examination_name TEXT NOT NULL,
    subject TEXT NOT NULL,
    section_id TEXT NOT NULL REFERENCES class_sections(section_id),
    student_id TEXT NOT NULL REFERENCES students(student_id),
    marks_obtained INTEGER NOT NULL,
    maximum_marks INTEGER NOT NULL,
    grade TEXT NOT NULL,
    academic_session TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    CHECK (marks_obtained >= 0 AND marks_obtained <= maximum_marks)
);
CREATE TABLE IF NOT EXISTS app_sessions (
    session_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES demo_users(user_id),
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(attendance_date);
CREATE INDEX IF NOT EXISTS idx_students_section ON students(section_id);
CREATE INDEX IF NOT EXISTS idx_issues_status ON technical_issues(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_email_scope ON employees(email,is_demo);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bus_number_scope ON buses(bus_number,is_demo);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bus_registration_scope ON buses(registration_number,is_demo);
CREATE UNIQUE INDEX IF NOT EXISTS idx_student_admission_scope ON students(admission_number,is_demo);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_receipt_scope ON fee_payments(receipt_number,is_demo);
CREATE UNIQUE INDEX IF NOT EXISTS idx_classroom_room_scope ON classrooms(room_number,is_demo);
"""

FIRST_NAMES = [
    "Aarav", "Aditi", "Advait", "Ananya", "Arjun", "Diya", "Ishaan", "Isha",
    "Kabir", "Kavya", "Krish", "Meera", "Neel", "Nisha", "Rohan", "Riya",
    "Saanvi", "Shaurya", "Tara", "Vihaan", "Aarohi", "Dev", "Mira", "Reyansh",
]
LAST_NAMES = [
    "Sharma", "Patel", "Verma", "Iyer", "Reddy", "Nair", "Mehta", "Kapoor",
    "Joshi", "Desai", "Rao", "Singh", "Kulkarni", "Chatterjee", "Malhotra",
    "Pillai", "Bose", "Khan", "Das", "Menon",
]
CLASS_NAMES = [
    "Nursery", "LKG", "UKG", "Class I", "Class II", "Class III", "Class IV",
    "Class V", "Class VI", "Class VII", "Class VIII", "Class IX", "Class X",
    "Class XI", "Class XII",
]
SUBJECTS = ["English", "Mathematics", "Environmental Studies", "Science",
            "Social Studies", "Hindi", "Computer Science", "Physics", "Chemistry",
            "Biology", "Economics", "Accountancy"]
ROUTES = [
    "North Campus - Civil Lines", "East Campus - Vaishali Nagar",
    "South Campus - Mansarovar", "West Campus - Shastri Nagar",
    "Airport Road - Central Market", "Lake Road - Green Park",
    "Railway Colony - City Centre", "University Road - Rose Garden",
]
ANNOUNCEMENTS = [
    ("Monday Assembly", "Students should assemble in the main courtyard by 8:00 AM on Monday.", "Students and Parents", "Normal"),
    ("Half-Yearly Examination Schedule", "The 2026-27 half-yearly examination timetable is available from the class teacher.", "Students and Parents", "High"),
    ("Winter Holiday Notice", "The school will be closed for the winter break from 24 December through 2 January.", "All School", "High"),
    ("Term Fee Reminder", "Please review the current term fee statement and contact the accounts office with questions.", "Parents", "Normal"),
    ("Parent-Teacher Meeting", "Parent-teacher meetings will be held on Saturday from 9:00 AM to 1:00 PM.", "Parents", "High"),
    ("Inter-House Sports Day", "Sports Day events begin at 8:30 AM on the school field. Students should wear house colours.", "Students and Parents", "Normal"),
    ("Science Exhibition", "Class VI-XII project registrations for the annual science exhibition close on Friday.", "Students", "Normal"),
    ("Library Week", "Library Week includes reading sessions and book talks in the school library.", "All School", "Low"),
    ("Transport Route Update", "Families on the North Campus route should check the updated pickup timing with transport staff.", "Parents", "High"),
    ("Art Competition", "Submit entries for the annual art competition to the activities coordinator by 30 November.", "Students", "Low"),
    ("Annual Day Rehearsal", "Performers should report to the auditorium after classes for the scheduled rehearsal.", "Students", "Normal"),
    ("Health and Wellness Session", "A student wellness and first-aid awareness session will take place in the auditorium.", "Students and Staff", "Normal"),
]


def connect(database: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(database))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def school_days(end: date, count: int) -> list[date]:
    days: list[date] = []
    current = end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return list(reversed(days))


def _password_record(password: str) -> tuple[str, str, int]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return salt.hex(), digest.hex(), PASSWORD_ITERATIONS


def _ensure_access_schema(db: sqlite3.Connection) -> None:
    columns = {row["name"] for row in db.execute("PRAGMA table_info(demo_users)")}
    if "employee_id" not in columns:
        db.execute(
            "ALTER TABLE demo_users ADD COLUMN employee_id TEXT REFERENCES employees(employee_id)"
        )
    child_columns = {row["name"] for row in db.execute("PRAGMA table_info(parent_children)")}
    if "is_demo" not in child_columns:
        db.execute(
            "ALTER TABLE parent_children ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 1 "
            "CHECK (is_demo IN (0, 1))"
        )
    issue_columns = {row["name"] for row in db.execute("PRAGMA table_info(technical_issues)")}
    if "repair_notes" not in issue_columns:
        db.execute(
            "ALTER TABLE technical_issues ADD COLUMN repair_notes TEXT NOT NULL DEFAULT ''"
        )


def _provision_demo_users(
    db: sqlite3.Connection, credential_output: list[dict[str, str]]
) -> None:
    user_specs = [
        ("director", "director.demo@schoolcommand.local", "Director", None, None),
        ("principal", "principal.demo@schoolcommand.local", "Principal", None, None),
        ("teacher", "teacher.demo@schoolcommand.local", "Class Teacher", None, "demo-teacher-001"),
        ("subject", "subject.demo@schoolcommand.local", "Subject Teacher", None, "demo-teacher-002"),
        ("parent", "parent.demo@schoolcommand.local", "Parent", "demo-student-0001", None),
        ("technical", "technical.demo@schoolcommand.local", "Technical Team", None, None),
    ]
    for user_id, email, role, student_id, employee_id in user_specs:
        stable_user_id = f"demo-user-{user_id}"
        existing = db.execute(
            "SELECT user_id FROM demo_users WHERE user_id=? AND is_demo=1",
            (stable_user_id,),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE demo_users SET role=?,student_id=?,employee_id=? WHERE user_id=?",
                (role, student_id, employee_id, stable_user_id),
            )
        else:
            password = secrets.token_urlsafe(15)
            salt, password_hash, iterations = _password_record(password)
            db.execute(
                "INSERT INTO demo_users "
                "(user_id,email,role,password_salt,password_hash,password_iterations,"
                "student_id,employee_id,is_demo) VALUES (?,?,?,?,?,?,?,?,1)",
                (stable_user_id, email, role, salt, password_hash, iterations,
                 student_id, employee_id),
            )
            credential_output.append({"role": role, "email": email, "password": password})
        if role == "Parent" and student_id:
            db.execute(
                "INSERT OR IGNORE INTO parent_children (user_id,student_id,is_demo) VALUES (?,?,1)",
                (stable_user_id, student_id),
            )
    users = db.execute(
        "SELECT user_id FROM demo_users WHERE is_demo=1 ORDER BY user_id"
    ).fetchall()
    for announcement_idx, announcement in enumerate(db.execute(
        "SELECT announcement_id FROM announcements WHERE is_demo=1 ORDER BY announcement_id"
    ).fetchall(), 1):
        for user_idx, user in enumerate(users):
            db.execute(
                "INSERT OR IGNORE INTO announcement_reads "
                "(announcement_id,user_id,read_status,is_demo) VALUES (?,?,?,1)",
                (announcement["announcement_id"], user["user_id"],
                 "Read" if (announcement_idx + user_idx) % 3 == 0 else "Unread"),
            )


def _student_dob(class_index: int, index: int, today: date) -> str:
    age = class_index + 3
    year = today.year - age
    month = (index % 12) + 1
    day = (index % 27) + 1
    return date(year, month, day).isoformat()


def _subjects_for(class_index: int) -> list[str]:
    if class_index < 3:
        return ["English", "Mathematics", "Environmental Studies"]
    if class_index < 8:
        return ["English", "Mathematics", "Science", "Social Studies", "Hindi"]
    if class_index < 11:
        return ["English", "Mathematics", "Science", "Social Studies", "Computer Science"]
    return ["English", "Physics", "Chemistry", "Biology", "Mathematics"]


def _grade(marks: int, maximum: int) -> str:
    percent = marks * 100 / maximum
    return "A+" if percent >= 90 else "A" if percent >= 80 else "B" if percent >= 70 else "C" if percent >= 60 else "D"


def seed_demo(db: sqlite3.Connection, today: date | None = None) -> list[dict[str, str]]:
    """Seed once when no demo records exist; return any newly generated logins."""
    db.executescript(SCHEMA)
    _ensure_access_schema(db)
    credential_output: list[dict[str, str]] = []
    if any(
        db.execute(f"SELECT COUNT(*) FROM {table} WHERE is_demo = 1").fetchone()[0]
        for table in DEMO_DATA_TABLES
    ):
        _provision_demo_users(db, credential_output)
        db.commit()
        return credential_output

    today = today or date.today()
    rng = random.Random(20261119)
    demo_flag = 1
    db.execute("BEGIN")
    try:
        # Buses and employees precede their dependent student/classroom records.
        bus_rows = []
        for i, route in enumerate(ROUTES, 1):
            bus_rows.append((
                f"demo-bus-{i:02}", f"BUS-{i:02}", f"RJ14-DEMO-{i:04}",
                f"{FIRST_NAMES[(i + 5) % len(FIRST_NAMES)]} {LAST_NAMES[(i + 3) % len(LAST_NAMES)]}",
                f"+91-90000-{i:05}", route, 40 + (i % 3) * 5,
                26.9124 + (i * 0.0061), 75.7873 + (i * 0.0043), "SIMULATED",
                "Active" if i != 8 else "Maintenance", demo_flag,
            ))
        db.executemany(
            "INSERT INTO buses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", bus_rows
        )

        teacher_classes: dict[int, list[str]] = {i: [] for i in range(30)}
        for i in range(30):
            section_idx_a = i
            section_idx_b = (i + 15) % 30
            teacher_classes[i] = [section_idx_a, section_idx_b]

        employees = []
        for i in range(30):
            assignments = teacher_classes[i]
            assigned = ", ".join(
                f"{CLASS_NAMES[idx // 2]}-{('A', 'B')[idx % 2]}"
                for idx in assignments
            )
            employee_name = f"{FIRST_NAMES[(i + 4) % len(FIRST_NAMES)]} {LAST_NAMES[(i + 2) % len(LAST_NAMES)]}"
            employees.append((
                f"demo-teacher-{i + 1:03}", "teacher", employee_name,
                ["Class Teacher", "Senior Teacher", "Subject Teacher"][i % 3],
                ["Primary", "Languages", "Mathematics", "Science", "Humanities"][i % 5],
                f"teacher{i + 1:02}@demo.schoolcommand.local",
                f"+91-91000-{i + 1:05}", date(today.year - (i % 12 + 1), (i % 12) + 1, 1).isoformat(),
                "Active", SUBJECTS[i % len(SUBJECTS)], assigned, demo_flag,
            ))
        for i in range(18):
            employees.append((
                f"demo-staff-{i + 1:03}", "staff",
                f"{FIRST_NAMES[(i + 9) % len(FIRST_NAMES)]} {LAST_NAMES[(i + 7) % len(LAST_NAMES)]}",
                ["Office Administrator", "Accountant", "Librarian", "Nurse", "Transport Coordinator"][i % 5],
                ["Administration", "Finance", "Library", "Health", "Transport"][i % 5],
                f"staff{i + 1:02}@demo.schoolcommand.local",
                f"+91-92000-{i + 1:05}", date(today.year - (i % 9 + 1), (i % 12) + 1, 1).isoformat(),
                "Active", "", "", demo_flag,
            ))
        db.executemany(
            "INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", employees
        )

        sections = []
        section_info = []
        for class_idx, class_name in enumerate(CLASS_NAMES):
            for section_name in ("A", "B"):
                idx = class_idx * 2 + (0 if section_name == "A" else 1)
                section_id = f"demo-section-{class_idx + 1:02}-{section_name}"
                teacher_id = f"demo-teacher-{(idx % 30) + 1:03}"
                sections.append((section_id, class_name, section_name, ACADEMIC_SESSION, teacher_id, demo_flag))
                section_info.append((class_idx, class_name, section_name, section_id, idx))
        db.executemany(
            "INSERT INTO class_sections VALUES (?,?,?,?,?,?)", sections
        )
        assignments = []
        assignment_number = 0
        for class_idx, _, _, section_id, idx in section_info:
            subjects = _subjects_for(class_idx)
            for teacher_index in (idx % 30, (idx + 15) % 30):
                assignment_number += 1
                assignments.append((
                    f"demo-assignment-{assignment_number:03}",
                    f"demo-teacher-{teacher_index + 1:03}", section_id,
                    subjects[(teacher_index // 2) % len(subjects)], demo_flag,
                ))
        db.executemany(
            "INSERT INTO teacher_assignments VALUES (?,?,?,?,?)", assignments
        )

        student_rows = []
        student_sections: list[tuple[str, str, int]] = []
        for class_idx, _, _, section_id, section_idx in section_info:
            for roll in range(1, 9):
                serial = len(student_rows) + 1
                full_name = (
                    f"{FIRST_NAMES[(serial - 1) % len(FIRST_NAMES)]} "
                    f"{LAST_NAMES[((serial - 1) // len(FIRST_NAMES)) % len(LAST_NAMES)]}"
                )
                bus_id = f"demo-bus-{(serial % 8) + 1:02}" if serial <= 120 and serial % 4 != 0 else None
                student_id = f"demo-student-{serial:04}"
                student_rows.append((
                    student_id, f"SC-{today.year}-{serial:04}", full_name,
                    _student_dob(class_idx, serial, today),
                    "Female" if serial % 2 == 0 else "Male", section_id, roll,
                    ACADEMIC_SESSION,
                    f"{FIRST_NAMES[(serial + 7) % len(FIRST_NAMES)]} {LAST_NAMES[(serial + 9) % len(LAST_NAMES)]}",
                    f"+91-93000-{serial:05}",
                    f"guardian{serial:04}@demo.schoolcommand.local",
                    f"{['C-12, Green Park', '18 Lake View Road', '42 Shanti Nagar', '7 Rose Garden'][serial % 4]}, Jaipur, Rajasthan",
                    date(today.year - (serial % 4), (serial % 12) + 1, 1).isoformat(),
                    bus_id, "Active", demo_flag,
                ))
                student_sections.append((student_id, section_id, class_idx))
        db.executemany(
            "INSERT INTO students VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", student_rows
        )

        attendance_rows = []
        for school_day in school_days(today, 30):
            for serial, (student_id, _, _) in enumerate(student_sections, 1):
                roll = rng.random()
                status = "Present" if roll < 0.88 else "Absent" if roll < 0.94 else "Late" if roll < 0.97 else "Leave"
                attendance_rows.append((
                    f"demo-att-{school_day:%Y%m%d}-{serial:04}", student_id,
                    school_day.isoformat(), status, demo_flag,
                ))
        db.executemany(
            "INSERT INTO attendance VALUES (?,?,?,?,?)", attendance_rows
        )

        fee_rows = []
        payment_rows = []
        for serial, (student_id, _, class_idx) in enumerate(student_sections, 1):
            annual_total = (18000 + class_idx * 1200) // 100 * 100
            group = serial % 10
            if group < 3:
                paid_amount = annual_total
            elif group < 6:
                paid_amount = annual_total // 2
            elif group < 8:
                paid_amount = annual_total // 4 if group == 6 else 0
            elif group == 8:
                paid_amount = 0
            else:
                paid_amount = annual_total // 3
            pending = annual_total - paid_amount
            due_date = (
                today + timedelta(days=30) if group == 7
                else today - timedelta(days=30) if group == 8
                else date(today.year, 9, 30)
            )
            payment_status = (
                "Paid" if pending == 0 else
                "Overdue" if due_date < today and paid_amount == 0 else
                "Partially Paid" if paid_amount else "Pending"
            )
            fee_id = f"demo-fee-{serial:04}"
            fee_rows.append((
                fee_id, student_id, ACADEMIC_SESSION, "Tuition and Annual Charges",
                annual_total, paid_amount, pending, due_date.isoformat(),
                payment_status, demo_flag,
            ))
            if paid_amount:
                parts = 2 if serial % 2 == 0 and paid_amount > 1 else 1
                first_payment = paid_amount // parts
                amounts = [first_payment, paid_amount - first_payment] if parts == 2 else [paid_amount]
                for part, amount in enumerate(amounts, 1):
                    if amount:
                        payment_rows.append((
                            f"demo-payment-{serial:04}-{part}",
                            fee_id, (today - timedelta(days=80 - part * 20)).isoformat(),
                            amount, ["UPI", "Bank Transfer", "Card"][serial % 3],
                            f"DEMO-RCPT-{serial:04}-{part}", demo_flag,
                        ))
        db.executemany("INSERT INTO fees VALUES (?,?,?,?,?,?,?,?,?,?)", fee_rows)
        db.executemany("INSERT INTO fee_payments VALUES (?,?,?,?,?,?,?)", payment_rows)

        classrooms = [(f"demo-room-{i:03}", f"{100 + i:03}", demo_flag) for i in range(1, 66)]
        db.executemany("INSERT INTO classrooms VALUES (?,?,?)", classrooms)
        board_statuses = (["Online"] * 46 + ["Offline"] * 8 + ["Error"] * 3 + ["Under Maintenance"] * 8)
        rng.shuffle(board_statuses)
        boards = []
        for i, status in enumerate(board_statuses, 1):
            boards.append((
                f"demo-board-{i:03}", f"demo-room-{i:03}", "BenQ", "Demo model",
                status, datetime.combine(today, time(7 + i % 4, i % 60), tzinfo=timezone.utc).isoformat(),
                demo_flag,
            ))
        db.executemany("INSERT INTO smart_boards VALUES (?,?,?,?,?,?,?)", boards)

        maintenance = []
        for i in range(1, 66):
            maintenance.append((
                f"demo-maintenance-{i:03}", f"demo-board-{i:03}",
                (today - timedelta(days=7 + i % 60)).isoformat(),
                "Routine inspection and firmware check.",
                f"demo-staff-{(i % 6) + 1:03}",
                "Completed" if i % 7 else "Scheduled", demo_flag,
            ))
        db.executemany(
            "INSERT INTO board_maintenance VALUES (?,?,?,?,?,?,?)", maintenance
        )

        issue_specs = [
            ("Connectivity", "Board intermittently disconnects from the classroom network.", "High", "Reported"),
            ("Display", "A faint vertical line is visible during presentations.", "Medium", "Acknowledged"),
            ("Audio", "Built-in speakers produce distorted audio at higher volume.", "Medium", "In Progress"),
            ("Touch", "Touch input is unresponsive near the lower-right corner.", "High", "Reported"),
            ("Power", "Board restarts unexpectedly during the first lesson.", "High", "Acknowledged"),
            ("Software", "Whiteboard application needs an update.", "Low", "In Progress"),
            ("Connectivity", "Wireless screen sharing is unavailable.", "Medium", "Reported"),
            ("Display", "Screen brightness is uneven along the left edge.", "Low", "Resolved"),
            ("Touch", "Calibration is required after startup.", "Medium", "Reported"),
            ("Power", "Remote control does not wake the display.", "Low", "Acknowledged"),
            ("Software", "Lesson annotations were not saved after closing the app.", "High", "In Progress"),
            ("Audio", "HDMI input has no audio output.", "Medium", "Resolved"),
            ("Connectivity", "Network connection drops during video playback.", "High", "Reported"),
            ("Display", "Input source selection is slow to respond.", "Low", "Resolved"),
        ]
        issues = []
        for i, (category, description, priority, status) in enumerate(issue_specs, 1):
            board_number = i + 2
            issues.append((
                f"demo-issue-{i:03}", f"demo-room-{board_number:03}",
                f"demo-board-{board_number:03}", category, description, priority,
                f"Teacher {FIRST_NAMES[(i + 3) % len(FIRST_NAMES)]}",
                (datetime.now(timezone.utc) - timedelta(days=i * 2, hours=i)).isoformat(),
                f"demo-staff-{(i % 6) + 1:03}", status, "", demo_flag,
            ))
        db.executemany(
            "INSERT INTO technical_issues "
            "(issue_id,classroom_id,board_id,category,description,priority,reporting_user,"
            "reported_at,technician_id,status,repair_notes,is_demo) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", issues
        )

        announcements = []
        published_base = datetime.now(timezone.utc)
        for i, (title, description, audience, priority) in enumerate(ANNOUNCEMENTS, 1):
            announcements.append((
                f"demo-announcement-{i:03}", title, description, audience,
                "School Administration" if i % 2 else "Activities Coordinator",
                (published_base - timedelta(days=i * 2)).isoformat(), priority, demo_flag,
            ))
        db.executemany("INSERT INTO announcements VALUES (?,?,?,?,?,?,?,?)", announcements)
        _provision_demo_users(db, credential_output)

        exam_rows = []
        exam_number = 0
        for serial, (student_id, section_id, class_idx) in enumerate(student_sections, 1):
            for exam_name, adjustment in (("Unit Test 1", -2), ("Half-Yearly Examination", 3)):
                for subject_idx, subject in enumerate(_subjects_for(class_idx)):
                    exam_number += 1
                    marks = max(28, min(99, 52 + ((serial * 7 + subject_idx * 11 + exam_number * 3) % 46) + adjustment))
                    exam_rows.append((
                        f"demo-exam-record-{exam_number:05}", exam_name, subject,
                        section_id, student_id, marks, 100, _grade(marks, 100),
                        ACADEMIC_SESSION, demo_flag,
                    ))
        db.executemany(
            "INSERT INTO examinations VALUES (?,?,?,?,?,?,?,?,?,?)", exam_rows
        )

        db.commit()
        return credential_output
    except Exception:
        db.rollback()
        raise


def reset_demo(db: sqlite3.Connection, today: date | None = None) -> list[dict[str, str]]:
    """Replace only demo-owned records; real school rows are never targeted."""
    db.executescript(SCHEMA)
    _ensure_access_schema(db)
    db.execute("BEGIN")
    try:
        db.execute(
            "UPDATE demo_users SET student_id=NULL,employee_id=NULL WHERE is_demo=1"
        )
        db.execute("DELETE FROM parent_children WHERE is_demo=1")
        for table in (
            "announcement_reads", "examinations", "technical_issues",
            "board_maintenance", "smart_boards", "classrooms", "fee_payments",
            "fees", "attendance", "teacher_assignments", "students", "announcements",
            "class_sections", "buses", "employees",
        ):
            if table == "announcement_reads":
                db.execute("DELETE FROM announcement_reads WHERE is_demo = 1")
            else:
                db.execute(f"DELETE FROM {table} WHERE is_demo = 1")
        # Keep provisioned accounts and their credentials for the administrator.
        db.commit()
    except Exception:
        db.rollback()
        raise
    return seed_demo(db, today)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize fictional SCHOOL COMMAND demo data.")
    parser.add_argument("--database", default="school_command.sqlite3")
    args = parser.parse_args()
    connection = connect(args.database)
    credentials = seed_demo(connection)
    print("Demo records are ready.")
    if credentials and os.environ.get("SCHOOL_COMMAND_ENV", "development").lower() != "production":
        print("Save these one-time demo credentials (generated randomly; do not use in production):")
        for credential in credentials:
            print(f"{credential['role']}: {credential['email']} / {credential['password']}")
    elif credentials:
        print("Demo users were provisioned; credential display is disabled in production.")
    else:
        print("Existing demo records found; no records were duplicated.")
    connection.close()
