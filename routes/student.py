from datetime import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import Fail, execute, query, transaction
from utils import (BOOKING_SELECT, EXAM_TYPES, PHONE_RE, SLOT_SELECT, booking_open, exam_select, flash_all, one_or_404,
                   refresh_slot, search_sql, student_conflict, to_int, today)

bp = Blueprint("student", __name__, url_prefix="/student")


@bp.before_request
def guard():
    if not g.user:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("auth.login"))
    if g.user["role"] != "student":
        abort(403)


def my_status_col():
    return ", (SELECT b.status FROM booking b WHERE b.exam_id = e.id AND b.student_id = ? AND b.status != 'Cancelled') AS my_status"


OPEN_NOW = "e.status = 'Booking Open' AND e.book_start <= ? AND e.book_end >= ? AND c.status = 'Active'"


@bp.route("/")
def dashboard():
    uid, t = g.user["id"], today()
    avail_exams = query(exam_select(my_status_col()) + f" WHERE {OPEN_NOW} ORDER BY e.book_end", (uid, t, t))
    avail_slots = query(SLOT_SELECT + f" WHERE {OPEN_NOW} AND s.status = 'Available' AND e.id NOT IN "
                        "(SELECT exam_id FROM booking WHERE student_id = ? AND status != 'Cancelled') "
                        "ORDER BY s.slot_date, s.start_time", (t, t, uid))
    booked = query(BOOKING_SELECT + " WHERE b.student_id = ? AND b.status = 'Booked' ORDER BY s.slot_date, s.start_time", (uid,))
    upcoming = [b for b in booked if b["slot_date"] >= t]
    results = query(BOOKING_SELECT + " WHERE b.student_id = ? AND b.total_marks IS NOT NULL AND e.results_published = 1 "
                    "ORDER BY b.evaluated_at DESC", (uid,))
    return render_template("student/dashboard.html", avail_exams=avail_exams, avail_slots=avail_slots, booked=booked,
                           upcoming=upcoming, results=results)


@bp.route("/exams")
def exams():
    q, etype, course = request.args.get("q", "").strip(), request.args.get("type", ""), to_int(request.args.get("course"))
    conds, args = ["e.status != 'Draft'", "c.status = 'Active'"], [g.user["id"]]
    if q:
        c, a = search_sql(q, ["e.name", "c.code", "c.name"], "e.id")
        conds.append(c)
        args += a
    if etype in EXAM_TYPES:
        conds.append("e.exam_type = ?")
        args.append(etype)
    if course:
        conds.append("e.course_id = ?")
        args.append(course)
    rows = query(exam_select(my_status_col()) + " WHERE " + " AND ".join(conds) + " ORDER BY e.book_start DESC", args)
    return render_template("student/exams.html", exams=rows, q=q, etype=etype, course=course, types=EXAM_TYPES,
                           courses=query("SELECT * FROM course WHERE status = 'Active' ORDER BY code"))


@bp.route("/exams/<int:exam_id>")
def exam_view(exam_id):
    exam = one_or_404(exam_select() + " WHERE e.id = ? AND e.status != 'Draft' AND c.status = 'Active'", (exam_id,))
    mine = query(BOOKING_SELECT + " WHERE b.student_id = ? AND b.exam_id = ? ORDER BY b.id DESC", (g.user["id"], exam_id))
    live = next((b for b in mine if b["status"] != "Cancelled"), None)
    slots = query(SLOT_SELECT + " WHERE s.exam_id = ? AND s.status = 'Available' ORDER BY s.slot_date, s.start_time", (exam_id,))
    rubrics = query("SELECT * FROM rubric WHERE exam_id = ? ORDER BY id", (exam_id,))
    return render_template("student/exam_view.html", exam=exam, live=live, slots=slots, rubrics=rubrics,
                           can_book=booking_open(exam) and live is None)


@bp.route("/slots/<int:slot_id>/book", methods=["POST"])
def book(slot_id):
    uid = g.user["id"]
    exam_id = None
    try:
        with transaction():  # BEGIN IMMEDIATE: checks and insert are atomic, so a slot can never be overbooked
            slot = query(SLOT_SELECT + " WHERE s.id = ?", (slot_id,), one=True)
            if not slot:
                raise Fail("Slot not found.")
            exam_id = slot["exam_id"]
            exam = query("SELECT e.*, c.status AS course_status FROM exam e JOIN course c ON c.id = e.course_id WHERE e.id = ?",
                         (exam_id,), one=True)
            if exam["course_status"] != "Active" or not booking_open(exam):
                raise Fail("Booking is not open for this examination.")
            if slot["status"] != "Available" or slot["booked"] >= slot["capacity"]:
                raise Fail("This slot is full or no longer available.")
            if query("SELECT 1 FROM booking WHERE student_id = ? AND exam_id = ? AND status != 'Cancelled'", (uid, exam_id), one=True):
                raise Fail("You already have a booking for this examination.")
            if student_conflict(uid, slot):
                raise Fail("You already have another booking that overlaps this time.")
            execute("INSERT INTO booking (student_id, slot_id, exam_id, booking_date) VALUES (?, ?, ?, ?)",
                    (uid, slot_id, exam_id, datetime.now().strftime("%Y-%m-%d %H:%M")))
            refresh_slot(slot_id)
        flash("Slot booked successfully.", "success")
        return redirect(url_for("student.bookings"))
    except Fail as e:
        flash(str(e), "danger")
        return redirect(url_for("student.exam_view", exam_id=exam_id) if exam_id else url_for("student.exams"))


@bp.route("/bookings")
def bookings():
    rows = query(BOOKING_SELECT + " WHERE b.student_id = ? ORDER BY s.slot_date DESC, s.start_time DESC", (g.user["id"],))
    return render_template("student/bookings.html", bookings=rows)


@bp.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel(booking_id):
    b = one_or_404(BOOKING_SELECT + " WHERE b.id = ? AND b.student_id = ?", (booking_id, g.user["id"]))
    if b["status"] != "Booked":
        flash("Only active bookings can be cancelled.", "warning")
    elif today() > b["book_end"] or b["exam_status"] in ("Completed",):
        flash("The booking deadline has passed, so this booking can no longer be cancelled.", "warning")
    else:
        with transaction():
            execute("UPDATE booking SET status = 'Cancelled' WHERE id = ?", (booking_id,))
            refresh_slot(b["slot_id"])
        flash("Booking cancelled. The seat is available again.", "success")
    return redirect(url_for("student.bookings"))


@bp.route("/results")
def results():
    rows = query(BOOKING_SELECT + " WHERE b.student_id = ? AND b.total_marks IS NOT NULL AND e.results_published = 1 "
                 "ORDER BY b.evaluated_at DESC", (g.user["id"],))
    breakdown = {b["id"]: query("SELECT r.criterion, r.max_marks, sc.marks FROM score sc JOIN rubric r ON r.id = sc.rubric_id "
                                "WHERE sc.booking_id = ? ORDER BY r.id", (b["id"],)) for b in rows}
    return render_template("student/results.html", results=rows, breakdown=breakdown)


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    d = dict(g.user)
    if request.method == "POST":
        d = request.form
        name, phone, dept = d.get("full_name", "").strip(), d.get("phone", "").strip(), d.get("department", "").strip()
        errors = []
        if len(name) < 2:
            errors.append("Full name is required.")
        if not PHONE_RE.match(phone):
            errors.append("Phone number must have 10 to 13 digits.")
        if not dept:
            errors.append("Department is required.")
        new_pw = d.get("new_password", "")
        if new_pw:
            if not check_password_hash(g.user["password_hash"], d.get("current_password", "")):
                errors.append("Current password is incorrect.")
            if len(new_pw) < 6:
                errors.append("New password must be at least 6 characters.")
        if errors:
            flash_all(errors)
        else:
            execute("UPDATE users SET full_name = ?, phone = ?, department = ? WHERE id = ?", (name, phone, dept, g.user["id"]))
            if new_pw:
                execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_pw), g.user["id"]))
            flash("Profile updated.", "success")
            return redirect(url_for("student.profile"))
    return render_template("student/profile.html", d=d)
