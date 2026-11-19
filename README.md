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

The setup step creates `data/school_command.sqlite3`, seeds the fictional records,
and provisions six demo logins. On the first setup in the development environment,
randomly generated passwords are printed once in the terminal. Save them; password
hashes are stored in the database, and the passwords are not shown on later runs.
There are no built-in or production passwords. To start over from scratch, explicitly
remove the local demo database after stopping the server and run setup again.

Open <http://127.0.0.1:8000>. Sign in using a generated Director, Principal,
Class Teacher, Subject Teacher, Parent, or Technical Team account. `SCHOOL_COMMAND_ENV=production` suppresses
credential display; this standalone demo is intended for local development and does
not implement production identity management or internet-facing deployment.

The server also initializes and seeds the database on first startup if the database
is empty. It does not reseed on ordinary restarts. The dashboard and module pages
query SQLite records rather than hardcoding statistics. All pages show a DEMO MODE
banner, rows are marked as demo, and bus coordinates are labeled simulated.

## Demo coverage

The idempotent generator creates 240 students in Nursery through Class XII, 30
teachers, 18 non-teaching staff, 30 class sections, 30 school days of attendance,
student fee accounts and payment history, 8 buses, 65 BenQ smart boards and
maintenance entries, 14 technical issues, 12 announcements with per-user read
states, and examination records/report-card summaries. Demo records have
`is_demo=1`; stable demo IDs and unique constraints keep relationships consistent.

Available views include the dashboard, student and staff directories, attendance,
fee accounts and payment history, transport, smart boards and maintenance history,
technical issues, announcements, academic results, and report-card summaries.

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
record depends on a demo record.

```sh
python app.py verify
python -m unittest discover -s tests -v
```

Verification checks minimum record counts, the 30-school-day attendance coverage,
student fee coverage, duplicate identifiers, and SQLite foreign keys. The tests
also exercise role-specific pages, record-level URL and write authorization,
restricted dashboards, and technical issue workflows. A separate database can be
selected with `--database /path/to/school.sqlite3` for setup, run, and verification
commands.

The generated database is local runtime data and is intentionally excluded from
version control.
