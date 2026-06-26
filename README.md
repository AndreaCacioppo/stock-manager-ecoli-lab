# Lab Stock Manager

Lab Stock Manager is a Flask and SQLite application for tracking laboratory
supplies from receipt through active use and archive. The application maintains
per-batch quantities across inventory, laboratory, and archive states, with an
audit log for stock movements and administrative account actions.

The user interface is server-rendered and presented in Italian. The database
stores normalized English enum values for states and actions, while display
labels are translated at render time.

## Scope

- Record received orders and their associated batches.
- Move stock from inventory to laboratory using FEFO selection when multiple
  matching batches are available.
- Mark units as consumed, expired, ineligible, removed, restored, or corrected.
- Generate per-batch PDF labels.
- Export the master audit log as CSV or JSONL.
- Manage users with role-based access for administrators and standard users.
- Run reconciliation checks to verify conservation of received quantities.

## Operational Notes

The database is SQLite. Schema creation and migrations are performed with the
management command shown below, not automatically by the web application at
startup.

Application data is stored under `instance/` by default. On first use, the
application creates the SQLite database file and a `SECRET_KEY` file if they do
not already exist.

Architecture details are documented in `ARCHITECTURE.md`. Deployment examples
and service files are in `deploy/`.

## Local Setup

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py init-db
.venv/bin/python manage.py create-admin --username alice
.venv/bin/python -m flask --app wsgi run --debug --port 8000
```

After startup, the local application is available at:

```text
http://127.0.0.1:8000/
```

## Verification

```bash
.venv/bin/python -m tests.test_concurrency
.venv/bin/python -m tests.test_smoke
```

## Operational Checks

```bash
.venv/bin/python manage.py reconcile
sudo -u stock /opt/stock-manager/deploy/backup.sh
```

The reconciliation command exits with a non-zero status if any batch fails the
quantity conservation check. The backup command creates a SQLite snapshot using
the configured production paths.
