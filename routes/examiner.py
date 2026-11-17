from datetime import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from db import execute, query, transaction
from utils import (BOOKING_SELECT, SLOT_SELECT, compute_total, examiner_conflict, exam_select, flash_all, one_or_404,
                   parse_date, parse_time, slot_creation_open, to_float, to_int, today)

bp = Blueprint("examiner", __name__, url_prefix="/examiner")


@bp.before_request
def guard():
    if not g.user:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("auth.login"))
    if g.user["role"] != "examiner":
        abort(403)
    if g.user["status"] != "Approved" and request.endpoint != "examiner.pending":
        return redirect(url_for("examiner.pending"))


def my_exam(exam_id):
    exam = one_or_404(exam_select() + " WHERE e.id = ? AND e.id IN (SELECT exam_id FROM exam_examiner WHERE examiner_id = ?)",
                      (exam_id, g.user["id"]))
    return exam


@bp.route("/pending")
def pending():
    if g.user["status"] == "Approved":
        return redirect(url_for("examiner.dashboard"))
    return render_template("examiner/pending.html")


@bp.route("/")
def dashboard():
    uid = g.user["id"]
    exams = query(exam_select() + " WHERE e.id IN (SELECT exam_id FROM exam_examiner WHERE examiner_id = ?) "
                  "ORDER BY e.id DESC", (uid,))
    slots = query(SLOT_SELECT + " WHERE s.examiner_id = ? AND s.status != 'Cancelled' ORDER BY s.slot_date, s.start_time", (uid,))
    pending_evals = query(BOOKING_SELECT + " WHERE s.examiner_id = ? AND b.status = 'Booked' "
                          "ORDER BY s.slot_date, s.start_time", (uid,))
    return render_template("examiner/dashboard.html", exams=exams, slots=slots, pending_evals=pending_evals,
                           booked_total=sum(s["booked"] for s in slots))


@bp.route("/exams")
def exams():
    rows = query(exam_select() + " WHERE e.id IN (SELECT exam_id FROM exam_examiner WHERE examiner_id = ?) "
                 "ORDER BY e.id DESC", (g.user["id"],))
    return render_template("examiner/exams.html", exams=rows)


def validate_slot(d, exam, exclude_slot=0):
    errors = []
    sd, st, et, cap = parse_date(d.get("slot_date")), parse_time(d.get("start_time")), parse_time(d.get("end_time")), to_int(d.get("capacity"))
    if not sd or sd < today():
        errors.append("Choose a slot date that is today or later.")
    if not st or not et or st >= et:
        errors.append("Start time must be earlier than end time.")
    if not cap or cap < 1 or cap > 500:
        errors.append("Maximum capacity must be between 1 and 500.")
    if not errors and examiner_conflict(g.user["id"], sd, st, et, exclude_slot):
        errors.append("You already have an overlapping slot at that time.")
    return errors, sd, st, et, cap


@bp.route("/exams/<int:exam_id>/slots", methods=["GET", "POST"])
def exam_slots(exam_id):
    exam = my_exam(exam_id)
    d = {}
    if request.method == "POST":
        if not slot_creation_open(exam):
            flash("Slot creation is not open for this examination right now (check its status and dates).", "warning")
            return redirect(url_for("examiner.exam_slots", exam_id=exam_id))
        d = request.form
        errors, sd, st, et, cap = validate_slot(d, exam)
        if errors:
            flash_all(errors)
        else:
            execute("INSERT INTO slot (exam_id, examiner_id, slot_date, start_time, end_time, capacity) VALUES (?, ?, ?, ?, ?, ?)",
                    (exam_id, g.user["id"], sd, st, et, cap))
            flash("Slot created.", "success")
            return redirect(url_for("examiner.exam_slots", exam_id=exam_id))
    slots = query(SLOT_SELECT + " WHERE s.exam_id = ? AND s.examiner_id = ? ORDER BY s.slot_date, s.start_time",
                  (exam_id, g.user["id"]))
    return render_template("examiner/exam_slots.html", exam=exam, slots=slots, d=d)


def my_slot(slot_id):
    return one_or_404(SLOT_SELECT + " WHERE s.id = ? AND s.examiner_id = ?", (slot_id, g.user["id"]))


def slot_editable(slot):
    if slot["exam_status"] not in ("Draft", "Slot Creation"):
        return "Booking has started for this examination, so its slots are locked."
    if slot["booked"] > 0:
        return "Students are already booked into this slot."
    return None


@bp.route("/slots/<int:slot_id>/edit", methods=["GET", "POST"])
def slot_edit(slot_id):
    slot = my_slot(slot_id)
    problem = slot_editable(slot)
    if problem:
        flash(problem, "warning")
        return redirect(url_for("examiner.exam_slots", exam_id=slot["exam_id"]))
    d = dict(slot)
    if request.method == "POST":
        d = request.form
        errors, sd, st, et, cap = validate_slot(d, None, exclude_slot=slot_id)
        if errors:
            flash_all(errors)
        else:
            execute("UPDATE slot SET slot_date=?, start_time=?, end_time=?, capacity=? WHERE id=?", (sd, st, et, cap, slot_id))
            flash("Slot updated.", "success")
            return redirect(url_for("examiner.exam_slots", exam_id=slot["exam_id"]))
    return render_template("examiner/slot_form.html", slot=slot, d=d)


@bp.route("/slots/<int:slot_id>/delete", methods=["POST"])
def slot_delete(slot_id):
    slot = my_slot(slot_id)
    problem = slot_editable(slot)
    if problem:
        flash(problem, "warning")
    else:
        execute("DELETE FROM slot WHERE id = ?", (slot_id,))
        flash("Slot removed.", "success")
    return redirect(url_for("examiner.exam_slots", exam_id=slot["exam_id"]))


@bp.route("/slots")
def slots():
    rows = query(SLOT_SELECT + " WHERE s.examiner_id = ? AND s.status != 'Cancelled' ORDER BY s.slot_date, s.start_time",
                 (g.user["id"],))
    students = {s["id"]: query(BOOKING_SELECT + " WHERE b.slot_id = ? AND b.status != 'Cancelled' ORDER BY st.full_name",
                               (s["id"],)) for s in rows}
    return render_template("examiner/slots.html", slots=rows, students=students)


@bp.route("/bookings/<int:booking_id>/evaluate", methods=["GET", "POST"])
def evaluate(booking_id):
    # restricting on s.examiner_id makes so that examiners can only evaluate students booked into their own slots
    b = one_or_404(BOOKING_SELECT + " WHERE b.id = ? AND s.examiner_id = ?", (booking_id, g.user["id"]))
    if b["status"] == "Cancelled":
        flash("This booking was cancelled, so it cannot be evaluated.", "warning")
        return redirect(url_for("examiner.slots"))
    rubrics = query("SELECT * FROM rubric WHERE exam_id = ? ORDER BY id", (b["exam_id"],))
    saved = {r["rubric_id"]: r["marks"] for r in query("SELECT * FROM score WHERE booking_id = ?", (booking_id,))}
    d = {f"m_{rid}": v for rid, v in saved.items()}
    d.update({"total": b["total_marks"], "remarks": b["remarks"]})
    if request.method == "POST":
        d, errors, marks = request.form, [], {}
        for r in rubrics:
            v = to_float(d.get(f"m_{r['id']}"))
            if v is None or v < 0 or v > r["max_marks"]:
                errors.append(f"'{r['criterion']}': enter marks between 0 and {r['max_marks']:g}.")
            else:
                marks[r["id"]] = v
        direct = None
        if not rubrics:
            direct = to_float(d.get("total"))
            if direct is None or direct < 0 or direct > b["max_marks"]:
                errors.append(f"Enter total marks between 0 and {b['max_marks']:g}.")
        if errors:
            flash_all(errors)
        else:
            total = compute_total(rubrics, marks, b["max_marks"]) if rubrics else direct
            with transaction():
                execute("DELETE FROM score WHERE booking_id = ?", (booking_id,))
                for rid, v in marks.items():
                    execute("INSERT INTO score (booking_id, rubric_id, marks) VALUES (?, ?, ?)", (booking_id, rid, v))
                execute("UPDATE booking SET total_marks = ?, remarks = ?, evaluated_at = ?, status = 'Completed' WHERE id = ?",
                        (total, d.get("remarks", "").strip(), datetime.now().strftime("%Y-%m-%d %H:%M"), booking_id))
            flash(f"Evaluation saved for {b['student_name']} (total {total:g} / {b['max_marks']:g}).", "success")
            return redirect(url_for("examiner.slots"))
    return render_template("examiner/evaluate.html", b=b, rubrics=rubrics, d=d)
