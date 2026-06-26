"""End-to-end web flow exercised with Flask's test client.

Creates an admin, logs in, creates an order, moves units to lab, consumes some,
and views the reconcile page, asserting HTTP 200s and the right quantities at
each step. CSRF is disabled here (standard for the test client); the forms still
carry tokens in real use.

Run with:  .venv/bin/python -m tests.test_smoke   (or via pytest).
"""

import os
import tempfile

from app import create_app, db
from app.auth import create_user


def _build_app(db_path):
    db.init_db(db_path)
    # Seed an admin directly (the CLI does the same thing).
    conn = db.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        create_user(conn, "admin", "secret", "admin")
        conn.execute("COMMIT")
    finally:
        conn.close()

    app = create_app(overrides={
        "DB_PATH": db_path,
        "SECRET_KEY": "test-key",
        "WTF_CSRF_ENABLED": False,
        "TESTING": True,
        "LABELS_DIR": os.path.join(os.path.dirname(db_path), "labels"),
    })
    return app


def run(db_path):
    app = _build_app(db_path)
    client = app.test_client()

    # 1. Login.
    r = client.post("/login", data={"username": "admin", "password": "secret"},
                    follow_redirects=True)
    assert r.status_code == 200, r.status_code
    # Post-login lands on the inventory list ("Magazzino").
    assert b"Magazzino" in r.data

    # 2. Create an order with one batch of 50 petri dishes.
    #    The order form is a FieldList of 5 batch rows; fill row 0, leave rest blank.
    form = {
        "order_date": "2026-06-09",
        "notes": "smoke test order",
        "batches-0-type": "other",
        "batches-0-product_name": "Petri dish",
        "batches-0-manufacturer": "Acme",
        "batches-0-batch_code": "LOT-1",
        "batches-0-expiring_date": "2027-01-01",
        "batches-0-quantity": "50",
        "batches-0-location": "shelf-A",
    }
    r = client.post("/orders/new", data=form, follow_redirects=True)
    assert r.status_code == 200, r.status_code

    conn = db.connect(db_path)
    try:
        supply_number = conn.execute(
            "SELECT supply_number FROM batches WHERE product_name = 'Petri dish'"
        ).fetchone()[0]
        inv = conn.execute(
            "SELECT quantity FROM stock WHERE supply_number=? AND state='inventory'",
            (supply_number,)).fetchone()[0]
        assert inv == 50, f"expected 50 in inventory, got {inv}"
    finally:
        conn.close()

    # 3. Move 3 units to the lab (identified by product name only).
    r = client.post("/move", data={"product_name": "Petri dish", "quantity": "3"},
                    follow_redirects=True)
    assert r.status_code == 200, r.status_code

    # 4. Consume 1 unit from the lab.
    r = client.post("/consume",
                    data={"product_name": "Petri dish", "quantity": "1",
                          "from_state": "lab"},
                    follow_redirects=True)
    assert r.status_code == 200, r.status_code

    # Verify quantities: inventory 47, lab 2, archived 1.
    conn = db.connect(db_path)
    try:
        inv = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM stock WHERE supply_number=? AND state='inventory'",
            (supply_number,)).fetchone()[0]
        lab = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM stock WHERE supply_number=? AND state='lab'",
            (supply_number,)).fetchone()[0]
        arch = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM archive WHERE supply_number=?",
            (supply_number,)).fetchone()[0]
        assert (inv, lab, arch) == (47, 2, 1), f"got inv={inv} lab={lab} arch={arch}"

        rows = db.reconcile(conn)
        assert all(r["ok"] for r in rows), f"reconcile failed: {rows}"
    finally:
        conn.close()

    # 5. Download the most recent label. (Reconciliation has no web page; the
    #    conservation check is verified directly via db.reconcile() above.)
    r = client.get(f"/label/{supply_number}")
    assert r.status_code == 200 and r.data[:4] == b"%PDF", "label was not a PDF"

    # 6. Log export works.
    r = client.get("/admin/log/export?fmt=csv")
    assert r.status_code == 200 and b"timestamp" in r.data, "csv export failed"

    print(f"OK: inventory={inv} lab={lab} archived={arch}; reconcile ok; "
          f"label PDF + log export served")


def run_csrf(db_path):
    """With CSRF protection ON, a token-less POST must be rejected (400).

    Checks one representative state-changing route (/move). CSRFProtect runs as
    a global before_request, ahead of @login_required, so it fires even without
    a session; the main smoke test disables CSRF (as test clients normally do),
    so this is the one place that exercises the protection.
    """
    app = _build_app(db_path)
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()

    # No csrf_token field -> Flask-WTF must reject the state-changing POST.
    r = client.post("/move", data={"product_name": "X", "quantity": "1"})
    assert r.status_code == 400, f"CSRF not enforced: got {r.status_code}"
    print("OK: CSRF-less POST rejected with 400")


def test_smoke_web_flow():
    """pytest entry point."""
    with tempfile.TemporaryDirectory() as d:
        run(os.path.join(d, "stock.db"))


def test_csrf_enforced():
    """pytest entry point for the CSRF check."""
    with tempfile.TemporaryDirectory() as d:
        run_csrf(os.path.join(d, "stock.db"))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(os.path.join(d, "stock.db"))
        print("smoke test passed")
    with tempfile.TemporaryDirectory() as d:
        run_csrf(os.path.join(d, "stock.db"))
        print("csrf test passed")
