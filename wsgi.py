"""Entry point that gunicorn imports.

    gunicorn --workers 3 --bind 127.0.0.1:8000 wsgi:app

`app` is a fully configured Flask application built by the factory.
"""

from app import create_app

app = create_app()
