"""SQLite access layer. The database file and every table are created in code (init_db) on first start."""
import os
import sqlite3
from contextlib import contextmanager

from flask import g
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "emp.sqlite3")

ADMIN_EMAIL = "admin@emp.com"
ADMIN_PASSWORD = "admin123"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'examiner', 'student')),
    phone TEXT,
    department TEXT,
    status TEXT NOT NULL DEFAULT 'Active',   -- examiner: Pending/Approved/Rejected/Deactivated
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS course (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'Active'
);
CREATE TABLE IF NOT EXISTS exam (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES course(id),
    name TEXT NOT NULL,
    exam_type TEXT NOT NULL,
    duration INTEGER NOT NULL,                -- minutes
    max_marks REAL NOT NULL,
    slot_create_start TEXT NOT NULL,
    slot_create_end TEXT NOT NULL,
    book_start TEXT NOT NULL,
    book_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft',     -- Draft / Slot Creation / Booking Open / Closed / Completed
    results_published INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS exam_examiner (
    exam_id INTEGER NOT NULL REFERENCES exam(id) ON DELETE CASCADE,
    examiner_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (exam_id, examiner_id)
);
CREATE TABLE IF NOT EXISTS rubric (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exam(id) ON DELETE CASCADE,
    criterion TEXT NOT NULL,
    max_marks REAL NOT NULL,
    weightage REAL NOT NULL DEFAULT 1,
    description TEXT
);
CREATE TABLE IF NOT EXISTS slot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exam(id) ON DELETE CASCADE,
    examiner_id INTEGER NOT NULL REFERENCES users(id),
    slot_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity >= 1),
    status TEXT NOT NULL DEFAULT 'Available'  -- Available / Full / Cancelled / Completed
);
CREATE TABLE IF NOT EXISTS booking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id),
    slot_id INTEGER NOT NULL REFERENCES slot(id) ON DELETE CASCADE,
    exam_id INTEGER NOT NULL REFERENCES exam(id) ON DELETE CASCADE,
    booking_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Booked',    -- Booked / Cancelled / Completed
    total_marks REAL,
    remarks TEXT,
    evaluated_at TEXT
);
-- Database-level guard: a student can hold only one live booking per examination.
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_booking ON booking(student_id, exam_id)
    WHERE status IN ('Booked', 'Completed');
CREATE TABLE IF NOT EXISTS score (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES booking(id) ON DELETE CASCADE,
    rubric_id INTEGER NOT NULL REFERENCES rubric(id),
    marks REAL NOT NULL,
    UNIQUE (booking_id, rubric_id)
);
"""


class Fail(Exception):
    """Raised inside a transaction to roll back and report a user-facing message."""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit; transactions are explicit
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    return get_db().execute(sql, args).lastrowid


@contextmanager
def transaction():
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db(app):
    with app.app_context():
        get_db().executescript(SCHEMA)
        if not query("SELECT 1 FROM users WHERE role = 'admin'", one=True):
            execute(
                "INSERT INTO users (email, password_hash, full_name, role, status) VALUES (?, ?, ?, 'admin', 'Active')",
                (ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD), "Administrator"),
            )
