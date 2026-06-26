"""Authentication, the User model, and role helpers.

Flask-Login provides authentication (who is logged in) via signed session
cookies. Authorisation (admin vs user) is a layer on top namely the
admin_required decorator. Passwords are salted and hashed by Werkzeug and are
never stored or compared in clear text.
"""

from functools import wraps

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session,
    url_for,
)
from flask_login import (
    LoginManager, UserMixin, login_required, login_user, logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import connect
from .forms import LoginForm

login_manager = LoginManager()
login_manager.login_view = "auth.login"

auth_bp = Blueprint("auth", __name__)


class User(UserMixin):
    """Wraps one row of the users table for Flask-Login.

    is_active reflects the soft-delete flag: a deactivated user (active=0) can
    neither log in nor keep using an existing session.
    """

    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.role = row["role"]
        self.active = row["active"]

    @property
    def is_active(self):
        return self.active == 1

    @property
    def is_admin(self):
        return self.role == "admin"

    def get_id(self):
        return str(self.id)


def _db_path():
    """Read the DB path from the running app's config (set in the factory)."""
    from flask import current_app
    return current_app.config["DB_PATH"]


@login_manager.user_loader
def load_user(user_id):
    """Reload a user from the session cookie. Returns None for unknown or
    deactivated accounts, so soft-deleted users lose access immediately.

    Reuses the per-request connection (get_db) instead of opening a separate
    one: Flask-Login calls this inside request handling, where the request
    connection already exists, so one connection open per request is enough."""
    from . import get_db
    row = get_db().execute(
        "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
    ).fetchone()
    return User(row) if row else None


def admin_required(view):
    """Decorator: allow only logged-in admins; otherwise 403."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        from flask_login import current_user
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def create_user(conn, username, password, role):
    """Insert a new account with a hashed password. Returns the new id."""
    conn.execute(
        "INSERT INTO users (username, password_hash, role, active) "
        "VALUES (?, ?, ?, 1)",
        (username, generate_password_hash(password), role),
    )
    return conn.execute("SELECT id FROM users WHERE username = ?",
                        (username,)).fetchone()["id"]


def admin_exists(conn):
    """True if at least one admin account exists (blocks a backdoor admin)."""
    row = conn.execute(
        "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
    ).fetchone()
    return row is not None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        conn = connect(_db_path())
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? AND active = 1",
                (form.username.data,),
            ).fetchone()
        finally:
            conn.close()

        if row and check_password_hash(row["password_hash"], form.password.data):
            login_user(User(row))
            session.permanent = True
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                nxt = url_for("main.dashboard")
            return redirect(nxt)
        flash("Nome utente o password non validi.", "error")
    return render_template("login.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Disconnesso.", "info")
    return redirect(url_for("auth.login"))
