import os
from datetime import datetime

from flask import Flask, g, render_template, session

from db import close_db, get_db, init_db, query
from utils import booking_open, slot_creation_open, today


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("EMP_SECRET_KEY", "dev-secret-change-me")
    app.teardown_appcontext(close_db)

    from routes.auth import bp as auth_bp
    from routes.admin import bp as admin_bp
    from routes.examiner import bp as examiner_bp
    from routes.student import bp as student_bp
    for bp in (auth_bp, admin_bp, examiner_bp, student_bp):
        app.register_blueprint(bp)

    @app.before_request
    def load_user():
        uid = session.get("user_id")
        g.user = query("SELECT * FROM users WHERE id = ?", (uid,), one=True) if uid else None
        if uid and g.user is None:
            session.clear()

    @app.context_processor
    def inject():
        local_css = os.path.exists(os.path.join(app.static_folder, "css", "bootstrap.min.css"))
        return dict(today=today(), slot_creation_open=slot_creation_open, booking_open=booking_open,
                    local_bootstrap=local_css)

    @app.template_filter("fdate")
    def fdate(v):
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d %b %Y")
        except ValueError:
            return v or "—"

    @app.template_filter("ftime")
    def ftime(v):
        return str(v)[:5] if v else "—"

    @app.template_filter("fdt")
    def fdt(v):
        try:
            return datetime.strptime(str(v)[:16], "%Y-%m-%d %H:%M").strftime("%d %b %Y, %H:%M")
        except ValueError:
            return v or "—"

    @app.template_filter("marks")
    def marks(v):
        return f"{v:g}" if v is not None else "—"

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, message="You do not have access to this page."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, message="We could not find what you were looking for."), 404

    init_db(app)  # creates emp.sqlite3, all tables and the pre-existing admin account
    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
