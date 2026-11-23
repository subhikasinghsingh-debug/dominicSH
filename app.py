"""Local SCHOOL COMMAND demo application. Uses only Python's standard library."""

from __future__ import annotations

import argparse
import csv
from io import StringIO
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import html
import mimetypes
import os
import re
import secrets
import sqlite3
import ssl
import sys
from datetime import date, datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from demo_data import (
    CLASS_NAMES,
    PASSWORD_ITERATIONS,
    connect,
    reset_demo,
    school_days,
    seed_demo,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "data" / "school_command.sqlite3"
NAV_ITEMS = [
    ("dashboard", "Dashboard"), ("students", "Students"), ("staff", "Staff"),
    ("classes", "Classes"),
    ("attendance", "Attendance"), ("fees", "Fees"), ("payments", "Payment History"),
    ("buses", "Transport"),
    ("boards", "Smart Boards"), ("issues", "Technical Issues"),
    ("announcements", "Announcements"), ("academics", "Examinations"),
    ("reports", "Report Cards"),
]
NAV_GROUPS = (
    ("Overview", ("dashboard",)),
    ("People & learning", ("students", "staff", "classes", "attendance", "academics", "reports")),
    ("Administration", ("fees", "payments")),
    ("Operations", ("buses", "boards", "issues")),
    ("Community", ("announcements",)),
)
NAV_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "students": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
    "staff": '<circle cx="9" cy="8" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0M16 5.5a3 3 0 0 1 0 5.8M17 14a5 5 0 0 1 3.5 4.8"/>',
    "classes": '<path d="M4 4h16v16H4zM4 10h16M10 10v10"/>',
    "attendance": '<path d="m5 12 4 4L19 6"/><path d="M20 12v7H4V5h11"/>',
    "fees": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h4"/>',
    "payments": '<path d="M7 7h13l-3-3M17 17H4l3 3M20 7l-4 4M4 17l4-4"/>',
    "buses": '<path d="M5 17V6a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v11M5 12h14M5 17h14"/><circle cx="8" cy="18" r="1.5"/><circle cx="16" cy="18" r="1.5"/>',
    "boards": '<rect x="3" y="4" width="18" height="13" rx="1"/><path d="M8 21h8M12 17v4"/>',
    "issues": '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
    "announcements": '<path d="M4 14V9a1 1 0 0 1 1-1h3l9-4v14l-9-4H5a1 1 0 0 1-1-1ZM8 14l2 6h4l-3-5"/>',
    "academics": '<path d="m3 9 9-5 9 5-9 5-9-5ZM6 11v6l6 3 6-3v-6M21 9v7"/>',
    "reports": '<path d="M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7"/>',
}
ADMIN_ROLES = {"Director", "Principal"}
DEMO_ACCOUNT_IDS = {
    "demo-user-director": "Director",
    "demo-user-principal": "Principal",
    "demo-user-teacher": "Class Teacher",
    "demo-user-subject": "Subject Teacher",
    "demo-user-parent": "Parent",
    "demo-user-technical": "Technical Team",
}
ROLE_MODULES = {
    "Director": {key for key, _ in NAV_ITEMS},
    "Principal": {key for key, _ in NAV_ITEMS},
    "Class Teacher": {"dashboard", "students", "classes", "attendance", "academics", "announcements"},
    "Subject Teacher": {"dashboard", "students", "classes", "academics", "announcements"},
    "Parent": {"dashboard", "students", "classes", "attendance", "fees", "payments", "academics", "announcements", "buses"},
    "Technical Team": {"dashboard", "boards", "issues"},
}
PAGE_ALIASES = {"examinations": "academics"}
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
            f"AND pc.student_id={student_alias}.student_id)",
            (user["user_id"],),
        )
    if role == "Class Teacher" and user["employee_id"] and section_alias:
        return (
            f"{section_alias}.section_id IN (SELECT section_id FROM class_sections "
            "WHERE homeroom_teacher_id=? )",
            (user["employee_id"],),
        )
    if role == "Subject Teacher" and user["employee_id"] and section_alias:
        subject_clause = " AND ta.subject=" + subject_alias if subject_alias else ""
        return (
            f"EXISTS (SELECT 1 FROM teacher_assignments ta "
            f"WHERE ta.employee_id=? AND ta.section_id={section_alias}.section_id "
            f"{subject_clause})",
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


def fee_status(paid_amount: int, annual_total: int, due_date: str) -> str:
    pending_amount = annual_total - paid_amount
    if pending_amount <= 0:
        return "Paid"
    if paid_amount > 0:
        return (
            "Overdue"
            if date.fromisoformat(due_date) < date.today()
            else "Partially Paid"
        )
    return "Overdue" if date.fromisoformat(due_date) < date.today() else "Pending"


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


def _login_content(show_error: bool = False) -> str:
    error = (
        '<p class="login-error" role="alert" aria-live="assertive">'
        "The email address or password was not accepted. Please try again.</p>"
        if show_error else ""
    )
    return f"""<main class="login-page" aria-labelledby="login-school-name">
<header class="login-brand">
<h1 id="login-school-name">St Dominic Savio's High School</h1>
<p class="login-motto"><span>Thorough</span></p>
</header>
<section class="login" aria-labelledby="login-heading">
<div class="login-card">
<h2 id="login-heading">Welcome Back</h2>
<p class="sub">Sign in to access your dashboard</p>
{error}
<form method="post" action="/login" id="login-form">
<div class="login-field">
<label for="email">Email address</label>
<div class="login-input-wrap">
<svg aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="m3 7 9 6 9-6"></path></svg>
<input id="email" name="email" type="email" required autocomplete="username" autocapitalize="none" spellcheck="false" aria-describedby="login-id-hint">
</div>
<span class="login-field-hint" id="login-id-hint">Use your email address or configured username.</span>
</div>
<div class="login-field">
<label for="password">Password</label>
<div class="login-input-wrap">
<svg aria-hidden="true" viewBox="0 0 24 24"><rect x="4" y="10" width="16" height="11" rx="2"></rect><path d="M8 10V7a4 4 0 0 1 8 0v3"></path></svg>
<input id="password" name="password" type="password" required autocomplete="current-password">
<button class="password-toggle" id="password-toggle" type="button" aria-label="Show password" aria-pressed="false">
<svg class="eye-show" aria-hidden="true" viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"></path><circle cx="12" cy="12" r="3"></circle></svg>
<svg class="eye-hide" aria-hidden="true" viewBox="0 0 24 24" hidden><path d="m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8"></path><path d="M9.9 5.2A10.7 10.7 0 0 1 12 5c6.5 0 10 7 10 7a14.8 14.8 0 0 1-3.1 3.9M6.2 6.2C3.5 8 2 12 2 12s3.5 7 10 7a10.5 10.5 0 0 0 4-.8"></path></svg>
</button>
</div>
</div>
<button class="login-submit" id="login-submit" type="submit">
<span class="login-spinner" id="login-spinner" aria-hidden="true" hidden></span>
<span id="login-submit-label">Sign In</span>
</button>
</form>
<p class="login-secure">
<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z"></path><path d="m9 12 2 2 4-4"></path></svg>
<span>Secure School Access</span>
</p>
</div>
</section>
</main>
<script>
(() => {{
    const form = document.getElementById("login-form");
    const identifier = document.getElementById("email");
    const password = document.getElementById("password");
    const toggle = document.getElementById("password-toggle");
    const submit = document.getElementById("login-submit");
    const spinner = document.getElementById("login-spinner");
    const label = document.getElementById("login-submit-label");
    if (!form || !identifier || !password || !toggle || !submit || !spinner || !label) return;
    form.noValidate = true;

    identifier.addEventListener("input", () => {{
        identifier.type = "email";
        identifier.setCustomValidity("");
    }});
    password.addEventListener("input", () => password.setCustomValidity(""));

    toggle.addEventListener("click", () => {{
        const isVisible = password.type === "text";
        password.type = isVisible ? "password" : "text";
        toggle.setAttribute("aria-label", isVisible ? "Show password" : "Hide password");
        toggle.setAttribute("aria-pressed", String(!isVisible));
        toggle.querySelector(".eye-show").hidden = !isVisible;
        toggle.querySelector(".eye-hide").hidden = isVisible;
        password.focus();
    }});

    form.addEventListener("submit", (event) => {{
        if (form.dataset.submitting === "true") {{
            event.preventDefault();
            return;
        }}
        const identifierValue = identifier.value.trim();
        identifier.type = identifierValue.includes("@") ? "email" : "text";
        identifier.setCustomValidity(
            identifierValue ? "" : "Enter your email address or username."
        );
        password.setCustomValidity(password.value ? "" : "Enter your password.");
        if (!form.reportValidity()) {{
            event.preventDefault();
            return;
        }}
        form.dataset.submitting = "true";
        submit.disabled = true;
        submit.setAttribute("aria-busy", "true");
        spinner.hidden = false;
        label.textContent = "Signing In...";
    }});
}})();
</script>"""


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
:root{{--green:#245541;--green-dark:#173d2e;--green-soft:#e6efe7;--ink:#24352c;--muted:#58695f;--line:rgba(54,79,63,.16);--bg:#f4f2e9;--red:#a63838;--amber:#805411;--gold:#d4be80;
--glass-ivory:rgba(255,255,255,.43);--glass-white:rgba(255,255,255,.34);--glass-strong:rgba(255,255,255,.57);--glass-border:rgba(255,255,255,.74);--glass-blur:blur(28px) saturate(180%);--glass-shadow:0 18px 48px rgba(48,63,46,.10),0 3px 12px rgba(48,63,46,.045),inset 0 1px 0 rgba(255,255,255,.5);--glass-shadow-hover:0 22px 54px rgba(48,63,46,.14),0 6px 16px rgba(48,63,46,.07),inset 0 1px 0 rgba(255,255,255,.7);--radius-sm:13px;--radius-md:20px;--radius-lg:28px;--space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:24px;--font-body:system-ui,-apple-system,"Segoe UI",sans-serif;--font-display:Georgia,"Times New Roman",serif;--text-sm:12px;--text-base:14px;--text-lg:17px;--motion-fast:160ms ease;--motion-smooth:220ms cubic-bezier(.2,.7,.2,1)}}
*{{box-sizing:border-box}}html{{min-height:100%;background:var(--bg)}}body{{position:relative;isolation:isolate;margin:0;background:radial-gradient(ellipse at 9% 7%,rgba(195,218,191,.64),transparent 34%),radial-gradient(ellipse at 88% 12%,rgba(255,255,255,.9),transparent 32%),radial-gradient(ellipse at 75% 88%,rgba(225,213,174,.34),transparent 38%),linear-gradient(135deg,#f6f3e8,#edf1e8 55%,#f7f5ed);color:var(--ink);font:var(--text-base)/1.55 var(--font-body);min-height:100vh}}
body::before{{content:"";position:fixed;z-index:-1;inset:0;pointer-events:none;background:radial-gradient(ellipse at 52% 42%,rgba(255,255,255,.48),transparent 48%),radial-gradient(ellipse at 5% 90%,rgba(198,220,192,.27),transparent 35%)}}
.top{{position:relative;margin:16px clamp(14px,2.5vw,36px) 10px;padding:15px 23px;border:1px solid var(--glass-border);border-radius:var(--radius-lg);background:rgba(255,255,255,.48);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur);color:var(--green-dark);display:flex;align-items:center;justify-content:space-between;gap:16px;box-shadow:var(--glass-shadow)}}
.brand{{font-weight:760;font-size:19px;letter-spacing:.055em}}.brand small{{display:block;font-size:12px;font-weight:450;letter-spacing:.015em;color:#63776a}}
.identity{{display:flex;align-items:center;gap:12px}}.user-role{{background:rgba(255,255,255,.42);border:1px solid rgba(255,255,255,.78);border-radius:20px;padding:5px 12px;color:var(--green-dark)}}
.demo{{width:fit-content;max-width:calc(100% - 28px);margin:0 auto 10px;padding:7px 15px;border:1px solid rgba(255,255,255,.72);border-radius:999px;background:rgba(244,235,207,.5);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur);color:#67542a;text-align:center;font-size:12px;font-weight:750;letter-spacing:.045em;box-shadow:0 5px 16px rgba(55,66,44,.045)}}
.app-shell{{display:grid;grid-template-columns:250px minmax(0,1fr);align-items:stretch;gap:8px;min-height:calc(100vh - 128px);padding:0 clamp(12px,2vw,28px) 22px}}
nav{{position:relative;align-self:stretch;margin:8px 0 24px;padding:23px 14px;border:1px solid var(--glass-border);border-radius:var(--radius-lg);background:rgba(255,255,255,.42);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur);display:flex;flex-direction:column;gap:var(--space-4);box-shadow:var(--glass-shadow)}}
.nav-group{{display:flex;flex-direction:column;gap:3px}}.nav-group-title{{padding:0 12px;margin:0 0 3px;color:#68746a;font-size:10px;font-weight:750;letter-spacing:.09em;text-transform:uppercase}}
nav a{{display:flex;align-items:center;gap:11px;color:#354b3e;text-decoration:none;padding:10px 12px;border:1px solid transparent;border-radius:var(--radius-sm);line-height:1.35;transition:background var(--motion-fast),color var(--motion-fast),transform var(--motion-fast),box-shadow var(--motion-fast)}}
.nav-icon{{width:17px;height:17px;flex:none;display:inline-flex;align-items:center;justify-content:center}}.nav-icon svg{{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}}
nav a:hover{{background:rgba(255,255,255,.52);border-color:rgba(255,255,255,.68);color:var(--green-dark);box-shadow:0 5px 14px rgba(39,66,47,.06);transform:translateY(-1px)}}nav a.active{{background:linear-gradient(135deg,rgba(255,255,255,.72),rgba(223,237,224,.54));border-color:rgba(255,255,255,.9);color:var(--green-dark);font-weight:680;box-shadow:inset 0 1px 0 rgba(255,255,255,.85),0 6px 17px rgba(42,72,49,.09)}}
.card-link{{display:block;color:inherit;text-decoration:none;transition:border-color var(--motion-smooth),box-shadow var(--motion-smooth),transform var(--motion-smooth)}}
.card-link:hover{{border-color:#b4c9b8;box-shadow:var(--glass-shadow-hover);transform:translateY(-2px)}}
.card-link:focus-visible{{outline:3px solid #bd9b42;outline-offset:2px}}
.module-tile .value{{font-size:16px}}.back-link{{margin:0 0 12px;font-size:12px}}.back-link a{{color:var(--green);font-weight:650}}
.workspace{{min-width:0;padding:8px 0 0}}main{{width:100%;max-width:1520px;margin:0 auto;padding:25px clamp(14px,2vw,30px) 44px}}
h1{{font-size:27px;line-height:1.25;letter-spacing:-.025em;margin:0 0 6px;color:var(--green-dark)}}h2{{font-size:var(--text-lg);line-height:1.35;margin:0 0 14px;color:var(--green-dark)}}.sub{{color:var(--muted);margin:0 0 22px}}
.dashboard-head{{display:flex;justify-content:space-between;align-items:center;gap:18px;background:rgba(255,255,255,.43);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur);border:1px solid var(--glass-border);border-radius:var(--radius-lg);padding:22px 25px;margin-bottom:20px;box-shadow:var(--glass-shadow)}}
.dashboard-head h1{{margin:2px 0 5px}}.dashboard-head .sub{{margin:0}}.eyebrow{{margin:0;color:var(--green);font-size:10px;font-weight:750;letter-spacing:.09em;text-transform:uppercase}}
.dashboard-date{{flex:none;text-align:right;padding-left:18px;border-left:1px solid var(--line)}}.dashboard-date span,.dashboard-date strong{{display:block}}.dashboard-date span{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:700}}.dashboard-date strong{{font-size:14px;color:var(--green-dark);margin-top:3px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,175px),1fr));gap:16px;margin:20px 0 24px}}
.card,.panel{{position:relative;overflow:hidden;background:rgba(255,255,255,.39);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur);border:1px solid var(--glass-border);border-radius:var(--radius-md);padding:var(--space-5);box-shadow:var(--glass-shadow);transition:box-shadow var(--motion-smooth),border-color var(--motion-smooth),transform var(--motion-smooth)}}
.card::before,.panel::before{{content:"";position:absolute;inset:0 0 auto;height:1px;pointer-events:none;background:linear-gradient(90deg,transparent,rgba(255,255,255,.92),transparent)}}
.card:hover{{border-color:rgba(255,255,255,.96);box-shadow:var(--glass-shadow-hover);transform:translateY(-2px)}}
.card .label{{color:var(--muted);font-size:12px;font-weight:650;letter-spacing:.015em}}.card .value{{font-size:28px;line-height:1.25;font-weight:740;letter-spacing:-.025em;color:var(--green-dark);margin:7px 0}}.card .hint{{font-size:11px;color:var(--muted);line-height:1.45}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:17px}}.grid>.panel{{background:rgba(255,255,255,.37)}}.grid>.panel h2{{margin-bottom:18px}}.barrow{{display:grid;grid-template-columns:minmax(95px,1fr) minmax(80px,3fr) 58px;gap:10px;align-items:center;margin:14px 0;font-size:12px}}.barrow strong{{text-align:right;font-variant-numeric:tabular-nums}}
.track{{height:9px;background:rgba(83,111,86,.15);border-radius:9px;overflow:hidden;box-shadow:inset 0 1px 2px rgba(45,64,47,.06)}}.bar{{height:100%;background:linear-gradient(90deg,#467c5e,#245541);border-radius:9px;box-shadow:0 1px 4px rgba(26,73,48,.18)}}
.panel>h2>a{{color:inherit;text-decoration:none}}.panel>h2>a:hover{{color:var(--green)}}
.table-wrap{{max-width:100%;overflow:auto;background:rgba(255,255,255,.27);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur);border:1px solid rgba(255,255,255,.74);border-radius:var(--radius-sm);box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 8px 20px rgba(45,63,47,.035)}}table{{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}}th,td{{padding:11px 13px;border-bottom:1px solid rgba(63,83,67,.12);text-align:left}}th{{background:rgba(224,235,222,.48);color:var(--green-dark);font-weight:680;position:sticky;top:0}}tbody tr:last-child td{{border-bottom:0}}tbody tr{{transition:background var(--motion-fast)}}tbody tr:hover{{background:rgba(255,255,255,.42)}}
.badge{{display:inline-block;border-radius:20px;padding:3px 8px;background:#eceee7;color:#3e4b43;font-size:10px;font-weight:700}}.badge.demo{{background:#f3e8c6;color:#4e3b12}}.status-online,.status-present,.status-resolved,.status-active,.status-paid{{color:#176442;font-weight:650}}.status-error,.status-absent,.status-overdue,.status-reported{{color:var(--red);font-weight:650}}.status-maintenance,.status-late,.status-in-progress,.status-acknowledged{{color:var(--amber);font-weight:650}}
.section-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:26px 0 13px}}.notice{{padding:12px 14px;border:1px solid rgba(126,171,140,.48);background:rgba(232,243,231,.54);color:#234c38;border-radius:var(--radius-sm);margin:12px 0;box-shadow:0 8px 20px rgba(36,76,56,.04)}}
.filter-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;align-items:end}}
.danger-panel{{border-color:rgba(164,74,66,.28)}}.danger-panel p{{color:var(--muted)}}input,select,textarea,button{{font:inherit}}input[type=text],input[type=email],input[type=password],input[type=number],input[type=date],select,textarea{{width:100%;max-width:100%;border:1px solid rgba(102,125,108,.3);background:rgba(255,255,255,.42);-webkit-backdrop-filter:blur(14px) saturate(150%);backdrop-filter:blur(14px) saturate(150%);color:var(--ink);padding:10px 11px;border-radius:var(--radius-sm);box-shadow:inset 0 1px 0 rgba(255,255,255,.58);transition:border-color var(--motion-fast),box-shadow var(--motion-fast),background var(--motion-fast)}}
input[type=text]:focus,input[type=email]:focus,input[type=password]:focus,input[type=number]:focus,input[type=date]:focus,select:focus,textarea:focus{{border-color:rgba(48,104,69,.65);background:rgba(255,255,255,.58);box-shadow:0 0 0 4px rgba(48,104,69,.11),inset 0 1px 0 rgba(255,255,255,.7);outline:none}}select option{{background:#f5f4ec;color:var(--ink)}}label{{display:block;margin:13px 0 5px;font-weight:620;color:#344c3c}}
button,.button{{background:linear-gradient(135deg,#467c5e,#245541);color:#fff;border:1px solid rgba(255,255,255,.72);border-radius:13px;padding:10px 15px;font-weight:660;cursor:pointer;text-decoration:none;display:inline-block;box-shadow:0 7px 17px rgba(24,73,47,.16),inset 0 1px 0 rgba(255,255,255,.28);transition:background var(--motion-fast),border-color var(--motion-fast),box-shadow var(--motion-fast),transform var(--motion-fast)}}button:hover,.button:hover{{background:linear-gradient(135deg,#376e50,#173d2e);box-shadow:0 10px 22px rgba(24,73,47,.2),inset 0 1px 0 rgba(255,255,255,.32);transform:translateY(-1px)}}button:focus-visible,.button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{{outline:3px solid #bd9b42;outline-offset:2px}}
.danger{{background:linear-gradient(135deg,#b34c43,#8d3431);border-color:rgba(255,255,255,.68)}}.danger:hover{{background:linear-gradient(135deg,#a34039,#762724)}}.link-button{{background:transparent;color:var(--green-dark);border:0;box-shadow:none;padding:4px;text-decoration:underline}}.link-button:hover{{background:rgba(57,94,67,.08);box-shadow:none;transform:none}}.button[style*="background:#fff"]{{background:rgba(255,255,255,.36)!important;color:var(--green-dark)!important;border-color:rgba(255,255,255,.82)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.65),0 5px 14px rgba(43,64,46,.06)}}.button[style*="background:#fff"]:hover{{background:rgba(255,255,255,.58)!important}}.inline{{display:inline}}
.login-page{{position:relative;isolation:isolate;display:flex;width:100%;max-width:none;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;min-height:100svh;margin:0;padding:clamp(30px,7vh,72px) 18px 38px;overflow:hidden;background:transparent}}
.login-page::before{{content:"";position:absolute;z-index:-1;inset:0;pointer-events:none;background-image:url("/static/images/school-logo.jpg");background-position:center 56%;background-repeat:no-repeat;background-size:min(100vw,700px) auto;opacity:.2;filter:saturate(.72)}}
.login-page::after{{content:"";position:absolute;z-index:-1;left:-12%;right:-12%;bottom:-100px;height:170px;border:1px solid #c9a34b38;border-radius:50% 50% 0 0 / 42% 42% 0 0;box-shadow:0 -8px 0 -7px #124b3525,0 -17px 0 -16px #c9a34b28;pointer-events:none}}
.login-brand{{width:min(100%,700px);text-align:center;margin:0 0 clamp(25px,4.5vh,42px);animation:login-appear .45s ease-out both}}
.login-brand h1{{margin:0;color:var(--green-dark);font:700 clamp(18px,4.2vw,34px)/1.16 var(--font-display);letter-spacing:-.035em;white-space:nowrap}}
.login-motto{{display:flex;align-items:center;justify-content:center;gap:13px;margin:9px 0 0;color:var(--green);font:italic 19px/1.2 var(--font-display)}}
.login-motto::before,.login-motto::after{{content:"";width:clamp(32px,8vw,72px);height:1px;background:#c9a34b}}
.login{{width:min(100%,470px);margin:0;animation:login-appear .55s .04s ease-out both}}
.login-card{{padding:clamp(24px,5vw,36px);background:rgba(255,255,255,.4);border:1px solid rgba(255,255,255,.78);border-radius:var(--radius-lg);box-shadow:var(--glass-shadow-hover),inset 0 1px 0 rgba(255,255,255,.7);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur)}}
.login-card h2{{margin:0;text-align:center;color:var(--green-dark);font:700 29px/1.2 var(--font-display)}}
.login-card>.sub{{margin:8px 0 25px;text-align:center;color:var(--muted);font-size:14px}}
.login-field{{margin:0 0 17px}}
.login-field label{{display:block;margin:0 0 7px;color:#303d35;font-size:13px;font-weight:650}}
.login-field-hint{{display:block;margin:6px 0 0;color:#647167;font-size:11px}}
.login-input-wrap{{position:relative;display:flex;align-items:center}}
.login-input-wrap>svg{{position:absolute;left:13px;width:18px;height:18px;fill:none;stroke:#718077;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}}
.login-input-wrap input{{height:48px;width:100%;padding:11px 43px 11px 42px;border:1px solid rgba(102,125,108,.3);border-radius:var(--radius-sm);background:rgba(255,255,255,.42);color:var(--ink);-webkit-backdrop-filter:blur(14px) saturate(150%);backdrop-filter:blur(14px) saturate(150%);box-shadow:inset 0 1px 0 rgba(255,255,255,.58)}}
.login-input-wrap input::placeholder{{color:#82877f}}
.login-input-wrap input:focus{{border-color:rgba(48,104,69,.65);background:rgba(255,255,255,.58);box-shadow:0 0 0 4px rgba(48,104,69,.11);outline:none}}
.password-toggle{{position:absolute;right:5px;display:grid;place-items:center;width:38px;height:38px;min-height:0;margin:0;padding:0;border:0;background:transparent;color:#5c6e63;box-shadow:none}}
.password-toggle:hover{{background:rgba(57,94,67,.08);color:var(--green-dark);box-shadow:none;transform:none}}
.password-toggle svg{{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}}
#login-spinner[hidden]{{display:none}}
.login-error{{margin:0 0 17px;padding:11px 13px;border:1px solid #bd8f80;border-radius:7px;background:#fff5f1;color:#713d32;font-size:13px}}
.login-submit{{width:100%;min-height:48px;margin-top:3px;padding:11px 16px;border:1px solid rgba(255,255,255,.72);border-radius:13px;background:linear-gradient(135deg,#467c5e,#245541);color:#fff;font-weight:700;box-shadow:0 7px 17px rgba(24,73,47,.16),inset 0 1px 0 rgba(255,255,255,.28);transition:background var(--motion-fast),border-color var(--motion-fast),transform var(--motion-fast),box-shadow var(--motion-fast)}}
.login-submit:hover{{background:linear-gradient(135deg,#376e50,#173d2e);border-color:rgba(255,255,255,.78);box-shadow:0 10px 22px rgba(24,73,47,.2)}}
.login-submit:active{{transform:translateY(1px)}}
.login-submit:disabled{{opacity:.78;cursor:wait}}
.login-spinner{{display:inline-block;width:15px;height:15px;margin-right:8px;vertical-align:-3px;border:2px solid #ffffff73;border-top-color:#fff;border-radius:50%;animation:login-spin .7s linear infinite}}
.login-secure{{display:flex;align-items:center;justify-content:center;gap:8px;margin:22px 0 0;padding-top:17px;border-top:1px solid rgba(77,102,81,.16);color:var(--muted);font-size:12px;letter-spacing:.02em}}
.login-secure svg{{width:15px;height:15px;fill:none;stroke:#9a7a31;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}}
@keyframes login-appear{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:translateY(0)}}}}
@keyframes login-spin{{to{{transform:rotate(360deg)}}}}
body[data-page="login"]>.top,body[data-page="login"]>.demo,body[data-page="login"]>#page-transition-indicator,body[data-page="login"]>.footer{{display:none}}
.empty{{color:var(--muted);padding:24px;text-align:center;background:rgba(255,255,255,.24);border-radius:var(--radius-sm)}}.footer{{text-align:center;color:var(--muted);font-size:11px;padding:20px}}
.profile-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}
.profile-item{{border-bottom:1px solid rgba(63,83,67,.13);padding:7px 0}}.profile-item:first-child{{padding-top:0}}.profile-item strong{{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.035em}}.profile-item span{{display:block;margin-top:2px}}
.profile-actions{{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:0 0 18px}}
.profile-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}}
.profile-summary .card{{margin:0}}.student-row{{cursor:pointer}}.student-row:hover{{background:rgba(255,255,255,.42)}}
.financial-actions{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:16px}}
button:active,.button:active{{transform:translateY(1px)}}button:disabled{{opacity:.6;cursor:not-allowed}}
dialog{{max-width:min(560px,calc(100vw - 32px));border:1px solid rgba(255,255,255,.84);border-radius:var(--radius-lg);padding:24px;background:rgba(255,255,255,.62);color:var(--ink);box-shadow:0 30px 90px rgba(22,42,30,.28),inset 0 1px 0 rgba(255,255,255,.75);-webkit-backdrop-filter:var(--glass-blur);backdrop-filter:var(--glass-blur)}}dialog::backdrop{{background:rgba(31,47,37,.32);-webkit-backdrop-filter:blur(10px) saturate(130%);backdrop-filter:blur(10px) saturate(130%)}}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){{.top,nav,.demo,.dashboard-head,.card,.panel,.table-wrap,dialog,.login-card{{background:rgba(246,246,238,.82)}}.login-input-wrap input{{background:rgba(255,255,255,.76)}}}}
@view-transition{{navigation:auto}}
nav{{view-transition-name:app-sidebar}}.workspace{{view-transition-name:app-content}}
::view-transition-old(app-content){{animation:page-out .12s ease-in both}}
::view-transition-new(app-content){{animation:page-in .18s ease-out both}}
::view-transition-old(app-sidebar),::view-transition-new(app-sidebar){{animation:none}}
@keyframes page-out{{to{{opacity:0;transform:translateY(-4px)}}}}
@keyframes page-in{{from{{opacity:.65;transform:translateY(6px)}}to{{opacity:1;transform:translateY(0)}}}}
.page-enter{{animation:page-in .18s ease-out both}}
#page-transition-indicator{{position:fixed;z-index:1000;top:0;left:0;width:0;height:3px;background:linear-gradient(90deg,#d4be80,#73a87e,#d4be80);box-shadow:0 1px 8px rgba(52,102,68,.32);pointer-events:none}}
#page-transition-indicator.is-visible{{width:35%;transition:width 1.2s ease-out}}
@media(prefers-reduced-motion:reduce){{*,*::before,*::after{{scroll-behavior:auto!important;transition:none!important;animation:none!important}}::view-transition-old(app-content),::view-transition-new(app-content),::view-transition-old(app-sidebar),::view-transition-new(app-sidebar){{animation:none!important}}}}
@media(max-width:900px){{.app-shell{{grid-template-columns:215px minmax(0,1fr)}}nav{{padding:18px 10px}}main{{padding:22px 16px 38px}}}}
@media(max-width:650px){{.top{{margin:12px 12px 9px;padding:13px 15px;border-radius:20px;flex-wrap:wrap}}.brand{{font-size:17px}}.brand small{{font-size:11px}}.identity{{gap:8px}}.demo{{margin-bottom:8px;font-size:10px}}.app-shell{{display:block;min-height:0;padding:0 10px 12px}}nav{{flex-direction:row;overflow-x:auto;overscroll-behavior-x:contain;padding:9px 10px;margin:0 0 10px;border:1px solid var(--glass-border);border-radius:20px;scrollbar-width:thin;gap:5px}}.nav-group{{display:contents}}.nav-group-title{{display:none}}nav a{{flex:none;padding:9px 11px}}.workspace{{padding:0}}main{{padding:18px 5px 30px}}.dashboard-head{{align-items:flex-start;padding:17px;gap:14px}}.dashboard-head h1{{font-size:23px}}.dashboard-date{{padding-left:12px}}.cards{{grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin:14px 0 18px}}.card,.panel{{padding:16px}}.card .value{{font-size:25px}}.grid{{grid-template-columns:minmax(0,1fr);gap:12px}}.section-head{{align-items:flex-start;flex-wrap:wrap}}.barrow{{grid-template-columns:minmax(72px,1fr) minmax(60px,2fr) 52px;gap:7px}}.login-page{{padding:30px 16px}}.login-brand{{margin-bottom:26px}}.login-card h2{{font-size:27px}}}}
@media(max-width:390px){{.top{{align-items:flex-start}}.identity{{font-size:12px}}.user-role{{padding:4px 8px}}.dashboard-head{{display:block}}.dashboard-date{{display:flex;gap:7px;align-items:baseline;text-align:left;border-left:0;padding:10px 0 0;margin-top:12px;border-top:1px solid var(--line)}}.barrow{{grid-template-columns:minmax(62px,1fr) minmax(55px,2fr) 48px;font-size:11px}}}}
@media(max-width:360px){{.login-brand h1{{font-size:17px;white-space:normal}}.login-motto{{font-size:17px;gap:9px}}.login-card{{padding:22px 18px}}.cards{{grid-template-columns:minmax(0,1fr)}}}}
@media print{{.top,nav,.demo,.profile-actions,.financial-actions,.footer{{display:none!important}}.app-shell{{display:block;padding:0}}.workspace{{padding:0}}main{{max-width:none;padding:0}}.panel{{break-inside:avoid;box-shadow:none}}.card,.panel,.table-wrap{{background:#fff;box-shadow:none;-webkit-backdrop-filter:none;backdrop-filter:none}}body{{background:#fff}}body::before{{display:none}}}}
@media(max-height:700px) and (min-width:651px){{.login-page{{padding-top:25px;padding-bottom:25px}}.login-brand{{margin-bottom:22px}}.login-card>.sub{{margin-bottom:18px}}.login-field{{margin-bottom:12px}}.login-secure{{margin-top:16px;padding-top:13px}}}}
</style></head><body data-page="{html.escape(active)}">
<header class="top"><div class="brand">SCHOOL COMMAND<small>St Dominic Savio's High School</small></div>
<div class="identity">{identity}</div></header>
<div class="demo">DEMO MODE · FICTIONAL DEMONSTRATION RECORDS · NOT REAL SCHOOL DATA</div>
<div id="page-transition-indicator" role="progressbar" aria-label="Loading page" aria-valuetext="Loading" hidden></div>
"""


def _page_end() -> str:
    return """<div class="footer">All displayed records are fictional demo data.</div>
<script>
(() => {
    const shell = document.querySelector(".app-shell");
    const workspace = document.querySelector(".workspace");
    const indicator = document.getElementById("page-transition-indicator");
    if (!shell || !workspace || !indicator) return;

    const navigationState = Object.assign({}, history.state, {
        schoolCommandPage: true,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
    });
    history.replaceState(navigationState, "");
    history.scrollRestoration = "manual";

    let activeRequest;
    let navigationId = 0;

    async function loadPage(url, addHistory, scrollPosition) {
        const requestId = ++navigationId;
        if (activeRequest) activeRequest.abort();
        const controller = new AbortController();
        activeRequest = controller;
        const loadingTimer = window.setTimeout(() => {
            if (requestId === navigationId) {
                indicator.hidden = false;
                indicator.classList.add("is-visible");
            }
        }, 120);

        try {
            const response = await fetch(url.href, {
                credentials: "same-origin",
                headers: { Accept: "text/html" },
                signal: controller.signal,
            });
            if (!response.ok || new URL(response.url).pathname !== "/") {
                throw new Error("Page navigation requires a full load.");
            }

            const page = new DOMParser().parseFromString(
                await response.text(),
                "text/html",
            );
            const nextWorkspace = page.querySelector(".workspace");
            const nextNav = page.querySelector("nav");
            if (!nextWorkspace || !nextNav || requestId !== navigationId) {
                throw new Error("The requested page did not return an app view.");
            }

            const updatePage = () => {
                workspace.innerHTML = nextWorkspace.innerHTML;
                const activeLink = nextNav.querySelector("a.active");
                document.querySelectorAll("nav a").forEach((link) => {
                    const isActive = activeLink
                        && link.getAttribute("href") === activeLink.getAttribute("href");
                    link.classList.toggle("active", Boolean(isActive));
                    if (isActive) link.setAttribute("aria-current", "page");
                    else link.removeAttribute("aria-current");
                });
                document.title = page.title;
                document.body.dataset.page = page.body.dataset.page || "";
                window.scrollTo(scrollPosition.x, scrollPosition.y);
            };

            if (addHistory) {
                history.replaceState(Object.assign({}, history.state, {
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                }), "");
                history.pushState({
                    schoolCommandPage: true,
                    scrollX: 0,
                    scrollY: 0,
                }, "", url.href);
            }
            if (document.startViewTransition) {
                document.startViewTransition(updatePage);
            } else {
                workspace.classList.remove("page-enter");
                updatePage();
                requestAnimationFrame(() => {
                    workspace.classList.add("page-enter");
                    window.setTimeout(
                        () => workspace.classList.remove("page-enter"),
                        200,
                    );
                });
            }
        } catch (error) {
            if (requestId !== navigationId) return;
            if (error.name === "AbortError") return;
            if (addHistory) window.location.assign(url.href);
            else window.location.reload();
        } finally {
            window.clearTimeout(loadingTimer);
            if (requestId === navigationId) {
                indicator.classList.remove("is-visible");
                indicator.hidden = true;
                activeRequest = null;
            }
        }
    }

    document.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0
            || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (!(event.target instanceof Element)) return;
        const link = event.target.closest("a");
        if (!link || link.target || link.hasAttribute("download")) return;
        const url = new URL(link.href, window.location.href);
        if (url.origin !== window.location.origin || url.pathname !== "/"
            || url.hash) return;
        event.preventDefault();
        loadPage(url, true, { x: 0, y: 0 });
    });

    window.addEventListener("popstate", (event) => {
        if (!event.state || !event.state.schoolCommandPage
            || window.location.pathname !== "/") {
            window.location.reload();
            return;
        }
        loadPage(new URL(window.location.href), false, {
            x: event.state.scrollX || 0,
            y: event.state.scrollY || 0,
        });
    });
})();
</script></body></html>"""


def _navigation(user: sqlite3.Row, active_module: str) -> str:
    items = dict(NAV_ITEMS)
    groups = []
    for title, keys in NAV_GROUPS:
        links = "".join(
            f'<a class="{"active" if key == active_module else ""}"'
            f'{" aria-current=page" if key == active_module else ""} '
            f'href="/?{urlencode({"page": key})}">'
            f'<span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            f'{NAV_ICONS[key]}</svg></span>{html.escape(items[key])}</a>'
            for key in keys if can_access_module(user, key)
        )
        if links:
            groups.append(
                f'<div class="nav-group"><p class="nav-group-title">{html.escape(title)}</p>'
                f'{links}</div>'
            )
    return '<nav aria-label="Main navigation">' + "".join(groups) + "</nav>"


def _credential_setup_content(
    user: sqlite3.Row,
    accounts: list[sqlite3.Row],
    message: str = "",
    usernames: dict[str, str] | None = None,
) -> str:
    username_values = usernames or {
        account["user_id"]: account["email"] for account in accounts
    }
    account_fields = "".join(
        f'<section class="panel"><h2>{html.escape(account["role"])}</h2>'
        f'<label for="username-{account["user_id"]}">Username</label>'
        f'<input id="username-{account["user_id"]}" name="username_{account["user_id"]}" '
        f'type="text" value="{html.escape(username_values.get(account["user_id"], ""))}" '
        'required minlength="3" maxlength="64" pattern="[A-Za-z0-9._@+-]{3,64}" '
        'autocomplete="off">'
        f'<label for="password-{account["user_id"]}">New password</label>'
        f'<input id="password-{account["user_id"]}" name="password_{account["user_id"]}" '
        'type="password" minlength="12" maxlength="128" autocomplete="new-password" '
        'placeholder="Leave blank to keep the current password">'
        '</section>'
        for account in accounts
    )
    notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    return (
        '<main><h1>Account credentials</h1>'
        '<p class="sub">Set usernames and passwords for all six demo roles. '
        'Passwords must be at least 12 characters. Leave a password blank to keep it.</p>'
        f'{notice}<form method="post" action="/credentials/update">'
        f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
        f'<div class="grid">{account_fields}</div>'
        '<p><button type="submit">Save account credentials</button> '
        '<a href="/">Cancel</a></p></form></main>'
    )


def _table(
    headers: list[str], rows: list[tuple], demo_rows: list[bool] | None = None,
) -> str:
    if not rows:
        return '<div class="empty">No records available.</div>'
    thead = "".join(f"<th>{html.escape(str(header))}</th>" for header in headers)
    body = []
    for row_index, row in enumerate(rows):
        cells = []
        for index, value in enumerate(row):
            if (
                index < len(headers)
                and headers[index] in {"Annual total", "Paid", "Pending", "Amount"}
                and isinstance(value, (int, float))
            ):
                value = money(value)
            text = html.escape("" if value is None else str(value))
            if text in ("Present", "Online", "Resolved", "Active", "Paid"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            elif text in ("Absent", "Error", "Overdue", "Reported"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            elif text in ("Late", "In Progress", "Acknowledged", "Under Maintenance"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            cells.append(f"<td>{text}</td>")
        is_demo = demo_rows[row_index] if demo_rows is not None else True
        if is_demo:
            cells[0] = '<td><span class="badge demo">DEMO</span> ' + cells[0][4:]
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _student_table(
    headers: list[str],
    fetched_rows: list[sqlite3.Row],
) -> str:
    if not fetched_rows:
        return '<div class="empty">No records available.</div>'
    thead = "".join(f"<th>{html.escape(str(header))}</th>" for header in headers)
    body = []
    for row in fetched_rows:
        student_id = row["student_id"]
        profile_url = "/?page=student&" + urlencode({"id": student_id})
        cells = []
        values = tuple(row)[1:-1]
        for index, value in enumerate(values):
            text = html.escape("" if value is None else str(value))
            if index == 0:
                demo_badge = (
                    '<span class="badge demo">DEMO</span> '
                    if row["is_demo"] else ""
                )
                text = (
                    f'{demo_badge}<a href="{html.escape(profile_url)}"><strong>{text}</strong><br>'
                    '<span>View profile</span></a>'
                )
            if text in ("Present", "Online", "Resolved", "Active", "Paid"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            elif text in ("Absent", "Error", "Overdue", "Reported"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            elif text in ("Late", "In Progress", "Acknowledged", "Under Maintenance"):
                text = f'<span class="status-{text.lower().replace(" ", "-")}">{text}</span>'
            cells.append(f"<td>{text}</td>")
        body.append(
            f'<tr class="student-row" onclick="if (!event.target.closest(\'a\')) '
            f'window.location.href=\'{html.escape(profile_url)}\'">' +
            "".join(cells) + "</tr>"
        )
    return f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _append_query_clause(query: str, clause: str) -> str:
    for token in (" GROUP BY ", " ORDER BY ", " LIMIT "):
        index = query.upper().find(token)
        if index >= 0:
            return query[:index] + clause + query[index:]
    return query + clause


def _filter_control(name: str, label: str, value: str, options: list[str] | None = None,
                    input_type: str = "text", placeholder: str = "") -> str:
    if options is not None:
        choices = "".join(
            f'<option value="{html.escape(option)}"'
            f'{" selected" if option == value else ""}>{html.escape(option)}</option>'
            for option in options
        )
        return (
            f'<label>{html.escape(label)}'
            f'<select name="{html.escape(name)}"><option value="">All</option>{choices}</select></label>'
        )
    return (
        f'<label>{html.escape(label)}'
        f'<input type="{html.escape(input_type)}" name="{html.escape(name)}" '
        f'value="{html.escape(value)}" placeholder="{html.escape(placeholder)}"></label>'
    )


def _filter_form(page: str, controls: str) -> str:
    return (
        '<section class="panel"><h2>Search and filters</h2>'
        '<form method="get" action="/">'
        f'<input type="hidden" name="page" value="{html.escape(page)}">'
        f'<div class="filter-grid">{controls}</div><div style="margin-top:14px">'
        '<button type="submit">Apply filters</button> '
        f'<a class="button" href="/?page={html.escape(page)}" style="background:#fff;color:#174b3a">Clear</a>'
        '</div></form></section>'
    )


def _student_form(
    user: sqlite3.Row,
    sections: list[sqlite3.Row],
    buses: list[sqlite3.Row],
    student: sqlite3.Row | None = None,
    error: str = "",
) -> str:
    def value(field: str) -> str:
        return str(student[field]) if student else ""

    action = "/students/update" if student else "/students/add"
    heading = "Edit student" if student else "Add student"
    submit = "Save changes" if student else "Add student"
    notice = f'<div class="notice" style="border-color:#e4c9c5;background:#fff3f1;color:#7f2f2f">{html.escape(error)}</div>' if error else ""
    section_options = "".join(
        f'<option value="{html.escape(row["section_id"])}"'
        f'{" selected" if student and row["section_id"] == student["section_id"] else ""}>'
        f'{html.escape(row["class_name"])} {html.escape(row["section_name"])} '
        f'- {html.escape(row["academic_session"])}</option>'
        for row in sections
    )
    bus_options = "".join(
        f'<option value="{html.escape(row["bus_id"])}"'
        f'{" selected" if student and row["bus_id"] == student["bus_id"] else ""}>'
        f'{html.escape(row["bus_number"])} - {html.escape(row["route"])}</option>'
        for row in buses
    )
    student_id_field = (
        f'<input type="hidden" name="student_id" value="{html.escape(student["student_id"])}">'
        if student else ""
    )
    return (
        f'<section class="panel"><h2>{heading}</h2>{notice}'
        f'<form method="post" action="{action}">'
        f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
        f'{student_id_field}'
        '<div class="grid">'
        '<div><label>Student name</label>'
        f'<input type="text" name="full_name" required maxlength="120" value="{html.escape(value("full_name"))}"></div>'
        '<div><label>Admission number</label>'
        f'<input type="text" name="admission_number" required maxlength="40" value="{html.escape(value("admission_number"))}"></div>'
        '<div><label>Class and section</label>'
        f'<select name="section_id" required><option value="">Select</option>{section_options}</select></div>'
        '<div><label>Roll number</label>'
        f'<input type="number" name="roll_number" required min="1" max="999" value="{html.escape(value("roll_number"))}"></div>'
        '<div><label>Date of birth</label>'
        f'<input type="date" name="date_of_birth" required value="{html.escape(value("date_of_birth"))}"></div>'
        '<div><label>Gender</label><select name="gender" required>'
        + "".join(
            f'<option value="{gender}"'
            f'{" selected" if student and student["gender"] == gender else ""}>{gender}</option>'
            for gender in ("Male", "Female", "Other")
        )
        + '</select></div>'
        '<div><label>Parent/guardian name</label>'
        f'<input type="text" name="guardian_name" required maxlength="120" value="{html.escape(value("guardian_name"))}"></div>'
        '<div><label>Contact number</label>'
        f'<input type="text" name="guardian_phone" required maxlength="30" value="{html.escape(value("guardian_phone"))}"></div>'
        '<div><label>Guardian email</label>'
        f'<input type="email" name="guardian_email" maxlength="120" value="{html.escape(value("guardian_email"))}"></div>'
        '<div><label>Address</label>'
        f'<input type="text" name="address" required maxlength="300" value="{html.escape(value("address"))}"></div>'
        '<div><label>Father&apos;s name</label>'
        f'<input type="text" name="father_name" maxlength="120" value="{html.escape(value("father_name"))}"></div>'
        '<div><label>Mother&apos;s name</label>'
        f'<input type="text" name="mother_name" maxlength="120" value="{html.escape(value("mother_name"))}"></div>'
        '<div><label>Emergency contact</label>'
        f'<input type="text" name="emergency_contact" maxlength="30" value="{html.escape(value("emergency_contact"))}"></div>'
        '<div><label>Admission date</label>'
        f'<input type="date" name="admission_date" value="{html.escape(value("admission_date"))}"></div>'
        '<div><label>Student status</label><select name="status" required>'
        + "".join(
            f'<option value="{status}"'
            f'{" selected" if (value("status") or "Active") == status else ""}>{status}</option>'
            for status in ("Active", "Inactive")
        )
        + '</select></div>'
        '<div><label>Assigned bus</label>'
        f'<select name="bus_id"><option value="">Not assigned</option>{bus_options}</select></div>'
        '</div><p><button type="submit">' + submit +
        '</button> <a class="button" href="/?page=students" style="background:#fff;color:#174b3a">Cancel</a></p>'
        '</form></section>'
    )


def _staff_form(
    user: sqlite3.Row,
    employee: sqlite3.Row | None = None,
) -> str:
    def value(field: str) -> str:
        return str(employee[field]) if employee else ""

    action = "/staff/update" if employee else "/staff/add"
    heading = "Edit staff member" if employee else "Add staff member"
    employee_id_field = (
        f'<input type="hidden" name="employee_id" value="{html.escape(value("employee_id"))}">'
        if employee else ""
    )
    category_options = "".join(
        f'<option value="{category}"'
        f'{" selected" if (value("category") or "teacher") == category else ""}>'
        f'{"Teacher" if category == "teacher" else "Non-teaching staff"}</option>'
        for category in ("teacher", "staff")
    )
    status_options = "".join(
        f'<option value="{status}"'
        f'{" selected" if (value("status") or "Active") == status else ""}>{status}</option>'
        for status in ("Active", "Inactive")
    )
    employee_id_control = (
        f'<input type="text" value="{html.escape(value("employee_id"))}" readonly>'
        if employee else
        '<input type="text" name="employee_id" required maxlength="40">'
    )
    return (
        f'<section class="panel"><h2>{heading}</h2>'
        f'<form method="post" action="{action}">'
        f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
        f'{employee_id_field}<div class="grid">'
        f'<div><label>Employee ID</label>{employee_id_control}</div>'
        '<div><label>Staff category</label>'
        f'<select name="category" required>{category_options}</select></div>'
        '<div><label>Name</label>'
        f'<input type="text" name="name" required maxlength="120" value="{html.escape(value("name"))}"></div>'
        '<div><label>Designation</label>'
        f'<input type="text" name="designation" required maxlength="120" value="{html.escape(value("designation"))}"></div>'
        '<div><label>Department</label>'
        f'<input type="text" name="department" required maxlength="120" value="{html.escape(value("department"))}"></div>'
        '<div><label>Email</label>'
        f'<input type="email" name="email" required maxlength="120" value="{html.escape(value("email"))}"></div>'
        '<div><label>Phone</label>'
        f'<input type="text" name="phone" required maxlength="30" value="{html.escape(value("phone"))}"></div>'
        '<div><label>Joining date</label>'
        f'<input type="date" name="joining_date" required value="{html.escape(value("joining_date"))}"></div>'
        '<div><label>Status</label>'
        f'<select name="status" required>{status_options}</select></div>'
        '</div><p><button type="submit">Save staff details</button> '
        '<a class="button" href="/?page=staff" style="background:#fff;color:#174b3a">Cancel</a>'
        '</p></form></section>'
    )


def _class_section_form(
    user: sqlite3.Row,
    teachers: list[sqlite3.Row],
    section: sqlite3.Row | None = None,
) -> str:
    def value(field: str, default: str = "") -> str:
        return str(section[field]) if section else default

    action = "/classes/update" if section else "/classes/add"
    section_id_field = (
        f'<input type="hidden" name="section_id" value="{html.escape(value("section_id"))}">'
        if section else ""
    )
    teacher_options = "".join(
        f'<option value="{html.escape(row["employee_id"])}"'
        f'{" selected" if section and row["employee_id"] == section["homeroom_teacher_id"] else ""}>'
        f'{html.escape(row["name"])} · {html.escape(row["employee_id"])}'
        f'{" (inactive)" if row["status"] != "Active" else ""}</option>'
        for row in teachers
    )
    return (
        f'<section class="panel"><h2>{"Edit class and section" if section else "Create class section"}</h2>'
        f'<form method="post" action="{action}">'
        f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
        f'{section_id_field}<div class="grid">'
        '<div><label>Class name</label>'
        f'<input type="text" name="class_name" required maxlength="80" value="{html.escape(value("class_name"))}"></div>'
        '<div><label>Section name</label>'
        f'<input type="text" name="section_name" required maxlength="40" value="{html.escape(value("section_name"))}"></div>'
        '<div><label>Academic session</label>'
        f'<input type="text" name="academic_session" required maxlength="20" value="{html.escape(value("academic_session"))}"></div>'
        '<div><label>Class teacher</label>'
        f'<select name="homeroom_teacher_id" required><option value="">Select</option>{teacher_options}</select></div>'
        '</div><p><button type="submit">Save class section</button> '
        '<a class="button" href="/?page=classes" style="background:#fff;color:#174b3a">Cancel</a>'
        '</p></form></section>'
    )


def _staff_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return '<div class="empty">No records available.</div>'
    headers = [
        "Name", "Employee ID", "Category", "Designation", "Department",
        "Subjects", "Assigned classes", "Email", "Phone", "Joined", "Status",
    ]
    thead = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = []
    for row in rows:
        values = tuple(row)
        cells = []
        for index, value in enumerate(values):
            text = html.escape("" if value is None else str(value))
            if index == 0:
                text = (
                    f'<a href="/?page=staff&amp;action=profile&amp;'
                    f'{html.escape(urlencode({"id": row["employee_id"]}))}">{text}</a>'
                )
            elif text in {"Active", "Inactive"}:
                text = f'<span class="status-{text.lower()}">{text}</span>'
            cells.append(f"<td>{text}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _class_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return '<div class="empty">No records available.</div>'
    headers = ["Class", "Section", "Academic session", "Homeroom teacher", "Active students", "Manage"]
    thead = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = []
    for row in rows:
        edit_link = (
            f'<a href="/?page=classes&amp;action=edit&amp;'
            f'{html.escape(urlencode({"id": row["section_id"]}))}">Edit</a>'
        )
        values = tuple(row)[:5]
        cells = [f"<td>{html.escape(str(value))}</td>" for value in values]
        cells.append(f"<td>{edit_link}</td>")
        if row["is_demo"]:
            cells[0] = '<td><span class="badge demo">DEMO</span> ' + cells[0][4:]
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _fee_form(
    user: sqlite3.Row,
    student_id: str,
    fee: sqlite3.Row | None = None,
) -> str:
    fee_id_field = (
        f'<input type="hidden" name="fee_id" value="{html.escape(fee["fee_id"])}">'
        if fee else ""
    )
    fee_category = fee["fee_category"] if fee else ""
    annual_total = fee["annual_total"] if fee else ""
    due_date = fee["due_date"] if fee else ""
    session = fee["academic_session"] if fee else ""
    return (
        '<section class="panel"><h3>Edit fee information</h3>'
        '<p>Recorded payments are not changed. The outstanding balance is recalculated from the existing paid amount.</p>'
        '<form method="post" action="/students/fees/update">'
        f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
        f'<input type="hidden" name="student_id" value="{html.escape(student_id)}">'
        f'{fee_id_field}'
        f'<p><strong>Academic session:</strong> {html.escape(session or "Current session")}</p>'
        '<div class="grid"><div><label>Fee category</label>'
        f'<input type="text" name="fee_category" required maxlength="120" value="{html.escape(str(fee_category))}"></div>'
        '<div><label>Total fee (₹)</label>'
        f'<input type="number" name="annual_total" required min="1" max="100000000" value="{html.escape(str(annual_total))}"></div>'
        '<div><label>Due date</label>'
        f'<input type="date" name="due_date" required value="{html.escape(str(due_date))}"></div></div>'
        '<p><button type="submit">Save fee changes</button> '
        f'<a class="button" href="/?page=student&amp;{html.escape(urlencode({"id": student_id}))}" '
        'style="background:#fff;color:#174b3a">Cancel</a></p></form></section>'
    )


def _student_financial_forms(
    user: sqlite3.Row,
    student_id: str,
    fee: sqlite3.Row,
) -> str:
    return (
        '<div class="financial-actions">'
        '<section class="panel"><h3>Record payment</h3>'
        '<form method="post" action="/students/payments/add">'
        f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
        f'<input type="hidden" name="student_id" value="{html.escape(student_id)}">'
        f'<input type="hidden" name="fee_id" value="{html.escape(fee["fee_id"])}">'
        '<div class="grid">'
        '<div><label>Amount (₹)</label><input type="number" name="amount" required min="1" max="100000000"></div>'
        '<div><label>Payment date</label><input type="date" name="payment_date" required></div>'
        '<div><label>Payment method</label><select name="method" required>'
        '<option value="">Select</option><option>Cash</option><option>Card</option>'
        '<option>Bank Transfer</option><option>Cheque</option><option>Online</option></select></div>'
        '<div><label>Receipt/reference number</label><input type="text" name="receipt_number" required maxlength="40"></div>'
        '</div><label>Remarks</label><textarea name="remarks" rows="3" maxlength="500"></textarea>'
        '<p><button type="submit" onclick="return confirm(&quot;Record this payment?&quot;)">Record payment</button></p>'
        '</form></section>'
        '<section class="panel"><h3>Adjust fee due</h3>'
        '<p>Use a positive amount to increase the due or a negative amount to reduce it. This is recorded separately from payments.</p>'
        '<form method="post" action="/students/fees/adjust">'
        f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
        f'<input type="hidden" name="student_id" value="{html.escape(student_id)}">'
        f'<input type="hidden" name="fee_id" value="{html.escape(fee["fee_id"])}">'
        '<div class="grid">'
        '<div><label>Adjustment amount (₹)</label><input type="number" name="amount" required min="-100000000" max="100000000"></div>'
        '<div><label>Adjustment date</label><input type="date" name="adjustment_date" required></div>'
        '</div><label>Reason</label><textarea name="reason" required rows="3" maxlength="500"></textarea>'
        '<p><button class="danger" type="submit" onclick="return confirm(&quot;Save this fee adjustment? It will not be recorded as a payment.&quot;)">Save adjustment</button></p>'
        '</form></section>'
        '</div>'
    )


def _bar_chart(rows: list[tuple[str, float]], suffix: str = "%") -> str:
    peak = max((value for _, value in rows), default=0) or 1
    bars = []
    for label, value in rows:
        display_value = money(value) if suffix == " rupees" else f"{value:.0f}{suffix}"
        bars.append(
            f'<div class="barrow"><span>{html.escape(label)}</span>'
            f'<div class="track" role="img" aria-label="{html.escape(label)}: '
            f'{html.escape(display_value, quote=True)}">'
            f'<div class="bar" style="width:{max(0, min(100, value / peak * 100)):.1f}%"></div>'
            f'</div><strong>{html.escape(display_value)}</strong></div>'
        )
    return "".join(bars) or '<div class="empty">No chart data available.</div>'


def _module_href(page: str, **filters: str) -> str:
    return "/?" + urlencode({"page": page, **filters})


def _dashboard_card(label: str, value: str, hint: str, href: str) -> str:
    return (
        f'<a class="card card-link" href="{html.escape(href, quote=True)}" '
        f'aria-label="{html.escape(label, quote=True)}: {html.escape(value, quote=True)}">'
        f'<div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        f'<div class="hint">{html.escape(hint)}</div></a>'
    )


def _dashboard_modules(user: sqlite3.Row) -> str:
    descriptions = {
        "students": "Search student records and profiles",
        "staff": "Browse teachers and staff",
        "classes": "Browse classes, sections, and rosters",
        "attendance": "Review and update attendance",
        "fees": "Review fee balances",
        "payments": "Review payment history",
        "buses": "Browse transport routes",
        "boards": "Review smart-board status",
        "issues": "Track technical support issues",
        "announcements": "Read or publish school notices",
        "academics": "Review and update examination marks",
        "reports": "Review student report cards",
    }
    tiles = "".join(
        f'<a class="card card-link module-tile" href="{html.escape(_module_href("examinations" if key == "academics" else key), quote=True)}">'
        f'<div class="label">{html.escape("Examinations" if key == "academics" else label)}</div>'
        f'<div class="value">Open module</div>'
        f'<div class="hint">{html.escape(descriptions[key])}</div></a>'
        for key, label in NAV_ITEMS
        if key != "dashboard" and can_access_module(user, key)
    )
    return (
        '<section class="panel"><div class="section-head"><h2>Modules</h2></div>'
        f'<div class="cards">{tiles}</div></section>'
    )


def _scoped_dashboard(db: sqlite3.Connection, user: sqlite3.Row) -> str:
    role = user["role"]
    cards: list[tuple[str, str, str, str]] = []
    panels = ""
    if role == "Technical Team":
        total_boards = db.execute(
            "SELECT COUNT(*) FROM smart_boards"
        ).fetchone()[0]
        online_boards = db.execute(
            "SELECT COUNT(*) FROM smart_boards WHERE status='Online'"
        ).fetchone()[0]
        unresolved = db.execute(
            "SELECT COUNT(*) FROM technical_issues WHERE status!='Resolved'"
        ).fetchone()[0]
        pending = db.execute(
            "SELECT i.issue_id,cl.room_number,i.category,i.priority,e.name,i.status "
            "FROM technical_issues i JOIN classrooms cl ON cl.classroom_id=i.classroom_id "
            "JOIN employees e ON e.employee_id=i.technician_id "
            "WHERE i.status!='Resolved' "
            "ORDER BY CASE i.priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END,"
            "i.reported_at DESC"
        ).fetchall()
        cards = [
            ("Smart boards", str(total_boards), "Installed classroom boards", _module_href("boards")),
            ("Online", str(online_boards), "Current board status", _module_href("boards", status="Online")),
            ("Pending repairs", str(unresolved), "Issues not yet resolved", _module_href("issues", status="Unresolved")),
        ]
        panels = f'<section class="panel"><h2><a href="{html.escape(_module_href("issues", status="Unresolved"), quote=True)}">Pending repair issues</a></h2>' + _table(
            ["Issue", "Room", "Category", "Priority", "Assigned technician", "Status"],
            [tuple(row) for row in pending],
        ) + "</section>"
        title = "Technical operations"
    elif role == "Parent":
        children = db.execute(
            "SELECT s.student_id,s.full_name,c.class_name,c.section_name "
            "FROM parent_children pc JOIN students s ON s.student_id=pc.student_id "
            "JOIN class_sections c ON c.section_id=s.section_id "
            "WHERE pc.user_id=? AND s.status='Active' ORDER BY s.full_name",
            (user["user_id"],),
        ).fetchall()
        student_count = len(children)
        student_ids = [row["student_id"] for row in children]
        fees = 0
        if student_ids:
            placeholders = ",".join("?" for _ in student_ids)
            fees = db.execute(
                f"SELECT COALESCE(SUM(pending_amount),0) FROM fees "
                f"WHERE student_id IN ({placeholders})",
                student_ids,
            ).fetchone()[0]
        cards = [
            ("Linked children", str(student_count), "Only your linked students", _module_href("students")),
            ("Fees pending", money(fees), "For your linked children", _module_href("fees", payment_status="Outstanding")),
        ]
        panels = f'<section class="panel"><h2><a href="{html.escape(_module_href("students"), quote=True)}">Your children</a></h2>' + _table(
            ["Student", "Class", "Section"],
            [(row["full_name"], row["class_name"], row["section_name"]) for row in children],
        ) + "</section>"
        title = "Family dashboard"
    else:
        student_filter, student_params = student_scope(user, "s", "c")
        student_count = db.execute(
            "SELECT COUNT(*) FROM students s JOIN class_sections c "
            "ON c.section_id=s.section_id WHERE " + student_filter,
            student_params,
        ).fetchone()[0]
        cards = [("Assigned students", str(student_count), "Only assigned classes", _module_href("students"))]
        panels = ""
        if role == "Class Teacher":
            latest = db.execute(
                "SELECT MAX(a.attendance_date) FROM attendance a JOIN students s "
                "ON s.student_id=a.student_id JOIN class_sections c ON c.section_id=s.section_id "
                "WHERE " + student_filter,
                student_params,
            ).fetchone()[0]
            if latest:
                attendance = db.execute(
                    "SELECT COUNT(*),SUM(CASE WHEN a.status IN ('Present','Late') THEN 1 ELSE 0 END) "
                    "FROM attendance a JOIN students s ON s.student_id=a.student_id "
                    "JOIN class_sections c ON c.section_id=s.section_id "
                    "WHERE a.attendance_date=? AND " + student_filter,
                    (latest, *student_params),
                ).fetchone()
                rate = 100 * (attendance[1] or 0) / attendance[0] if attendance[0] else 0
                cards.append((
                    "Attendance", f"{rate:.1f}%", f"Assigned classes · {latest}",
                    _module_href("attendance", attendance_date=latest),
                ))
        exam_filter, exam_params = student_scope(user, "s", "c", "e.subject")
        averages = db.execute(
            "SELECT c.class_name || ' ' || c.section_name, "
            "ROUND(AVG(100.0*e.marks_obtained/e.maximum_marks),1) "
            "FROM examinations e JOIN students s ON s.student_id=e.student_id "
            "JOIN class_sections c ON c.section_id=e.section_id "
            "WHERE " + exam_filter +
            " GROUP BY c.section_id ORDER BY c.class_name,c.section_name",
            exam_params,
        ).fetchall()
        panels = f'<section class="panel"><h2><a href="{html.escape(_module_href("examinations"), quote=True)}">Assigned-class academic results</a></h2>' + _bar_chart(
            [(row[0], row[1]) for row in averages]
        ) + "</section>"
        title = "Class dashboard" if role == "Class Teacher" else "Subject dashboard"
    card_html = "".join(_dashboard_card(*card) for card in cards)
    today = date.today().strftime("%d %B %Y")
    return (
        f'<main><header class="dashboard-head"><div>'
        f'<p class="eyebrow">St Dominic Savio&#39;s High School</p>'
        f'<h1>{title}</h1>'
        '<p class="sub">A focused view of your school responsibilities and assigned records.</p>'
        f'</div><div class="dashboard-date"><span>Today</span><strong>{today}</strong></div></header>'
        f'<section class="cards">{card_html}</section><div class="grid">{panels}</div>'
        f'{_dashboard_modules(user)}</main>'
    )


def dashboard(db: sqlite3.Connection, user: sqlite3.Row) -> str:
    if user["role"] not in ADMIN_ROLES:
        return _scoped_dashboard(db, user)
    scalar = lambda sql, params=(): db.execute(sql, params).fetchone()[0]
    students = scalar("SELECT COUNT(*) FROM students")
    teachers = scalar(
        "SELECT COUNT(*) FROM employees WHERE category='teacher' AND status='Active'"
    )
    staff = scalar(
        "SELECT COUNT(*) FROM employees WHERE category='staff' AND status='Active'"
    )
    today = datetime.now().date().isoformat()
    attendance = db.execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) present, "
        "SUM(CASE WHEN status='Absent' THEN 1 ELSE 0 END) absent, "
        "SUM(CASE WHEN status='Late' THEN 1 ELSE 0 END) late "
        "FROM attendance WHERE attendance_date=?",
        (today,),
    ).fetchone()
    # Weekends have no attendance, so present the most recent school-day snapshot.
    attendance_date = today
    if not attendance["total"]:
        latest = db.execute(
            "SELECT MAX(attendance_date) FROM attendance"
        ).fetchone()[0]
        attendance_date = latest or today
        attendance = db.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) present, "
            "SUM(CASE WHEN status='Absent' THEN 1 ELSE 0 END) absent, "
            "SUM(CASE WHEN status='Late' THEN 1 ELSE 0 END) late "
            "FROM attendance WHERE attendance_date=?",
            (attendance_date,),
        ).fetchone()
    present, absent, late = (attendance[name] or 0 for name in ("present", "absent", "late"))
    attendance_rate = (present + late) * 100 / attendance["total"] if attendance["total"] else 0
    collected = scalar(
        "SELECT COALESCE(SUM(amount),0) FROM fee_payments"
    )
    pending = scalar("SELECT COALESCE(SUM(pending_amount),0) FROM fees")
    online = scalar("SELECT COUNT(*) FROM smart_boards WHERE status='Online'")
    unresolved = scalar("SELECT COUNT(*) FROM technical_issues WHERE status!='Resolved'")
    active_buses = scalar("SELECT COUNT(*) FROM buses WHERE status='Active'")
    cards = [
        ("Students", f"{students:,}", "Enrolled demo students", _module_href("students")),
        ("Teachers", str(teachers), "Active teachers", _module_href("staff", category="teacher", status="Active")),
        ("Non-teaching staff", str(staff), "Active non-teaching staff", _module_href("staff", category="staff", status="Active")),
        (f"Present · {attendance_date}", f"{present:,}", "Latest school-day snapshot",
         _module_href("attendance", attendance_date=attendance_date)),
        (f"Absent · {attendance_date}", f"{absent:,}", f"Latest snapshot · Late: {late:,}",
         _module_href("attendance", attendance_date=attendance_date, status="Absent")),
        ("Attendance", f"{attendance_rate:.1f}%", "Present and late / recorded students",
         _module_href("attendance", attendance_date=attendance_date)),
        ("Fees collected", money(collected), "Sum of recorded payments", _module_href("payments")),
        ("Fees pending", money(pending), "Outstanding fee balances",
         _module_href("fees", payment_status="Outstanding")),
        ("Online smart boards", str(online), "Calculated from board status",
         _module_href("boards", status="Online")),
        ("Unresolved issues", str(unresolved), "All issues except resolved",
         _module_href("issues", status="Unresolved")),
        ("Active buses", str(active_buses), "Routes with active status",
         _module_href("buses", status="Active")),
    ]
    cards_html = "".join(_dashboard_card(*card) for card in cards)
    class_rows = db.execute(
        "SELECT c.class_name || ' ' || c.section_name, "
        "ROUND(100.0 * SUM(CASE WHEN a.status IN ('Present','Late') THEN 1 ELSE 0 END) / COUNT(*),1) "
        "FROM attendance a JOIN students s ON s.student_id=a.student_id "
        "JOIN class_sections c ON c.section_id=s.section_id "
        "WHERE a.attendance_date=? "
        "GROUP BY c.section_id ORDER BY c.class_name,c.section_name",
        (attendance_date,),
    ).fetchall()
    board_rows = db.execute(
        "SELECT status, COUNT(*) FROM smart_boards GROUP BY status "
        "ORDER BY CASE status WHEN 'Online' THEN 1 WHEN 'Offline' THEN 2 WHEN 'Error' THEN 3 ELSE 4 END"
    ).fetchall()
    academic_rows = db.execute(
        "SELECT c.class_name, ROUND(AVG(100.0*e.marks_obtained/e.maximum_marks),1) "
        "FROM examinations e JOIN class_sections c ON c.section_id=e.section_id "
        "WHERE 1=1 GROUP BY c.class_name ORDER BY c.class_name"
    ).fetchall()
    today_label = date.today().strftime("%d %B %Y")
    content = f"""<main><header class="dashboard-head"><div>
<p class="eyebrow">St Dominic Savio&#39;s High School</p><h1>School overview</h1>
<p class="sub">Live school summary · attendance snapshot {attendance_date}</p>
</div><div class="dashboard-date"><span>Today</span><strong>{today_label}</strong></div></header>
<section class="cards">{cards_html}</section>
<div class="grid">
<section class="panel"><h2><a href="{html.escape(_module_href('attendance', attendance_date=attendance_date), quote=True)}">Class attendance</a></h2>{_bar_chart([(row[0], row[1]) for row in class_rows])}</section>
<section class="panel"><h2><a href="{html.escape(_module_href('boards'), quote=True)}">Smart-board status</a></h2>{_bar_chart([(row[0], row[1]) for row in board_rows], " boards")}</section>
<section class="panel"><h2><a href="{html.escape(_module_href('examinations'), quote=True)}">Average examination score by class</a></h2>{_bar_chart([(row[0], row[1]) for row in academic_rows])}</section>
<section class="panel"><h2><a href="{html.escape(_module_href('fees', payment_status='Outstanding'), quote=True)}">Fee position</a></h2>{_bar_chart([("Collected", collected), ("Outstanding", pending)], " rupees")}</section>
</div>{_dashboard_modules(user)}</main>"""
    return content


def attendance_page(
    db: sqlite3.Connection, user: sqlite3.Row, params: dict[str, list[str]],
) -> str:
    scope, scope_params = student_scope(user, "s", "c")
    sections = db.execute(
        "SELECT DISTINCT c.section_id,c.class_name,c.section_name,c.academic_session "
        "FROM class_sections c LEFT JOIN students s ON s.section_id=c.section_id "
        "WHERE " + scope +
        " ORDER BY c.class_name,c.section_name,c.academic_session",
        scope_params,
    ).fetchall()
    students = db.execute(
        "SELECT s.student_id,s.full_name,s.admission_number,c.class_name,"
        "c.section_name,s.roll_number FROM students s "
        "JOIN class_sections c ON c.section_id=s.section_id "
        "WHERE s.status='Active' AND " + scope +
        " ORDER BY c.class_name,c.section_name,s.roll_number",
        scope_params,
    ).fetchall()

    section_by_id = {row["section_id"]: row for row in sections}
    classes = sorted({row["class_name"] for row in sections})
    selected_class = params.get("class_name", [""])[0].strip()
    selected_section_id = params.get("section_id", [""])[0].strip()
    requested_section = section_by_id.get(selected_section_id)
    if requested_section and (
        not selected_class or requested_section["class_name"] == selected_class
    ):
        selected_class = requested_section["class_name"]
    else:
        requested_section = None
        selected_section_id = ""
    if not selected_section_id and selected_class:
        requested_section = next(
            (row for row in sections if row["class_name"] == selected_class), None
        )
        selected_section_id = requested_section["section_id"] if requested_section else ""
    if not selected_section_id and sections and not selected_class:
        requested_section = sections[0]
        selected_section_id = requested_section["section_id"]
        selected_class = requested_section["class_name"]

    requested_date = params.get("attendance_date", [""])[0].strip()
    date_error = ""
    try:
        selected_date = date.fromisoformat(requested_date) if requested_date else date.today()
        if requested_date and selected_date.isoformat() != requested_date:
            raise ValueError
    except ValueError:
        selected_date = date.today()
        date_error = "Enter a valid attendance date."
    attendance_date = selected_date.isoformat()

    def options(values: list[str], selected: str) -> str:
        return "".join(
            f'<option value="{html.escape(value)}"'
            f'{" selected" if value == selected else ""}>'
            f'{html.escape(value)}</option>'
            for value in values
        )

    def render_table(headers: list[str], rows: list[tuple]) -> str:
        if not rows:
            return '<div class="empty">No records available.</div>'
        head = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
        body = "".join(
            "<tr>" + "".join(
                f"<td>{html.escape('' if value is None else str(value))}</td>"
                for value in row
            ) + "</tr>"
            for row in rows
        )
        return (
            '<div class="table-wrap"><table><thead><tr>' + head +
            "</tr></thead><tbody>" + body + "</tbody></table></div>"
        )

    notice = ""
    if params.get("saved") == ["1"]:
        notice = '<div class="notice">Attendance saved successfully.</div>'
    if date_error:
        notice += f'<div class="notice" role="alert">{date_error}</div>'
    if params.get("section_id") and not requested_section:
        notice += (
            '<div class="notice" role="alert">'
            'Choose a class section available to your account.</div>'
        )

    class_options = options(classes, selected_class)
    section_options = "".join(
        f'<option value="{html.escape(row["section_id"])}"'
        f'{" selected" if row["section_id"] == selected_section_id else ""}>'
        f'{html.escape(row["class_name"])} · {html.escape(row["section_name"])}'
        f' ({html.escape(row["academic_session"])})</option>'
        for row in sections
        if not selected_class or row["class_name"] == selected_class
    )
    selection_form = f"""<section class="panel"><h2>Select class, section and date</h2>
<form method="get" action="/"><input type="hidden" name="page" value="attendance">
<div class="filter-grid">
<label>Class<select name="class_name"><option value="">All available classes</option>{class_options}</select></label>
<label>Section<select name="section_id"><option value="">Select a section</option>{section_options}</select></label>
<label>Attendance date<input type="date" name="attendance_date" value="{attendance_date}" required></label>
</div><div style="margin-top:14px"><button type="submit">Load attendance</button></div></form></section>"""

    roster = db.execute(
        "SELECT s.student_id,s.full_name,s.admission_number,s.roll_number,"
        "a.status FROM students s JOIN class_sections c ON c.section_id=s.section_id "
        "LEFT JOIN attendance a ON a.student_id=s.student_id AND a.attendance_date=? "
        "WHERE s.section_id=? AND s.status='Active' AND " + scope +
        " ORDER BY s.roll_number,s.full_name",
        (attendance_date, selected_section_id, *scope_params),
    ).fetchall() if requested_section else []
    total = len(roster)
    counts = {
        status: sum(row["status"] == status for row in roster)
        for status in ("Present", "Absent", "Late")
    }
    percentage = (
        (counts["Present"] + counts["Late"]) * 100 / total if total else 0
    )
    cards = (
        '<div class="cards">' +
        "".join(
            f'<section class="card"><div class="label">{label}</div>'
            f'<div class="value">{value}</div><div class="hint">{hint}</div></section>'
            for label, value, hint in (
                ("Total students", total, "Active students in the selected section"),
                ("Present", counts["Present"], "Marked present"),
                ("Absent", counts["Absent"], "Marked absent"),
                ("Late", counts["Late"], "Arrived late"),
                ("Attendance", f"{percentage:.1f}%", "Present and late / total students"),
            )
        ) + "</div>"
    ) if requested_section else ""

    can_manage = user["role"] in ADMIN_ROLES | {"Class Teacher"}
    if requested_section and roster:
        roster_rows = []
        for row in roster:
            previous = row["status"] or ""
            status_options = "".join(
                f'<option value="{status}"'
                f'{" selected" if previous == status else ""}>{status}</option>'
                for status in ("Present", "Absent", "Late")
            )
            if previous == "Leave":
                status_options += '<option value="Leave" selected>Leave (legacy)</option>'
            roster_rows.append(
                "<tr>" +
                f'<td>{row["roll_number"]}</td>'
                f'<td>{html.escape(row["full_name"])}</td>'
                f'<td>{html.escape(row["admission_number"])}</td>'
                + (
                    f'<td><select class="attendance-status" name="status_{html.escape(row["student_id"])}" '
                    'aria-label="Attendance status" required>'
                    '<option value="">Choose status</option>' + status_options +
                    "</select></td>"
                    if can_manage else
                    f'<td>{html.escape(previous or "Not marked")}</td>'
                ) +
                "</tr>"
            )
        if can_manage:
            roster_content = (
                '<form method="post" action="/attendance/mark" id="attendance-form">'
                f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
                f'<input type="hidden" name="section_id" value="{html.escape(selected_section_id)}">'
                f'<input type="hidden" name="attendance_date" value="{attendance_date}">'
                '<div class="profile-actions"><button type="button" id="mark-all-present">'
                'Mark all present</button><button type="submit">Save attendance</button></div>'
                '<div class="table-wrap"><table><thead><tr><th>Roll</th><th>Student</th>'
                '<th>Admission no.</th><th>Status</th></tr></thead><tbody>' +
                "".join(roster_rows) +
                '</tbody></table></div></form>'
                '<script>document.getElementById("mark-all-present").addEventListener("click",'
                '()=>document.querySelectorAll("#attendance-form .attendance-status").forEach(select=>select.value="Present"));</script>'
            )
        else:
            roster_content = (
                '<div class="table-wrap"><table><thead><tr><th>Roll</th><th>Student</th>'
                '<th>Admission no.</th><th>Status</th></tr></thead><tbody>' +
                "".join(roster_rows) +
                '</tbody></table></div>'
            )
    elif requested_section:
        roster_content = '<div class="empty">No active students in this section.</div>'
    else:
        roster_content = (
            '<div class="empty">Select a class section to view its attendance.</div>'
        )
    if requested_section and can_manage:
        roster_panel = (
            '<section class="panel"><h2>Daily class register · ' +
            html.escape(requested_section["class_name"]) + " · " +
            html.escape(requested_section["section_name"]) + " · " +
            attendance_date + "</h2>" + roster_content + "</section>"
        )
    else:
        roster_panel = (
            '<section class="panel"><h2>Daily class register</h2>' +
            roster_content + "</section>"
        )

    overview_query = (
        "SELECT a.attendance_date,c.class_name,c.section_name,c.section_id,"
        "SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN a.status='Absent' THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN a.status='Late' THEN 1 ELSE 0 END),"
        "(SELECT COUNT(*) FROM students active WHERE active.section_id=c.section_id "
        "AND active.status='Active') "
        "FROM attendance a JOIN students s ON s.student_id=a.student_id "
        "JOIN class_sections c ON c.section_id=s.section_id "
        "WHERE s.status='Active' AND " + scope
    )
    overview_params: tuple[object, ...] = (*scope_params,)
    if selected_class:
        overview_query += " AND c.class_name=?"
        overview_params += (selected_class,)
    if requested_section:
        overview_query += " AND c.section_id=?"
        overview_params += (selected_section_id,)
    overview_rows = db.execute(
        overview_query +
        " GROUP BY a.attendance_date,c.section_id "
        "ORDER BY a.attendance_date DESC,c.class_name,c.section_name LIMIT 30",
        overview_params,
    ).fetchall()
    overview_table = render_table(
        ["Date", "Class", "Section", "Present", "Absent", "Late", "Attendance"],
        [
            (
                row["attendance_date"], row["class_name"], row["section_name"],
                row[4], row[5], row[6],
                f"{(row[4] + row[6]) * 100 / row[7]:.1f}%" if row[7] else "0.0%",
            )
            for row in overview_rows
        ],
    )

    absent_date = params.get("absent_date", [attendance_date])[0].strip()
    absent_class = params.get("absent_class", [""])[0].strip()
    absent_section = params.get("absent_section_id", [""])[0].strip()
    absent_error = ""
    try:
        parsed_absent_date = date.fromisoformat(absent_date)
        if parsed_absent_date.isoformat() != absent_date:
            raise ValueError
    except ValueError:
        absent_error = "Enter a valid date for the absent-student filter."
        absent_date = attendance_date
    absent_query = (
        "SELECT s.full_name,s.admission_number,c.class_name,c.section_name,"
        "a.attendance_date,a.status FROM attendance a "
        "JOIN students s ON s.student_id=a.student_id "
        "JOIN class_sections c ON c.section_id=s.section_id "
        "WHERE a.status='Absent' AND s.status='Active' AND " + scope
    )
    absent_params: tuple[object, ...] = (*scope_params,)
    if absent_date:
        absent_query += " AND a.attendance_date=?"
        absent_params += (absent_date,)
    if absent_class:
        absent_query += " AND c.class_name=?"
        absent_params += (absent_class,)
    if absent_section:
        if absent_section in section_by_id:
            absent_query += " AND c.section_id=?"
            absent_params += (absent_section,)
        else:
            absent_query += " AND 0=1"
    absent_rows = db.execute(
        absent_query + " ORDER BY c.class_name,c.section_name,s.full_name",
        absent_params,
    ).fetchall()
    absent_controls = (
        '<section class="panel"><h2>Absent students</h2>' +
        (f'<div class="notice" role="alert">{absent_error}</div>' if absent_error else "") +
        '<form method="get" action="/"><input type="hidden" name="page" value="attendance">'
        f'<input type="hidden" name="attendance_date" value="{attendance_date}">'
        '<div class="filter-grid">'
        f'<label>Date<input type="date" name="absent_date" value="{html.escape(absent_date)}" required></label>'
        '<label>Class<select name="absent_class"><option value="">All available classes</option>' +
        options(classes, absent_class) + "</select></label>"
        '<label>Section<select name="absent_section_id"><option value="">All sections</option>' +
        "".join(
            f'<option value="{html.escape(row["section_id"])}"'
            f'{" selected" if row["section_id"] == absent_section else ""}>'
            f'{html.escape(row["class_name"])} · {html.escape(row["section_name"])}</option>'
            for row in sections
        ) +
        '</select></label></div><div style="margin-top:14px">'
        '<button type="submit">Filter absent students</button></div></form><br>' +
        render_table(
            ["Student", "Admission no.", "Class", "Section", "Date", "Status"],
            [tuple(row) for row in absent_rows],
        ) + "</section>"
    )

    history_id = params.get("history_student_id", [""])[0].strip()
    history_from = params.get("history_from", [""])[0].strip()
    history_to = params.get("history_to", [""])[0].strip()
    history_error = ""
    parsed_from = parsed_to = None
    for label, value in (("start", history_from), ("end", history_to)):
        if value:
            try:
                parsed_value = date.fromisoformat(value)
                if parsed_value.isoformat() != value:
                    raise ValueError
                if label == "start":
                    parsed_from = parsed_value
                else:
                    parsed_to = parsed_value
            except ValueError:
                history_error = "Enter valid start and end dates for attendance history."
    if parsed_from and parsed_to and parsed_from > parsed_to:
        history_error = "The history start date must not be after its end date."
    student_options = "".join(
        f'<option value="{html.escape(row["student_id"])}"'
        f'{" selected" if row["student_id"] == history_id else ""}>'
        f'{html.escape(row["full_name"])} · {html.escape(row["admission_number"])} '
        f'· {html.escape(row["class_name"])} {html.escape(row["section_name"])}</option>'
        for row in students
    )
    history_content = '<div class="empty">Select a student to view attendance history.</div>'
    history_student = next(
        (row for row in students if row["student_id"] == history_id), None
    )
    if history_id and not history_student:
        history_error = "That student is not available to your account."
    elif history_student and not history_error:
        history_query = (
            "SELECT attendance_date,status FROM attendance WHERE student_id=?"
        )
        history_params: tuple[object, ...] = (history_id,)
        if parsed_from:
            history_query += " AND attendance_date>=?"
            history_params += (history_from,)
        if parsed_to:
            history_query += " AND attendance_date<=?"
            history_params += (history_to,)
        history_rows = db.execute(
            history_query + " ORDER BY attendance_date DESC",
            history_params,
        ).fetchall()
        overall = db.execute(
            "SELECT COUNT(*),SUM(CASE WHEN status IN ('Present','Late') "
            "THEN 1 ELSE 0 END) FROM attendance WHERE student_id=?",
            (history_id,),
        ).fetchone()
        overall_total = overall[0] or 0
        overall_attended = overall[1] or 0
        overall_percentage = (
            overall_attended * 100 / overall_total if overall_total else 0
        )
        history_content = (
            '<div class="profile-summary"><section class="card"><div class="label">'
            'Overall attendance</div><div class="value">'
            f'{overall_percentage:.1f}%</div><div class="hint">'
            f'{overall_attended} attended of {overall_total} recorded days</div></section></div>' +
            render_table(
                ["Date", "Status"],
                [(row["attendance_date"], row["status"]) for row in history_rows],
            )
        )
    history_panel = (
        '<section class="panel"><h2>Student attendance history</h2>' +
        (f'<div class="notice" role="alert">{history_error}</div>' if history_error else "") +
        '<form method="get" action="/"><input type="hidden" name="page" value="attendance">'
        f'<input type="hidden" name="attendance_date" value="{attendance_date}">'
        '<div class="filter-grid"><label>Student<select name="history_student_id">'
        '<option value="">Select a student</option>' + student_options +
        '</select></label>'
        f'<label>From<input type="date" name="history_from" value="{html.escape(history_from)}"></label>'
        f'<label>To<input type="date" name="history_to" value="{html.escape(history_to)}"></label>'
        '</div><div style="margin-top:14px"><button type="submit">View history</button></div>'
        '</form><br>' + history_content + "</section>"
    )

    return (
        '<main><div class="dashboard-head"><div><p class="eyebrow">People & learning</p>'
        '<h1>Attendance</h1><p class="sub">Daily class registers, attendance summaries, '
        'absent students and student history.</p></div></div>' +
        notice + selection_form + cards + roster_panel +
        '<section class="panel" style="margin-top:18px"><h2>Date-wise attendance overview</h2>' +
        overview_table + "</section>" + absent_controls + history_panel + "</main>"
    )


def module_page(db: sqlite3.Connection, page: str, user: sqlite3.Row,
                params: dict[str, list[str]] | None = None) -> str:
    if not can_access_module(user, page):
        raise PermissionError
    params = params or {}
    if page == "attendance":
        return attendance_page(db, user, params)
    config = {
        "students": (
            "Students", "Student records across Nursery to Class XII.",
            """SELECT s.student_id, s.full_name, s.admission_number, c.class_name, c.section_name,
            s.roll_number, s.date_of_birth, s.gender, s.guardian_name,
            s.guardian_phone, COALESCE(b.bus_number,'Not assigned'), s.status,s.is_demo
            FROM students s JOIN class_sections c ON c.section_id=s.section_id
            LEFT JOIN buses b ON b.bus_id=s.bus_id
            WHERE 1=1 ORDER BY c.class_name,c.section_name,s.roll_number""",
            ["Student", "Admission no.", "Class", "Section", "Roll", "Date of birth",
             "Gender", "Parent/guardian", "Parent phone", "Bus", "Status"],
        ),
        "staff": (
            "Teachers and staff", "Teacher assignments, departments, and non-teaching staff.",
            """SELECT e.name,e.employee_id,
            CASE e.category WHEN 'teacher' THEN 'Teacher' ELSE 'Non-teaching staff' END,
            e.designation,e.department,
            COALESCE((SELECT GROUP_CONCAT(DISTINCT ta.subject) FROM teacher_assignments ta
                WHERE ta.employee_id=e.employee_id),''),
            COALESCE((SELECT GROUP_CONCAT(DISTINCT c.class_name || ' ' || c.section_name)
                FROM teacher_assignments ta JOIN class_sections c ON c.section_id=ta.section_id
                WHERE ta.employee_id=e.employee_id),''),
            e.email,e.phone,e.joining_date,e.status
            FROM employees e WHERE 1=1 ORDER BY e.category,e.name""",
            ["Name", "Employee ID", "Category", "Designation", "Department", "Subjects",
             "Assigned classes", "Email", "Phone", "Joined", "Status"],
        ),
        "classes": (
            "Classes and sections", "Database-backed classes, homeroom teachers, and accessible student counts.",
            """SELECT c.class_name,c.section_name,c.academic_session,
            COALESCE(e.name,'Unassigned'),COUNT(DISTINCT s.student_id),c.section_id,c.is_demo
            FROM class_sections c
            LEFT JOIN employees e ON e.employee_id=c.homeroom_teacher_id
            LEFT JOIN students s ON s.section_id=c.section_id AND s.status='Active'
            WHERE 1=1
            GROUP BY c.section_id ORDER BY c.class_name,c.section_name""",
            ["Class", "Section", "Academic session", "Homeroom teacher", "Active students"],
        ),
        "attendance": (
            "Attendance", "Individual daily attendance records for the previous 30 school days.",
            """SELECT a.attendance_date, s.full_name, c.class_name, c.section_name,
            s.roll_number, a.status FROM attendance a
            JOIN students s ON s.student_id=a.student_id
            JOIN class_sections c ON c.section_id=s.section_id
            WHERE 1=1 ORDER BY a.attendance_date DESC,c.class_name,c.section_name,s.roll_number
            LIMIT 1500""",
            ["Date", "Student", "Class", "Section", "Roll", "Status"],
        ),
        "fees": (
            "Fees", "Fee balances and collected payments are linked to individual students.",
            """SELECT s.full_name, s.admission_number, c.class_name, f.fee_category,
            f.annual_total, f.paid_amount, f.pending_amount, f.due_date, f.payment_status
            FROM fees f JOIN students s ON s.student_id=f.student_id
            JOIN class_sections c ON c.section_id=s.section_id
            WHERE 1=1 ORDER BY f.payment_status,f.pending_amount DESC""",
            ["Student", "Admission no.", "Class", "Fee category", "Annual total",
             "Paid", "Pending", "Due date", "Payment status"],
        ),
        "payments": (
            "Fee payment history", "Actual demo payment records linked to fee accounts and students.",
            """SELECT p.receipt_number,s.full_name,s.admission_number,p.payment_date,
            p.amount,p.method,f.academic_session FROM fee_payments p
            JOIN fees f ON f.fee_id=p.fee_id JOIN students s ON s.student_id=f.student_id
            JOIN class_sections c ON c.section_id=s.section_id
            WHERE 1=1 ORDER BY p.payment_date DESC,p.receipt_number""",
            ["Receipt", "Student", "Admission no.", "Payment date", "Amount", "Method", "Session"],
        ),
        "buses": (
            "Transport", "GPS coordinates and locations are simulated for demonstration only.",
            """SELECT b.bus_number,b.registration_number,b.driver_name,b.driver_phone,b.route,
            b.capacity,(SELECT COUNT(*) FROM students s WHERE s.bus_id=b.bus_id),
            (SELECT GROUP_CONCAT(s.full_name, ', ') FROM students s WHERE s.bus_id=b.bus_id),
            printf('%.4f, %.4f · %s',b.latitude,b.longitude,b.location_label),b.status
            FROM buses b WHERE 1=1 ORDER BY b.bus_number""",
            ["Bus no.", "Registration", "Driver", "Phone", "Route", "Capacity",
            "Assigned student count", "Assigned students", "Simulated location", "Status"],
        ),
        "boards": (
            "Smart boards", "One BenQ board is installed in each demo classroom.",
            """SELECT cl.room_number,b.board_id,b.brand,b.model,b.status,b.last_update,
            COUNT(m.maintenance_id) FROM smart_boards b
            JOIN classrooms cl ON cl.classroom_id=b.classroom_id
            LEFT JOIN board_maintenance m ON m.board_id=b.board_id
            WHERE 1=1 GROUP BY b.board_id ORDER BY cl.room_number""",
            ["Room", "Board ID", "Brand", "Model", "Status", "Last update", "Maintenance entries"],
        ),
        "issues": (
            "Technical issues", "Reported smart-board problems, including unresolved work.",
            """SELECT i.issue_id,cl.room_number,i.board_id,i.category,i.description,
            i.priority,i.reporting_user,i.reported_at,e.name,i.status,i.repair_notes
            FROM technical_issues i JOIN classrooms cl ON cl.classroom_id=i.classroom_id
            JOIN employees e ON e.employee_id=i.technician_id
            WHERE 1=1 ORDER BY i.reported_at DESC""",
            ["Issue ID", "Room", "Board ID", "Category", "Description", "Priority",
             "Reported by", "Reported at", "Technician", "Status", "Repair notes"],
        ),
        "announcements": (
            "Announcements", "Published school notices with this account's read/unread status.",
            """SELECT a.title,a.description,a.audience,a.author,a.published_at,a.priority,
            r.read_status FROM announcements a LEFT JOIN announcement_reads r
            ON r.announcement_id=a.announcement_id AND r.user_id=?
            WHERE 1=1 ORDER BY a.published_at DESC""",
            ["Title", "Description", "Audience", "Author", "Date", "Priority", "Read status"],
        ),
        "academics": (
            "Examinations", "Examination marks are linked to students, classes, subjects, and sessions.",
            """SELECT s.full_name,c.class_name,c.section_name,e.examination_name,e.subject,
            e.marks_obtained,e.maximum_marks,e.grade,e.academic_session
            FROM examinations e JOIN students s ON s.student_id=e.student_id
            JOIN class_sections c ON c.section_id=e.section_id
            WHERE 1=1 ORDER BY c.class_name,c.section_name,e.examination_name,s.full_name,e.subject
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
            JOIN examinations e ON e.student_id=s.student_id
            WHERE 1=1 GROUP BY s.student_id ORDER BY c.class_name,c.section_name,s.full_name""",
            ["Student", "Admission no.", "Class", "Section", "Subject records",
             "Average score", "Overall grade"],
        ),
    }
    heading, description, query, headers = config[page]
    if page == "students" and user["role"] == "Subject Teacher":
        query = """SELECT s.student_id,s.full_name,s.admission_number,c.class_name,c.section_name,
            s.roll_number,s.status,s.is_demo FROM students s
            JOIN class_sections c ON c.section_id=s.section_id
            WHERE 1=1 ORDER BY c.class_name,c.section_name,s.roll_number"""
        headers = ["Student", "Admission no.", "Class", "Section", "Roll", "Status"]
    scoped_params: tuple[str, ...] = ()
    if page in {"students", "attendance", "fees", "academics"}:
        scope, scoped_params = student_scope(user, "s", "c", "e.subject" if page == "academics" else None)
        marker = {
            "students": "WHERE 1=1",
            "attendance": "WHERE 1=1",
            "fees": "WHERE 1=1",
            "academics": "WHERE 1=1",
        }[page]
        query = query.replace(marker, marker + " AND " + scope)
    elif page == "classes":
        scope, scoped_params = student_scope(user, "s", "c")
        query = query.replace("WHERE 1=1", "WHERE 1=1 AND " + scope)
    elif page == "reports":
        scope, scoped_params = student_scope(user, "s", "c")
        query = query.replace("WHERE 1=1", "WHERE 1=1 AND " + scope)
    elif page == "payments" and user["role"] == "Parent":
        scope, scoped_params = student_scope(user, "s", "c")
        query = query.replace(
            "WHERE 1=1",
            "WHERE 1=1 AND " + scope,
        )
    elif page == "buses" and user["role"] == "Parent":
        query = query.replace(
            "(SELECT COUNT(*) FROM students s WHERE s.bus_id=b.bus_id)",
            "(SELECT COUNT(*) FROM students s JOIN parent_children pc "
            "ON pc.student_id=s.student_id WHERE s.bus_id=b.bus_id "
            "AND pc.user_id=?)",
        ).replace(
            "(SELECT GROUP_CONCAT(s.full_name, ', ') FROM students s WHERE s.bus_id=b.bus_id)",
            "(SELECT GROUP_CONCAT(s.full_name, ', ') FROM students s "
            "JOIN parent_children pc ON pc.student_id=s.student_id "
            "WHERE s.bus_id=b.bus_id AND pc.user_id=?)",
        ).replace(
            "WHERE 1=1",
            "WHERE 1=1 AND EXISTS (SELECT 1 FROM students s "
            "JOIN parent_children pc ON pc.student_id=s.student_id "
            "WHERE pc.user_id=? AND s.bus_id=b.bus_id)",
        )
        scoped_params = (user["user_id"], user["user_id"], user["user_id"])
    elif page == "announcements" and user["role"] not in ADMIN_ROLES:
        audiences = ANNOUNCEMENT_AUDIENCES.get(user["role"], ())
        if not audiences:
            query = query.replace("WHERE 1=1", "WHERE 0=1")
        else:
            audience_slots = ",".join("?" for _ in audiences)
            query = query.replace(
                "WHERE 1=1",
                f"WHERE 1=1 AND a.audience IN ({audience_slots})",
            )
            scoped_params = audiences

    filter_values: dict[str, str] = {}
    filter_controls = ""
    if page == "students":
        filter_values = {
            "q": params.get("q", [""])[0].strip(),
            "class_name": params.get("class_name", [""])[0].strip(),
            "section_name": params.get("section_name", [""])[0].strip(),
            "status": params.get("status", [""])[0].strip(),
        }
        if filter_values["q"]:
            needle = "%" + filter_values["q"].lower() + "%"
            query = _append_query_clause(query, " AND (LOWER(s.full_name) LIKE ? OR LOWER(s.admission_number) LIKE ? OR LOWER(c.class_name) LIKE ?)")
            scoped_params += (needle, needle, needle)
        if filter_values["class_name"]:
            query = _append_query_clause(query, " AND c.class_name=?")
            scoped_params += (filter_values["class_name"],)
        if filter_values["section_name"]:
            query = _append_query_clause(query, " AND c.section_name=?")
            scoped_params += (filter_values["section_name"],)
        if filter_values["status"]:
            query = _append_query_clause(query, " AND s.status=?")
            scoped_params += (filter_values["status"],)
        accessible_sections = db.execute(
            "SELECT DISTINCT c.class_name,c.section_name "
            "FROM class_sections c LEFT JOIN students s ON s.section_id=c.section_id "
            "WHERE " + scope + " ORDER BY c.class_name,c.section_name",
            student_scope(user, "s", "c")[1],
        ).fetchall()
        classes = sorted({row["class_name"] for row in accessible_sections})
        sections = sorted({row["section_name"] for row in accessible_sections})
        filter_controls = _filter_form(page,
            _filter_control("q", "Student search", filter_values["q"], placeholder="Name, admission no. or class")
            + _filter_control("class_name", "Class", filter_values["class_name"], classes)
            + _filter_control("section_name", "Section", filter_values["section_name"], sections)
            + _filter_control("status", "Status", filter_values["status"], ["Active", "Inactive"]))
    elif page == "classes":
        filter_values = {
            "class_name": params.get("class_name", [""])[0].strip(),
            "section_name": params.get("section_name", [""])[0].strip(),
            "academic_session": params.get("academic_session", [""])[0].strip(),
        }
        accessible_sections = db.execute(
            """SELECT DISTINCT c.class_name,c.section_name,c.academic_session
            FROM class_sections c
            LEFT JOIN students s ON s.section_id=c.section_id
            WHERE """ + scope +
            " ORDER BY c.class_name,c.section_name,c.academic_session",
            scoped_params,
        ).fetchall()
        if filter_values["class_name"]:
            query = _append_query_clause(query, " AND c.class_name=?")
            scoped_params += (filter_values["class_name"],)
        if filter_values["section_name"]:
            query = _append_query_clause(query, " AND c.section_name=?")
            scoped_params += (filter_values["section_name"],)
        if filter_values["academic_session"]:
            query = _append_query_clause(query, " AND c.academic_session=?")
            scoped_params += (filter_values["academic_session"],)
        classes = sorted({row["class_name"] for row in accessible_sections})
        sections = sorted({row["section_name"] for row in accessible_sections})
        sessions = sorted({row["academic_session"] for row in accessible_sections})
        filter_controls = _filter_form(page,
            _filter_control("class_name", "Class", filter_values["class_name"], classes)
            + _filter_control("section_name", "Section", filter_values["section_name"], sections)
            + _filter_control("academic_session", "Academic session",
                              filter_values["academic_session"], sessions))
    elif page == "staff":
        filter_values = {
            "q": params.get("q", [""])[0].strip(),
            "designation": params.get("designation", [""])[0].strip(),
            "category": params.get("category", [""])[0].strip(),
            "status": params.get("status", [""])[0].strip(),
        }
        if filter_values["category"]:
            query = _append_query_clause(
                query, " AND e.category=?"
            )
            scoped_params += (filter_values["category"],)
        if filter_values["designation"]:
            query = _append_query_clause(
                query, " AND e.designation=?"
            )
            scoped_params += (filter_values["designation"],)
        if filter_values["status"]:
            query = _append_query_clause(query, " AND e.status=?")
            scoped_params += (filter_values["status"],)
        if filter_values["q"]:
            needle = "%" + filter_values["q"].lower() + "%"
            query = _append_query_clause(
                query,
                " AND (LOWER(e.name) LIKE ? OR LOWER(e.employee_id) LIKE ? OR LOWER(e.designation) LIKE ?)",
            )
            scoped_params += (needle, needle, needle)
        designations = [row[0] for row in db.execute(
            "SELECT DISTINCT designation FROM employees ORDER BY designation"
        )]
        filter_controls = _filter_form(page,
            _filter_control("q", "Staff search", filter_values["q"], placeholder="Name, employee ID or designation")
            + _filter_control("designation", "Designation", filter_values["designation"], designations)
            + _filter_control("category", "Staff category", filter_values["category"],
                              ["teacher", "staff"])
            + _filter_control("status", "Status", filter_values["status"],
                              ["Active", "Inactive"]))
    elif page == "attendance":
        filter_values = {
            "attendance_date": params.get("attendance_date", [""])[0].strip(),
            "class_name": params.get("class_name", [""])[0].strip(),
            "status": params.get("status", [""])[0].strip(),
        }
        if filter_values["attendance_date"]:
            query = _append_query_clause(query, " AND a.attendance_date=?")
            scoped_params += (filter_values["attendance_date"],)
        if filter_values["class_name"]:
            query = _append_query_clause(query, " AND c.class_name=?")
            scoped_params += (filter_values["class_name"],)
        if filter_values["status"]:
            query = _append_query_clause(query, " AND a.status=?")
            scoped_params += (filter_values["status"],)
        classes = [row[0] for row in db.execute(
            "SELECT DISTINCT class_name FROM class_sections ORDER BY class_name"
        )]
        filter_controls = _filter_form(page,
            _filter_control("attendance_date", "Date", filter_values["attendance_date"], input_type="date")
            + _filter_control("class_name", "Class", filter_values["class_name"], classes)
            + _filter_control("status", "Status", filter_values["status"],
                              ["Present", "Absent", "Late", "Leave"]))
    elif page == "academics":
        filter_values = {
            "class_name": params.get("class_name", [""])[0].strip(),
            "subject": params.get("subject", [""])[0].strip(),
            "examination": params.get("examination", [""])[0].strip(),
        }
        if filter_values["class_name"]:
            query = _append_query_clause(query, " AND c.class_name=?")
            scoped_params += (filter_values["class_name"],)
        if filter_values["subject"]:
            query = _append_query_clause(query, " AND LOWER(e.subject) LIKE ?")
            scoped_params += ("%" + filter_values["subject"].lower() + "%",)
        if filter_values["examination"]:
            query = _append_query_clause(query, " AND LOWER(e.examination_name) LIKE ?")
            scoped_params += ("%" + filter_values["examination"].lower() + "%",)
        classes = [row[0] for row in db.execute(
            "SELECT DISTINCT class_name FROM class_sections ORDER BY class_name"
        )]
        if user["role"] == "Subject Teacher" and user["employee_id"]:
            subjects = [row[0] for row in db.execute(
                "SELECT DISTINCT subject FROM teacher_assignments "
                "WHERE employee_id=? ORDER BY subject",
                (user["employee_id"],),
            )]
        else:
            subjects = [row[0] for row in db.execute(
                "SELECT DISTINCT subject FROM examinations ORDER BY subject"
            )]
        examinations = [row[0] for row in db.execute(
            "SELECT DISTINCT examination_name FROM examinations ORDER BY examination_name"
        )]
        filter_controls = _filter_form(page,
            _filter_control("class_name", "Class", filter_values["class_name"], classes)
            + _filter_control("subject", "Subject", filter_values["subject"], subjects)
            + _filter_control("examination", "Examination", filter_values["examination"], examinations))
    elif page == "fees":
        filter_values = {
            "q": params.get("q", [""])[0].strip(),
            "class_name": params.get("class_name", [""])[0].strip(),
            "payment_status": params.get("payment_status", [""])[0].strip(),
        }
        if filter_values["q"]:
            needle = "%" + filter_values["q"].lower() + "%"
            query = _append_query_clause(query, " AND (LOWER(s.full_name) LIKE ? OR LOWER(s.admission_number) LIKE ?)")
            scoped_params += (needle, needle)
        if filter_values["class_name"]:
            query = _append_query_clause(query, " AND c.class_name=?")
            scoped_params += (filter_values["class_name"],)
        if filter_values["payment_status"]:
            if filter_values["payment_status"] == "Outstanding":
                query = _append_query_clause(query, " AND f.pending_amount>0")
            else:
                query = _append_query_clause(query, " AND f.payment_status=?")
                scoped_params += (filter_values["payment_status"],)
        classes = [row[0] for row in db.execute(
            "SELECT DISTINCT class_name FROM class_sections ORDER BY class_name"
        )]
        filter_controls = _filter_form(page,
            _filter_control("q", "Student", filter_values["q"], placeholder="Name or admission no.")
            + _filter_control("class_name", "Class", filter_values["class_name"], classes)
            + _filter_control("payment_status", "Payment status", filter_values["payment_status"],
                              ["Paid", "Partially Paid", "Pending", "Overdue", "Outstanding"]))
    elif page == "payments":
        filter_values = {
            "q": params.get("q", [""])[0].strip(),
            "class_name": params.get("class_name", [""])[0].strip(),
            "method": params.get("method", [""])[0].strip(),
        }
        if filter_values["q"]:
            needle = "%" + filter_values["q"].lower() + "%"
            query = _append_query_clause(query, " AND (LOWER(s.full_name) LIKE ? OR LOWER(s.admission_number) LIKE ? OR LOWER(p.receipt_number) LIKE ?)")
            scoped_params += (needle, needle, needle)
        if filter_values["class_name"]:
            query = _append_query_clause(query, " AND c.class_name=?")
            scoped_params += (filter_values["class_name"],)
        if filter_values["method"]:
            query = _append_query_clause(query, " AND p.method=?")
            scoped_params += (filter_values["method"],)
        classes = [row[0] for row in db.execute(
            "SELECT DISTINCT class_name FROM class_sections ORDER BY class_name"
        )]
        methods = [row[0] for row in db.execute(
            "SELECT DISTINCT method FROM fee_payments ORDER BY method"
        )]
        filter_controls = _filter_form(page,
            _filter_control("q", "Search payments", filter_values["q"], placeholder="Student, admission no. or receipt")
            + _filter_control("class_name", "Class", filter_values["class_name"], classes)
            + _filter_control("method", "Payment method", filter_values["method"], methods))
    elif page == "buses":
        filter_values = {
            "q": params.get("q", [""])[0].strip(),
            "route": params.get("route", [""])[0].strip(),
            "student": params.get("student", [""])[0].strip(),
            "status": params.get("status", [""])[0].strip(),
        }
        if filter_values["q"]:
            needle = "%" + filter_values["q"].lower() + "%"
            query = _append_query_clause(query, " AND (LOWER(b.bus_number) LIKE ? OR LOWER(b.registration_number) LIKE ?)")
            scoped_params += (needle, needle)
        if filter_values["route"]:
            query = _append_query_clause(query, " AND LOWER(b.route) LIKE ?")
            scoped_params += ("%" + filter_values["route"].lower() + "%",)
        if filter_values["status"]:
            query = _append_query_clause(query, " AND b.status=?")
            scoped_params += (filter_values["status"],)
        if filter_values["student"]:
            scope, scope_params = student_scope(user, "s", "c")
            needle = "%" + filter_values["student"].lower() + "%"
            query = _append_query_clause(
                query,
                " AND EXISTS (SELECT 1 FROM students s "
                "JOIN class_sections c ON c.section_id=s.section_id "
                "WHERE s.bus_id=b.bus_id AND " + scope +
                " AND (LOWER(s.full_name) LIKE ? OR LOWER(s.admission_number) LIKE ?))",
            )
            scoped_params += (*scope_params, needle, needle)
        route_query = "SELECT DISTINCT b.route FROM buses b WHERE 1=1"
        route_params: tuple[str, ...] = ()
        if user["role"] == "Parent":
            route_query += (
                " AND EXISTS (SELECT 1 FROM students s JOIN parent_children pc "
                "ON pc.student_id=s.student_id WHERE s.bus_id=b.bus_id "
                "AND pc.user_id=?)"
            )
            route_params = (user["user_id"],)
        routes = [row[0] for row in db.execute(
            route_query + " ORDER BY b.route", route_params
        )]
        bus_statuses = [row[0] for row in db.execute(
            "SELECT DISTINCT status FROM buses ORDER BY status"
        )]
        filter_controls = _filter_form(page,
            _filter_control("q", "Bus", filter_values["q"], placeholder="Bus no. or registration")
            + _filter_control("route", "Route", filter_values["route"], routes)
            + _filter_control("student", "Assigned student", filter_values["student"],
                              placeholder="Name or admission no.")
            + _filter_control("status", "Status", filter_values["status"], bus_statuses))
    elif page == "boards":
        filter_values = {
            "q": params.get("q", [""])[0].strip(),
            "status": params.get("status", [""])[0].strip(),
        }
        if filter_values["q"]:
            needle = "%" + filter_values["q"].lower() + "%"
            query = _append_query_clause(
                query, " AND (LOWER(b.board_id) LIKE ? OR LOWER(cl.room_number) LIKE ?)"
            )
            scoped_params += (needle, needle)
        if filter_values["status"]:
            query = _append_query_clause(query, " AND b.status=?")
            scoped_params += (filter_values["status"],)
        filter_controls = _filter_form(page,
            _filter_control("q", "Board or room", filter_values["q"],
                            placeholder="Board ID or room number")
            + _filter_control("status", "Status", filter_values["status"],
                              ["Online", "Offline", "Error", "Under Maintenance"]))
    elif page == "issues":
        filter_values = {
            "room": params.get("room", [""])[0].strip(),
            "status": params.get("status", [""])[0].strip(),
            "priority": params.get("priority", [""])[0].strip(),
        }
        if filter_values["room"]:
            query = _append_query_clause(query, " AND LOWER(cl.room_number) LIKE ?")
            scoped_params += ("%" + filter_values["room"].lower() + "%",)
        if filter_values["status"]:
            if filter_values["status"] == "Unresolved":
                query = _append_query_clause(query, " AND i.status!='Resolved'")
            else:
                query = _append_query_clause(query, " AND i.status=?")
                scoped_params += (filter_values["status"],)
        if filter_values["priority"]:
            query = _append_query_clause(query, " AND i.priority=?")
            scoped_params += (filter_values["priority"],)
        filter_controls = _filter_form(page,
            _filter_control("room", "Room", filter_values["room"], placeholder="Room number")
            + _filter_control("status", "Status", filter_values["status"],
                              ["Reported", "Acknowledged", "In Progress", "Resolved", "Unresolved"])
            + _filter_control("priority", "Priority", filter_values["priority"], ["Low", "Medium", "High"]))
    elif page == "announcements":
        filter_values = {
            "q": params.get("q", [""])[0].strip(),
            "category": params.get("category", [""])[0].strip(),
            "date": params.get("date", [""])[0].strip(),
        }
        if filter_values["q"]:
            needle = "%" + filter_values["q"].lower() + "%"
            query = _append_query_clause(query, " AND (LOWER(a.title) LIKE ? OR LOWER(a.description) LIKE ?)")
            scoped_params += (needle, needle)
        if filter_values["category"]:
            query = _append_query_clause(query, " AND a.audience=?")
            scoped_params += (filter_values["category"],)
        if filter_values["date"]:
            query = _append_query_clause(query, " AND substr(a.published_at,1,10)=?")
            scoped_params += (filter_values["date"],)
        audiences = [row[0] for row in db.execute(
            "SELECT DISTINCT audience FROM announcements ORDER BY audience"
        )]
        filter_controls = _filter_form(page,
            _filter_control("q", "Title", filter_values["q"], placeholder="Search announcement")
            + _filter_control("category", "Category", filter_values["category"], audiences)
            + _filter_control("date", "Published date", filter_values["date"], input_type="date"))
    elif page == "reports":
        filter_values = {
            "q": params.get("q", [""])[0].strip(),
            "class_name": params.get("class_name", [""])[0].strip(),
        }
        if filter_values["q"]:
            needle = "%" + filter_values["q"].lower() + "%"
            query = _append_query_clause(query, " AND (LOWER(s.full_name) LIKE ? OR LOWER(s.admission_number) LIKE ?)")
            scoped_params += (needle, needle)
        if filter_values["class_name"]:
            query = _append_query_clause(query, " AND c.class_name=?")
            scoped_params += (filter_values["class_name"],)
        classes = [row[0] for row in db.execute(
            "SELECT DISTINCT class_name FROM class_sections ORDER BY class_name"
        )]
        filter_controls = _filter_form(page,
            _filter_control("q", "Student", filter_values["q"], placeholder="Name or admission no.")
            + _filter_control("class_name", "Class", filter_values["class_name"], classes))

    if page == "announcements":
        fetched_rows = db.execute(query, (user["user_id"], *scoped_params)).fetchall()
    else:
        fetched_rows = db.execute(query, scoped_params).fetchall()
    student_records: list[tuple[str, str, str, bool]] = []
    section_roster_actions = ""
    if page == "students":
        student_records = [
            (row["student_id"], row["full_name"], row["admission_number"], bool(row["is_demo"]))
            for row in fetched_rows
        ]
        rows = [tuple(row)[1:-1] for row in fetched_rows]
    elif page == "classes":
        rows = [tuple(row)[:-1] for row in fetched_rows]
        section_roster_actions = (
            '<section class="panel"><h2>Section rosters</h2><div class="profile-actions">' +
            "".join(
                f'<a class="button" href="{html.escape(_module_href("students", class_name=row["class_name"], section_name=row["section_name"]), quote=True)}">'
                f'View {html.escape(row["class_name"])} · {html.escape(row["section_name"])} roster</a>'
                for row in fetched_rows
            ) + "</div></section>"
        ) if fetched_rows else ""
    else:
        rows = [tuple(row) for row in fetched_rows]
    table = (
        _student_table(headers, fetched_rows)
        if page == "students"
        else _staff_table(fetched_rows)
        if page == "staff"
        else _class_table(fetched_rows)
        if page == "classes"
        else _table(headers, rows)
    )
    forms = ""
    if page == "announcements":
        announcement_options = "".join(
            f'<option value="{html.escape(row["announcement_id"])}">{html.escape(row["title"])}</option>'
            for row in db.execute(
                "SELECT announcement_id,title FROM announcements "
                "ORDER BY published_at DESC"
            )
        )
        forms = f"""<section class="panel"><h2>Announcement actions</h2>
<form method="post" action="/announcements/read"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<label>Mark announcement read <select name="announcement_id" required>{announcement_options}</select></label>
<button type="submit">Mark read</button></form></section>"""
        if user["role"] in ADMIN_ROLES:
            forms += f"""<section class="panel"><h2>Create announcement</h2>
<form method="post" action="/announcements/create"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<div class="grid">
<div><label>Title</label><input name="title" type="text" maxlength="120" required></div>
<div><label>Audience</label><select name="audience" required><option>All School</option><option>Students and Staff</option><option>Students and Parents</option><option>Staff</option><option>Parents</option><option>Students</option></select></div>
<div><label>Priority</label><select name="priority" required><option>Normal</option><option>Important</option><option>Urgent</option></select></div>
</div><label>Description</label><textarea name="description" maxlength="2000" required></textarea>
<button type="submit">Publish announcement</button></form></section>"""
    elif page == "payments" and user["role"] in ADMIN_ROLES:
        fee_options = "".join(
            f'<option value="{html.escape(row["fee_id"])}">'
            f'{html.escape(row["full_name"])} · {html.escape(row["admission_number"])} '
            f'· ₹{row["pending_amount"]:,.0f} pending</option>'
            for row in db.execute(
                "SELECT f.fee_id,f.pending_amount,s.full_name,s.admission_number "
                "FROM fees f JOIN students s ON s.student_id=f.student_id "
                "WHERE f.pending_amount>0 ORDER BY s.full_name"
            )
        )
        forms = f"""<section class="panel"><h2>Record a payment</h2>
<form method="post" action="/payments/add"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<label>Student fee account <select name="fee_id" required><option value="">Select</option>{fee_options}</select></label>
<div class="grid">
<div><label>Receipt number</label><input name="receipt_number" type="text" maxlength="40" required></div>
<div><label>Payment date</label><input name="payment_date" type="date" required></div>
<div><label>Amount</label><input name="amount" type="number" min="1" required></div>
<div><label>Method</label><select name="method" required><option>Cash</option><option>Card</option><option>Bank Transfer</option><option>Cheque</option><option>Online</option></select></div>
</div><button type="submit">Record payment</button></form></section>"""
    elif page == "attendance" and user["role"] in ADMIN_ROLES | {"Class Teacher"}:
        scope, params = student_scope(user, "s", "c")
        student_options = "".join(
            f'<option value="{html.escape(row["student_id"])}">'
            f'{html.escape(row["full_name"])} · {html.escape(row["class_name"])} '
            f'{html.escape(row["section_name"])}</option>'
            for row in db.execute(
                "SELECT s.student_id,s.full_name,c.class_name,c.section_name "
                "FROM students s JOIN class_sections c ON c.section_id=s.section_id "
                "WHERE s.status='Active' AND " + scope +
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
                "WHERE " + scope +
                " ORDER BY c.class_name,c.section_name,s.full_name,e.subject LIMIT 1000",
                params,
            )
        )
        forms = f"""<section class="panel"><h2>Update examination mark</h2>
<form method="post" action="/academics/update"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<label>Student and examination <select name="exam_id" required>{exam_options}</select></label>
<label>Marks <input name="marks" type="number" min="0" required></label>
<button type="submit">Save mark</button></form></section>"""
    elif page == "boards" and user["role"] in ADMIN_ROLES | {"Technical Team"}:
        board_options = "".join(
            f'<option value="{html.escape(row["board_id"])}">'
            f'{html.escape(row["room_number"])} · {html.escape(row["board_id"])} '
            f'· {html.escape(row["status"])}</option>'
            for row in db.execute(
                "SELECT b.board_id,b.status,cl.room_number FROM smart_boards b "
                "JOIN classrooms cl ON cl.classroom_id=b.classroom_id "
                "ORDER BY cl.room_number"
            )
        )
        forms = f"""<section class="panel"><h2>Update smart-board status</h2>
<form method="post" action="/boards/update"><input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<label>Board <select name="board_id" required>{board_options}</select></label>
<label>Status <select name="status" required><option>Online</option><option>Offline</option><option>Error</option><option>Under Maintenance</option></select></label>
<button type="submit">Save board status</button></form></section>"""
    elif page == "issues" and issue_status_allowed(user):
        status_options = "".join(
            f'<option value="{status}">{status}</option>'
            for status in ("Reported", "Acknowledged", "In Progress", "Resolved")
        )
        issue_options = "".join(
            f'<option value="{html.escape(row["issue_id"])}">'
            f'{html.escape(row["issue_id"])} · {html.escape(row["category"])}</option>'
            for row in db.execute(
                "SELECT issue_id,category FROM technical_issues "
                "ORDER BY reported_at DESC"
            )
        )
        board_options = "".join(
            f'<option value="{html.escape(row["board_id"])}">'
            f'{html.escape(row["room_number"])} · {html.escape(row["board_id"])}</option>'
            for row in db.execute(
                "SELECT b.board_id,cl.room_number FROM smart_boards b "
                "JOIN classrooms cl ON cl.classroom_id=b.classroom_id "
                "ORDER BY cl.room_number"
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
            "ORDER BY m.maintenance_date DESC,m.maintenance_id"
        ).fetchall()
        maintenance = (
            '<section class="panel"><h2>Maintenance history</h2>' +
            _table(["Room", "Date", "Work performed", "Assigned technician", "Status"],
                   [tuple(row) for row in history]) + "</section>"
        )
    staff_tools = ""
    if page == "staff" and user["role"] in ADMIN_ROLES:
        action = params.get("action", [""])[0]
        staff_tools = (
            '<div class="section-head"><h2>Staff administration</h2>'
            '<a class="button" href="/?page=staff&amp;action=add">Add staff member</a></div>'
        )
        if action == "add":
            staff_tools += _staff_form(user)
        elif action in {"profile", "edit"}:
            employee_id = params.get("id", [""])[0]
            employee = db.execute(
                "SELECT * FROM employees WHERE employee_id=?", (employee_id,)
            ).fetchone()
            if not employee:
                staff_tools += '<div class="notice">Staff member not found.</div>'
            elif action == "edit":
                staff_tools += _staff_form(user, employee)
            else:
                profile_items = (
                    ("Name", employee["name"]),
                    ("Employee ID", employee["employee_id"]),
                    ("Category", "Teacher" if employee["category"] == "teacher" else "Non-teaching staff"),
                    ("Designation", employee["designation"]),
                    ("Department", employee["department"]),
                    ("Email", employee["email"]),
                    ("Phone", employee["phone"]),
                    ("Joining date", employee["joining_date"]),
                    ("Status", employee["status"]),
                )
                profile = '<div class="profile-grid">' + "".join(
                    '<div class="profile-item"><strong>' + html.escape(label) +
                    '</strong><span>' + html.escape(str(value)) + '</span></div>'
                    for label, value in profile_items
                ) + '</div>'
                staff_tools += (
                    '<section class="panel"><h2>Staff profile</h2>' + profile +
                    f'<p><a class="button" href="/?page=staff&amp;action=edit&amp;'
                    f'{html.escape(urlencode({"id": employee_id}))}">Edit profile</a></p>'
                    '<form method="post" action="/staff/status">'
                    f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
                    f'<input type="hidden" name="employee_id" value="{html.escape(employee_id)}">'
                    '<label>Staff status <select name="status">'
                    + "".join(
                        f'<option value="{status}"'
                        f'{" selected" if employee["status"] == status else ""}>{status}</option>'
                        for status in ("Active", "Inactive")
                    )
                    + '</select></label> <button type="submit">Update status</button>'
                    '</form></section>'
                )
                if employee["category"] == "teacher":
                    assignments = db.execute(
                        "SELECT ta.assignment_id,ta.subject,c.class_name,c.section_name "
                        "FROM teacher_assignments ta JOIN class_sections c "
                        "ON c.section_id=ta.section_id WHERE ta.employee_id=? "
                        "ORDER BY c.class_name,c.section_name,ta.subject",
                        (employee_id,),
                    ).fetchall()
                    assignment_rows = []
                    for assignment in assignments:
                        remove_form = (
                            '<form method="post" action="/staff/assignments/remove">'
                            f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
                            f'<input type="hidden" name="employee_id" value="{html.escape(employee_id)}">'
                            f'<input type="hidden" name="assignment_id" value="{html.escape(assignment["assignment_id"])}">'
                            '<button class="danger" type="submit">Remove</button></form>'
                        )
                        assignment_rows.append((
                            assignment["class_name"], assignment["section_name"],
                            assignment["subject"], remove_form,
                        ))
                    assignment_table = (
                        '<div class="table-wrap"><table><thead><tr><th>Class</th><th>Section</th>'
                        '<th>Subject</th><th>Action</th></tr></thead><tbody>' +
                        "".join(
                            "<tr>" + "".join(
                                f"<td>{value if index == 3 else html.escape(str(value))}</td>"
                                for index, value in enumerate(row)
                            ) + "</tr>"
                            for row in assignment_rows
                        ) + '</tbody></table></div>'
                        if assignments else '<div class="empty">No teaching assignments.</div>'
                    )
                    sections = db.execute(
                        "SELECT section_id,class_name,section_name,academic_session "
                        "FROM class_sections ORDER BY class_name,section_name,academic_session"
                    ).fetchall()
                    section_options = "".join(
                        f'<option value="{html.escape(row["section_id"])}">'
                        f'{html.escape(row["class_name"])} · {html.escape(row["section_name"])} '
                        f'({html.escape(row["academic_session"])})</option>'
                        for row in sections
                    )
                    staff_tools += (
                        '<section class="panel"><h2>Teacher assignments</h2>'
                        + assignment_table +
                        '<form method="post" action="/staff/assignments/add">'
                        f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
                        f'<input type="hidden" name="employee_id" value="{html.escape(employee_id)}">'
                        '<div class="grid"><div><label>Class and section</label>'
                        f'<select name="section_id" required><option value="">Select</option>{section_options}</select></div>'
                        '<div><label>Subject</label><input name="subject" required maxlength="80"></div>'
                        '</div><p><button type="submit">Assign teacher</button></p></form></section>'
                    )
    class_tools = ""
    if page == "classes" and user["role"] in ADMIN_ROLES:
        action = params.get("action", [""])[0]
        class_tools = (
            '<div class="section-head"><h2>Class and section administration</h2>'
            '<a class="button" href="/?page=classes&amp;action=add">Create class section</a></div>'
        )
        if action == "add":
            teachers = db.execute(
                "SELECT employee_id,name,status FROM employees "
                "WHERE category='teacher' AND status='Active' ORDER BY name"
            ).fetchall()
            class_tools += _class_section_form(user, teachers)
        elif action == "edit":
            section_id = params.get("id", [""])[0]
            section = db.execute(
                "SELECT section_id,class_name,section_name,academic_session,homeroom_teacher_id "
                "FROM class_sections WHERE section_id=?", (section_id,)
            ).fetchone()
            if not section:
                class_tools += '<div class="notice">Class section not found.</div>'
            else:
                teachers = db.execute(
                    "SELECT employee_id,name,status FROM employees "
                    "WHERE category='teacher' AND (status='Active' OR employee_id=?) "
                    "ORDER BY name",
                    (section["homeroom_teacher_id"],),
                ).fetchall()
                class_tools += _class_section_form(user, teachers, section)
    student_tools = ""
    if page == "students" and user["role"] in ADMIN_ROLES:
        action = params.get("action", [""])[0]
        sections = db.execute(
            "SELECT section_id,class_name,section_name,academic_session "
            "FROM class_sections ORDER BY class_name,section_name,academic_session"
        ).fetchall()
        buses = db.execute(
            "SELECT bus_id,bus_number,route FROM buses ORDER BY bus_number"
        ).fetchall()
        student_tools = (
            '<div class="section-head"><h2>Student administration</h2>'
            '<a class="button" href="/?page=students&action=add">Add student</a></div>'
        )
        if action == "add":
            student_tools += _student_form(user, sections, buses)
        elif action == "edit":
            student_id = params.get("id", [""])[0]
            student = db.execute(
                "SELECT s.*,COALESCE(g.father_name,'') AS father_name,"
                "COALESCE(g.mother_name,'') AS mother_name,"
                "COALESCE(g.emergency_contact,'') AS emergency_contact "
                "FROM students s LEFT JOIN student_guardian_details g "
                "ON g.student_id=s.student_id WHERE s.student_id=?",
                (student_id,),
            ).fetchone()
            if student:
                student_tools += _student_form(user, sections, buses, student)
            else:
                student_tools += '<div class="notice" style="border-color:#e4c9c5;background:#fff3f1;color:#7f2f2f">Student not found.</div>'
    if page == "students":
        action_links = "".join(
            f'<div><strong>{html.escape(name)}</strong> '
            f'<span>{html.escape(admission_number)}</span> '
            f'<a href="/?page=student&amp;{html.escape(urlencode({"id": student_id}))}">View</a>'
            + (
                f' <a href="/?page=students&amp;action=edit&amp;{html.escape(urlencode({"id": student_id}))}">Edit</a>'
                if user["role"] in ADMIN_ROLES else ""
            )
            + "</div>"
            for student_id, name, admission_number, _ in student_records
        )
        if user["role"] not in ADMIN_ROLES:
            student_tools = (
                '<section class="panel"><h2>Student profiles</h2>'
                f'<div style="max-height:220px;overflow:auto">{action_links}</div></section>'
            )
        elif not params.get("action", [""])[0] and student_records:
            student_options = "".join(
                f'<option value="{html.escape(student_id)}">'
                f'{html.escape(name)} - {html.escape(admission_number)}</option>'
                for student_id, name, admission_number, _ in student_records
            )
            student_tools += (
                '<section class="panel"><h2>Student actions</h2>'
                f'<div style="max-height:220px;overflow:auto;margin-bottom:16px">{action_links}</div>'
                '<form method="post" action="/students/deactivate">'
                f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
                '<label>Student to deactivate</label>'
                f'<select name="student_id" required>{student_options}</select>'
                '<label><input type="checkbox" name="confirm" value="DEACTIVATE" required> '
                'I confirm that this student should be deactivated.</label>'
                '<button class="danger" type="submit">Deactivate student</button>'
                '</form></section>'
            )
    export_link = ""
    if page == "reports":
        export_link = '<p><a class="button" href="/export/reports">Export report cards CSV</a></p>'
    elif page == "payments":
        export_link = '<p><a class="button" href="/export/payments">Export payments CSV</a></p>'
    return (
        f'<main><h1>{heading}</h1><p class="sub">{description} · {len(rows):,} displayed records.</p>'
        f'{filter_controls}{export_link}{staff_tools}{class_tools}{student_tools}{table}'
        f'{section_roster_actions}{forms}{maintenance}</main>'
    )


def student_profile(db: sqlite3.Connection, student_id: str, user: sqlite3.Row) -> str:
    if not can_access_module(user, "students"):
        raise PermissionError
    scope, params = student_scope(user, "s", "c")
    row = db.execute(
        "SELECT s.student_id,s.full_name,s.admission_number,s.date_of_birth,s.gender,"
        "s.roll_number,s.academic_session,s.status,s.guardian_name,s.guardian_phone,"
        "s.guardian_email,s.address,s.admission_date,"
        "COALESCE(g.father_name,'') AS father_name,"
        "COALESCE(g.mother_name,'') AS mother_name,"
        "COALESCE(g.emergency_contact,'') AS emergency_contact,"
        "s.bus_id,c.class_name,c.section_name,"
        "b.bus_number,b.route,s.is_demo "
        "FROM students s JOIN class_sections c ON c.section_id=s.section_id "
        "LEFT JOIN buses b ON b.bus_id=s.bus_id "
        "LEFT JOIN student_guardian_details g ON g.student_id=s.student_id "
        "WHERE s.student_id=? AND " + scope,
        (student_id, *params),
    ).fetchone()
    if not row:
        if user["role"] not in ADMIN_ROLES:
            raise PermissionError
        raise LookupError
    attendance = db.execute(
        "SELECT attendance_date,status,is_demo FROM attendance "
        "WHERE student_id=? ORDER BY attendance_date DESC LIMIT 30",
        (student_id,),
    ).fetchall()
    attendance_counts = db.execute(
        "SELECT status,COUNT(*) AS count FROM attendance "
        "WHERE student_id=? GROUP BY status",
        (student_id,),
    ).fetchall()
    attendance_summary = {item["status"]: item["count"] for item in attendance_counts}
    present = attendance_summary.get("Present", 0)
    late = attendance_summary.get("Late", 0)
    absent = attendance_summary.get("Absent", 0)
    leave = attendance_summary.get("Leave", 0)
    recorded = present + late + absent + leave
    attendance_rate = (present + late) * 100 / recorded if recorded else 0

    academic_scope, academic_params = student_scope(user, "s", "c", "e.subject")
    academics = db.execute(
        "SELECT e.examination_name,e.subject,e.marks_obtained,e.maximum_marks,"
        "e.grade,e.academic_session,e.is_demo "
        "FROM examinations e JOIN students s ON s.student_id=e.student_id "
        "JOIN class_sections c ON c.section_id=e.section_id "
        "WHERE e.student_id=? AND " + academic_scope +
        " ORDER BY e.examination_name,e.subject",
        (student_id, *academic_params),
    ).fetchall()
    fees = db.execute(
        "SELECT fee_id,academic_session,fee_category,annual_total,"
        "paid_amount,pending_amount,due_date,payment_status,is_demo "
        "FROM fees WHERE student_id=? ORDER BY academic_session DESC",
        (student_id,),
    ).fetchall()
    payments = db.execute(
        "SELECT p.receipt_number,p.payment_date,p.amount,p.method,"
        "COALESCE(r.remarks,''),f.academic_session,p.is_demo FROM fee_payments p "
        "JOIN fees f ON f.fee_id=p.fee_id "
        "LEFT JOIN fee_payment_remarks r ON r.payment_id=p.payment_id "
        "WHERE f.student_id=? "
        "ORDER BY p.payment_date DESC,p.receipt_number",
        (student_id,),
    ).fetchall()
    adjustments = db.execute(
        "SELECT a.adjustment_date,a.amount,a.reason,a.recorded_by,a.is_demo "
        "FROM fee_adjustments a JOIN fees f ON f.fee_id=a.fee_id "
        "WHERE f.student_id=? ORDER BY a.adjustment_date DESC",
        (student_id,),
    ).fetchall()
    buses = db.execute(
        "SELECT bus_id,bus_number,route FROM buses ORDER BY bus_number"
    ).fetchall() if user["role"] in ADMIN_ROLES else []

    def profile_items(items: list[tuple[str, object]]) -> str:
        return '<div class="profile-grid">' + "".join(
            '<div class="profile-item"><strong>' + html.escape(label) + '</strong><span>' +
            html.escape("" if value is None else str(value)) + '</span></div>'
            for label, value in items
        ) + '</div>'

    basic = profile_items([
        ("Full name", row["full_name"]),
        ("Student ID", row["student_id"]),
        ("Admission number", row["admission_number"]),
        ("Date of birth", row["date_of_birth"]),
        ("Gender", row["gender"]),
        ("Class", row["class_name"]),
        ("Section", row["section_name"]),
        ("Roll number", row["roll_number"]),
        ("Admission date", row["admission_date"]),
        ("Student status", row["status"]),
    ])
    guardian_visible = user["role"] in ADMIN_ROLES | {"Parent"}
    guardian = profile_items([
        ("Father's name", row["father_name"]),
        ("Mother's name", row["mother_name"]),
        ("Guardian's name", row["guardian_name"]),
        ("Guardian phone", row["guardian_phone"]),
        ("Emergency contact", row["emergency_contact"]),
        ("Guardian email", row["guardian_email"]),
        ("Address", row["address"]),
    ]) if guardian_visible else '<div class="empty">Guardian details are not available to this account.</div>'

    total_applicable = sum(fee["annual_total"] for fee in fees)
    total_paid = sum(fee["paid_amount"] for fee in fees)
    total_outstanding = sum(fee["pending_amount"] for fee in fees)
    fee_status = "Paid" if fees and total_outstanding == 0 else "Outstanding" if fees else "No fee record"

    profile_summary = (
        '<div class="profile-summary">'
        '<section class="card"><div class="label">Attendance rate</div>'
        f'<div class="value">{attendance_rate:.1f}%</div>'
        f'<div class="hint">{recorded} days recorded</div></section>'
        '<section class="card"><div class="label">Applicable fees</div>'
        f'<div class="value">{html.escape(money(total_applicable))}</div>'
        '<div class="hint">Across displayed sessions</div></section>'
        '<section class="card"><div class="label">Outstanding</div>'
        f'<div class="value">{html.escape(money(total_outstanding))}</div>'
        f'<div class="hint">{html.escape(fee_status)}</div></section>'
        '</div>'
    )

    admin_actions = ""
    if user["role"] in ADMIN_ROLES:
        admin_actions = (
            '<div class="profile-actions">'
            '<a class="button" href="/?page=students&action=edit&id=' +
            html.escape(student_id) +
            '">Edit profile</a>'
            '<button type="button" onclick="window.print()">Print profile</button>'
            '<a class="button" href="/?page=students" style="background:#fff;color:#174b3a">Back to student list</a>'
            '</div>'
        )
    else:
        admin_actions = (
            '<div class="profile-actions">'
            '<a class="button" href="/?page=students" style="background:#fff;color:#174b3a">Back to student list</a>'
            '<button type="button" onclick="window.print()">Print profile</button>'
            '</div>'
        )

    panels = ""
    panels += '<section class="panel"><h2>Basic information</h2>' + basic + '</section>'
    panels += '<section class="panel"><h2>Parent / guardian information</h2>' + guardian + '</section>'

    if user["role"] in ADMIN_ROLES | {"Class Teacher", "Subject Teacher", "Parent"}:
        panels += (
            '<section class="panel"><h2>Academic information</h2>'
            f'<p><strong>Class:</strong> {html.escape(row["class_name"])} &nbsp; '
            f'<strong>Section:</strong> {html.escape(row["section_name"])} &nbsp; '
            f'<strong>Roll number:</strong> {html.escape(str(row["roll_number"]))} &nbsp; '
            f'<strong>Academic session:</strong> {html.escape(row["academic_session"])}</p>'
            f'<p><strong>Attendance summary:</strong> '
            f'{present} present, {late} late, {absent} absent, {leave} leave '
            f'({attendance_rate:.1f}% present/late rate).</p>'
            '<h3>Attendance history</h3>' +
            _table(
                ["Date", "Status"], [tuple(row)[:2] for row in attendance],
                [bool(row["is_demo"]) for row in attendance],
            ) +
            '<h3 style="margin-top:18px">Previous academic information</h3>' +
            _table(
                ["Examination", "Subject", "Marks", "Out of", "Grade", "Session"],
                [tuple(record)[:6] for record in academics],
                [bool(record["is_demo"]) for record in academics],
            ) + '</section>'
        )

    if user["role"] in ADMIN_ROLES | {"Parent"}:
        panels += (
            '<section class="panel"><h2>Fee information</h2>' +
            _table(
                ["Session", "Fee category", "Annual total", "Paid", "Pending", "Due date", "Status"],
                [
                    (
                        fee["academic_session"], fee["fee_category"], fee["annual_total"],
                        fee["paid_amount"], fee["pending_amount"], fee["due_date"],
                        fee["payment_status"],
                    )
                    for fee in fees
                ],
                [bool(fee["is_demo"]) for fee in fees],
            ) +
            f'<p><strong>Total applicable:</strong> {html.escape(money(total_applicable))} &nbsp; '
            f'<strong>Total paid:</strong> {html.escape(money(total_paid))} &nbsp; '
            f'<strong>Total outstanding:</strong> {html.escape(money(total_outstanding))}</p>'
            '</section>'
        )
        panels += (
            '<section class="panel"><h2>Payment history</h2>' +
            _table(
                ["Receipt", "Date", "Amount", "Method", "Remarks", "Academic session"],
                [tuple(payment)[:6] for payment in payments],
                [bool(payment["is_demo"]) for payment in payments],
            ) + '</section>'
        )
        panels += (
            '<section class="panel"><h2>Fee adjustments</h2>' +
            _table(
                ["Date", "Amount", "Reason", "Recorded by"],
                [tuple(adjustment)[:4] for adjustment in adjustments],
                [bool(adjustment["is_demo"]) for adjustment in adjustments],
            ) + '</section>'
        )

    if user["role"] in ADMIN_ROLES | {"Parent"}:
        panels += (
            '<section class="panel"><h2>Transport information</h2>'
            f'<p><strong>Assigned bus:</strong> {html.escape(row["bus_number"] or "Not assigned")}</p>'
            f'<p><strong>Route:</strong> {html.escape(row["route"] or "Not assigned")}</p>'
        )
        if user["role"] in ADMIN_ROLES:
            bus_options = "".join(
                f'<option value="{html.escape(bus["bus_id"])}"'
                f'{" selected" if bus["bus_id"] == row["bus_id"] else ""}>'
                f'{html.escape(bus["bus_number"])} - {html.escape(bus["route"])}</option>'
                for bus in buses
            )
            panels += (
                '<form method="post" action="/students/transport">'
                f'<input type="hidden" name="csrf" value="{html.escape(user["csrf_token"])}">'
                f'<input type="hidden" name="student_id" value="{html.escape(student_id)}">'
                '<label>Bus and route<select name="bus_id"><option value="">Not assigned</option>'
                f'{bus_options}</select></label>'
                '<button type="submit">Save transport assignment</button> '
                f'<a href="/?page=student&amp;{html.escape(urlencode({"id": student_id}))}">Cancel</a>'
                '</form>'
            )
        panels += '</section>'
    if user["role"] in ADMIN_ROLES:
        for fee in fees:
            panels += _fee_form(user, student_id, fee)
            panels += _student_financial_forms(user, student_id, fee)
        if not fees:
            panels += _fee_form(user, student_id)
    return (
        f'<main><h1>{html.escape(row["full_name"])}</h1>'
        f'<p class="sub">Student profile · {html.escape(row["admission_number"])}</p>'
        f'{admin_actions}{profile_summary}{panels}</main>'
    )


class SchoolHandler(BaseHTTPRequestHandler):
    database: Path
    server_version = "SchoolCommandDemo/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(f"[school-command] {self.address_string()} {fmt % args}\n")

    def send_csv(self, filename: str, headers: list[str], rows: list[tuple]) -> None:
        buffer = StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(headers)
        writer.writerows(rows)
        payload = buffer.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

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

    def send_static(self, url_path: str) -> None:
        relative_path = Path(unquote(url_path.removeprefix("/static/")))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            self.send_error(404)
            return

        static_root = (ROOT / "static").resolve()
        try:
            file_path = (static_root / relative_path).resolve(strict=True)
            file_path.relative_to(static_root)
            if not file_path.is_file():
                self.send_error(404)
                return
            payload = file_path.read_bytes()
        except (OSError, ValueError):
            self.send_error(404)
            return

        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
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
        if path.path.startswith("/static/"):
            self.send_static(path.path)
            return
        if path.path in ("/export/reports", "/export/payments"):
            user = self.user()
            if not user:
                self.redirect("/login")
                return
            if path.path == "/export/reports":
                if not can_access_module(user, "reports"):
                    self.send_html(
                        _page_start("Not authorized", "", user) +
                        "<main><h1>Not authorized</h1></main>" + _page_end(), 403,
                    )
                    return
                with database_connection(self.database) as db:
                    rows = db.execute(
                        "SELECT s.full_name,s.admission_number,c.class_name,c.section_name,"
                        "COUNT(e.exam_id),ROUND(AVG(100.0*e.marks_obtained/e.maximum_marks),1),"
                        "CASE WHEN AVG(100.0*e.marks_obtained/e.maximum_marks)>=90 THEN 'A+' "
                        "WHEN AVG(100.0*e.marks_obtained/e.maximum_marks)>=80 THEN 'A' "
                        "WHEN AVG(100.0*e.marks_obtained/e.maximum_marks)>=70 THEN 'B' "
                        "WHEN AVG(100.0*e.marks_obtained/e.maximum_marks)>=60 THEN 'C' ELSE 'D' END "
                        "FROM students s JOIN class_sections c ON c.section_id=s.section_id "
                        "JOIN examinations e ON e.student_id=s.student_id "
                        "WHERE 1=1 GROUP BY s.student_id "
                        "ORDER BY c.class_name,c.section_name,s.full_name"
                    ).fetchall()
                self.send_csv(
                    "student-report-cards.csv",
                    ["Student", "Admission no.", "Class", "Section", "Subject records", "Average score", "Overall grade"],
                    [tuple(row) for row in rows],
                )
                return
            if not can_access_module(user, "payments"):
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1></main>" + _page_end(), 403,
                )
                return
            with database_connection(self.database) as db:
                if user["role"] == "Parent":
                    scope, params = student_scope(user, "s", "c")
                    rows = db.execute(
                        "SELECT p.receipt_number,s.full_name,s.admission_number,p.payment_date,"
                        "p.amount,p.method,f.academic_session "
                        "FROM fee_payments p JOIN fees f ON f.fee_id=p.fee_id "
                        "JOIN students s ON s.student_id=f.student_id "
                        "JOIN class_sections c ON c.section_id=s.section_id "
                        "WHERE " + scope +
                        " ORDER BY p.payment_date DESC,p.receipt_number",
                        params,
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT p.receipt_number,s.full_name,s.admission_number,p.payment_date,"
                        "p.amount,p.method,f.academic_session "
                        "FROM fee_payments p JOIN fees f ON f.fee_id=p.fee_id "
                        "JOIN students s ON s.student_id=f.student_id "
                        "WHERE 1=1 ORDER BY p.payment_date DESC,p.receipt_number"
                    ).fetchall()
            self.send_csv(
                "fee-payments.csv",
                ["Receipt", "Student", "Admission no.", "Payment date", "Amount", "Method", "Session"],
                [tuple(row) for row in rows],
            )
            return
        if path.path in ("/students/add", "/students/edit", "/students/deactivate"):
            user = self.user()
            if not user:
                self.redirect("/login")
                return
            params = parse_qs(path.query)
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1></main>" + _page_end(), 403,
                )
                return
            if path.path == "/students/add":
                self.redirect("/?page=students&action=add")
            elif path.path == "/students/edit":
                student_id = params.get("id", [""])[0]
                self.redirect("/?page=students&action=edit&" + urlencode({"id": student_id}))
            else:
                self.redirect("/?page=students")
            return
        if path.path == "/credentials":
            user = self.user()
            if not user:
                self.redirect("/login")
                return
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1><p>Only the Director or Principal can manage account credentials.</p></main>" +
                    _page_end(), 403,
                )
                return
            with database_connection(self.database) as db:
                accounts_by_id = {
                    row["user_id"]: row for row in db.execute(
                        "SELECT user_id,role,email FROM demo_users WHERE is_demo=1"
                    )
                }
            if (
                set(accounts_by_id) != set(DEMO_ACCOUNT_IDS)
                or any(
                    accounts_by_id[user_id]["role"] != role
                    for user_id, role in DEMO_ACCOUNT_IDS.items()
                )
            ):
                self.send_html(
                    _page_start("Account setup unavailable", "", user) +
                    "<main><h1>Account setup unavailable</h1><p>The six demo accounts could not be verified.</p></main>" +
                    _page_end(), 500,
                )
                return
            accounts = [accounts_by_id[user_id] for user_id in DEMO_ACCOUNT_IDS]
            message = (
                "Credentials saved. Your current session is still active."
                if parse_qs(path.query).get("saved") == ["1"] else ""
            )
            self.send_html(
                _page_start("Account credentials", "", user) +
                _credential_setup_content(user, accounts, message) + _page_end()
            )
            return
        if path.path == "/login":
            user = self.user()
            if user:
                self.redirect("/")
                return
            self.send_html(_page_start("Sign in", "login") + _login_content() + _page_end())
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
        requested_page = params.get("page", ["dashboard"])[0]
        page = PAGE_ALIASES.get(requested_page, requested_page)
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
                    content = module_page(db, page, user, params)
            if page != "dashboard":
                content = content.replace(
                    "<main>",
                    '<main><p class="back-link"><a href="/">&#8592; Back to dashboard</a></p>',
                    1,
                )
            reset_notice = '<div class="notice">Demo records have been safely reset and reseeded. Demo logins were retained.</div>' if params.get("reset") == ["1"] else ""
            if params.get("staff_saved") == ["1"]:
                reset_notice += '<div class="notice">Staff details saved successfully.</div>'
            elif params.get("staff_status") == ["1"]:
                reset_notice += '<div class="notice">Staff status updated successfully.</div>'
            elif params.get("assignment_saved") == ["1"]:
                reset_notice += '<div class="notice">Teacher assignment updated successfully.</div>'
            elif params.get("class_saved") == ["1"]:
                reset_notice += '<div class="notice">Class section saved successfully.</div>'
            elif params.get("added") == ["1"]:
                reset_notice += '<div class="notice">Student added successfully.</div>'
            elif params.get("edited") == ["1"]:
                reset_notice += '<div class="notice">Student information updated successfully.</div>'
            elif params.get("fee_updated") == ["1"]:
                reset_notice += '<div class="notice">Fee information saved. Payment history was preserved and the balance was recalculated.</div>'
            elif params.get("transport_updated") == ["1"]:
                reset_notice += '<div class="notice">Transport assignment updated successfully.</div>'
            elif params.get("deactivated") == ["1"]:
                reset_notice += '<div class="notice">Student deactivated safely. Historical records were preserved.</div>'
            elif params.get("payment_added") == ["1"]:
                reset_notice += '<div class="notice">Payment recorded successfully. Fee balances were updated.</div>'
            elif params.get("fee_adjusted") == ["1"]:
                reset_notice += '<div class="notice">Fee adjustment recorded separately. Payment history was preserved.</div>'
            elif params.get("board_updated") == ["1"]:
                reset_notice += '<div class="notice">Smart-board status updated successfully.</div>'
            elif params.get("created") == ["1"]:
                reset_notice += '<div class="notice">Announcement published successfully.</div>'
            elif params.get("read") == ["1"]:
                reset_notice += '<div class="notice">Announcement marked as read.</div>'
            reset_panel = ""
            if user["role"] in ADMIN_ROLES:
                reset_panel = f"""<section class="panel danger-panel" style="margin-top:22px">
<h2>Reset demo data</h2><p>Rebuild fictional demo records only. Actual school records are not selected or deleted.
Type <strong>RESET DEMO DATA</strong> to confirm. Your demo login will be retained.</p>
<form method="post" action="/reset-demo">
<input type="hidden" name="csrf" value="{html.escape(user['csrf_token'])}">
<input type="text" name="confirmation" required autocomplete="off" aria-label="Reset confirmation">
<button class="danger" type="submit">Reset demo records</button></form></section>
<section class="panel" style="margin-top:22px"><h2>Account credentials</h2>
<p>Change usernames and passwords for the six demo roles.</p>
<a class="button" href="/credentials">Configure account credentials</a></section>"""
            if page == "dashboard":
                content = content.replace("</main>", reset_notice + reset_panel + "</main>")
            elif page in ("students", "student", "staff", "classes") and reset_notice:
                content = content.replace("<main>", "<main>" + reset_notice, 1)
            page_title = (
                "Dashboard" if page == "dashboard" else
                "Examinations" if requested_page == "examinations" else
                "Student profile" if page == "student" else dict(NAV_ITEMS)[page]
            )
            active_module = "students" if page == "student" else page
            body = _page_start(page_title, active_module, user)
            body += '<div class="app-shell">' + _navigation(user, active_module)
            body += '<div class="workspace">' + content + "</div></div>"
            self.send_html(body + _page_end())
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
            username = form.get("username", form.get("email", [""]))[0].strip().lower()
            password = form.get("password", [""])[0]
            with database_connection(self.database) as db:
                user = db.execute(
                    "SELECT * FROM demo_users WHERE lower(email)=? AND is_demo=1",
                    (username,),
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
            self.send_html(
                _page_start("Sign in", "login") + _login_content(show_error=True) +
                _page_end(), 401,
            )
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
        if self.path == "/credentials/update":
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1><p>Only the Director or Principal can manage account credentials.</p></main>" +
                    _page_end(), 403,
                )
                return
            with database_connection(self.database) as db:
                accounts_by_id = {
                    row["user_id"]: row for row in db.execute(
                        "SELECT user_id,role,email FROM demo_users WHERE is_demo=1"
                    )
                }
                if (
                    set(accounts_by_id) != set(DEMO_ACCOUNT_IDS)
                    or any(
                        accounts_by_id[user_id]["role"] != role
                        for user_id, role in DEMO_ACCOUNT_IDS.items()
                    )
                ):
                    self.send_html(
                        _page_start("Account setup unavailable", "", user) +
                        "<main><h1>Account setup unavailable</h1><p>The six demo accounts could not be verified.</p></main>" +
                        _page_end(), 500,
                    )
                    return

                usernames: dict[str, str] = {}
                passwords: dict[str, str | None] = {}
                error = ""
                seen_usernames: set[str] = set()
                for user_id in DEMO_ACCOUNT_IDS:
                    username = form.get(f"username_{user_id}", [""])[0].strip()
                    password = form.get(f"password_{user_id}", [""])[0]
                    usernames[user_id] = username
                    if not re.fullmatch(r"[A-Za-z0-9._@+-]{3,64}", username):
                        error = (
                            "Each username must be 3–64 characters and use only "
                            "letters, numbers, dots, underscores, @, +, or hyphens."
                        )
                        break
                    if username.casefold() in seen_usernames:
                        error = "Each account must have a different username."
                        break
                    seen_usernames.add(username.casefold())
                    if password and not 12 <= len(password) <= 128:
                        error = "New passwords must be between 12 and 128 characters."
                        break
                    passwords[user_id] = password or None

                if error:
                    accounts = [accounts_by_id[user_id] for user_id in DEMO_ACCOUNT_IDS]
                    self.send_html(
                        _page_start("Check account details", "", user) +
                        _credential_setup_content(user, accounts, error, usernames) +
                        _page_end(), 400,
                    )
                    return

                changed_ids = {
                    user_id for user_id in DEMO_ACCOUNT_IDS
                    if usernames[user_id] != accounts_by_id[user_id]["email"]
                    or passwords[user_id] is not None
                }
                if changed_ids:
                    # Move names aside first so accounts can safely swap usernames.
                    for user_id in DEMO_ACCOUNT_IDS:
                        db.execute(
                            "UPDATE demo_users SET email=? WHERE user_id=? AND is_demo=1",
                            (f"setup-{secrets.token_hex(16)}-{user_id}", user_id),
                        )
                    for user_id in DEMO_ACCOUNT_IDS:
                        password = passwords[user_id]
                        if password is None:
                            db.execute(
                                "UPDATE demo_users SET email=? WHERE user_id=? AND is_demo=1",
                                (usernames[user_id], user_id),
                            )
                        else:
                            salt = secrets.token_bytes(16)
                            digest = hashlib.pbkdf2_hmac(
                                "sha256", password.encode("utf-8"), salt,
                                PASSWORD_ITERATIONS,
                            )
                            db.execute(
                                "UPDATE demo_users SET email=?,password_salt=?,"
                                "password_hash=?,password_iterations=? "
                                "WHERE user_id=? AND is_demo=1",
                                (usernames[user_id], salt.hex(), digest.hex(),
                                 PASSWORD_ITERATIONS, user_id),
                            )
                    cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                    session = cookie.get("school_session")
                    current_session_hash = (
                        hashlib.sha256(session.value.encode()).hexdigest()
                        if session else ""
                    )
                    for user_id in changed_ids:
                        db.execute(
                            "DELETE FROM app_sessions WHERE user_id=? AND session_hash<>?",
                            (user_id, current_session_hash),
                        )
            self.redirect("/credentials?saved=1")
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
        if self.path == "/attendance/mark":
            if user["role"] not in ADMIN_ROLES | {"Class Teacher"}:
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            section_id = form.get("section_id", [""])[0].strip()
            attendance_date = form.get("attendance_date", [""])[0].strip()
            try:
                parsed_date = date.fromisoformat(attendance_date)
                if (
                    not re.fullmatch(r"\d{4}-\d{2}-\d{2}", attendance_date)
                    or parsed_date.isoformat() != attendance_date
                ):
                    raise ValueError
            except ValueError:
                self.send_html("<main><h1>Invalid attendance date</h1></main>", 400)
                return
            if not section_id:
                self.send_html("<main><h1>Select a class section</h1></main>", 400)
                return
            with database_connection(self.database) as db:
                section_query = (
                    "SELECT section_id,class_name,section_name FROM class_sections "
                    "WHERE section_id=?"
                )
                section_params: tuple[str, ...] = (section_id,)
                if user["role"] == "Class Teacher":
                    if not user["employee_id"]:
                        self.send_html("<main><h1>Not authorized</h1></main>", 403)
                        return
                    section_query += " AND homeroom_teacher_id=?"
                    section_params += (user["employee_id"],)
                section = db.execute(section_query, section_params).fetchone()
                if not section:
                    self.send_html(
                        "<main><h1>Not authorized</h1>"
                        "<p>Attendance can only be managed for an assigned class section.</p></main>",
                        403,
                    )
                    return
                roster = db.execute(
                    "SELECT student_id FROM students "
                    "WHERE section_id=? AND status='Active' ORDER BY roll_number",
                    (section_id,),
                ).fetchall()
                if not roster:
                    self.send_html(
                        "<main><h1>No active students</h1>"
                        "<p>There are no students to mark in this section.</p></main>",
                        400,
                    )
                    return
                statuses = {
                    row["student_id"]: form.get(
                        "status_" + row["student_id"], [""]
                    )[0]
                    for row in roster
                }
                existing_statuses = {
                    row["student_id"]: row["status"]
                    for row in db.execute(
                        "SELECT student_id,status FROM attendance "
                        "WHERE attendance_date=? AND student_id IN (" +
                        ",".join("?" for _ in roster) + ")",
                        (attendance_date, *(row["student_id"] for row in roster)),
                    )
                }
                if any(
                    status not in {"Present", "Absent", "Late"}
                    and not (status == "Leave" and existing_statuses.get(student_id) == "Leave")
                    for student_id, status in statuses.items()
                ):
                    self.send_html(
                        "<main><h1>Invalid attendance status</h1>"
                        "<p>Choose Present, Absent, or Late for every active student.</p></main>",
                        400,
                    )
                    return
                for student_id, status in statuses.items():
                    db.execute(
                        "INSERT INTO attendance "
                        "(attendance_id,student_id,attendance_date,status,is_demo) "
                        "VALUES (?,?,?,?,0) ON CONFLICT(student_id,attendance_date) "
                        "DO UPDATE SET status=excluded.status",
                        (
                            "attendance-" + secrets.token_hex(12), student_id,
                            attendance_date, status,
                        ),
                    )
            self.redirect(
                "/?page=attendance&" +
                urlencode({
                    "attendance_date": attendance_date,
                    "class_name": section["class_name"],
                    "section_id": section_id,
                    "saved": "1",
                })
            )
            return
        if self.path == "/attendance/update":
            if user["role"] not in ADMIN_ROLES | {"Class Teacher"}:
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            student_id = form.get("student_id", [""])[0]
            attendance_date = form.get("attendance_date", [""])[0]
            status = form.get("status", [""])[0]
            try:
                parsed_date = date.fromisoformat(attendance_date)
                if (
                    not re.fullmatch(r"\d{4}-\d{2}-\d{2}", attendance_date)
                    or parsed_date.isoformat() != attendance_date
                ):
                    raise ValueError
            except ValueError:
                self.send_html("<main><h1>Invalid attendance date</h1></main>", 400)
                return
            if status not in {"Present", "Absent", "Late"}:
                self.send_html("<main><h1>Invalid attendance status</h1></main>", 400)
                return
            scope, params = student_scope(user, "s", "c")
            with database_connection(self.database) as db:
                accessible = db.execute(
                    "SELECT s.student_id,s.status FROM students s "
                    "JOIN class_sections c ON c.section_id=s.section_id "
                    "WHERE s.student_id=? AND " + scope,
                    (student_id, *params),
                ).fetchone()
                if not accessible:
                    self.send_html("<main><h1>Not authorized</h1><p>No accessible attendance record was changed.</p></main>", 403)
                    return
                existing = db.execute(
                    "SELECT attendance_id,is_demo FROM attendance "
                    "WHERE student_id=? AND attendance_date=?",
                    (student_id, attendance_date),
                ).fetchone()
                if not existing and accessible["status"] != "Active":
                    self.send_html(
                        "<main><h1>Inactive student</h1>"
                        "<p>New attendance cannot be recorded for an inactive student.</p></main>",
                        403,
                    )
                    return
                if existing:
                    db.execute(
                        "UPDATE attendance SET status=? WHERE student_id=? AND attendance_date=?",
                        (status, student_id, attendance_date),
                    )
                else:
                    db.execute(
                        "INSERT INTO attendance "
                        "(attendance_id,student_id,attendance_date,status,is_demo) "
                        "VALUES (?,?,?,?,0)",
                        ("attendance-" + secrets.token_hex(12), student_id, attendance_date, status),
                    )
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
                    "WHERE e.exam_id=? AND " + scope,
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
                    "WHERE e.exam_id=? AND EXISTS ("
                    "SELECT 1 FROM students s JOIN class_sections c ON c.section_id=e.section_id "
                    "WHERE s.student_id=e.student_id AND " + scope + ")",
                    (marks, grade, exam_id, *params),
                )
                if result.rowcount != 1:
                    self.send_html("<main><h1>Not authorized</h1><p>No accessible examination record was changed.</p></main>", 403)
                    return
            self.redirect("/?page=academics")
            return
        if self.path == "/payments/add":
            if user["role"] not in ADMIN_ROLES:
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            fee_id = form.get("fee_id", [""])[0].strip()
            receipt_number = form.get("receipt_number", [""])[0].strip().upper()
            payment_date = form.get("payment_date", [""])[0].strip()
            method = form.get("method", [""])[0].strip()
            try:
                amount = int(form.get("amount", [""])[0])
                date.fromisoformat(payment_date)
            except ValueError:
                self.send_html("<main><h1>Invalid payment details</h1></main>", 400)
                return
            if not fee_id or not receipt_number or len(receipt_number) > 40:
                self.send_html("<main><h1>Invalid payment details</h1></main>", 400)
                return
            if method not in {"Cash", "Card", "Bank Transfer", "Cheque", "Online"} or amount <= 0:
                self.send_html("<main><h1>Invalid payment details</h1></main>", 400)
                return
            with database_connection(self.database) as db:
                fee = db.execute(
                    "SELECT fee_id,annual_total,paid_amount,pending_amount "
                    "FROM fees WHERE fee_id=?",
                    (fee_id,),
                ).fetchone()
                if not fee:
                    self.send_html("<main><h1>Fee account not found</h1></main>", 404)
                    return
                if amount > fee["pending_amount"]:
                    self.send_html("<main><h1>Invalid payment amount</h1><p>The payment exceeds the outstanding balance.</p></main>", 400)
                    return
                duplicate = db.execute(
                    "SELECT payment_id FROM fee_payments WHERE LOWER(receipt_number)=LOWER(?)",
                    (receipt_number,),
                ).fetchone()
                if duplicate:
                    self.send_html("<main><h1>Duplicate receipt number</h1></main>", 400)
                    return
                paid_amount = fee["paid_amount"] + amount
                pending_amount = fee["annual_total"] - paid_amount
                due_date = db.execute(
                    "SELECT due_date FROM fees WHERE fee_id=?", (fee_id,)
                ).fetchone()["due_date"]
                payment_status = fee_status(paid_amount, fee["annual_total"], due_date)
                payment_id = "payment-" + secrets.token_hex(12)
                db.execute(
                    "INSERT INTO fee_payments "
                    "(payment_id,fee_id,payment_date,amount,method,receipt_number,is_demo) "
                    "VALUES (?,?,?,?,?,?,0)",
                    (payment_id, fee_id, payment_date, amount, method, receipt_number),
                )
                db.execute(
                    "UPDATE fees SET paid_amount=?,pending_amount=?,payment_status=? "
                    "WHERE fee_id=?",
                    (paid_amount, pending_amount, payment_status, fee_id),
                )
            self.redirect("/?page=payments&payment_added=1")
            return
        if self.path == "/boards/update":
            if user["role"] not in ADMIN_ROLES | {"Technical Team"}:
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            board_id = form.get("board_id", [""])[0].strip()
            status = form.get("status", [""])[0].strip()
            if status not in {"Online", "Offline", "Error", "Under Maintenance"}:
                self.send_html("<main><h1>Invalid board status</h1></main>", 400)
                return
            with database_connection(self.database) as db:
                result = db.execute(
                    "UPDATE smart_boards SET status=?,last_update=? "
                    "WHERE board_id=?",
                    (status, datetime.now(timezone.utc).isoformat(), board_id),
                )
                if result.rowcount != 1:
                    self.send_html("<main><h1>Smart board not found</h1></main>", 404)
                    return
            self.redirect("/?page=boards&board_updated=1")
            return
        if self.path == "/announcements/read":
            announcement_id = form.get("announcement_id", [""])[0].strip()
            with database_connection(self.database) as db:
                announcement = db.execute(
                    "SELECT announcement_id,audience FROM announcements WHERE announcement_id=?",
                    (announcement_id,),
                ).fetchone()
                allowed = False
                if announcement:
                    if user["role"] in ADMIN_ROLES:
                        allowed = True
                    else:
                        allowed = announcement["audience"] in ANNOUNCEMENT_AUDIENCES.get(user["role"], ())
                if not allowed:
                    self.send_html("<main><h1>Not authorized</h1></main>", 403)
                    return
                db.execute(
                    "INSERT INTO announcement_reads "
                    "(announcement_id,user_id,read_status,is_demo) "
                    "VALUES (?,?, 'Read',0) "
                    "ON CONFLICT(announcement_id,user_id) DO UPDATE SET read_status='Read'",
                    (announcement_id, user["user_id"]),
                )
            self.redirect("/?page=announcements&read=1")
            return
        if self.path == "/announcements/create":
            if user["role"] not in ADMIN_ROLES:
                self.send_html("<main><h1>Not authorized</h1></main>", 403)
                return
            title = form.get("title", [""])[0].strip()
            description = form.get("description", [""])[0].strip()
            audience = form.get("audience", [""])[0].strip()
            priority = form.get("priority", [""])[0].strip()
            valid_audiences = {
                "All School", "Students and Staff", "Students and Parents",
                "Staff", "Parents", "Students",
            }
            if not title or len(title) > 120 or not description or len(description) > 2000:
                self.send_html("<main><h1>Invalid announcement details</h1></main>", 400)
                return
            if audience not in valid_audiences or priority not in {"Normal", "Important", "Urgent"}:
                self.send_html("<main><h1>Invalid announcement details</h1></main>", 400)
                return
            with database_connection(self.database) as db:
                db.execute(
                    "INSERT INTO announcements "
                    "(announcement_id,title,description,audience,author,published_at,priority,is_demo) "
                    "VALUES (?,?,?,?,?,?,?,0)",
                    ("announcement-" + secrets.token_hex(12), title, description, audience,
                     user["role"], datetime.now(timezone.utc).isoformat(), priority),
                )
            self.redirect("/?page=announcements&created=1")
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
                    "SELECT board_id,is_demo FROM technical_issues WHERE issue_id=?",
                    (issue_id,),
                ).fetchone()
                if not issue:
                    self.send_html("<main><h1>Technical issue not found</h1></main>", 404)
                    return
                db.execute(
                    "UPDATE technical_issues SET status=?,repair_notes=?,technician_id=? "
                    "WHERE issue_id=?",
                    (status, repair_notes, "demo-staff-001", issue_id),
                )
                db.execute(
                    "INSERT INTO board_maintenance VALUES (?,?,?,?,?,?,?)",
                    (secrets.token_hex(16), issue["board_id"],
                     datetime.now(timezone.utc).date().isoformat(),
                     f"Status: {status}. {repair_notes}".strip(),
                     "demo-staff-001", status, issue["is_demo"]),
                )
            self.redirect("/?page=issues")
            return
        if self.path in {
            "/staff/add", "/staff/update", "/staff/status",
            "/staff/assignments/add", "/staff/assignments/remove",
            "/classes/add", "/classes/update",
        }:
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1><p>Only the Director or Principal can manage staff and class records.</p></main>" +
                    _page_end(), 403,
                )
                return

            if self.path == "/staff/status":
                employee_id = form.get("employee_id", [""])[0].strip()
                status = form.get("status", [""])[0].strip()
                if not employee_id or status not in {"Active", "Inactive"}:
                    self.send_html("<main><h1>Invalid staff status</h1></main>", 400)
                    return
                with database_connection(self.database) as db:
                    result = db.execute(
                        "UPDATE employees SET status=? WHERE employee_id=?",
                        (status, employee_id),
                    )
                    if result.rowcount != 1:
                        self.send_html("<main><h1>Staff member not found</h1></main>", 404)
                        return
                self.redirect(
                    "/?page=staff&action=profile&" +
                    urlencode({"id": employee_id}) + "&staff_status=1"
                )
                return

            if self.path in {"/staff/assignments/add", "/staff/assignments/remove"}:
                employee_id = form.get("employee_id", [""])[0].strip()
                with database_connection(self.database) as db:
                    employee = db.execute(
                        "SELECT category,status FROM employees WHERE employee_id=?",
                        (employee_id,),
                    ).fetchone()
                    if not employee or employee["category"] != "teacher":
                        self.send_html("<main><h1>Teacher not found</h1></main>", 404)
                        return
                    if self.path == "/staff/assignments/remove":
                        assignment_id = form.get("assignment_id", [""])[0].strip()
                        result = db.execute(
                            "DELETE FROM teacher_assignments "
                            "WHERE assignment_id=? AND employee_id=?",
                            (assignment_id, employee_id),
                        )
                        if result.rowcount != 1:
                            self.send_html("<main><h1>Teacher assignment not found</h1></main>", 404)
                            return
                    else:
                        section_id = form.get("section_id", [""])[0].strip()
                        subject = form.get("subject", [""])[0].strip()
                        if (
                            employee["status"] != "Active" or not section_id or
                            not subject or len(subject) > 80 or
                            any(ord(character) < 32 for character in subject)
                        ):
                            self.send_html("<main><h1>Invalid teacher assignment</h1></main>", 400)
                            return
                        section = db.execute(
                            "SELECT 1 FROM class_sections WHERE section_id=?",
                            (section_id,),
                        ).fetchone()
                        if not section:
                            self.send_html("<main><h1>Class section not found</h1></main>", 404)
                            return
                        duplicate = db.execute(
                            "SELECT 1 FROM teacher_assignments "
                            "WHERE employee_id=? AND section_id=? AND subject=?",
                            (employee_id, section_id, subject),
                        ).fetchone()
                        if duplicate:
                            self.send_html("<main><h1>Teacher assignment already exists</h1></main>", 409)
                            return
                        try:
                            db.execute(
                                "INSERT INTO teacher_assignments "
                                "(assignment_id,employee_id,section_id,subject,is_demo) "
                                "VALUES (?,?,?,?,0)",
                                ("assignment-" + secrets.token_hex(12),
                                 employee_id, section_id, subject),
                            )
                        except sqlite3.IntegrityError:
                            self.send_html("<main><h1>Teacher assignment already exists</h1></main>", 409)
                            return
                self.redirect(
                    "/?page=staff&action=profile&" +
                    urlencode({"id": employee_id}) + "&assignment_saved=1"
                )
                return

            if self.path in {"/staff/add", "/staff/update"}:
                employee_id = form.get("employee_id", [""])[0].strip()
                category = form.get("category", [""])[0].strip()
                name = form.get("name", [""])[0].strip()
                designation = form.get("designation", [""])[0].strip()
                department = form.get("department", [""])[0].strip()
                email = form.get("email", [""])[0].strip()
                phone = form.get("phone", [""])[0].strip()
                joining_date = form.get("joining_date", [""])[0].strip()
                status = form.get("status", [""])[0].strip()
                try:
                    parsed_joining_date = date.fromisoformat(joining_date)
                except ValueError:
                    parsed_joining_date = None
                if (
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", employee_id) or
                    category not in {"teacher", "staff"} or
                    not all((name, designation, department, email, phone)) or
                    any(len(value) > limit for value, limit in (
                        (name, 120), (designation, 120), (department, 120),
                        (email, 120), (phone, 30),
                    )) or
                    not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or
                    not re.fullmatch(r"[0-9+().\-\s]{6,30}", phone) or
                    parsed_joining_date is None or
                    status not in {"Active", "Inactive"}
                ):
                    self.send_html("<main><h1>Invalid staff details</h1><p>Complete all required fields using a valid employee ID, email, phone, and joining date.</p></main>", 400)
                    return
                try:
                    with database_connection(self.database) as db:
                        existing = None
                        if self.path == "/staff/update":
                            existing = db.execute(
                                "SELECT employee_id,category FROM employees WHERE employee_id=?",
                                (employee_id,),
                            ).fetchone()
                            if not existing:
                                self.send_html("<main><h1>Staff member not found</h1></main>", 404)
                                return
                            if existing["category"] == "teacher" and category != "teacher":
                                assigned = db.execute(
                                    "SELECT 1 FROM teacher_assignments WHERE employee_id=? "
                                    "UNION SELECT 1 FROM class_sections WHERE homeroom_teacher_id=? LIMIT 1",
                                    (employee_id, employee_id),
                                ).fetchone()
                                if assigned:
                                    self.send_html(
                                        "<main><h1>Teacher category cannot be changed</h1>"
                                        "<p>Remove the teacher's class and subject assignments first.</p></main>",
                                        400,
                                    )
                                    return
                        duplicate = db.execute(
                            "SELECT employee_id FROM employees "
                            "WHERE LOWER(employee_id)=LOWER(?) "
                            + ("AND employee_id<>?" if existing else ""),
                            (employee_id, employee_id) if existing else (employee_id,),
                        ).fetchone()
                        if duplicate:
                            self.send_html("<main><h1>Duplicate employee ID</h1></main>", 409)
                            return
                        if existing:
                            db.execute(
                                "UPDATE employees SET category=?,name=?,designation=?,department=?,"
                                "email=?,phone=?,joining_date=?,status=? WHERE employee_id=?",
                                (category, name, designation, department, email, phone,
                                 joining_date, status, employee_id),
                            )
                        else:
                            db.execute(
                                "INSERT INTO employees "
                                "(employee_id,category,name,designation,department,email,phone,"
                                "joining_date,status,is_demo) VALUES (?,?,?,?,?,?,?,?,?,0)",
                                (employee_id, category, name, designation, department,
                                 email, phone, joining_date, status),
                            )
                except sqlite3.IntegrityError:
                    self.send_html("<main><h1>Duplicate employee ID</h1></main>", 409)
                    return
                self.redirect(
                    "/?page=staff&action=profile&" +
                    urlencode({"id": employee_id}) + "&staff_saved=1"
                )
                return

            section_id = form.get("section_id", [""])[0].strip()
            class_name = re.sub(r"\s+", " ", form.get("class_name", [""])[0].strip())
            section_name = re.sub(r"\s+", " ", form.get("section_name", [""])[0].strip())
            academic_session = form.get("academic_session", [""])[0].strip()
            teacher_id = form.get("homeroom_teacher_id", [""])[0].strip()
            if (
                not class_name or len(class_name) > 80 or
                not section_name or len(section_name) > 40 or
                not academic_session or len(academic_session) > 20 or
                any(ord(character) < 32 for character in class_name + section_name + academic_session) or
                not teacher_id
            ):
                self.send_html("<main><h1>Invalid class section</h1><p>Class, section, session, and class teacher are required.</p></main>", 400)
                return
            with database_connection(self.database) as db:
                teacher = db.execute(
                    "SELECT category,status FROM employees WHERE employee_id=?",
                    (teacher_id,),
                ).fetchone()
                existing_section = None
                if self.path == "/classes/update":
                    existing_section = db.execute(
                        "SELECT section_id,homeroom_teacher_id FROM class_sections "
                        "WHERE section_id=?",
                        (section_id,),
                    ).fetchone()
                    if not existing_section:
                        self.send_html("<main><h1>Class section not found</h1></main>", 404)
                        return
                retained_inactive_teacher = bool(
                    existing_section and
                    existing_section["homeroom_teacher_id"] == teacher_id
                )
                if (
                    not teacher or teacher["category"] != "teacher" or
                    (teacher["status"] != "Active" and not retained_inactive_teacher)
                ):
                    self.send_html("<main><h1>Choose an active teacher</h1></main>", 400)
                    return
                duplicate = db.execute(
                    "SELECT section_id FROM class_sections "
                    "WHERE class_name=? COLLATE NOCASE "
                    "AND section_name=? COLLATE NOCASE "
                    "AND academic_session=? COLLATE NOCASE "
                    + ("AND section_id<>?" if existing_section else ""),
                    (class_name, section_name, academic_session, section_id)
                    if existing_section else (class_name, section_name, academic_session),
                ).fetchone()
                if duplicate:
                    self.send_html("<main><h1>Duplicate class and section</h1><p>This section already exists for the selected academic session.</p></main>", 409)
                    return
                try:
                    if existing_section:
                        db.execute(
                            "UPDATE class_sections SET class_name=?,section_name=?,"
                            "academic_session=?,homeroom_teacher_id=? WHERE section_id=?",
                            (class_name, section_name, academic_session, teacher_id, section_id),
                        )
                    else:
                        section_id = "section-" + secrets.token_hex(12)
                        db.execute(
                            "INSERT INTO class_sections "
                            "(section_id,class_name,section_name,academic_session,"
                            "homeroom_teacher_id,is_demo) VALUES (?,?,?,?,?,0)",
                            (section_id, class_name, section_name, academic_session, teacher_id),
                        )
                except sqlite3.IntegrityError:
                    self.send_html("<main><h1>Duplicate class and section</h1></main>", 409)
                    return
            self.redirect("/?page=classes&class_saved=1")
            return
        if self.path in ("/students/add", "/students/update", "/students/deactivate"):
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1><p>Only the Director or Principal can administer student records.</p></main>" +
                    _page_end(), 403,
                )
                return

            if self.path == "/students/deactivate":
                student_id = form.get("student_id", [""])[0].strip()
                confirmation = form.get("confirm", [""])[0]
                if confirmation != "DEACTIVATE":
                    self.send_html(
                        _page_start("Deactivation not confirmed", "students", user) +
                        "<main><h1>Deactivation not confirmed</h1><p>Choose a student and confirm the deactivation checkbox.</p></main>" +
                        _page_end(), 400,
                    )
                    return
                with database_connection(self.database) as db:
                    record = db.execute(
                        "SELECT student_id,status FROM students WHERE student_id=?",
                        (student_id,),
                    ).fetchone()
                    if not record:
                        self.send_html(
                            _page_start("Student not found", "students", user) +
                            "<main><h1>Student not found</h1></main>" + _page_end(), 404,
                        )
                        return
                    db.execute(
                        "UPDATE students SET status='Inactive' WHERE student_id=?",
                        (student_id,),
                    )
                self.redirect("/?page=students&deactivated=1")
                return

            full_name = form.get("full_name", [""])[0].strip()
            admission_number = form.get("admission_number", [""])[0].strip().upper()
            section_id = form.get("section_id", [""])[0].strip()
            roll_text = form.get("roll_number", [""])[0].strip()
            dob = form.get("date_of_birth", [""])[0].strip()
            gender = form.get("gender", [""])[0].strip()
            guardian_name = form.get("guardian_name", [""])[0].strip()
            guardian_phone = form.get("guardian_phone", [""])[0].strip()
            guardian_email = form.get("guardian_email", [""])[0].strip()
            address = form.get("address", [""])[0].strip()
            father_name = form.get("father_name", [""])[0].strip()
            mother_name = form.get("mother_name", [""])[0].strip()
            emergency_contact = form.get("emergency_contact", [""])[0].strip()
            admission_date = form.get("admission_date", [""])[0].strip()
            bus_id = form.get("bus_id", [""])[0].strip()
            student_id = form.get("student_id", [""])[0].strip()

            student_status = form.get("status", [""])[0].strip()
            if not all((full_name, admission_number, section_id, roll_text, dob,
                        gender, guardian_name, guardian_phone, address)):
                self.send_html(
                    _page_start("Invalid student details", "students", user) +
                    "<main><h1>Required fields missing</h1><p>Complete all required student fields and try again.</p></main>" +
                    _page_end(), 400,
                )
                return
            if (
                len(full_name) > 120 or len(admission_number) > 40 or
                len(guardian_name) > 120 or len(guardian_phone) > 30 or
                len(guardian_email) > 120 or len(address) > 300 or
                len(father_name) > 120 or len(mother_name) > 120 or
                len(emergency_contact) > 30
            ):
                self.send_html(
                    _page_start("Invalid student details", "students", user) +
                    "<main><h1>Invalid student details</h1><p>One or more student or guardian fields exceed the allowed length.</p></main>" +
                    _page_end(), 400,
                )
                return
            try:
                birth_date = date.fromisoformat(dob)
                roll_number = int(roll_text)
                parsed_admission_date = date.fromisoformat(admission_date) if admission_date else None
            except ValueError:
                self.send_html(
                    _page_start("Invalid student details", "students", user) +
                    "<main><h1>Invalid student details</h1><p>Enter valid dates and a roll number.</p></main>" +
                    _page_end(), 400,
                )
                return
            if (
                birth_date > date.today() or
                not re.fullmatch(r"[0-9+().\-\s]{6,30}", guardian_phone) or
                (guardian_email and not re.fullmatch(
                    r"[^@\s]+@[^@\s]+\.[^@\s]+", guardian_email
                )) or
                (emergency_contact and not re.fullmatch(r"[0-9+().\-\s]{6,30}", emergency_contact)) or
                (parsed_admission_date is not None and parsed_admission_date > date.today()) or
                not 1 <= roll_number <= 999 or
                gender not in {"Male", "Female", "Other"} or
                (student_status and student_status not in {"Active", "Inactive"})
            ):
                self.send_html(
                    _page_start("Invalid student details", "students", user) +
                    "<main><h1>Invalid student details</h1><p>Enter a valid date of birth, contact number, email, roll number, and gender.</p></main>" +
                    _page_end(), 400,
                )
                return

            try:
                with database_connection(self.database) as db:
                    section = db.execute(
                        "SELECT section_id,academic_session FROM class_sections "
                        "WHERE section_id=?",
                        (section_id,),
                    ).fetchone()
                    if not section:
                        self.send_html(
                            _page_start("Invalid class section", "students", user) +
                            "<main><h1>Invalid class section</h1></main>" + _page_end(), 400,
                        )
                        return
                    duplicate = db.execute(
                        "SELECT student_id FROM students WHERE LOWER(admission_number)=LOWER(?) "                        + ("AND student_id<>?" if self.path == "/students/update" else ""),
                        (admission_number, student_id) if self.path == "/students/update" else (admission_number,),
                    ).fetchone()
                    if duplicate:
                        self.send_html(
                            _page_start("Duplicate admission number", "students", user) +
                            "<main><h1>Duplicate admission number</h1><p>This admission number is already assigned.</p></main>" +
                            _page_end(), 400,
                        )
                        return
                    duplicate_roll = db.execute(
                        "SELECT student_id FROM students WHERE section_id=? AND roll_number=? "
                        + ("AND student_id<>?" if self.path == "/students/update" else ""),
                        (section_id, roll_number, student_id) if self.path == "/students/update" else (section_id, roll_number),
                    ).fetchone()
                    if duplicate_roll:
                        self.send_html(
                            _page_start("Duplicate roll number", "students", user) +
                            "<main><h1>Duplicate roll number</h1><p>This roll number is already used in the selected class and section.</p></main>" +
                            _page_end(), 400,
                        )
                        return
                    if bus_id:
                        bus = db.execute(
                            "SELECT bus_id FROM buses WHERE bus_id=?",
                            (bus_id,),
                        ).fetchone()
                        if not bus:
                            self.send_html(
                                _page_start("Invalid bus", "students", user) +
                                "<main><h1>Invalid bus assignment</h1></main>" + _page_end(), 400,
                            )
                            return

                    if self.path == "/students/update":
                        existing = db.execute(
                            "SELECT student_id FROM students WHERE student_id=?",
                            (student_id,),
                        ).fetchone()
                        if not existing:
                            self.send_html(
                                _page_start("Student not found", "students", user) +
                                "<main><h1>Student not found</h1></main>" + _page_end(), 404,
                            )
                            return
                        db.execute(
                            "UPDATE students SET admission_number=?,full_name=?,date_of_birth=?,"
                            "gender=?,section_id=?,roll_number=?,academic_session=?,guardian_name=?,"
                            "guardian_phone=?,guardian_email=?,address=?,bus_id=?,"
                            "admission_date=CASE WHEN ?='' THEN admission_date ELSE ? END,"
                            "status=CASE WHEN ?='' THEN status ELSE ? END "
                            "WHERE student_id=?",
                            (admission_number, full_name, dob, gender, section_id, roll_number,
                             section["academic_session"], guardian_name, guardian_phone,
                             guardian_email, address, bus_id or None, admission_date,
                             admission_date, student_status, student_status, student_id),
                        )
                        db.execute(
                            "INSERT INTO student_guardian_details "
                            "(student_id,father_name,mother_name,emergency_contact,is_demo) "
                            "VALUES (?,?,?,?,?) "
                            "ON CONFLICT(student_id) DO UPDATE SET "
                            "father_name=excluded.father_name,mother_name=excluded.mother_name,"
                            "emergency_contact=excluded.emergency_contact",
                            (student_id, father_name, mother_name, emergency_contact,
                             db.execute("SELECT is_demo FROM students WHERE student_id=?", (student_id,))
                             .fetchone()["is_demo"]),
                        )
                        self.redirect("/?page=student&" + urlencode({"id": student_id}) + "&edited=1")
                        return

                    new_student_id = "student-" + secrets.token_hex(12)
                    db.execute(
                        "INSERT INTO students "
                        "(student_id,admission_number,full_name,date_of_birth,gender,section_id,"
                        "roll_number,academic_session,guardian_name,guardian_phone,guardian_email,"
                        "address,admission_date,bus_id,status,is_demo) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (new_student_id, admission_number, full_name, dob, gender, section_id,
                         roll_number, section["academic_session"], guardian_name, guardian_phone,
                         guardian_email, address,
                         admission_date or date.today().isoformat(),
                         bus_id or None, student_status or "Active", 0),
                    )
                    db.execute(
                        "INSERT INTO student_guardian_details "
                        "(student_id,father_name,mother_name,emergency_contact,is_demo) "
                        "VALUES (?,?,?,?,0)",
                        (new_student_id, father_name, mother_name, emergency_contact),
                    )
                self.redirect("/?page=students&added=1")
            except sqlite3.Error:
                self.log_error("Student write failed")
                self.send_html(
                    _page_start("Unable to save student", "students", user) +
                    "<main><h1>Unable to save student</h1><p>The database rejected the student record. Check the entered data and try again.</p></main>" +
                    _page_end(), 500,
                )
            return
        if self.path == "/students/transport":
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1><p>Only the Director or Principal can change transport assignments.</p></main>" +
                    _page_end(), 403,
                )
                return
            student_id = form.get("student_id", [""])[0].strip()
            bus_id = form.get("bus_id", [""])[0].strip()
            with database_connection(self.database) as db:
                student = db.execute(
                    "SELECT student_id FROM students WHERE student_id=?",
                    (student_id,),
                ).fetchone()
                if not student:
                    self.send_html("<main><h1>Student not found</h1></main>", 404)
                    return
                if bus_id and not db.execute(
                    "SELECT 1 FROM buses WHERE bus_id=?", (bus_id,)
                ).fetchone():
                    self.send_html("<main><h1>Invalid bus assignment</h1></main>", 400)
                    return
                result = db.execute(
                    "UPDATE students SET bus_id=? WHERE student_id=?",
                    (bus_id or None, student_id),
                )
                if result.rowcount != 1:
                    self.send_html("<main><h1>Student transport update failed</h1></main>", 500)
                    return
            self.redirect(
                "/?page=student&" + urlencode({"id": student_id}) + "&transport_updated=1"
            )
            return
        if self.path == "/students/payments/add":
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1></main>" + _page_end(), 403,
                )
                return
            student_id = form.get("student_id", [""])[0].strip()
            fee_id = form.get("fee_id", [""])[0].strip()
            receipt_number = form.get("receipt_number", [""])[0].strip().upper()
            payment_date = form.get("payment_date", [""])[0].strip()
            method = form.get("method", [""])[0].strip()
            remarks = form.get("remarks", [""])[0].strip()
            try:
                amount = int(form.get("amount", [""])[0])
                date.fromisoformat(payment_date)
            except ValueError:
                self.send_html("<main><h1>Invalid payment details</h1></main>", 400)
                return
            if (
                not student_id or not fee_id or not receipt_number or
                len(receipt_number) > 40 or len(remarks) > 500 or
                method not in {"Cash", "Card", "Bank Transfer", "Cheque", "Online"} or
                amount <= 0 or amount > 100_000_000
            ):
                self.send_html("<main><h1>Invalid payment details</h1></main>", 400)
                return
            with database_connection(self.database) as db:
                fee = db.execute(
                    "SELECT fee_id,annual_total,paid_amount,pending_amount "
                    "FROM fees WHERE fee_id=? AND student_id=?",
                    (fee_id, student_id),
                ).fetchone()
                if not fee:
                    self.send_html("<main><h1>Fee account not found</h1></main>", 404)
                    return
                if amount > fee["pending_amount"]:
                    self.send_html(
                        "<main><h1>Invalid payment amount</h1>"
                        "<p>The payment exceeds the outstanding balance.</p></main>",
                        400,
                    )
                    return
                if db.execute(
                    "SELECT 1 FROM fee_payments WHERE LOWER(receipt_number)=LOWER(?)",
                    (receipt_number,),
                ).fetchone():
                    self.send_html("<main><h1>Duplicate receipt number</h1></main>", 400)
                    return
                paid_amount = fee["paid_amount"] + amount
                pending_amount = fee["annual_total"] - paid_amount
                due_date = db.execute(
                    "SELECT due_date FROM fees WHERE fee_id=?", (fee_id,)
                ).fetchone()["due_date"]
                payment_status = fee_status(paid_amount, fee["annual_total"], due_date)
                payment_id = "payment-" + secrets.token_hex(12)
                db.execute(
                    "INSERT INTO fee_payments "
                    "(payment_id,fee_id,payment_date,amount,method,receipt_number,is_demo) "
                    "VALUES (?,?,?,?,?,?,0)",
                    (payment_id, fee_id, payment_date, amount, method, receipt_number),
                )
                db.execute(
                    "INSERT INTO fee_payment_remarks "
                    "(payment_id,remarks,is_demo) VALUES (?,?,0)",
                    (payment_id, remarks),
                )
                db.execute(
                    "UPDATE fees SET paid_amount=?,pending_amount=?,payment_status=? "
                    "WHERE fee_id=?",
                    (paid_amount, pending_amount, payment_status, fee_id),
                )
            self.redirect(
                "/?page=student&" + urlencode({"id": student_id}) + "&payment_added=1"
            )
            return
        if self.path == "/students/fees/adjust":
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1></main>" + _page_end(), 403,
                )
                return
            student_id = form.get("student_id", [""])[0].strip()
            fee_id = form.get("fee_id", [""])[0].strip()
            adjustment_date = form.get("adjustment_date", [""])[0].strip()
            reason = form.get("reason", [""])[0].strip()
            try:
                amount = int(form.get("amount", [""])[0])
                date.fromisoformat(adjustment_date)
            except ValueError:
                self.send_html("<main><h1>Invalid fee adjustment</h1></main>", 400)
                return
            if (
                not student_id or not fee_id or amount == 0 or
                abs(amount) > 100_000_000 or not reason or len(reason) > 500
            ):
                self.send_html(
                    "<main><h1>Invalid fee adjustment</h1>"
                    "<p>Enter a non-zero signed amount and a reason.</p></main>",
                    400,
                )
                return
            with database_connection(self.database) as db:
                fee = db.execute(
                    "SELECT fee_id,paid_amount,due_date,is_demo "
                    "FROM fees WHERE fee_id=? AND student_id=?",
                    (fee_id, student_id),
                ).fetchone()
                if not fee:
                    self.send_html("<main><h1>Fee account not found</h1></main>", 404)
                    return
                db.execute(
                    "INSERT INTO fee_adjustments "
                    "(adjustment_id,fee_id,adjustment_date,amount,reason,recorded_by,is_demo) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("adjustment-" + secrets.token_hex(12), fee_id, adjustment_date,
                     amount, reason, user["email"], fee["is_demo"]),
                )
                adjustment_total = db.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM fee_adjustments WHERE fee_id=?",
                    (fee_id,),
                ).fetchone()[0]
                base = db.execute(
                    "SELECT fee_base_total FROM fee_bases WHERE fee_id=?",
                    (fee_id,),
                ).fetchone()
                if base:
                    fee_base_total = base["fee_base_total"]
                else:
                    pre_adjustment_base = db.execute(
                        "SELECT annual_total FROM fees WHERE fee_id=?", (fee_id,)
                    ).fetchone()["annual_total"] - (adjustment_total - amount)
                    fee_base_total = pre_adjustment_base
                    db.execute(
                        "INSERT INTO fee_bases "
                        "(fee_id,fee_base_total,is_demo) VALUES (?,?,?)",
                        (fee_id, fee_base_total, fee["is_demo"]),
                    )
                annual_total = fee_base_total + adjustment_total
                if annual_total < fee["paid_amount"] or annual_total < 1:
                    db.rollback()
                    self.send_html(
                        "<main><h1>Invalid fee adjustment</h1>"
                        "<p>The adjustment would make the applicable fee less than the amount already paid.</p></main>",
                        400,
                    )
                    return
                pending_amount = annual_total - fee["paid_amount"]
                payment_status = fee_status(
                    fee["paid_amount"], annual_total, fee["due_date"]
                )
                db.execute(
                    "UPDATE fees SET annual_total=?,pending_amount=?,payment_status=? "
                    "WHERE fee_id=?",
                    (annual_total, pending_amount, payment_status, fee_id),
                )
            self.redirect(
                "/?page=student&" + urlencode({"id": student_id}) + "&fee_adjusted=1"
            )
            return
        if self.path == "/students/fees/update":
            if user["role"] not in ADMIN_ROLES:
                self.send_html(
                    _page_start("Not authorized", "", user) +
                    "<main><h1>Not authorized</h1><p>Only the Director or Principal can edit fee information.</p></main>" +
                    _page_end(), 403,
                )
                return
            student_id = form.get("student_id", [""])[0].strip()
            fee_id = form.get("fee_id", [""])[0].strip()
            fee_category = form.get("fee_category", [""])[0].strip()
            due_date = form.get("due_date", [""])[0].strip()
            try:
                annual_total = int(form.get("annual_total", [""])[0])
                parsed_due_date = date.fromisoformat(due_date)
            except ValueError:
                self.send_html("<main><h1>Invalid fee details</h1><p>Enter a valid total fee and due date.</p></main>", 400)
                return
            if (
                not student_id or not fee_category or len(fee_category) > 120 or
                not 1 <= annual_total <= 100_000_000 or
                parsed_due_date.isoformat() != due_date
            ):
                self.send_html("<main><h1>Invalid fee details</h1><p>Enter a fee category, a total from ₹1 to ₹100,000,000, and a valid due date.</p></main>", 400)
                return
            with database_connection(self.database) as db:
                student = db.execute(
                    "SELECT academic_session,is_demo FROM students WHERE student_id=?",
                    (student_id,),
                ).fetchone()
                if not student:
                    self.send_html("<main><h1>Student not found</h1></main>", 404)
                    return
                if fee_id:
                    fee = db.execute(
                        "SELECT fee_id,paid_amount FROM fees "
                        "WHERE fee_id=? AND student_id=?",
                        (fee_id, student_id),
                    ).fetchone()
                    if not fee:
                        self.send_html("<main><h1>Fee account not found</h1></main>", 404)
                        return
                    paid_amount = fee["paid_amount"]
                    adjustment_total = db.execute(
                        "SELECT COALESCE(SUM(amount),0) FROM fee_adjustments WHERE fee_id=?",
                        (fee_id,),
                    ).fetchone()[0]
                else:
                    if db.execute(
                        "SELECT 1 FROM fees WHERE student_id=? AND academic_session=?",
                        (student_id, student["academic_session"]),
                    ).fetchone():
                        self.send_html("<main><h1>Fee account already exists</h1><p>Edit the existing account instead.</p></main>", 400)
                        return
                    paid_amount = 0
                    adjustment_total = 0
                if annual_total + adjustment_total < paid_amount:
                    self.send_html(
                        "<main><h1>Total fee is below the amount already paid</h1>"
                        "<p>Choose a total at least as large as recorded payments. Payment history has not been changed.</p></main>",
                        400,
                    )
                    return
                applicable_total = annual_total + adjustment_total
                pending_amount = applicable_total - paid_amount
                payment_status = fee_status(
                    paid_amount, applicable_total, parsed_due_date.isoformat()
                )
                if fee_id:
                    db.execute(
                        "UPDATE fees SET fee_category=?,annual_total=?,pending_amount=?,"
                        "due_date=?,payment_status=? WHERE fee_id=? AND student_id=?",
                        (fee_category, applicable_total, pending_amount, due_date,
                         payment_status, fee_id, student_id),
                    )
                    db.execute(
                        "INSERT INTO fee_bases "
                        "(fee_id,fee_base_total,is_demo) VALUES (?,?,?) "
                        "ON CONFLICT(fee_id) DO UPDATE SET fee_base_total=excluded.fee_base_total",
                        (fee_id, annual_total,
                         db.execute("SELECT is_demo FROM fees WHERE fee_id=?", (fee_id,))
                         .fetchone()["is_demo"]),
                    )
                else:
                    new_fee_id = "fee-" + secrets.token_hex(12)
                    db.execute(
                        "INSERT INTO fees "
                        "(fee_id,student_id,academic_session,fee_category,annual_total,"
                        "paid_amount,pending_amount,due_date,payment_status,is_demo) "
                        "VALUES (?,?,?,?,?,0,?,?,?,?)",
                        (new_fee_id, student_id,
                         student["academic_session"], fee_category, applicable_total,
                         pending_amount, due_date, payment_status, student["is_demo"]),
                    )
                    db.execute(
                        "INSERT INTO fee_bases "
                        "(fee_id,fee_base_total,is_demo) VALUES (?,?,?)",
                        (new_fee_id, annual_total, student["is_demo"]),
                    )
            self.redirect(
                "/?page=student&" + urlencode({"id": student_id}) + "&fee_updated=1"
            )
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
                    "SELECT classroom_id FROM smart_boards WHERE board_id=?",
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
    default_host = (
        "0.0.0.0"
        if os.environ.get("CODESPACES", "").lower() == "true"
        else "127.0.0.1"
    )
    parser.add_argument("--host", default=default_host)
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
