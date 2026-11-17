"""Shared helpers: validation, SQL fragments and business rules."""
import math
import re
from datetime import date, datetime

from flask import abort, flash

from db import query, execute

EXAM_TYPES = ["Viva", "Practical", "Project Demo", "Assessment"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{10,13}$")


# ---------- parsing / validation ----------
def today():
    return date.today().isoformat()


def parse_date(v):
    try:
        return datetime.strptime((v or "").strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def parse_time(v):
    try:
        return datetime.strptime((v or "").strip()[:5], "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def flash_all(errors):
    for e in errors:
        flash(e, "danger")


def one_or_404(sql, args=()):
    row = query(sql, args, one=True)
    if row is None:
        abort(404)
    return row


def search_sql(q, cols, id_col=None):
    """Build a LIKE search over `cols`; also matches an ID such as '12' or 'STU0012' when id_col is given."""
    conds = [f"{c} LIKE ?" for c in cols]
    args = [f"%{q}%"] * len(cols)
    m = re.fullmatch(r"[A-Za-z]{0,3}0*(\d+)", q.strip())
    if id_col and m:
        conds.append(f"{id_col} = ?")
        args.append(int(m.group(1)))
    return "(" + " OR ".join(conds) + ")", args


# ---------- shared SELECTs ----------
def exam_select(extra=""):
    return (
        "SELECT e.*, c.code AS course_code, c.name AS course_name, c.status AS course_status, "
        "(SELECT COUNT(*) FROM slot s WHERE s.exam_id = e.id AND s.status != 'Cancelled') AS slot_count, "
        "(SELECT COUNT(*) FROM booking b WHERE b.exam_id = e.id AND b.status != 'Cancelled') AS booking_count "
        f"{extra} FROM exam e JOIN course c ON c.id = e.course_id"
    )


SLOT_SELECT = (
    "SELECT s.*, e.name AS exam_name, e.exam_type, e.status AS exam_status, e.book_end, "
    "c.code AS course_code, u.full_name AS examiner_name, "
    "(SELECT COUNT(*) FROM booking b WHERE b.slot_id = s.id AND b.status != 'Cancelled') AS booked "
    "FROM slot s JOIN exam e ON e.id = s.exam_id JOIN course c ON c.id = e.course_id "
    "JOIN users u ON u.id = s.examiner_id"
)

BOOKING_SELECT = (
    "SELECT b.*, st.full_name AS student_name, st.email AS student_email, e.name AS exam_name, e.exam_type, "
    "e.max_marks, e.results_published, e.book_end, e.status AS exam_status, c.code AS course_code, "
    "s.slot_date, s.start_time, s.end_time, s.examiner_id, ex.full_name AS examiner_name "
    "FROM booking b JOIN users st ON st.id = b.student_id JOIN exam e ON e.id = b.exam_id "
    "JOIN course c ON c.id = e.course_id JOIN slot s ON s.id = b.slot_id JOIN users ex ON ex.id = s.examiner_id"
)


# ---------- business rules ----------
def slot_creation_open(exam):
    return exam["status"] == "Slot Creation" and exam["slot_create_start"] <= today() <= exam["slot_create_end"]


def booking_open(exam):
    return exam["status"] == "Booking Open" and exam["book_start"] <= today() <= exam["book_end"]


def examiner_conflict(examiner_id, slot_date, start, end, exclude_slot=0):
    return query(
        "SELECT 1 FROM slot WHERE examiner_id = ? AND slot_date = ? AND status != 'Cancelled' "
        "AND start_time < ? AND end_time > ? AND id != ?",
        (examiner_id, slot_date, end, start, exclude_slot), one=True,
    )


def student_conflict(student_id, slot, exclude_booking=0):
    return query(
        "SELECT 1 FROM booking b JOIN slot s ON s.id = b.slot_id WHERE b.student_id = ? AND b.status = 'Booked' "
        "AND s.slot_date = ? AND s.start_time < ? AND s.end_time > ? AND b.id != ?",
        (student_id, slot["slot_date"], slot["end_time"], slot["start_time"], exclude_booking), one=True,
    )


def refresh_slot(slot_id):
    """Auto-close a slot when capacity is reached and re-open it when a seat frees up."""
    execute(
        "UPDATE slot SET status = CASE WHEN (SELECT COUNT(*) FROM booking b WHERE b.slot_id = slot.id "
        "AND b.status != 'Cancelled') >= capacity THEN 'Full' ELSE 'Available' END "
        "WHERE id = ? AND status IN ('Available', 'Full')", (slot_id,),
    )


def compute_total(rubrics, marks, exam_max):
    """Weighted total scaled to the exam's maximum marks (equal weights when weightage is all zero)."""
    total_w = sum(r["weightage"] for r in rubrics)
    score = sum((r["weightage"] if total_w > 0 else 1) * marks[r["id"]] / r["max_marks"] for r in rubrics)
    return round(score / (total_w if total_w > 0 else len(rubrics)) * exam_max, 2)
