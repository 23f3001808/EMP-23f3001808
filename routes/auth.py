from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import execute, query
from utils import EMAIL_RE, PHONE_RE, flash_all

bp = Blueprint("auth", __name__)


def home_for(user):
    return url_for({"admin": "admin.dashboard", "examiner": "examiner.dashboard", "student": "student.dashboard"}[user["role"]])


@bp.route("/")
def index():
    return redirect(home_for(g.user) if g.user else url_for("auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(home_for(g.user))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash("Invalid email or password.", "danger")
        elif user["status"] in ("Deactivated", "Rejected"):
            flash(f"Your account is {user['status'].lower()}. Please contact the administrator.", "danger")
        else:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(home_for(user))
    return render_template("auth/login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(home_for(g.user))
    d = request.form if request.method == "POST" else {"role": "student"}
    if request.method == "POST":
        name, email = d.get("full_name", "").strip(), d.get("email", "").strip().lower()
        pw, role = d.get("password", ""), d.get("role")
        phone, dept = d.get("phone", "").strip(), d.get("department", "").strip()
        errors = []
        if len(name) < 2:
            errors.append("Please enter your full name.")
        if not EMAIL_RE.match(email):
            errors.append("Please enter a valid email address.")
        elif query("SELECT 1 FROM users WHERE email = ?", (email,), one=True):
            errors.append("An account with this email already exists.")
        if len(pw) < 6:
            errors.append("Password must be at least 6 characters.")
        if role not in ("student", "examiner"):
            errors.append("Choose whether you are registering as a student or an examiner.")
        if not PHONE_RE.match(phone):
            errors.append("Phone number must have 10 to 13 digits.")
        if not dept:
            errors.append("Department is required.")
        if errors:
            flash_all(errors)
        else:
            status = "Pending" if role == "examiner" else "Active"
            execute("INSERT INTO users (email, password_hash, full_name, role, phone, department, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (email, generate_password_hash(pw), name, role, phone, dept, status))
            flash("Registration successful. " + ("Your account needs admin approval before you can use the dashboard."
                                                 if role == "examiner" else "You can now log in."), "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", d=d)


@bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
