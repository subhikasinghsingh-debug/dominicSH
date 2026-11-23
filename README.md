# SCHOOL COMMAND demo

A self-contained, local-first school administration demonstration. It uses Python's
standard library and SQLite; no cloud account, dependency installation, or real
school data is required.

## Setup and run

Requires Python 3.10 or later.

```sh
python app.py setup
python app.py
```

In GitHub Codespaces, run `python app.py` in the terminal. The server binds to
`0.0.0.0` there so Codespaces can forward port 8000; outside Codespaces it stays
bound to `127.0.0.1` for local development. In the **Ports** tab, make sure port
`8000` is forwarded, then click its **Open in Browser** button. If port 8000 is
not listed, use **Forward a Port** and enter `8000`.

The setup step creates `data/school_command.sqlite3`, seeds the fictional records,
and provisions six demo logins. On the first setup in the development environment,
randomly generated passwords are printed once in the terminal. Save them; password
hashes are stored in the database, and the passwords are not shown on later runs.
There are no built-in or production passwords. To start over from scratch, explicitly
remove the local demo database after stopping the server and run setup again.

To choose your own credentials, sign in with the Director or Principal account and
select **Configure account credentials** on the dashboard. Set a username for each
role and enter a new password wherever you want to change it; leave a password blank
to keep it as-is. Usernames must be 3–64 letters, numbers, dots, underscores, `@`,
`+`, or hyphens. New passwords must be 12–128 characters. Passwords are stored as
salted PBKDF2 hashes, never as plain text. This page is available only to Directors
and Principals and requires a valid signed-in session and CSRF token.

Outside Codespaces, open <http://127.0.0.1:8000>. In Codespaces, use the forwarded
port URL shown in the **Ports** tab. Sign in using the configured username and
password for any Director, Principal, Class Teacher, Subject Teacher, Parent, or
Technical Team account. `SCHOOL_COMMAND_ENV=production` suppresses
credential display; this standalone demo is intended for local development and does
not implement production identity management or internet-facing deployment.

The server also initializes and seeds the database on first startup if the database
is empty. It does not reseed on ordinary restarts. The dashboard and module pages
query SQLite records rather than hardcoding statistics. All pages show a DEMO MODE
banner, demo rows are marked as demo, and bus coordinates are labeled simulated.

## Demo coverage

The idempotent generator creates 240 students in Nursery through Class XII, 30
teachers, 18 non-teaching staff, 30 class sections, 30 school days of attendance,
student fee accounts and payment history, 8 buses, 65 BenQ smart boards and
maintenance entries, 14 technical issues, 12 announcements with per-user read
states, and examination records/report-card summaries. Demo records have
`is_demo=1`; stable demo IDs and unique constraints keep relationships consistent.

Available views include the dashboard, student and staff directories, classes and
section rosters, attendance, fee accounts and payment history, transport, smart
boards and maintenance history, technical issues, announcements, examination
records, and report-card summaries. The Examinations dashboard shortcut opens the
existing academic-records and mark-management view rather than duplicating it.
Dashboard statistics and module shortcuts open their corresponding views, with
filters applied where relevant. Module pages include navigation back to the
dashboard and role-scoped side navigation.
The directories and relevant operational views include database-backed searches and
combined filters with a clear action. Directors and Principals can add, view, edit,
and deactivate student records. Deactivation retains the student row and related
history; newly added student records are stored as non-demo rows and are never
deleted by a demo-data reset.
From a student profile, administrators can edit personal, class, contact, and status
details; adjust fee category, total, and due date; and assign, change, or remove a
bus assignment. Fee edits preserve recorded payments and recalculate the balance.

Phase 2 operational features include:

- adding attendance records for active, accessible students and updating existing
  attendance history for accessible students;
- recording fee payments and recalculating fee balances;
- updating smart-board status and technical issue workflows;
- creating announcements and marking visible announcements as read;
- database-backed payment and report-card filters;
- CSV exports for payment history and report cards.

## Role-based access

The Director and Principal can access every module. Class Teachers are limited to
their homeroom sections; Subject Teachers are limited to their assigned sections
and subjects. Parents are limited to students explicitly linked to their account.
Technical Team accounts can access only smart boards and technical issues. The
server checks module access and record scope on every page and write request; hidden
navigation links are not the authorization boundary. Unknown roles receive no
module access. Sessions use opaque, hashed tokens, expire after 12 hours, and
require CSRF tokens for state-changing requests.

## Reset and verification

Director and Principal accounts can reset demo data from the dashboard. The action
requires typing `RESET DEMO DATA`; it deletes and rebuilds only rows marked
`is_demo=1`, retains the provisioned demo accounts and active login, and leaves
non-demo rows untouched. Foreign-key constraints also prevent removal if another
record depends on a demo record. For example, a newly added student may reference a
demo class section or bus; in that case reset is rejected and rolled back rather
than deleting the referenced data.

```sh
python app.py verify
python -m unittest discover -s tests -v
```

Verification checks minimum record counts, the 30-school-day attendance coverage,
student fee coverage, duplicate identifiers, and SQLite foreign keys. The tests
also exercise role-specific pages, record-level URL and write authorization,
student edits, fee balance and payment-history preservation, transport assignment,
restricted dashboards, and technical issue workflows. A separate database can be
selected with `--database /path/to/school.sqlite3` for setup, run, and verification
commands.

The generated database is local runtime data and is intentionally excluded from
version control.
