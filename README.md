# Examination Management Portal (EMP)

Flask + Jinja2 + HTML/CSS + Bootstrap 5 (CSS only) + SQLite. No JavaScript is used for any core feature.
The only third-party Python package is **Flask** (SQLite access uses Python's built-in `sqlite3`).

## Run in PyCharm
1. Open this folder as a project, create/select a virtual environment (Python 3.10+).
2. `pip install -r requirements.txt` (PyCharm will offer to do this).
3. Run `app.py`, then open http://127.0.0.1:5000

The database file `emp.sqlite3` and every table are **created programmatically** on first start
(`db.py -> init_db`). Delete the file to reset all data.

**Pre-existing admin:** `admin@emp.com` / `admin123`

## Bootstrap offline
Pages load Bootstrap's CSS from a CDN. For an offline demo, save
https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css as `static/css/bootstrap.min.css`;
the app then uses the local copy automatically.

## Layout
```
app.py            app factory, filters, error pages
db.py             schema (created in code), sqlite helpers, transactions
utils.py          validation, shared SQL, business rules (slot windows, conflicts, scoring)
routes/           auth.py  admin.py  examiner.py  student.py
templates/        base + auth/ admin/ examiner/ student/
static/css/       style.css
```

## Suggested demo flow
1. Admin: create a course -> create an examination (set dates to include today) -> add rubric criteria.
2. Register an examiner (needs admin approval) and a student; approve the examiner; assign the examiner to the examination.
3. Admin: *Open slot creation* -> examiner creates slots -> admin *Open student booking*.
4. Student books a slot -> examiner evaluates -> admin *Close booking* -> *Publish results* -> student sees marks & remarks.
