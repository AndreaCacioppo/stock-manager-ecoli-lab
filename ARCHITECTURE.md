# Architecture

Design, layout, and invariants of this repository

## What this is

A Flask + SQLite web application for managing laboratory stock (reagents,
primers, consumables). Server-rendered Jinja templates, no JavaScript framework,
one CSS file. The user interface is in Italian; enum values are stored in English
and translated for display by the `it` Jinja filter.

## Stack

- Python 3.11+ (pure Python, no C extensions).
- Flask, Flask-Login (auth), Flask-WTF + WTForms (forms + CSRF), Werkzeug
  (password hashing, WSGI), fpdf2 (label PDFs), gunicorn (production server).
- SQLite, single file at `instance/stock.db`, WAL mode.
- Pinned versions in `requirements.txt`.

## Layout

```
app/
  __init__.py     App factory create_app(); get_db(); IT_LABELS; it/itdate filters.
  db.py           Connections, migrations, and every stock mutation. Core module.
  auth.py         User model, login/logout, load_user, admin_required.
  main.py         Routes for all logged-in users (browse views + shared mutations).
  admin.py        Admin-only routes (orders, corrections, log, thresholds, users).
  forms.py        Flask-WTF form definitions.
  labels.py       Batch label PDF generation (fpdf2).
  schema.sql      Migration 1: tables, append-only triggers, indexes.
  static/style.css  Single stylesheet (design tokens in :root).
  templates/      Jinja templates; base.html is the shared shell.
config.py         Config class; SECRET_KEY loading; session/cookie settings.
manage.py         CLI: init-db, create-admin, create-user, list-feedback, reconcile.
wsgi.py           gunicorn entry point (app = create_app()).
deploy/           gunicorn/nginx/systemd configs and the deployment guide.
tests/            Smoke (web flow) and concurrency (conservation under load) tests.
```

## Data model

Tables (migration 1 in `schema.sql`; migrations 2-3 in `db.py` add `feedback` and
`product_thresholds`):

- `users` — accounts; soft-deactivated via `active=0`, never deleted.
- `orders` — one delivery; contains many batches.
- `batches` — identical items from one order. `supply_number` (AUTOINCREMENT) is
  the never-reused identifier the audit trail depends on. `received_total` is the
  conservation anchor.
- `stock` — current quantity of a batch in an active state (`inventory` or `lab`)
  at a location. `UNIQUE(supply_number, state, location)`. A batch can be split
  across states at once (e.g. 47 in inventory, 3 in lab), so a state is a quantity
  of a batch, not a property of the batch.
- `archive` — append-only history of units that left active stock. `quantity` is
  signed; a `correction` returns units and is negative.
- `master_log` — append-only audit trail, written in the same transaction as each
  stock change.

## Invariants to preserve

- Conservation: `received_total == inventory_qty + lab_qty + archived_qty`
  (archived summed signed). Re-derived by `db.reconcile()`.
- `archive` and `master_log` are append-only, enforced by SQL triggers.
- Every stock change goes through `db.run_mutation()` (one `BEGIN IMMEDIATE`
  transaction, retry on a locked database) and uses atomic conditional
  decrements (`UPDATE ... WHERE quantity >= :n`, then check rowcount).
- All state-changing routes are CSRF-protected and `@login_required`; admin
  routes use `@admin_required`.
- Account-management guards (in `db.py`): an admin cannot reset another admin's
  password, deactivate another admin, or change another admin's role; the last
  active admin cannot be deactivated or demoted.
- Migrations are incremental via `PRAGMA user_version` against `SCHEMA_VERSION`,
  and idempotent (`CREATE ... IF NOT EXISTS`). An existing database must keep
  working after an upgrade.

## Conventions

- Stored enum values are English; display them with the `it` filter. Dates store
  as ISO `YYYY-MM-DD`; display with the `itdate` filter (`GG/MM/AAAA`).
- SQL is always parameterized. The resolver's dynamic columns come from the
  `_CRITERIA_COLUMNS` whitelist in `db.py`, never from request input.
- Labels are generated after a transaction commits, never while holding the write
  lock.

## Run and test

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py init-db
.venv/bin/python manage.py create-admin --username alice
.venv/bin/python -m flask --app wsgi run --port 8000      # local dev
.venv/bin/python -m tests.test_smoke                       # web flow
.venv/bin/python -m tests.test_concurrency                 # conservation under load
```

Production deployment (gunicorn + nginx + systemd, HTTPS) is documented in
`deploy/README.md`.

## Notes

- Reconciliation has no web page. It runs nightly via `manage.py reconcile`
  (`deploy/stock-reconcile.timer`) and exits non-zero on a mismatch.
- `instance/` (database + `secret_key`) and `labels/` are gitignored
