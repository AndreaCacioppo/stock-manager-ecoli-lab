"""Application package: the app factory and per-request database connection.

create_app() is the Flask application factory. Blueprints are imported inside
the factory so that `import app.db` (used by the migration CLI and the
concurrency test) does not drag in the whole web stack.

get_db() hands out one SQLite connection per request, stored on flask.g and
closed automatically when the request ends.
"""

import sqlite3

from flask import Flask, g, render_template

from .db import connect

IT_LABELS = {
    "inventory": "magazzino", "lab": "laboratorio", "archive": "archivio",
    "consumed": "consumato", "expired": "scaduto", "ineligible": "non idoneo",
    "removed": "rimosso", "correction": "correzione", "delivered": "consegnato",
    "microbiology": "microbiologia", "primers": "primer", "other": "altro",
    "order": "ordine", "move": "spostamento", "moved": "spostato",
    "remove": "rimozione",
    "correct_intake": "correzione carico", "user_admin": "gestione utenti",
    "user": "utente", "admin": "amministratore", "corrected": "corretto",
}


def get_db():
    """Return this request's SQLite connection, opening it on first use."""
    from flask import current_app
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def create_app(config_object="config.Config", overrides=None):
    """Build and configure the Flask app.

    `config_object` is an import path to a config class (default: production-ish
    config.Config). `overrides` is an optional dict for tests (e.g. a temp DB
    path, a fixed SECRET_KEY, WTF_CSRF_ENABLED=False).
    """
    app = Flask(__name__)
    app.config.from_object(config_object)
    if overrides:
        app.config.update(overrides)

    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=0)

    from flask_wtf import CSRFProtect
    CSRFProtect(app)

    from .auth import login_manager
    login_manager.init_app(app)
    login_manager.login_message = "Accedi per continuare."
    login_manager.login_message_category = "info"

    @app.teardown_appcontext
    def _close_db(exception=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    from .auth import auth_bp
    from .main import main_bp
    from .admin import admin_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)

    @app.context_processor
    def _inject_autocomplete():
        from .db import autocomplete_options
        def autocomplete():
            return autocomplete_options(get_db())
        return {"autocomplete": autocomplete}

    @app.template_filter("it")
    def _it_label(value):
        return IT_LABELS.get(value, value)

    @app.template_filter("itdate")
    def _it_date(value):
        if not value:
            return value
        datepart, _, timepart = str(value).partition("T")
        parts = datepart.split("-")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            year, month, day = parts
            out = f"{day}/{month}/{year}"
            if timepart:
                out += " " + timepart.rstrip("Z")
            return out
        return value

    @app.errorhandler(500)
    def _internal_error(error):
        return render_template("error.html"), 500

    @app.errorhandler(sqlite3.OperationalError)
    def _database_busy(error):
        return render_template("error.html"), 500

    _check_schema_version(app)

    return app


def _check_schema_version(app):
    """Warn (in the app log) if the on-disk database schema is not the expected
    version. Reads PRAGMA user_version; does nothing if the DB file is absent."""
    import os
    from .db import SCHEMA_VERSION

    db_path = app.config.get("DB_PATH")
    if not db_path or not os.path.exists(db_path):
        return
    try:
        conn = connect(db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return
    if version != SCHEMA_VERSION:
        app.logger.warning(
            "Database schema version is %s but the app expects %s. "
            "Run 'python manage.py init-db' to migrate.", version, SCHEMA_VERSION)
