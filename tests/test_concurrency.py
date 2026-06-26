"""Conservation under concurrency: quantity stays conserved under load.

Seed one batch with 100 units in inventory (received_total = 100 too, or the
reconciliation check would fail for an unrelated reason). Spawn ~20 threads,
each with its OWN connection, each moving 1 unit inventory -> lab. Then assert:
  - inventory + lab == 100   (no unit lost or duplicated), and
  - reconcile() reports ok for that batch.

Exercises the concurrency design under load. Run
with:  .venv/bin/python -m tests.test_concurrency   (or via pytest).
"""

import os
import tempfile
import threading

from app import db


def _seed_batch(db_path):
    """Create one order + one batch with 100 units in inventory."""
    conn = db.connect(db_path)
    try:
        # received_total is set to the batch quantity (100) by create_order, so
        # reconciliation has the right anchor.
        order_number, supply_numbers = db.create_order(
            conn,
            user_id=None,
            order_date="2026-06-09",
            notes="concurrency seed",
            batches=[{
                "type": "other",
                "product_name": "Petri dish",
                "manufacturer": "Acme",
                "batch_code": "LOT-1",
                "expiring_date": "2027-01-01",
                "quality_flag": 0,
                "quantity": 100,
                "location": "shelf-A",
            }],
        )
        return supply_numbers[0]
    finally:
        conn.close()


def run(db_path):
    db.init_db(db_path)
    supply_number = _seed_batch(db_path)

    n_threads = 20
    errors = []

    def worker():
        # Each thread uses its own connection, as a separate gunicorn worker would.
        conn = db.connect(db_path)
        try:
            db.move_to_lab(conn, user_id=None,
                           criteria={"supply_number": supply_number}, qty=1)
        except Exception as e:  # collect, don't swallow
            errors.append(repr(e))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads raised: {errors}"

    # Verify conservation.
    conn = db.connect(db_path)
    try:
        inv = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM stock "
            "WHERE supply_number = ? AND state = 'inventory'", (supply_number,)
        ).fetchone()[0]
        lab = conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM stock "
            "WHERE supply_number = ? AND state = 'lab'", (supply_number,)
        ).fetchone()[0]

        assert inv + lab == 100, f"conservation broken: inv={inv} lab={lab}"
        assert lab == n_threads, f"expected {n_threads} moved, got lab={lab}"

        rows = db.reconcile(conn)
        batch_row = next(r for r in rows if r["supply_number"] == supply_number)
        assert batch_row["ok"], f"reconcile failed: {batch_row}"
    finally:
        conn.close()

    print(f"OK: inventory={inv} lab={lab} (sum={inv + lab}), reconcile ok")
    return inv, lab


def test_concurrency_conserves_quantity():
    """pytest entry point."""
    with tempfile.TemporaryDirectory() as d:
        run(os.path.join(d, "stock.db"))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        run(os.path.join(d, "stock.db"))
        print("concurrency test passed")
