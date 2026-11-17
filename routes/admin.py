from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from db import Fail, execute, query, transaction
from utils import (BOOKING_SELECT, EMAIL_RE, EXAM_TYPES, PHONE_RE, SLOT_SELECT, examiner_conflict, exam_select,
                   flash_all, one_or_404, parse_date, refresh_slot, search_sql, student_conflict, to_float, to_int)

bp = Blueprint("admin", __name__, url_prefix="/admin")

# action -> (allowed current statuses, new status, button label, bootstrap colour)
ACTIONS = {
    "open_slots": (("Draft",), "Slot Creation", "Open slot creation", "info"),
    "close_slots": (("Slot Creation",), "Draft", "Close slot creation", "secondary"),
    "open_booking": (("Draft", "Slot Creation"), "Booking Open", "Open student booking", "success"),
    "close_booking": (("Booking Open",), "Closed", "Close student booking", "warning"),
    "reopen_booking": (("Closed",), "Booking Open", "Reopen booking", "success"),
    "complete": (("Closed",), "Completed", "Mark completed", "dark"),
}


@bp.before_request
def guard():
    if not g.user:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("auth.login"))
    if g.user["role"] != "admin":
        abort(403)


def count(sql, args=()):
    return query(sql, args, one=True)[0]


# dashboard & search
@bp.route("/")
def dashboard():
    stats = [
        ("Courses", count("SELECT COUNT(*) FROM course"), "admin.courses"),
        ("Examinations", count("SELECT COUNT(*) FROM exam"), "admin.exams"),
        ("Examiners", count("SELECT COUNT(*) FROM users WHERE role = 'examiner'"), "admin.examiners"),
        ("Students", count("SELECT COUNT(*) FROM users WHERE role = 'student'"), "admin.students"),
        ("Examination slots", count("SELECT COUNT(*) FROM slot"), "admin.slots"),
        ("Bookings", count("SELECT COUNT(*) FROM booking"), "admin.bookings"),
    ]
    pending = query("SELECT * FROM users WHERE role = 'examiner' AND status = 'Pending' ORDER BY id")
    recent = query(BOOKING_SELECT + " ORDER BY b.id DESC LIMIT 5")
    return render_template("admin/dashboard.html", stats=stats, pending=pending, recent=recent)


def booking_search(q):
    cond, args = search_sql(q, ["st.full_name", "st.email", "e.name", "c.code"], "b.id")
    return query(BOOKING_SELECT + f" WHERE {cond} ORDER BY b.id DESC", args)


@bp.route("/search")
def search():
    q = request.args.get("q", "").strip()
    res = {}
    if q:
        cond, args = search_sql(q, ["full_name", "email"], "id")
        res["students"] = query(f"SELECT * FROM users WHERE role = 'student' AND {cond}", args)
        res["examiners"] = query(f"SELECT * FROM users WHERE role = 'examiner' AND {cond}", args)
        cond, args = search_sql(q, ["e.name", "c.code", "c.name"], "e.id")
        res["exams"] = query(exam_select() + f" WHERE {cond} ORDER BY e.id DESC", args)
        res["bookings"] = booking_search(q)
    return render_template("admin/search.html", q=q, res=res)


# courses
@bp.route("/courses")
def courses():
    q = request.args.get("q", "").strip()
    sql = "SELECT c.*, (SELECT COUNT(*) FROM exam e WHERE e.course_id = c.id) AS exam_count FROM course c"
    args = []
    if q:
        cond, args = search_sql(q, ["c.code", "c.name"], "c.id")
        sql += f" WHERE {cond}"
    return render_template("admin/courses.html", courses=query(sql + " ORDER BY c.code", args), q=q)


@bp.route("/courses/new", methods=["GET", "POST"])
@bp.route("/courses/<int:course_id>/edit", methods=["GET", "POST"])
def course_form(course_id=None):
    course = one_or_404("SELECT * FROM course WHERE id = ?", (course_id,)) if course_id else None
    d = dict(course) if course else {"status": "Active"}
    if request.method == "POST":
        d = request.form
        code, name = d.get("code", "").strip().upper(), d.get("name", "").strip()
        errors = []
        if not code or not name:
            errors.append("Course code and name are required.")
        if code and query("SELECT 1 FROM course WHERE code = ? AND id != ?", (code, course_id or 0), one=True):
            errors.append("Another course already uses this code.")
        if d.get("status") not in ("Active", "Inactive"):
            errors.append("Invalid status.")
        if errors:
            flash_all(errors)
        else:
            desc = d.get("description", "").strip()
            if course:
                execute("UPDATE course SET code=?, name=?, description=?, status=? WHERE id=?",
                        (code, name, desc, d["status"], course_id))
            else:
                execute("INSERT INTO course (code, name, description, status) VALUES (?, ?, ?, ?)",
                        (code, name, desc, d["status"]))
            flash("Course saved.", "success")
            return redirect(url_for("admin.courses"))
    return render_template("admin/course_form.html", d=d, course=course)


@bp.route("/courses/<int:course_id>/delete", methods=["POST"])
def course_delete(course_id):
    one_or_404("SELECT id FROM course WHERE id = ?", (course_id,))
    if query("SELECT 1 FROM exam WHERE course_id = ?", (course_id,), one=True):
        flash("This course has examinations, so it cannot be removed. Mark it Inactive instead.", "warning")
    else:
        execute("DELETE FROM course WHERE id = ?", (course_id,))
        flash("Course removed.", "success")
    return redirect(url_for("admin.courses"))


# examinations
@bp.route("/exams")
def exams():
    q, status = request.args.get("q", "").strip(), request.args.get("status", "")
    conds, args = [], []
    if q:
        c, a = search_sql(q, ["e.name", "c.code", "c.name"], "e.id")
        conds.append(c)
        args += a
    if status:
        conds.append("e.status = ?")
        args.append(status)
    sql = exam_select() + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY e.id DESC"
    return render_template("admin/exams.html", exams=query(sql, args), q=q, status=status,
                           statuses=["Draft", "Slot Creation", "Booking Open", "Closed", "Completed"])


@bp.route("/exams/new", methods=["GET", "POST"])
@bp.route("/exams/<int:exam_id>/edit", methods=["GET", "POST"])
def exam_form(exam_id=None):
    exam = one_or_404("SELECT * FROM exam WHERE id = ?", (exam_id,)) if exam_id else None
    courses = query("SELECT * FROM course WHERE status = 'Active' OR id = ? ORDER BY code", (exam["course_id"] if exam else 0,))
    if exam:
        d = dict(exam)
    else:
        t = date.today()
        d = {"slot_create_start": t.isoformat(), "slot_create_end": (t + timedelta(days=7)).isoformat(),
             "book_start": (t + timedelta(days=8)).isoformat(), "book_end": (t + timedelta(days=15)).isoformat()}
    if request.method == "POST":
        d = request.form
        errors = []
        course_id = to_int(d.get("course_id"))
        if not any(c["id"] == course_id for c in courses):
            errors.append("Select an active course.")
        name = d.get("name", "").strip()
        if not name:
            errors.append("Examination name is required.")
        if d.get("exam_type") not in EXAM_TYPES:
            errors.append("Select a valid examination type.")
        duration, max_marks = to_int(d.get("duration")), to_float(d.get("max_marks"))
        if not duration or duration <= 0:
            errors.append("Duration must be a positive number of minutes.")
        if not max_marks or max_marks <= 0:
            errors.append("Maximum marks must be greater than zero.")
        dates = {k: parse_date(d.get(k)) for k in ("slot_create_start", "slot_create_end", "book_start", "book_end")}
        if not all(dates.values()):
            errors.append("All four timeline dates are required.")
        else:
            if dates["slot_create_start"] > dates["slot_create_end"]:
                errors.append("Slot creation end date cannot be before its start date.")
            if dates["book_start"] > dates["book_end"]:
                errors.append("Booking end date cannot be before its start date.")
        if errors:
            flash_all(errors)
        else:
            vals = (course_id, name, d["exam_type"], duration, max_marks, dates["slot_create_start"],
                    dates["slot_create_end"], dates["book_start"], dates["book_end"])
            if exam:
                execute("UPDATE exam SET course_id=?, name=?, exam_type=?, duration=?, max_marks=?, slot_create_start=?, "
                        "slot_create_end=?, book_start=?, book_end=? WHERE id=?", vals + (exam_id,))
                new_id = exam_id
            else:
                new_id = execute("INSERT INTO exam (course_id, name, exam_type, duration, max_marks, slot_create_start, "
                                 "slot_create_end, book_start, book_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", vals)
            flash("Examination saved.", "success")
            return redirect(url_for("admin.exam_view", exam_id=new_id))
    return render_template("admin/exam_form.html", d=d, exam=exam, courses=courses, types=EXAM_TYPES)


@bp.route("/exams/<int:exam_id>")
def exam_view(exam_id):
    exam = one_or_404(exam_select() + " WHERE e.id = ?", (exam_id,))
    rubrics = query("SELECT * FROM rubric WHERE exam_id = ? ORDER BY id", (exam_id,))
    assigned = query("SELECT u.* FROM users u JOIN exam_examiner x ON x.examiner_id = u.id WHERE x.exam_id = ? "
                     "ORDER BY u.full_name", (exam_id,))
    addable = query("SELECT * FROM users WHERE role = 'examiner' AND status = 'Approved' AND id NOT IN "
                    "(SELECT examiner_id FROM exam_examiner WHERE exam_id = ?) ORDER BY full_name", (exam_id,))
    slots = query(SLOT_SELECT + " WHERE s.exam_id = ? ORDER BY s.slot_date, s.start_time", (exam_id,))
    pending_eval = count("SELECT COUNT(*) FROM booking WHERE exam_id = ? AND status = 'Booked'", (exam_id,))
    actions = [(k, v[2], v[3]) for k, v in ACTIONS.items() if exam["status"] in v[0]]
    return render_template("admin/exam_view.html", exam=exam, rubrics=rubrics, assigned=assigned, addable=addable,
                           slots=slots, pending_eval=pending_eval, actions=actions,
                           rubric_marks=sum(r["max_marks"] for r in rubrics),
                           rubric_weight=sum(r["weightage"] for r in rubrics))


@bp.route("/exams/<int:exam_id>/delete", methods=["POST"])
def exam_delete(exam_id):
    one_or_404("SELECT id FROM exam WHERE id = ?", (exam_id,))
    if count("SELECT COUNT(*) FROM booking WHERE exam_id = ?", (exam_id,)):
        flash("This examination has booking history and cannot be deleted.", "warning")
        return redirect(url_for("admin.exam_view", exam_id=exam_id))
    execute("DELETE FROM exam WHERE id = ?", (exam_id,))
    flash("Examination deleted.", "success")
    return redirect(url_for("admin.exams"))


@bp.route("/exams/<int:exam_id>/status", methods=["POST"])
def exam_status(exam_id):
    exam = one_or_404("SELECT * FROM exam WHERE id = ?", (exam_id,))
    action = ACTIONS.get(request.form.get("action"))
    if not action or exam["status"] not in action[0]:
        flash("That action is not available for the current status.", "warning")
        return redirect(url_for("admin.exam_view", exam_id=exam_id))
    if request.form["action"] == "open_booking" and not count(
            "SELECT COUNT(*) FROM slot WHERE exam_id = ? AND status = 'Available'", (exam_id,)):
        flash("Create at least one available slot before opening student booking.", "warning")
        return redirect(url_for("admin.exam_view", exam_id=exam_id))
    with transaction():
        execute("UPDATE exam SET status = ? WHERE id = ?", (action[1], exam_id))
        if action[1] == "Completed":
            execute("UPDATE slot SET status = 'Completed' WHERE exam_id = ? AND status != 'Cancelled'", (exam_id,))
    flash(f"Examination status is now '{action[1]}'.", "success")
    return redirect(url_for("admin.exam_view", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/publish", methods=["POST"])
def exam_publish(exam_id):
    exam = one_or_404("SELECT * FROM exam WHERE id = ?", (exam_id,))
    if exam["status"] not in ("Closed", "Completed"):
        flash("Results can be published only after booking is closed.", "warning")
    else:
        execute("UPDATE exam SET results_published = ? WHERE id = ?", (0 if exam["results_published"] else 1, exam_id))
        flash("Results are now " + ("hidden from" if exam["results_published"] else "visible to") + " students.", "success")
    return redirect(url_for("admin.exam_view", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/examiners", methods=["POST"])
def exam_assign(exam_id):
    one_or_404("SELECT id FROM exam WHERE id = ?", (exam_id,))
    ex = query("SELECT id FROM users WHERE id = ? AND role = 'examiner' AND status = 'Approved'",
               (to_int(request.form.get("examiner_id")),), one=True)
    if ex:
        execute("INSERT OR IGNORE INTO exam_examiner (exam_id, examiner_id) VALUES (?, ?)", (exam_id, ex["id"]))
        flash("Examiner assigned.", "success")
    else:
        flash("Select an approved examiner.", "danger")
    return redirect(url_for("admin.exam_view", exam_id=exam_id))


@bp.route("/exams/<int:exam_id>/examiners/<int:examiner_id>/remove", methods=["POST"])
def exam_unassign(exam_id, examiner_id):
    if count("SELECT COUNT(*) FROM slot WHERE exam_id = ? AND examiner_id = ? AND status != 'Cancelled'",
             (exam_id, examiner_id)):
        flash("This examiner already has slots for the examination. Change the examiner on those slots first.", "warning")
    else:
        execute("DELETE FROM exam_examiner WHERE exam_id = ? AND examiner_id = ?", (exam_id, examiner_id))
        flash("Examiner removed from the examination.", "success")
    return redirect(url_for("admin.exam_view", exam_id=exam_id))


# rubrics
@bp.route("/exams/<int:exam_id>/rubrics/new", methods=["GET", "POST"])
@bp.route("/exams/<int:exam_id>/rubrics/<int:rubric_id>/edit", methods=["GET", "POST"])
def rubric_form(exam_id, rubric_id=None):
    exam = one_or_404("SELECT * FROM exam WHERE id = ?", (exam_id,))
    rubric = one_or_404("SELECT * FROM rubric WHERE id = ? AND exam_id = ?", (rubric_id, exam_id)) if rubric_id else None
    d = dict(rubric) if rubric else {"weightage": 1}
    if request.method == "POST":
        d = request.form
        crit, desc = d.get("criterion", "").strip(), d.get("description", "").strip()
        mm, w = to_float(d.get("max_marks")), to_float(d.get("weightage"))
        errors = []
        if not crit:
            errors.append("Criterion name is required.")
        if mm is None or mm <= 0:
            errors.append("Maximum marks must be greater than zero.")
        if w is None or w < 0:
            errors.append("Weightage must be zero or more.")
        if errors:
            flash_all(errors)
        else:
            if rubric:
                execute("UPDATE rubric SET criterion=?, max_marks=?, weightage=?, description=? WHERE id=?",
                        (crit, mm, w, desc, rubric_id))
            else:
                execute("INSERT INTO rubric (exam_id, criterion, max_marks, weightage, description) VALUES (?, ?, ?, ?, ?)",
                        (exam_id, crit, mm, w, desc))
            flash("Rubric saved.", "success")
            return redirect(url_for("admin.exam_view", exam_id=exam_id))
    return render_template("admin/rubric_form.html", d=d, exam=exam, rubric=rubric)


@bp.route("/exams/<int:exam_id>/rubrics/<int:rubric_id>/delete", methods=["POST"])
def rubric_delete(exam_id, rubric_id):
    if count("SELECT COUNT(*) FROM score WHERE rubric_id = ?", (rubric_id,)):
        flash("This criterion has already been used in evaluations and cannot be deleted.", "warning")
    else:
        execute("DELETE FROM rubric WHERE id = ? AND exam_id = ?", (rubric_id, exam_id))
        flash("Rubric deleted.", "success")
    return redirect(url_for("admin.exam_view", exam_id=exam_id))


# examiners
EXAMINER_ACTIONS = {"approve": ("Approved", ("Pending",)), "reject": ("Rejected", ("Pending",)),
                    "deactivate": ("Deactivated", ("Approved",)), "reactivate": ("Approved", ("Deactivated", "Rejected"))}


@bp.route("/examiners")
def examiners():
    q, status = request.args.get("q", "").strip(), request.args.get("status", "")
    conds, args = ["u.role = 'examiner'"], []
    if q:
        c, a = search_sql(q, ["u.full_name", "u.email", "u.department"], "u.id")
        conds.append(c)
        args += a
    if status:
        conds.append("u.status = ?")
        args.append(status)
    rows = query("SELECT u.*, (SELECT COUNT(*) FROM exam_examiner x WHERE x.examiner_id = u.id) AS exam_count, "
                 "(SELECT COUNT(*) FROM slot s WHERE s.examiner_id = u.id AND s.status != 'Cancelled') AS slot_count "
                 "FROM users u WHERE " + " AND ".join(conds) + " ORDER BY u.id DESC", args)
    return render_template("admin/examiners.html", examiners=rows, q=q, status=status)


@bp.route("/examiners/new", methods=["GET", "POST"])
def examiner_new():
    d = request.form if request.method == "POST" else {}
    if request.method == "POST":
        name, email, pw = d.get("full_name", "").strip(), d.get("email", "").strip().lower(), d.get("password", "")
        phone, dept = d.get("phone", "").strip(), d.get("department", "").strip()
        errors = []
        if len(name) < 2:
            errors.append("Full name is required.")
        if not EMAIL_RE.match(email):
            errors.append("Enter a valid email address.")
        elif query("SELECT 1 FROM users WHERE email = ?", (email,), one=True):
            errors.append("An account with this email already exists.")
        if len(pw) < 6:
            errors.append("Password must be at least 6 characters.")
        if not PHONE_RE.match(phone):
            errors.append("Phone number must have 10 to 13 digits.")
        if not dept:
            errors.append("Department is required.")
        if errors:
            flash_all(errors)
        else:
            execute("INSERT INTO users (email, password_hash, full_name, role, phone, department, status) "
                    "VALUES (?, ?, ?, 'examiner', ?, ?, 'Approved')", (email, generate_password_hash(pw), name, phone, dept))
            flash("Examiner added and approved.", "success")
            return redirect(url_for("admin.examiners"))
    return render_template("admin/examiner_form.html", d=d)


@bp.route("/examiners/<int:examiner_id>")
def examiner_view(examiner_id):
    ex = one_or_404("SELECT * FROM users WHERE id = ? AND role = 'examiner'", (examiner_id,))
    exams_ = query("SELECT e.*, c.code AS course_code FROM exam e JOIN course c ON c.id = e.course_id "
                   "JOIN exam_examiner x ON x.exam_id = e.id WHERE x.examiner_id = ?", (examiner_id,))
    slots = query(SLOT_SELECT + " WHERE s.examiner_id = ? ORDER BY s.slot_date, s.start_time", (examiner_id,))
    return render_template("admin/examiner_view.html", ex=ex, exams=exams_, slots=slots)


@bp.route("/examiners/<int:examiner_id>/status", methods=["POST"])
def examiner_status(examiner_id):
    ex = one_or_404("SELECT * FROM users WHERE id = ? AND role = 'examiner'", (examiner_id,))
    action = EXAMINER_ACTIONS.get(request.form.get("action"))
    back = request.form.get("next") or url_for("admin.examiners")
    if not action or ex["status"] not in action[1]:
        flash("That action is not available for this examiner.", "warning")
    elif action[0] == "Deactivated" and count(
            "SELECT COUNT(*) FROM booking b JOIN slot s ON s.id = b.slot_id WHERE s.examiner_id = ? AND b.status = 'Booked'",
            (examiner_id,)):
        flash("This examiner still has booked students. Change the examiner on those slots before deactivating.", "warning")
    else:
        execute("UPDATE users SET status = ? WHERE id = ?", (action[0], examiner_id))
        flash(f"{ex['full_name']} is now {action[0].lower()}.", "success")
    return redirect(back)


# students
@bp.route("/students")
def students():
    q = request.args.get("q", "").strip()
    sql = ("SELECT u.*, (SELECT COUNT(*) FROM booking b WHERE b.student_id = u.id) AS booking_count "
           "FROM users u WHERE u.role = 'student'")
    args = []
    if q:
        c, args = search_sql(q, ["u.full_name", "u.email", "u.department"], "u.id")
        sql += f" AND {c}"
    return render_template("admin/students.html", students=query(sql + " ORDER BY u.id DESC", args), q=q)


@bp.route("/students/<int:student_id>")
def student_view(student_id):
    st = one_or_404("SELECT * FROM users WHERE id = ? AND role = 'student'", (student_id,))
    bookings = query(BOOKING_SELECT + " WHERE b.student_id = ? ORDER BY b.id DESC", (student_id,))
    return render_template("admin/student_view.html", st=st, bookings=bookings)


# slots
@bp.route("/slots")
def slots():
    q, status, exam_id = request.args.get("q", "").strip(), request.args.get("status", ""), to_int(request.args.get("exam_id"))
    conds, args = [], []
    if q:
        c, a = search_sql(q, ["e.name", "u.full_name", "c.code"], "s.id")
        conds.append(c)
        args += a
    if status:
        conds.append("s.status = ?")
        args.append(status)
    if exam_id:
        conds.append("s.exam_id = ?")
        args.append(exam_id)
    sql = SLOT_SELECT + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY s.slot_date, s.start_time"
    return render_template("admin/slots.html", slots=query(sql, args), q=q, status=status, exam_id=exam_id,
                           exams=query("SELECT id, name FROM exam ORDER BY name"))


@bp.route("/slots/<int:slot_id>/examiner", methods=["GET", "POST"])
def slot_examiner(slot_id):
    slot = one_or_404(SLOT_SELECT + " WHERE s.id = ?", (slot_id,))
    if slot["status"] in ("Cancelled", "Completed"):
        flash("The examiner cannot be changed on a cancelled or completed slot.", "warning")
        return redirect(url_for("admin.slots"))
    cands = query("SELECT u.* FROM users u JOIN exam_examiner x ON x.examiner_id = u.id WHERE x.exam_id = ? "
                  "AND u.status = 'Approved' AND u.id != ? ORDER BY u.full_name", (slot["exam_id"], slot["examiner_id"]))
    if request.method == "POST":
        new_id = to_int(request.form.get("examiner_id"))
        if not any(c["id"] == new_id for c in cands):
            flash("Select an approved examiner assigned to this examination.", "danger")
        elif examiner_conflict(new_id, slot["slot_date"], slot["start_time"], slot["end_time"]):
            flash("That examiner already has an overlapping slot at this time.", "danger")
        else:
            execute("UPDATE slot SET examiner_id = ? WHERE id = ?", (new_id, slot_id))
            flash("Assigned examiner changed. Existing bookings stay with the slot.", "success")
            return redirect(url_for("admin.slots"))
    return render_template("admin/slot_examiner.html", slot=slot, cands=cands)


# bookings
@bp.route("/bookings")
def bookings():
    q, status, exam_id = request.args.get("q", "").strip(), request.args.get("status", ""), to_int(request.args.get("exam_id"))
    conds, args = [], []
    if q:
        c, a = search_sql(q, ["st.full_name", "st.email", "e.name", "c.code"], "b.id")
        conds.append(c)
        args += a
    if status:
        conds.append("b.status = ?")
        args.append(status)
    if exam_id:
        conds.append("b.exam_id = ?")
        args.append(exam_id)
    sql = BOOKING_SELECT + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY b.id DESC"
    return render_template("admin/bookings.html", bookings=query(sql, args), q=q, status=status, exam_id=exam_id,
                           exams=query("SELECT id, name FROM exam ORDER BY name"))


@bp.route("/bookings/<int:booking_id>/reschedule", methods=["GET", "POST"])
def reschedule(booking_id):
    b = one_or_404(BOOKING_SELECT + " WHERE b.id = ?", (booking_id,))
    if b["status"] != "Booked":
        flash("Only active (booked, not yet evaluated) bookings can be rescheduled.", "warning")
        return redirect(url_for("admin.bookings"))
    options = query(SLOT_SELECT + " WHERE s.exam_id = ? AND s.id != ? AND s.status = 'Available' "
                    "ORDER BY s.slot_date, s.start_time", (b["exam_id"], b["slot_id"]))
    if request.method == "POST":
        try:
            with transaction():
                new = query(SLOT_SELECT + " WHERE s.id = ?", (to_int(request.form.get("slot_id")),), one=True)
                if not new or new["exam_id"] != b["exam_id"] or new["id"] == b["slot_id"]:
                    raise Fail("Choose a different slot of the same examination.")
                if new["status"] != "Available" or new["booked"] >= new["capacity"]:
                    raise Fail("That slot is full or unavailable.")
                if student_conflict(b["student_id"], new, exclude_booking=booking_id):
                    raise Fail("The student already has another booking that overlaps this slot.")
                execute("UPDATE booking SET slot_id = ? WHERE id = ?", (new["id"], booking_id))
                refresh_slot(b["slot_id"])
                refresh_slot(new["id"])
            flash("Booking rescheduled.", "success")
            return redirect(url_for("admin.bookings"))
        except Fail as e:
            flash(str(e), "danger")
    return render_template("admin/reschedule.html", b=b, options=options)
