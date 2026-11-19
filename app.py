"""Local SCHOOL COMMAND demo application. Uses only Python's standard library."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import html
import os
import secrets
import sqlite3
import ssl
import sys
from datetime import date, datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from demo_data import CLASS_NAMES, connect, reset_demo, school_days, seed_demo

ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "data" / "school_command.sqlite3"
NAV_ITEMS = [
    ("dashboard", "Dashboard"), ("students", "Students"), ("staff", "Staff"),
    ("attendance", "Attendance"), ("fees", "Fees"), ("payments", "Payment History"),
    ("buses", "Transport"),
    ("boards", "Smart Boards"), ("issues", "Technical Issues"),
    ("announcements", "Announcements"), ("academics", "Academics"),
    ("reports", "Report Cards"),
]
ADMIN_ROLES = {"Director", "Principal"}
ROLE_MODULES = {
    "Director": {key for key, _ in NAV_ITEMS},
    "Principal": {key for key, _ in NAV_ITEMS},
    "Class Teacher": {"dashboard", "students", "attendance", "academics", "announcements"},
    "Subject Teacher": {"dashboard", "students", "academics", "announcements"},
    "Parent": {"dashboard", "students", "attendance", "fees", "payments", "academics", "announcements", "buses"},
    "Technical Team": {"dashboard", "boards", "issues"},
}
ANNOUNCEMENT_AUDIENCES = {
    "Class Teacher": ("All School", "Students and Staff", "Students and Parents", "Staff"),
    "Subject Teacher": ("All School", "Students and Staff", "Students and Parents", "Staff"),
    "Parent": ("All School", "Students and Parents", "Parents", "Students"),
}


def can_access_module(user: sqlite3.Row, module: str) -> bool:
    return module in ROLE_MODULES.get(user["role"], set())


def student_scope(
    user: sqlite3.Row, student_alias: str = "s", section_alias: str | None = None,
    subject_alias: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    role = user["role"]
    if role in ADMIN_ROLES:
        return "1=1", ()
    if role == "Parent":
        return (
            f"EXISTS (SELECT 1 FROM parent_children pc WHERE pc.user_id=? "
            f"AND pc.student_id={student_alias}.student_id AND pc.is_demo=1)",
            (user["user_id"],),
        )
    if role == "Class Teacher" and user["employee_id"] and section_alias:
        return (
            f"{section_alias}.section_id IN (SELECT section_id FROM class_sections "
            "WHERE homeroom_teacher_id=? AND is_demo=1)",
            (user["employee_id"],),
        )
    if role == "Subject Teacher" and user["employee_id"] and section_alias:
        subject_clause = " AND ta.subject=" + subject_alias if subject_alias else ""
        return (
            f"EXISTS (SELECT 1 FROM teacher_assignments ta "
            f"WHERE ta.employee_id=? AND ta.section_id={section_alias}.section_id "
            f"AND ta.is_demo=1{subject_clause})",
            (user["employee_id"],),
        )
    return "0=1", ()


def issue_status_allowed(user: sqlite3.Row) -> bool:
    return user["role"] in ADMIN_ROLES | {"Technical Team"}


@contextmanager
def database_connection(database: Path) -> Iterator[sqlite3.Connection]:
    db = connect(database)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def money(amount: int | float) -> str:
    return f"₹{amount:,.0f}"


def verify_password(password: str, user: sqlite3.Row) -> bool:
    salt = bytes.fromhex(user["password_salt"])
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, user["password_iterations"]
    )
    return secrets.compare_digest(actual.hex(), user["password_hash"])


def ensure_database(database: Path) -> list[dict[str, str]]:
    database.parent.mkdir(parents=True, exist_ok=True)
    db = connect(database)
    try:
        credentials = seed_demo(db)
    finally:
        db.close()
    return credentials


def startup(database: Path) -> None:
    credentials = ensure_database(database)
    environment = os.environ.get("SCHOOL_COMMAND_ENV", "development").lower()
    if credentials and environment != "production":
        print(
            "\nFirst-time SCHOOL COMMAND demo setup complete. Save these generated "
            "credentials; they will not be shown again:"
        )
        for item in credentials:
            print(f"  {item['role']}: {item['email']} / {item['password']}")
        print()
    elif credentials:
        print("Demo users were provisioned. Set up authorized credentials before login.")
    else:
        print("SCHOOL COMMAND demo database already exists; no duplicate data was added.")


def database_summary(db: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "students", "employees", "class_sections", "teacher_assignments",
        "attendance", "fees", "fee_payments", "buses", "classrooms",
        "smart_boards", "board_maintenance", "technical_issues",
        "announcements", "announcement_reads", "examinations", "demo_users",
    )
    return {
        table: db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE is_demo = 1"
        ).fetchone()[0]
        for table in tables
    }


def verify_database(database: Path) -> list[str]:
    db = connect(database)
    errors: list[str] = []
    try:
        counts = database_summary(db)
        minimums = {
            "students": 200, "employees": 40, "class_sections": 30,
            "teacher_assignments": 30, "attendance": 240 * 30,
            "fees": 200, "fee_payments": 100, "buses": 8,
            "classrooms": 65, "smart_boards": 65, "board_maintenance": 65,
            "technical_issues": 10, "announcements": 10,
            "announcement_reads": 50, "examinations": 1000, "demo_users": 6,
        }
        for table, minimum in minimums.items():
            if counts[table] < minimum:
                errors.append(f"{table}: expected at least {minimum}; found {counts[table]}")
        for category, minimum in (("teacher", 25), ("staff", 15)):
            actual = db.execute(
                "SELECT COUNT(*) FROM employees WHERE is_demo = 1 AND category = ?",
                (category,),
            ).fetchone()[0]
            if actual < minimum:
                errors.append(f"{category}s: expected at least {minimum}; found {actual}")
        class_names = {
            row[0] for row in db.execute(
                "SELECT DISTINCT class_name FROM class_sections WHERE is_demo=1"
            )
        }
        missing_classes = sorted(set(CLASS_NAMES) - class_names)
        if missing_classes:
            errors.append(f"missing class records: {', '.join(missing_classes)}")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("foreign-key check reported invalid references")
        for table, column in (
            ("students", "admission_number"), ("students", "student_id"),
            ("employees", "employee_id"), ("smart_boards", "board_id"),
            ("buses", "bus_number"), ("buses", "registration_number"),
        ):
            duplicate = db.execute(
                f"SELECT {column}, COUNT(*) n FROM {table} WHERE is_demo = 1 "
                f"GROUP BY {column} HAVING n > 1 LIMIT 1"
            ).fetchone()
            if duplicate:
                errors.append(f"duplicate {table}.{column}: {duplicate[0]}")
        dates = db.execute(
            "SELECT COUNT(DISTINCT attendance_date), MIN(attendance_date), "
            "MAX(attendance_date), COUNT(*) FROM attendance WHERE is_demo = 1"
        ).fetchone()
        if dates[0] < 30 or dates[3] < 240 * 30:
            errors.append("attendance does not cover 30 school days for all demo students")
        elif dates[2]:
            expected_days = {
                day.isoformat() for day in school_days(date.fromisoformat(dates[2]), 30)
            }
            actual_days = {
                row[0] for row in db.execute(
                    "SELECT DISTINCT attendance_date FROM attendance WHERE is_demo=1"
                )
            }
            if actual_days != expected_days:
                errors.append("attendance dates are not 30 consecutive school days")
        fee_coverage = db.execute(
            "SELECT COUNT(*) FROM students s LEFT JOIN fees f "
            "ON f.student_id = s.student_id AND f.is_demo = 1 "
            "WHERE s.is_demo = 1 AND f.fee_id IS NULL"
        ).fetchone()[0]
        if fee_coverage:
            errors.append(f"{fee_coverage} demo students have no fee record")
        attendance_gaps = db.execute(
            "SELECT COUNT(*) FROM students s LEFT JOIN ("
            "SELECT student_id,COUNT(DISTINCT attendance_date) AS day_count "
            "FROM attendance WHERE is_demo=1 GROUP BY student_id"
            ") a ON a.student_id=s.student_id "
            "WHERE s.is_demo=1 AND COALESCE(a.day_count,0)<30"
        ).fetchone()[0]
        if attendance_gaps:
            errors.append(f"{attendance_gaps} demo students lack 30 school-day attendance records")
        payment_mismatches = db.execute(
            "SELECT COUNT(*) FROM fees f LEFT JOIN ("
            "SELECT fee_id,SUM(amount) AS paid FROM fee_payments "
            "WHERE is_demo=1 GROUP BY fee_id"
            ") p ON p.fee_id=f.fee_id "
            "WHERE f.is_demo=1 AND f.paid_amount<>COALESCE(p.paid,0)"
        ).fetchone()[0]
        if payment_mismatches:
            errors.append(f"{payment_mismatches} demo fee accounts do not reconcile to payment history")
        required_fee_statuses = {"Paid", "Partially Paid", "Pending", "Overdue"}
        fee_statuses = {
            row[0] for row in db.execute(
                "SELECT DISTINCT payment_status FROM fees WHERE is_demo=1"
            )
        }
        if not required_fee_statuses.issubset(fee_statuses):
            errors.append("fee records do not include paid, partial, pending, and overdue examples")
        issue_statuses = {
            row[0] for row in db.execute(
                "SELECT DISTINCT status FROM technical_issues WHERE is_demo=1"
            )
        }
        if not {"Reported", "Acknowledged", "In Progress", "Resolved"}.issubset(issue_statuses):
            errors.append("technical issues do not cover every requested status")
        board_statuses = {
            row[0] for row in db.execute(
                "SELECT DISTINCT status FROM smart_boards WHERE is_demo=1"
            )
        }
        if not {"Online", "Offline", "Error", "Under Maintenance"}.issubset(board_statuses):
            errors.append("smart boards do not cover every requested status")
        online_boards = db.execute(
            "SELECT COUNT(*) FROM smart_boards WHERE is_demo=1 AND status='Online'"
        ).fetchone()[0]
        if online_boards <= counts["smart_boards"] / 2:
            errors.append("most demo smart boards should be online")
        required_roles = {
            "Director", "Principal", "Class Teacher", "Subject Teacher",
            "Parent", "Technical Team",
        }
        roles = {
            row[0] for row in db.execute(
                "SELECT role FROM demo_users WHERE is_demo=1"
            )
        }
        if not required_roles.issubset(roles):
            errors.append("one or more required demo login roles are missing")
    except sqlite3.Error as error:
        errors.append(f"database verification could not complete: {error}")
    finally:
        db.close()
    return errors


def _page_start(title: str, active: str, user: sqlite3.Row | None = None) -> str:
    identity = ""
    if user:
        identity = (
            f'<span class="user-role">{html.escape(user["role"])}</span>'
            '<form method="post" action="/logout" class="inline">'
            f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
            '<button class="link-button" type="submit">Sign out</button></form>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · SCHOOL COMMAND</title>
<style>
:root{{--navy:#12233f;--blue:#2456a6;--ink:#17243a;--muted:#66758b;--line:#e4e9f1;--bg:#f4f7fb;--white:#fff;--green:#167c58;--red:#b83d4b;--amber:#a56512}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
.top{{background:var(--navy);color:white;display:flex;align-items:center;justify-content:space-between;padding:15px 25px;gap:12px}}
.brand{{font-weight:750;font-size:19px;letter-spacing:.02em}}.brand small{{display:block;font-size:11px;font-weight:500;opacity:.72}}
.identity{{display:flex;align-items:center;gap:16px}}.user-role{{background:#294568;border-radius:20px;padding:5px 11px}}
.demo{{background:#fff0d3;color:#754300;border-bottom:1px solid #f1d59d;text-align:center;padding:8px;font-size:12px;font-weight:750;letter-spacing:.04em}}
nav{{background:white;border-bottom:1px solid var(--line);display:flex;gap:5px;padding:0 20px;overflow:auto}}
nav a{{color:#526179;text-decoration:none;padding:13px 10px;white-space:nowrap;border-bottom:2px solid transparent}}
nav a.active{{color:var(--blue);font-weight:700;border-color:var(--blue)}}
main{{max-width:1450px;margin:auto;padding:26px 24px 48px}}h1{{font-size:25px;margin:0 0 5px}}h2{{font-size:17px;margin:0 0 14px}}.sub{{color:var(--muted);margin:0 0 22px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:13px;margin:20px 0}}
.card,.panel{{background:white;border:1px solid var(--line);border-radius:11px;padding:17px}}
.card .label{{color:var(--muted);font-size:12px}}.card .value{{font-size:26px;font-weight:760;margin:3px 0}}.card .hint{{font-size:11px;color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}}.barrow{{display:grid;grid-template-columns:minmax(95px,1fr) 3fr 45px;gap:10px;align-items:center;margin:10px 0;font-size:12px}}
.track{{height:9px;background:#edf1f7;border-radius:9px;overflow:hidden}}.bar{{height:100%;background:#4379c8;border-radius:9px}}
.table-wrap{{overflow:auto;background:white;border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}}th,td{{padding:10px 12px;border-bottom:1px solid #edf0f5;text-align:left}}th{{background:#f8fafd;color:#596980;font-weight:650;position:sticky;top:0}}
.badge{{display:inline-block;border-radius:20px;padding:3px 8px;background:#eef2f8;color:#46566c;font-size:10px;font-weight:700}}.badge.demo{{background:#fff0d3;color:#754300}}.status-online,.status-present,.status-resolved,.status-active,.status-paid{{color:var(--green)}}.status-error,.status-absent,.status-overdue,.status-reported{{color:var(--red)}}.status-maintenance,.status-late,.status-in-progress,.status-acknowledged{{color:var(--amber)}}
.section-head{{display:flex;justify-content:space-between;align-items:center;margin:24px 0 12px}}.notice{{padding:12px 14px;border:1px solid #b8e4d3;background:#eaf8f1;border-radius:8px;margin:12px 0}}
.danger-panel{{border-color:#f0d7d9}}.danger-panel p{{color:var(--muted)}}input,button{{font:inherit}}input[type=text],input[type=email],input[type=password]{{border:1px solid #cbd4e1;padding:10px;border-radius:6px;max-width:100%}}
button,.button{{background:var(--blue);color:white;border:0;border-radius:6px;padding:10px 14px;font-weight:650;cursor:pointer;text-decoration:none;display:inline-block}}
.danger{{background:#a83240}}.link-button{{background:none;color:#fff;padding:4px;text-decoration:underline}}.inline{{display:inline}}.login{{max-width:430px;margin:55px auto}}.login .panel{{padding:27px}}.login label{{display:block;margin:13px 0 5px;font-weight:600}}.login input{{width:100%}}.login button{{margin-top:18px;width:100%}}
.empty{{color:var(--muted);padding:24px;text-align:center}}.footer{{text-align:center;color:#718096;font-size:11px;padding:20px}}
@media(max-width:600px){{.top{{padding:12px 15px}}main{{padding:20px 13px}}.identity{{gap:8px}}nav{{padding:0 7px}}}}
</style></head><body data-page="{html.escape(active)}">
<header class="top"><div class="brand">SCHOOL COMMAND<small>School operations platform</small></div>
<div class="identity">{identity}</div></header>
<div class="demo">DEMO MODE · FICTIONAL DEMONSTRATION RECORDS · NOT REAL SCHOOL DATA</div>
"""


def _page_end() -> str:
    return '<div class="footer">All displayed records are fictional demo data.</div></body></html>'


def _table(headers: list[str], rows: list[tuple]) -> str:
    if not rows:
        return '<div class="empty">No records available.</div>'
    thead = "".join(f"<th>{html.escape(str(header))}</th>" for header in headers)
    body = []
    for row in rows:
        cells = []
        for value in row:
            text = html.escape("" if value is None else str(value))
            if text in ("Present", "Online", "Resolved", "Active", "Paid"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            elif text in ("Absent", "Error", "Overdue", "Reported"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            elif text in ("Late", "In Progress", "Acknowledged", "Under Maintenance"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            cells.append(f"<td>{text}</td>")
        cells[0] = '<td><span class="badge demo">DEMO</span> ' + cells[0][4:]
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _bar_chart(rows: list[tuple[str, float]], suffix: str = "%") -> str:
    peak = max((value for _, value in rows), default=0) or 1
    return "".join(
        f'<div class="barrow"><span>{html.escape(label)}</span><div class="track">'
        f'<div class="bar" style="width:{max(0, min(100, value / peak * 100)):.1f}%"></div>'
        f'</div><strong>{value:.0f}{suffix}</strong></div>'
        for label, value in rows
    ) or '<div class="empty">No chart data available.</div>'


def _scoped_dashboard(db: sqlite3.Connection, user: sqlite3.Row) -> str:
    role = user["role"]
    cards: list[tuple[str, str, str]] = []
    panels = ""
    if role == "Technical Team":
        total_boards = db.execute(
            "SELECT COUNT(*) FROM smart_boards WHERE is_demo=1"
        ).fetchone()[0]
        online_boards = db.execute(
            "SELECT COUNT(*) FROM smart_boards WHERE is_demo=1 AND status='Online'"
        ).fetchone()[0]
        unresolved = db.execute(
            "SELECT COUNT(*) FROM technical_issues WHERE is_demo=1 AND status!='Resolved'"
        ).fetchone()[0]
        pending = db.execute(
            "SELECT i.issue_id,cl.room_number,i.category,i.priority,e.name,i.status "
            "FROM technical_issues i JOIN classrooms cl ON cl.classroom_id=i.classroom_id "
            "JOIN employees e ON e.employee_id=i.technician_id "
            "WHERE i.is_demo=1 AND i.status!='Resolved' "
            "ORDER BY CASE i.priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END,"
            "i.reported_at DESC"
        ).fetchall()
        cards = [
            ("Smart boards", str(total_boards), "Installed classroom boards"),
            ("Online", str(online_boards), "Current board status"),
            ("Pending repairs", str(unresolved), "Issues not yet resolved"),
        ]
        panels = '<section class="panel"><h2>Pending repair issues</h2>' + _table(
            ["Issue", "Room", "Category", "Priority", "Assigned technician", "Status"],
            [tuple(row) for row in pending],
        ) + "</section>"
        title = "Technical operations"
    elif role == "Parent":
        children = db.execute(
            "SELECT s.student_id,s.full_name,c.class_name,c.section_name "
            "FROM parent_children pc JOIN students s ON s.student_id=pc.student_id "
            "JOIN class_sections c ON c.section_id=s.section_id "
            "WHERE pc.user_id=? AND pc.is_demo=1 AND s.is_demo=1 ORDER BY s.full_name",
            (user["user_id"],),
        ).fetchall()
        student_count = len(children)
        student_ids = [row["student_id"] for row in children]
        fees = 0
        if student_ids:
            placeholders = ",".join("?" for _ in student_ids)
            fees = db.execute(
                f"SELECT COALESCE(SUM(pending_amount),0) FROM fees "
                f"WHERE is_demo=1 AND student_id IN ({placeholders})",
                student_ids,
            ).fetchone()[0]
        cards = [
            ("Linked children", str(student_count), "Only your linked students"),
            ("Fees pending", money(fees), "For your linked children"),
        ]
        panels = '<section class="panel"><h2>Your children</h2>' + _table(
            ["Student", "Class", "Section"],
            [(row["full_name"], row["class_name"], row["section_name"]) for row in children],
        ) + "</section>"
        title = "Family dashboard"
    else:
        student_filter, student_params = student_scope(user, "s", "c")
        student_count = db.execute(
            "SELECT COUNT(*) FROM students s JOIN class_sections c "
            "ON c.section_id=s.section_id WHERE s.is_demo=1 AND " + student_filter,
            student_params,
        ).fetchone()[0]
        cards = [("Assigned students", str(student_count), "Only assigned classes")]
        panels = ""
        if role == "Class Teacher":
            latest = db.execute(
                "SELECT MAX(a.attendance_date) FROM attendance a JOIN students s "
                "ON s.student_id=a.student_id JOIN class_sections c ON c.section_id=s.section_id "
                "WHERE a.is_demo=1 AND " + student_filter,
                student_params,
            ).fetchone()[0]
            if latest:
                attendance = db.execute(
                    "SELECT COUNT(*),SUM(CASE WHEN a.status IN ('Present','Late') THEN 1 ELSE 0 END) "
                    "FROM attendance a JOIN students s ON s.student_id=a.student_id "
                    "JOIN class_sections c ON c.section_id=s.section_id "
                    "WHERE a.is_demo=1 AND a.attendance_date=? AND " + student_filter,
                    (latest, *student_params),
                ).fetchone()
                rate = 100 * (attendance[1] or 0) / attendance[0] if attendance[0] else 0
                cards.append(("Attendance", f"{rate:.1f}%", f"Assigned classes · {latest}"))
        exam_filter, exam_params = student_scope(user, "s", "c", "e.subject")
        averages = db.execute(
            "SELECT c.class_name || ' ' || c.section_name, "
            "ROUND(AVG(100.0*e.marks_obtained/e.maximum_marks),1) "
            "FROM examinations e JOIN students s ON s.student_id=e.student_id "
            "JOIN class_sections c ON c.section_id=e.section_id "
            "WHERE e.is_demo=1 AND " + exam_filter +
            " GROUP BY c.section_id ORDER BY c.class_name,c.section_name",
            exam_params,
        ).fetchall()
        panels = '<section class="panel"><h2>Assigned-class academic results</h2>' + _bar_chart(
            [(row[0], row[1]) for row in averages]
        ) + "</section>"
        title = "Class dashboard" if role == "Class Teacher" else "Subject dashboard"
    card_html = "".join(
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        f'<div class="hint">{html.escape(hint)}</div></div>'
        for label, value, hint in cards
    )
    return (
        f"<main><h1>{title}</h1><p class=\"sub\">Dashboard data is limited to your role and assignments.</p>"
        f'<section class="cards">{card_html}</section><div class="grid">{panels}</div></main>'
    )


def dashboard(db: sqlite3.Connection, user: sqlite3.Row) -> str:
    if user["role"] not in ADMIN_ROLES:
        return _scoped_dashboard(db, user)
    scalar = lambda sql, params=(): db.execute(sql, params).fetchone()[0]
    students = scalar("SELECT COUNT(*) FROM students WHERE is_demo=1")
    teachers = scalar("SELECT COUNT(*) FROM employees WHERE is_demo=1 AND category='teacher'")
    staff = scalar("SELECT COUNT(*) FROM employees WHERE is_demo=1 AND category='staff'")
    today = datetime.now().date().isoformat()
    attendance = db.execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) present, "
        "SUM(CASE WHEN status='Absent' THEN 1 ELSE 0 END) absent, "
        "SUM(CASE WHEN status='Late' THEN 1 ELSE 0 END) late "
        "FROM attendance WHERE is_demo=1 AND attendance_date=?",
        (today,),
    ).fetchone()
    # Weekends have no attendance, so present the most recent school-day snapshot.
    attendance_date = today
    if not attendance["total"]:
        latest = db.execute(
            "SELECT MAX(attendance_date) FROM attendance WHERE is_demo=1"
        ).fetchone()[0]
        attendance_date = latest or today
        attendance = db.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) present, "
            "SUM(CASE WHEN status='Absent' THEN 1 ELSE 0 END) absent, "
            "SUM(CASE WHEN status='Late' THEN 1 ELSE 0 END) late "
            "FROM attendance WHERE is_demo=1 AND attendance_date=?",
            (attendance_date,),
        ).fetchone()
    present, absent, late = (attendance[name] or 0 for name in ("present", "absent", "late"))
    attendance_rate = (present + late) * 100 / attendance["total"] if attendance["total"] else 0
    collected = scalar(
        "SELECT COALESCE(SUM(amount),0) FROM fee_payments WHERE is_demo=1"
    )
    pending = scalar("SELECT COALESCE(SUM(pending_amount),0) FROM fees WHERE is_demo=1")
    online = scalar("SELECT COUNT(*) FROM smart_boards WHERE is_demo=1 AND status='Online'")
    unresolved = scalar("SELECT COUNT(*) FROM technical_issues WHERE is_demo=1 AND status!='Resolved'")
    active_buses = scalar("SELECT COUNT(*) FROM buses WHERE is_demo=1 AND status='Active'")
    cards = [
        ("Students", f"{students:,}", "Enrolled demo students"),
        ("Teachers", str(teachers), "Active demo teachers"),
        ("Non-teaching staff", str(staff), "Demo staff members"),
        (f"Present · {attendance_date}", f"{present:,}", "Latest school-day snapshot"),
        ("Absent today", f"{absent:,}", f"Late: {late:,}"),
        ("Attendance", f"{attendance_rate:.1f}%", "Present and late / recorded students"),
        ("Fees collected", money(collected), "Sum of recorded payments"),
        ("Fees pending", money(pending), "Outstanding fee balances"),
        ("Online smart boards", str(online), "Calculated from board status"),
        ("Unresolved issues", str(unresolved), "All issues except resolved"),
        ("Active buses", str(active_buses), "Routes with active status"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="label">{label}</div><div class="value">{value}</div><div class="hint">{hint}</div></div>'
        for label, value, hint in cards
    )
    class_rows = db.execute(
        "SELECT c.class_name || ' ' || c.section_name, "
        "ROUND(100.0 * SUM(CASE WHEN a.status IN ('Present','Late') THEN 1 ELSE 0 END) / COUNT(*),1) "
        "FROM attendance a JOIN students s ON s.student_id=a.student_id "
        "JOIN class_sections c ON c.section_id=s.section_id "
        "WHERE a.is_demo=1 AND a.attendance_date=? "
        "GROUP BY c.section_id ORDER BY c.class_name,c.section_name",
        (attendance_date,),
    ).fetchall()
    board_rows = db.execute(
        "SELECT status, COUNT(*) FROM smart_boards WHERE is_demo=1 GROUP BY status "
        "ORDER BY CASE status WHEN 'Online' THEN 1 WHEN 'Offline' THEN 2 WHEN 'Error' THEN 3 ELSE 4 END"
    ).fetchall()
    academic_rows = db.execute(
        "SELECT c.class_name, ROUND(AVG(100.0*e.marks_obtained/e.maximum_marks),1) "
        "FROM examinations e JOIN class_sections c ON c.section_id=e.section_id "
        "WHERE e.is_demo=1 GROUP BY c.class_name ORDER BY c.class_name"
    ).fetchall()
    content = f"""<main><h1>School overview</h1>
<p class="sub">Live summary calculated from the demo database · attendance snapshot {attendance_date}</p>
<section class="cards">{cards_html}</section>
<div class="grid">
<section class="panel"><h2>Class attendance</h2>{_bar_chart([(row[0], row[1]) for row in class_rows])}</section>
<section class="panel"><h2>Smart-board status</h2>{_bar_chart([(row[0], row[1]) for row in board_rows], " boards")}</section>
<section class="panel"><h2>Average examination score by class</h2>{_bar_chart([(row[0], row[1]) for row in academic_rows])}</section>
<section class="panel"><h2>Fee position</h2>{_bar_chart([("Collected", collected), ("Outstanding", pending)], " rupees")}</section>
</div></main>"""
    return content


def module_page(db: sqlite3.Connection, page: str, user: sqlite3.Row) -> str:
    if not can_access_module(user, page):
        raise PermissionError
    config = {
        "students": (
            "Students", "All demo students across Nursery to Class XII.",
            """SELECT s.full_name, s.admission_number, c.class_name, c.section_name,
            s.roll_number, s.date_of_birth, s.gender, s.guardian_name,
            s.guardian_phone, COALESCE(b.bus_number,'Not assigned'), s.status
            FROM students s JOIN class_sections c ON c.section_id=s.section_id
            LEFT JOIN buses b ON b.bus_id=s.bus_id
            WHERE s.is_demo=1 ORDER BY c.class_name,c.section_name,s.roll_number""",
            ["Student", "Admission no.", "Class", "Section", "Roll", "Date of birth",
             "Gender", "Parent/guardian", "Parent phone", "Bus", "Status"],
        ),
        "staff": (
            "Teachers and staff", "Teacher assignments, departments, and non-teaching staff.",
            """SELECT category, name, employee_id, designation, department, subjects,
            assigned_classes, email, phone, joining_date, status
            FROM employees WHERE is_demo=1 ORDER BY category,name""",
            ["Category", "Name", "Employee ID", "Designation", "Department", "Subjects",
             "Assigned classes", "Email", "Phone", "Joined", "Status"],
        ),
        "attendance": (
            "Attendance", "Individual daily attendance records for the previous 30 school days.",
            """SELECT a.attendance_date, s.full_name, c.class_name, c.section_name,
            s.roll_number, a.status FROM attendance a
            JOIN students s ON s.student_id=a.student_id
            JOIN class_sections c ON c.section_id=s.section_id
            WHERE a.is_demo=1 ORDER BY a.attendance_date DESC,c.class_name,c.section_name,s.roll_number
            LIMIT 1500""",
            ["Date", "Student", "Class", "Section", "Roll", "Status"],
        ),
        "fees": (
            "Fees", "Fee balances and collected payments are linked to individual students.",
            """SELECT s.full_name, s.admission_number, c.class_name, f.fee_category,
            f.annual_total, f.paid_amount, f.pending_amount, f.due_date, f.payment_status
            FROM fees f JOIN students s ON s.student_id=f.student_id
            JOIN class_sections c ON c.section_id=s.section_id
            WHERE f.is_demo=1 ORDER BY f.payment_status,f.pending_amount DESC""",
            ["Student", "Admission no.", "Class", "Fee category", "Annual total",
             "Paid", "Pending", "Due date", "Payment status"],
        ),
        "payments": (
            "Fee payment history", "Actual demo payment records linked to fee accounts and students.",
            """SELECT p.receipt_number,s.full_name,s.admission_number,p.payment_date,
            p.amount,p.method,f.academic_session FROM fee_payments p
            JOIN fees f ON f.fee_id=p.fee_id JOIN students s ON s.student_id=f.student_id
            WHERE p.is_demo=1 ORDER BY p.payment_date DESC,p.receipt_number""",
            ["Receipt", "Student", "Admission no.", "Payment date", "Amount", "Method", "Session"],
        ),
        "buses": (
            "Transport", "GPS coordinates and locations are simulated for demonstration only.",
            """SELECT b.bus_number,b.registration_number,b.driver_name,b.driver_phone,b.route,
            b.capacity,(SELECT COUNT(*) FROM students s WHERE s.bus_id=b.bus_id AND s.is_demo=1),
            printf('%.4f, %.4f · %s',b.latitude,b.longitude,b.location_label),b.status
            FROM buses b WHERE b.is_demo=1 ORDER BY b.bus_number""",
            ["Bus no.", "Registration", "Driver", "Phone", "Route", "Capacity",
             "Assigned students", "Simulated location", "Status"],
        ),
        "boards": (
            "Smart boards", "One BenQ board is installed in each demo classroom.",
            """SELECT cl.room_number,b.board_id,b.brand,b.model,b.status,b.last_update,
            COUNT(m.maintenance_id) FROM smart_boards b
            JOIN classrooms cl ON cl.classroom_id=b.classroom_id
            LEFT JOIN board_maintenance m ON m.board_id=b.board_id AND m.is_demo=1
            WHERE b.is_demo=1 GROUP BY b.board_id ORDER BY cl.room_number""",
            ["Room", "Board ID", "Brand", "Model", "Status", "Last update", "Maintenance entries"],
        ),
        "issues": (
            "Technical issues", "Reported smart-board problems, including unresolved work.",
            """SELECT i.issue_id,cl.room_number,i.board_id,i.category,i.description,
            i.priority,i.reporting_user,i.reported_at,e.name,i.status,i.repair_notes
            FROM technical_issues i JOIN classrooms cl ON cl.classroom_id=i.classroom_id
            JOIN employees e ON e.employee_id=i.technician_id
            WHERE i.is_demo=1 ORDER BY i.reported_at DESC""",
            ["Issue ID", "Room", "Board ID", "Category", "Description", "Priority",
             "Reported by", "Reported at", "Technician", "Status", "Repair notes"],
        ),
        "announcements": (
            "Announcements", "Published school notices with this account's read/unread status.",
            """SELECT a.title,a.description,a.audience,a.author,a.published_at,a.priority,
            r.read_status FROM announcements a LEFT JOIN announcement_reads r
            ON r.announcement_id=a.announcement_id AND r.user_id=?
            WHERE a.is_demo=1 ORDER BY a.published_at DESC""",
            ["Title", "Description", "Audience", "Author", "Date", "Priority", "Read status"],
        ),
        "academics": (
            "Academic records", "Examination marks are linked to students, classes, subjects, and sessions.",
            """SELECT s.full_name,c.class_name,c.section_name,e.examination_name,e.subject,
            e.marks_obtained,e.maximum_marks,e.grade,e.academic_session
            FROM examinations e JOIN students s ON s.student_id=e.student_id
            JOIN class_sections c ON c.section_id=e.section_id
            WHERE e.is_demo=1 ORDER BY c.class_name,c.section_name,e.examination_name,s.full_name,e.subject
            LIMIT 1500""",
            ["Student", "Class", "Section", "Examination", "Subject", "Marks", "Out of",
             "Grade", "Session"],
        ),
        "reports": (
            "Student report cards", "Report-card summaries are calculated from each student's examination records.",
            """SELECT s.full_name,s.admission_number,c.class_name,c.section_name,
            COUNT(e.exam_id),ROUND(AVG(100.0*e.marks_obtained/e.maximum_marks),1),
            CASE WHEN AVG(100.0*e.marks_obtained/e.maximum_marks)>=90 THEN 'A+'
                 WHEN AVG(100.0*e.marks_obtained/e.maximum_marks)>=80 THEN 'A'
                 WHEN AVG(100.0*e.marks_obtained/e.maximum_marks)>=70 THEN 'B'
                 WHEN AVG(100.0*e.marks_obtained/e.maximum_marks)>=60 THEN 'C' ELSE 'D' END
            FROM students s JOIN class_sections c ON c.section_id=s.section_id
            JOIN examinations e ON e.student_id=s.student_id AND e.is_demo=1
            WHERE s.is_demo=1 GROUP BY s.student_id ORDER BY c.class_name,c.section_name,s.full_name""",
            ["Student", "Admission no.", "Class", "Section", "Subject records",
             "Average score", "Overall grade"],
        ),
    }
    heading, description, query, headers = config[page]
    if page == "students" and user["role"] == "Subject Teacher":
        query = """SELECT s.full_name,s.admission_number,c.class_name,c.section_name,
            s.roll_number,s.status FROM students s
            JOIN class_sections c ON c.section_id=s.section_id
            WHERE s.is_demo=1 ORDER BY c.class_name,c.section_name,s.roll_number"""
        headers = ["Student", "Admission no.", "Class", "Section", "Roll", "Status"]
    scoped_params: tuple[str, ...] = ()
    if page in {"students", "attendance", "fees", "academics"}:
        scope, scoped_params = student_scope(user, "s", "c", "e.subject" if page == "academics" else None)
        marker = {
            "students": "WHERE s.is_demo=1",
            "attendance": "WHERE a.is_demo=1",
            "fees": "WHERE f.is_demo=1",
            "academics": "WHERE e.is_demo=1",
        }[page]
        query = query.replace(marker, marker + " AND " + scope)
    elif page == "reports":
        scope, scoped_params = student_scope(user, "s", "c")
        query = query.replace("WHERE s.is_demo=1", "WHERE s.is_demo=1 AND " + scope)
    elif page == "payments" and user["role"] == "Parent":
        scope, scoped_params = student_scope(user, "s", "c")
        query = query.replace(
            "WHERE p.is_demo=1",
            "WHERE p.is_demo=1 AND " + scope,
        )
    elif page == "buses" and user["role"] == "Parent":
        query = query.replace(
            "(SELECT COUNT(*) FROM students s WHERE s.bus_id=b.bus_id AND s.is_demo=1)",
            "(SELECT COUNT(*) FROM students s JOIN parent_children pc "
            "ON pc.student_id=s.student_id WHERE s.bus_id=b.bus_id AND s.is_demo=1 "
            "AND pc.user_id=? AND pc.is_demo=1)",
        ).replace(
            "WHERE b.is_demo=1",
            "WHERE b.is_demo=1 AND EXISTS (SELECT 1 FROM students s "
            "JOIN parent_children pc ON pc.student_id=s.student_id "
            "WHERE pc.user_id=? AND pc.is_demo=1 AND s.bus_id=b.bus_id AND s.is_demo=1)",
        )
        scoped_params = (user["user_id"], user["user_id"])
    elif page == "announcements" and user["role"] not in ADMIN_ROLES:
        audiences = ANNOUNCEMENT_AUDIENCES.get(user["role"], ())
        if not audiences:
            query = query.replace("WHERE a.is_demo=1", "WHERE 0=1")
        else:
            audience_slots = ",".join("?" for _ in audiences)
            query = query.replace(
                "WHERE a.is_demo=1",
                f"WHERE a.is_demo=1 AND a.audience IN ({audience_slots})",
            )
            scoped_params = audiences
    if page == "announcements":
        rows = [tuple(row) for row in db.execute(query, (user["user_id"], *scoped_params)).fetchall()]
    else:
        rows = [tuple(row) for row in db.execute(query, scoped_params).fetchall()]
    table = _table(headers, rows)
    forms = ""
    if page == "attendance" and user["role"] in ADMIN_ROLES | {"Class Teacher"}:
        scope, params = student_scope(user, "s", "c")
        student_options = "".join(
            f'<option value="{html.escape(row["student_id"])}">'
            f'{html.escape(row["full_name"])} · {html.escape(row["class_name"])} '
            f'{html.escape(row["section_name"])}</option>'
            for row in db.execute(
                "SELECT s.student_id,s.full_name,c.class_name,c.section_name "
                "FROM students s JOIN class_sections c ON c.section_id=s.section_id "
                "WHERE s.is_demo=1 AND " + scope +
                " ORDER BY c.class_name,c.section_name,s.roll_number",
                params,
            )
        )
        forms = f"""<section class="panel"><h2>Update attendance</h2>
<form method="post" action="/attendance/update"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<label>Student <select name="student_id" required>{student_options}</select></label>
<label>Date <input name="attendance_date" type="date" required></label>
<label>Status <select name="status"><option>Present</option><option>Absent</option><option>Late</option><option>Leave</option></select></label>
<button type="submit">Save attendance</button></form></section>"""
    elif page == "academics" and user["role"] in ADMIN_ROLES | {"Class Teacher", "Subject Teacher"}:
        scope, params = student_scope(user, "s", "c", "e.subject")
        exam_options = "".join(
            f'<option value="{html.escape(row["exam_id"])}">'
            f'{html.escape(row["full_name"])} · {html.escape(row["class_name"])} '
            f'{html.escape(row["subject"])} · {html.escape(row["examination_name"])}</option>'
            for row in db.execute(
                "SELECT e.exam_id,e.subject,e.examination_name,s.full_name,c.class_name "
                "FROM examinations e JOIN students s ON s.student_id=e.student_id "
                "JOIN class_sections c ON c.section_id=e.section_id "
                "WHERE e.is_demo=1 AND " + scope +
                " ORDER BY c.class_name,c.section_name,s.full_name,e.subject LIMIT 1000",
                params,
            )
        )
        forms = f"""<section class="panel"><h2>Update examination mark</h2>
<form method="post" action="/academics/update"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<label>Student and examination <select name="exam_id" required>{exam_options}</select></label>
<label>Marks <input name="marks" type="number" min="0" required></label>
<button type="submit">Save mark</button></form></section>"""
    elif page == "issues" and issue_status_allowed(user):
        status_options = "".join(
            f'<option value="{status}">{status}</option>'
            for status in ("Reported", "Acknowledged", "In Progress", "Resolved")
        )
        issue_options = "".join(
            f'<option value="{html.escape(row["issue_id"])}">'
            f'{html.escape(row["issue_id"])} · {html.escape(row["category"])}</option>'
            for row in db.execute(
                "SELECT issue_id,category FROM technical_issues WHERE is_demo=1 "
                "ORDER BY reported_at DESC"
            )
        )
        board_options = "".join(
            f'<option value="{html.escape(row["board_id"])}">'
            f'{html.escape(row["room_number"])} · {html.escape(row["board_id"])}</option>'
            for row in db.execute(
                "SELECT b.board_id,cl.room_number FROM smart_boards b "
                "JOIN classrooms cl ON cl.classroom_id=b.classroom_id "
                "WHERE b.is_demo=1 ORDER BY cl.room_number"
            )
        )
        forms = f"""<div class="grid"><section class="panel"><h2>Update repair status</h2>
<form method="post" action="/issues/update"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<label>Issue <select name="issue_id" required>{issue_options}</select></label>
<label>Status <select name="status" required>{status_options}</select></label>
<label>Repair information <input name="repair_notes" type="text" maxlength="1000"></label>
<button type="submit">Save status</button></form></section>
<section class="panel"><h2>Report a technical issue</h2>
<form method="post" action="/issues/report"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<label>Classroom and board <select name="board_id" required>{board_options}</select></label>
<label>Category <input name="category" type="text" maxlength="80" required></label>
<label>Description <input name="description" type="text" maxlength="1000" required></label>
<label>Priority <select name="priority"><option>Low</option><option>Medium</option><option>High</option></select></label>
<button type="submit">Report issue</button></form></section></div>"""
    maintenance = ""
    if page == "boards" and user["role"] in ADMIN_ROLES | {"Technical Team"}:
        history = db.execute(
            "SELECT cl.room_number,m.maintenance_date,m.description,e.name,m.status "
            "FROM board_maintenance m JOIN smart_boards b ON b.board_id=m.board_id "
            "JOIN classrooms cl ON cl.classroom_id=b.classroom_id "
            "JOIN employees e ON e.employee_id=m.technician_id "
            "WHERE m.is_demo=1 ORDER BY m.maintenance_date DESC,m.maintenance_id"
        ).fetchall()
        maintenance = (
            '<section class="panel"><h2>Maintenance history</h2>' +
            _table(["Room", "Date", "Work performed", "Assigned technician", "Status"],
                   [tuple(row) for row in history]) + "</section>"
        )
    return f'<main><h1>{heading}</h1><p class="sub">{description} · {len(rows):,} displayed records.</p>{table}{forms}{maintenance}</main>'


def student_profile(db: sqlite3.Connection, student_id: str, user: sqlite3.Row) -> str:
    if not can_access_module(user, "students"):
        raise PermissionError
    scope, params = student_scope(user, "s", "c")
    parent_fields = (
        ",s.guardian_name,s.guardian_phone,s.guardian_email,s.address,b.bus_number,b.route"
        if user["role"] == "Parent" else ""
    )
    row = db.execute(
        "SELECT s.student_id,s.full_name,s.admission_number,s.date_of_birth,s.gender,"
        "s.roll_number,c.class_name,c.section_name" + parent_fields + " "
        "FROM students s JOIN class_sections c ON c.section_id=s.section_id "
        "LEFT JOIN buses b ON b.bus_id=s.bus_id "
        "WHERE s.student_id=? AND s.is_demo=1 AND " + scope,
        (student_id, *params),
    ).fetchone()
    if not row:
        if user["role"] not in ADMIN_ROLES:
            raise PermissionError
        raise LookupError
    details = [
        ("Student", row["full_name"]),
        ("Admission number", row["admission_number"]),
        ("Class and section", f'{row["class_name"]} {row["section_name"]}'),
        ("Roll number", row["roll_number"]),
        ("Date of birth", row["date_of_birth"]),
        ("Gender", row["gender"]),
    ]
    if user["role"] == "Parent":
        details.extend([
            ("Parent/guardian", row["guardian_name"]),
            ("Guardian phone", row["guardian_phone"]),
            ("Guardian email", row["guardian_email"]),
            ("Address", row["address"]),
            ("Assigned bus", row["bus_number"] or "Not assigned"),
            ("Bus route", row["route"] or "Not assigned"),
        ])
    table = _table(["Information", "Value"], details)
    return f'<main><h1>Student profile</h1>{table}</main>'


class SchoolHandler(BaseHTTPRequestHandler):
    database: Path
    server_version = "SchoolCommandDemo/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(f"[school-command] {self.address_string()} {fmt % args}\n")

    def send_html(self, body: str, status: int = 200, headers: list[tuple[str, str]] | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str, headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()

    def form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 16_384:
            raise ValueError("invalid form size")
        return parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)

    def user(self) -> sqlite3.Row | None:
        cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("school_session")
        if not morsel:
            return None
        digest = hashlib.sha256(morsel.value.encode()).hexdigest()
        with database_connection(self.database) as db:
            return db.execute(
                "SELECT u.user_id,u.email,u.role,u.student_id,u.employee_id,s.csrf_token FROM app_sessions s "
                "JOIN demo_users u ON u.user_id=s.user_id "
                "WHERE s.session_hash=? AND s.expires_at>? AND u.is_demo=1",
                (digest, datetime.now(timezone.utc).isoformat()),
            ).fetchone()

    def do_GET(self) -> None:
        path = urlparse(self.path)
        if path.path == "/login":
            user = self.user()
            if user:
                self.redirect("/")
                return
            content = """<main class="login"><div class="panel"><h1>Sign in</h1>
<p class="sub">Use a demo account provisioned during local setup.</p>
<form method="post" action="/login">
<label for="email">Email</label><input id="email" name="email" type="email" required autocomplete="username">
<label for="password">Password</label><input id="password" name="password" type="password" required autocomplete="current-password">
<button type="submit">Sign in to demo</button></form></div></main>"""
            self.send_html(_page_start("Sign in", "login") + content + _page_end())
            return
        if path.path not in ("/", "/health"):
            self.send_html(_page_start("Not found", "", self.user()) + "<main><h1>Page not found</h1></main>" + _page_end(), 404)
            return
        if path.path == "/health":
            self.send_html(_page_start("Health", "", self.user()) + "<main><h1>Demo app is running</h1></main>" + _page_end())
            return
        user = self.user()
        if not user:
            self.redirect("/login")
            return
        params = parse_qs(path.query)
        page = params.get("page", ["dashboard"])[0]
        if page not in {key for key, _ in NAV_ITEMS} | {"student"}:
            self.send_html(
                _page_start("Not found", "", user) + "<main><h1>Page not found</h1></main>" + _page_end(),
                404,
            )
            return
        if page == "student" and not can_access_module(user, "students"):
            self.send_html(
                _page_start("Not authorized", "", user) +
                "<main><h1>Not authorized</h1><p>This record is not available to your account.</p></main>" +
                _page_end(), 403,
            )
            return
        if page != "student" and not can_access_module(user, page):
            self.send_html(
                _page_start("Not authorized", "", user) +
                "<main><h1>Not authorized</h1><p>This module is not available to your account.</p></main>" +
                _page_end(), 403,
            )
            return
        try:
            with database_connection(self.database) as db:
                if page == "dashboard":
                    content = dashboard(db, user)
                elif page == "student":
                    content = student_profile(db, params.get("id", [""])[0], user)
                else:
                    content = module_page(db, page, user)
            reset_notice = '<div class="notice">Demo records have been safely reset and reseeded. Demo logins were retained.</div>' if params.get("reset") == ["1"] else ""
            reset_panel = ""
            if user["role"] in ADMIN_ROLES:
                reset_panel = f"""<section class="panel danger-panel" style="margin-top:22px">
<h2>Reset demo data</h2><p>Rebuild fictional demo records only. Actual school records are not selected or deleted.
Type <strong>RESET DEMO DATA</strong> to confirm. Your demo login will be retained.</p>
<form method="post" action="/reset-demo">
<input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<input type="text" name="confirmation" required autocomplete="off" aria-label="Reset confirmation">
<button class="danger" type="submit">Reset demo records</button></form></section>"""
            if page == "dashboard":
                content = content.replace("</main>", reset_notice + reset_panel + "</main>")
            page_title = (
                "Dashboard" if page == "dashboard" else
                "Student profile" if page == "student" else dict(NAV_ITEMS)[page]
            )
            body = _page_start(page_title, page, user)
            body += "<nav>" + "".join(
                f'<a class="{"active" if key == page else ""}" href="/?{urlencode({"page": key})}">{html.escape(label)}</a>'
                for key, label in NAV_ITEMS if can_access_module(user, key)
            ) + "</nav>"
            self.send_html(body + content + _page_end())
        except PermissionError:
            self.send_html(
                _page_start("Not authorized", "", user) +
                "<main><h1>Not authorized</h1><p>This record is not available to your account.</p></main>" +
                _page_end(), 403,
            )
        except LookupError:
            self.send_html(
                _page_start("Not found", "", user) +
                "<main><h1>Student not found</h1></main>" + _page_end(), 404,
            )
        except sqlite3.Error:
            self.log_error("Database error while rendering %s", page)
            self.send_html(_page_start("Application error", page, user) +
                           "<main><h1>Unable to load this page</h1><p>The database request failed. Check the application log and retry.</p></main>" +
                           _page_end(), 500)

    def do_POST(self) -> None:
        try:
            form = self.form()
        except ValueError:
            self.send_html("<main><h1>Request too large or invalid</h1></main>", 413)
            return
        if self.path == "/login":
            email = form.get("email", [""])[0].strip().lower()
            password = form.get("password", [""])[0]
            with database_connection(self.database) as db:
                user = db.execute(
                    "SELECT * FROM demo_users WHERE lower(email)=? AND is_demo=1", (email,)
                ).fetchone()
                if user and verify_password(password, user):
                    token = secrets.token_urlsafe(32)
                    csrf = secrets.token_urlsafe(24)
                    secure_cookie = "; Secure" if isinstance(self.connection, ssl.SSLSocket) else ""
                    db.execute(
                        "INSERT INTO app_sessions VALUES (?,?,?,?)",
                        (hashlib.sha256(token.encode()).hexdigest(), user["user_id"], csrf,
                         (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()),
                    )
                    self.redirect("/", [(
                        "Set-Cookie",
                        f"school_session={token}; HttpOnly; SameSite=Lax; Path=/; "
                        f"Max-Age=43200{secure_cookie}",
                    )])
                    return
            content = """<main class="login"><div class="panel"><h1>Sign in failed</h1>
<p class="sub">The email or password was not accepted.</p><a href="/login">Try again</a></div></main>"""
            self.send_html(_page_start("Sign in failed", "login") + content + _page_end(), 401)
            return
        user = self.user()
        if not user:
            self.redirect("/login")
            return
        if not secrets.compare_digest(form.get("csrf", [""])[0], user["csrf_token"]):
            self.send_html(_page_start("Request rejected", "", user) +
                           "<main><h1>Request rejected</h1><p>Refresh the page and try again.</p></main>" +
                           _page_end(), 403)
            return
        if self.path == "/logout":
            cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("school_session")
            if morsel:
                digest = hashlib.sha256(morsel.value.encode()).hexdigest()
                with database_connection(self.database) as db:
                    db.execute("DELETE FROM app_sessions WHERE session_hash=?", (digest,))
            secure_cookie = "; Secure" if isinstance(self.connection, ssl.SSLSocket) else ""
            self.redirect("/login", [(
                "Set-Cookie",
                f"school_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0{secure_cookie}",
            )])
            return
        if self.path == "/attendance/update":
            if user["role"] not in ADMIN_ROLES | {"Class Teacher"}:
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            student_id = form.get("student_id", [""])[0]
            attendance_date = form.get("attendance_date", [""])[0]
            status = form.get("status", [""])[0]
            try:
                date.fromisoformat(attendance_date)
            except ValueError:
                self.send_html("<main><h1>Invalid attendance date</h1></main>", 400)
                return
            if status not in {"Present", "Absent", "Late", "Leave"}:
                self.send_html("<main><h1>Invalid attendance status</h1></main>", 400)
                return
            scope, params = student_scope(user, "s", "c")
            with database_connection(self.database) as db:
                result = db.execute(
                    "UPDATE attendance SET status=? WHERE student_id=? AND attendance_date=? "
                    "AND is_demo=1 AND EXISTS (SELECT 1 FROM students s "
                    "JOIN class_sections c ON c.section_id=s.section_id "
                    "WHERE s.student_id=attendance.student_id AND " + scope + ")",
                    (status, student_id, attendance_date, *params),
                )
                if result.rowcount != 1:
                    self.send_html("<main><h1>Not authorized</h1><p>No accessible attendance record was changed.</p></main>", 403)
                    return
            self.redirect("/?page=attendance")
            return
        if self.path == "/academics/update":
            if user["role"] not in ADMIN_ROLES | {"Class Teacher", "Subject Teacher"}:
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            exam_id = form.get("exam_id", [""])[0]
            try:
                marks = int(form.get("marks", [""])[0])
            except ValueError:
                self.send_html("<main><h1>Invalid mark</h1></main>", 400)
                return
            scope, params = student_scope(user, "s", "c", "e.subject")
            with database_connection(self.database) as db:
                record = db.execute(
                    "SELECT e.maximum_marks FROM examinations e "
                    "JOIN students s ON s.student_id=e.student_id "
                    "JOIN class_sections c ON c.section_id=e.section_id "
                    "WHERE e.exam_id=? AND e.is_demo=1 AND " + scope,
                    (exam_id, *params),
                ).fetchone()
                if not record:
                    self.send_html("<main><h1>Not authorized</h1><p>No accessible examination record was changed.</p></main>", 403)
                    return
                if not 0 <= marks <= record["maximum_marks"]:
                    self.send_html("<main><h1>Invalid mark</h1></main>", 400)
                    return
                percentage = marks * 100 / record["maximum_marks"]
                grade = "A+" if percentage >= 90 else "A" if percentage >= 80 else "B" if percentage >= 70 else "C" if percentage >= 60 else "D"
                result = db.execute(
                    "UPDATE examinations AS e SET marks_obtained=?,grade=? "
                    "WHERE e.exam_id=? AND e.is_demo=1 AND EXISTS ("
                    "SELECT 1 FROM students s JOIN class_sections c ON c.section_id=e.section_id "
                    "WHERE s.student_id=e.student_id AND " + scope + ")",
                    (marks, grade, exam_id, *params),
                )
                if result.rowcount != 1:
                    self.send_html("<main><h1>Not authorized</h1><p>No accessible examination record was changed.</p></main>", 403)
                    return
            self.redirect("/?page=academics")
            return
        if self.path == "/issues/update":
            if not issue_status_allowed(user):
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            issue_id = form.get("issue_id", [""])[0]
            status = form.get("status", [""])[0]
            repair_notes = form.get("repair_notes", [""])[0].strip()
            if status not in {"Reported", "Acknowledged", "In Progress", "Resolved"} or len(repair_notes) > 1000:
                self.send_html("<main><h1>Invalid repair information</h1></main>", 400)
                return
            with database_connection(self.database) as db:
                issue = db.execute(
                    "SELECT board_id FROM technical_issues WHERE issue_id=? AND is_demo=1",
                    (issue_id,),
                ).fetchone()
                if not issue:
                    self.send_html("<main><h1>Technical issue not found</h1></main>", 404)
                    return
                db.execute(
                    "UPDATE technical_issues SET status=?,repair_notes=?,technician_id=? "
                    "WHERE issue_id=? AND is_demo=1",
                    (status, repair_notes, "demo-staff-001", issue_id),
                )
                db.execute(
                    "INSERT INTO board_maintenance VALUES (?,?,?,?,?,?,1)",
                    (secrets.token_hex(16), issue["board_id"],
                     datetime.now(timezone.utc).date().isoformat(),
                     f"Status: {status}. {repair_notes}".strip(),
                     "demo-staff-001", status),
                )
            self.redirect("/?page=issues")
            return
        if self.path == "/issues/report":
            if not issue_status_allowed(user):
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            board_id = form.get("board_id", [""])[0]
            category = form.get("category", [""])[0].strip()
            description = form.get("description", [""])[0].strip()
            priority = form.get("priority", [""])[0]
            if (
                not category or len(category) > 80 or not description or
                len(description) > 1000 or priority not in {"Low", "Medium", "High"}
            ):
                self.send_html("<main><h1>Invalid issue details</h1></main>", 400)
                return
            with database_connection(self.database) as db:
                board = db.execute(
                    "SELECT classroom_id FROM smart_boards WHERE board_id=? AND is_demo=1",
                    (board_id,),
                ).fetchone()
                if not board:
                    self.send_html("<main><h1>Smart board not found</h1></main>", 404)
                    return
                db.execute(
                    "INSERT INTO technical_issues "
                    "(issue_id,classroom_id,board_id,category,description,priority,reporting_user,"
                    "reported_at,technician_id,status,repair_notes,is_demo) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'Reported','',1)",
                    (secrets.token_hex(16), board["classroom_id"], board_id, category,
                     description, priority, user["role"],
                     datetime.now(timezone.utc).isoformat(), "demo-staff-001"),
                )
            self.redirect("/?page=issues")
            return
        if self.path == "/reset-demo":
            if user["role"] not in ADMIN_ROLES:
                self.send_html(_page_start("Not authorized", "", user) +
                               "<main><h1>Not authorized</h1><p>Only demo administrators can reset demo records.</p></main>" +
                               _page_end(), 403)
                return
            if not secrets.compare_digest(
                form.get("confirmation", [""])[0], "RESET DEMO DATA"
            ):
                self.send_html(_page_start("Confirmation required", "dashboard", user) +
                               "<main><h1>Reset not performed</h1><p>Confirmation did not match; no records were changed.</p><a href='/'>Return to dashboard</a></main>" +
                               _page_end(), 400)
                return
            try:
                with database_connection(self.database) as db:
                    reset_demo(db)
                self.redirect("/?reset=1")
            except sqlite3.Error:
                self.log_error("Demo data reset failed")
                self.send_html(_page_start("Reset failed", "dashboard", user) +
                               "<main><h1>Reset failed</h1><p>The database prevented the reset. No actual records were targeted; review the application log.</p></main>" +
                               _page_end(), 500)
            return
        self.send_html(_page_start("Not found", "", user) + "<main><h1>Not found</h1></main>" + _page_end(), 404)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SCHOOL COMMAND with fictional local demo data.")
    parser.add_argument("command", nargs="?", choices=("serve", "setup", "verify"), default="serve")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.command in ("serve", "setup"):
        startup(args.database)
        if args.command == "setup":
            print(f"Demo database created at {args.database}")
            return 0
        SchoolHandler.database = args.database
        server = ThreadingHTTPServer((args.host, args.port), SchoolHandler)
        print(f"SCHOOL COMMAND is available at http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping SCHOOL COMMAND.")
        finally:
            server.server_close()
        return 0

    if not args.database.exists():
        print(f"Database not found: {args.database}. Run `python app.py setup` first.", file=sys.stderr)
        return 2
    errors = verify_database(args.database)
    if errors:
        print("Demo data verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Demo data verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
