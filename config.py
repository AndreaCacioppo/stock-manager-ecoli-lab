"""Application configuration.

SECRET_KEY:
Flask signs session cookies with it, so it must be stable across restarts and
across all gunicorn workers. It is stored once in a gitignored file
(instance/secret_key, chmod 600) and read on every start.
"""

import os
import secrets
from datetime import timedelta


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


def load_or_create_secret_key(instance_dir=INSTANCE_DIR):
    """Return a stable secret key, generating and storing it once on first use.

    Stored in instance/secret_key with permissions 0600 (owner read/write only).
    """
    os.makedirs(instance_dir, exist_ok=True)
    key_path = os.path.join(instance_dir, "secret_key")

    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if len(key) < 32:
            raise RuntimeError(
                "instance/secret_key is empty or too short; delete it so a "
                "fresh key is generated on the next start."
            )
        return key

    key = secrets.token_hex(32)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(key_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


class Config:
    """Base configuration used by the running application."""

    SECRET_KEY = load_or_create_secret_key()

    DB_PATH = os.path.join(INSTANCE_DIR, "stock.db")

    LABELS_DIR = os.path.join(BASE_DIR, "labels")

    EXPIRY_WINDOW_DAYS = 30

    WTF_CSRF_ENABLED = True

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
