"""Database layer: connections, schema migrations, and stock mutations.

All stock changes go through run_mutation(), which wraps the work in one
BEGIN IMMEDIATE transaction (write lock taken up front) and retries on a locked
database. Inside it, each change is an atomic conditional decrement
(UPDATE ... WHERE quantity >= :n, then check rowcount == 1) plus the matching
increment or archive insert plus the master_log insert, so state and audit
commit together or not at all.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

SCHEMA_VERSION = 3

MAX_RETRIES = 5
BACKOFF_SECONDS = 0.05

# Inventory location used for units added by intake corrections.
CORRECTED_LOCATION = "corrected"


class StockError(Exception):
    """Base class for stock-operation errors."""


class InsufficientStock(StockError):
    """Requested quantity is not available (nothing was changed)."""


class StockBusy(StockError):
    """The database stayed locked after all retries; the caller should retry."""


def connect(db_path):
    """Open a connection with the per-connection PRAGMAs set first.

    isolation_level=None puts the connection in autocommit mode, so the code
    controls transactions explicitly with BEGIN IMMEDIATE. The two PRAGMAs must
    run before any statement and outside a transaction, or they silently no-op.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now():
    """ISO-8601 UTC timestamp at second precision (lexically sortable, human-readable)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path):
    """Create/upgrade the database. Sets WAL once, then runs migrations.

    journal_mode = WAL is persistent (stored in the DB file), so it is set once
    here at init, not on every connection.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        _run_migrations(conn)
    finally:
        conn.close()


def _run_migrations(conn):
    """Apply any migrations whose version is newer than the DB's user_version.

    Each step (after the initial schema load) runs inside its own explicit
    transaction so the table creation and the matching user_version bump commit
    together. SQLite supports transactional DDL, so a crash mid-step either rolls
    the whole step back or leaves the version already advanced — it can never
    leave a created table behind a stale version (which used to wedge init-db on
    the next run). The CREATE TABLE statements are also IF NOT EXISTS, so even a
    re-run is harmless.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version < 1:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.execute("PRAGMA user_version = 1")

    if version < 2:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS feedback ("
                "  id        INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  user_id   INTEGER REFERENCES users(id),"
                "  message   TEXT NOT NULL,"
                "  page      TEXT,"
                "  timestamp TEXT NOT NULL"
                ")"
            )
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if version < 3:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS product_thresholds ("
                "  product_name TEXT PRIMARY KEY,"
                "  min_qty      INTEGER NOT NULL CHECK (min_qty >= 0)"
                ")"
            )
            conn.execute("PRAGMA user_version = 3")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _safe_rollback(conn):
    """Roll back without raising if there is nothing to roll back."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def run_mutation(conn, work):
    """Run `work(cursor)` inside one BEGIN IMMEDIATE transaction, with retry.

    `work` does all the guarded decrements / inserts and returns whatever the
    caller needs. On a transient "database is locked" error the transaction is
    rolled back and retried with a short backoff; any other error aborts and
    rolls back.
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            result = work(cur)
            cur.execute("COMMIT")
            return result
        except sqlite3.OperationalError as e:
            _safe_rollback(conn)
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                last_error = e
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
                continue
            raise
        except Exception:
            _safe_rollback(conn)
            raise
    raise StockBusy("database stayed locked after retries") from last_error


def _decrement(cur, supply_number, state, location, n):
    """Atomic conditional decrement of one stock line. Raises if it can't apply.

    The WHERE clause includes `quantity >= :n`, so rowcount == 0 cleanly means
    "not enough stock", with no separate read-then-write step to race on.
    """
    cur.execute(
        "UPDATE stock SET quantity = quantity - :n "
        "WHERE supply_number = :s AND state = :st AND location = :loc "
        "AND quantity >= :n",
        {"n": n, "s": supply_number, "st": state, "loc": location},
    )
    if cur.rowcount != 1:
        raise InsufficientStock(
            f"impossibile prelevare {n} dal lotto {supply_number} "
            f"({state}/{location})"
        )


def _increment(cur, supply_number, state, location, n):
    """Add n units to a stock line, creating it if absent (upsert)."""
    cur.execute(
        "INSERT INTO stock (supply_number, state, quantity, location) "
        "VALUES (:s, :st, :n, :loc) "
        "ON CONFLICT (supply_number, state, location) "
        "DO UPDATE SET quantity = quantity + excluded.quantity",
        {"s": supply_number, "st": state, "n": n, "loc": location},
    )


def _archive(cur, supply_number, quantity, causale, user_id):
    """Append a row to the archive (signed quantity; see schema.sql).

    Returns the new archive row id, so an out-flow can record it in its
    master_log details and a later reversal/restore can be tied back to the exact
    archived event (keeps the Archivio 'net' view consistent with reconcile)."""
    cur.execute(
        "INSERT INTO archive (supply_number, quantity, causale, timestamp, user_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (supply_number, quantity, causale, _now(), user_id),
    )
    return cur.lastrowid


def _log(cur, user_id, action, supply_number, from_state, to_state,
         quantity, causale, ref_log_id=None, details=None):
    """Append a master_log row and return its id."""
    details_json = json.dumps(details) if details is not None else None
    cur.execute(
        "INSERT INTO master_log "
        "(timestamp, user_id, action, supply_number, from_state, to_state, "
        " quantity, causale, ref_log_id, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_now(), user_id, action, supply_number, from_state, to_state,
         quantity, causale, ref_log_id, details_json),
    )
    return cur.lastrowid


_CRITERIA_COLUMNS = {
    "supply_number": "b.supply_number",
    "product_name": "b.product_name",
    "manufacturer": "b.manufacturer",
    "type": "b.type",
    "batch_code": "b.batch_code",
    "quality_flag": "b.quality_flag",
}


def _resolve(cur, criteria, qty, state):
    """Resolve a request to a list of (supply_number, location, take) picks,
    where `location` is each pick's SOURCE location within `state`.

    FEFO with spill: order candidate stock lines by expiring_date
    (NULLs sort last, treated as never expiring), then lowest supply_number, take greedily
    across them until `qty` is satisfied. If total available < qty, raise
    InsufficientStock and change nothing (caller is inside a transaction).
    """
    where = ["s.state = :state", "s.quantity > 0"]
    params = {"state": state}
    for key, value in criteria.items():
        if key not in _CRITERIA_COLUMNS:
            raise StockError(f"unknown batch attribute: {key}")
        where.append(f"{_CRITERIA_COLUMNS[key]} = :{key}")
        params[key] = value

    sql = (
        "SELECT s.supply_number, s.location, s.quantity "
        "FROM stock s JOIN batches b ON b.supply_number = s.supply_number "
        "WHERE " + " AND ".join(where) +
        " ORDER BY b.expiring_date IS NULL, b.expiring_date, s.supply_number"
    )
    rows = cur.execute(sql, params).fetchall()

    picks = []
    remaining = qty
    for row in rows:
        if remaining <= 0:
            break
        take = min(row["quantity"], remaining)
        picks.append((row["supply_number"], row["location"], take))
        remaining -= take

    if remaining > 0:
        raise InsufficientStock(
            f"richiesti {qty} ma disponibili solo {qty - remaining}"
        )
    return picks


def create_order(conn, user_id, order_date, notes, batches):
    """Insert an order plus its batches, each landing as inventory stock.

    `batches` is a list of dicts with keys: type, product_name, manufacturer,
    batch_code, expiring_date, quality_flag, quantity, location.
    Returns (order_number, [supply_number, ...]).
    """
    def work(cur):
        cur.execute(
            "INSERT INTO orders (order_date, created_by, created_at, notes) "
            "VALUES (?, ?, ?, ?)",
            (order_date, user_id, _now(), notes),
        )
        order_number = cur.lastrowid

        supply_numbers = []
        for b in batches:
            # Database-level guard for the received_total conservation anchor.
            if b["quantity"] is None or b["quantity"] < 1:
                raise StockError("quantità lotto non valida")
            cur.execute(
                "INSERT INTO batches "
                "(order_number, type, product_name, manufacturer, batch_code, "
                " expiring_date, quality_flag, received_total, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_number, b["type"], b["product_name"], b.get("manufacturer"),
                 b.get("batch_code"), b.get("expiring_date") or None,
                 int(b.get("quality_flag", 0)), b["quantity"], _now()),
            )
            supply_number = cur.lastrowid
            location = b.get("location") or "inventory"
            _increment(cur, supply_number, "inventory", location, b["quantity"])
            _log(cur, user_id, "order", supply_number, None, "inventory",
                 b["quantity"], "delivered",
                 details={"order_number": order_number, "location": location})
            supply_numbers.append(supply_number)
        return order_number, supply_numbers

    return run_mutation(conn, work)


def move_to_lab(conn, user_id, criteria, qty, to_location="lab"):
    """Move `qty` units inventory -> lab, resolving criteria FEFO with spill.

    Returns the list of (supply_number, location, take) picks. Labels are
    generated by the caller after the transaction commits, so disk I/O never
    holds the database write lock.
    """
    def work(cur):
        picks = _resolve(cur, criteria, qty, "inventory")
        for supply_number, location, take in picks:
            _decrement(cur, supply_number, "inventory", location, take)
            _increment(cur, supply_number, "lab", to_location, take)
            _log(cur, user_id, "move", supply_number, "inventory", "lab", take, "moved")
        return picks

    return run_mutation(conn, work)


def archive_units(conn, user_id, criteria, qty, causale, from_state="lab"):
    """Move `qty` units from active stock to the archive (consumed/expired/...).

    `causale` is one of consumed | expired | ineligible. Resolves FEFO with
    spill from `from_state` (default 'lab'). Returns the picks.
    """
    if causale not in ("consumed", "expired", "ineligible"):
        raise StockError(f"archive_units does not handle causale {causale!r}")

    def work(cur):
        picks = _resolve(cur, criteria, qty, from_state)
        for supply_number, location, take in picks:
            _decrement(cur, supply_number, from_state, location, take)
            archive_id = _archive(cur, supply_number, take, causale, user_id)
            _log(cur, user_id, causale, supply_number, from_state, "archive",
                 take, causale, details={"archive_id": archive_id})
        return picks

    return run_mutation(conn, work)


def remove_batch(conn, user_id, supply_number):
    """Admin: remove a whole batch -> archive (causale='removed'). No hard delete.

    Archives all remaining active units (inventory + lab) of the batch in one
    transaction. The current quantities are read inside BEGIN IMMEDIATE; the
    write lock is already held, so no other writer can change them first.
    """
    def work(cur):
        rows = cur.execute(
            "SELECT state, location, quantity FROM stock "
            "WHERE supply_number = ? AND quantity > 0",
            (supply_number,),
        ).fetchall()
        total = 0
        for r in rows:
            _decrement(cur, supply_number, r["state"], r["location"], r["quantity"])
            total += r["quantity"]
        if total == 0:
            raise StockError("il lotto non ha unità attive da rimuovere")
        archive_id = _archive(cur, supply_number, total, "removed", user_id)
        _log(cur, user_id, "remove", supply_number, None, "archive", total, "removed",
             details={"archive_id": archive_id})
        return total

    return run_mutation(conn, work)


def correct_intake(conn, user_id, supply_number, new_total, reason):
    """Admin: auditably correct received_total (e.g. 50 recorded, 48 arrived).

    Adjusts inventory by the same delta so reconciliation stays balanced, and
    writes a master_log 'correction' (old -> new, actor, reason). No archive row:
    the difference is units that never existed (or newly found), not units
    consumed.
    """
    def work(cur):
        row = cur.execute(
            "SELECT received_total FROM batches WHERE supply_number = ?",
            (supply_number,),
        ).fetchone()
        if row is None:
            raise StockError(f"lotto inesistente: {supply_number}")
        old_total = row["received_total"]
        delta = new_total - old_total

        if delta < 0:
            # Missing units are removed from inventory locations for this batch.
            needed = -delta
            inv_rows = cur.execute(
                "SELECT location, quantity FROM stock "
                "WHERE supply_number = ? AND state = 'inventory' AND quantity > 0 "
                "ORDER BY quantity DESC",
                (supply_number,),
            ).fetchall()
            for r in inv_rows:
                if needed <= 0:
                    break
                take = min(r["quantity"], needed)
                _decrement(cur, supply_number, "inventory", r["location"], take)
                needed -= take
            if needed > 0:
                raise InsufficientStock(
                    "impossibile ridurre il carico sotto le unità già in "
                    "laboratorio o archivio"
                )
        elif delta > 0:
            # Newly found units enter inventory at the correction location.
            _increment(cur, supply_number, "inventory", CORRECTED_LOCATION, delta)

        cur.execute(
            "UPDATE batches SET received_total = ? WHERE supply_number = ?",
            (new_total, supply_number),
        )
        _log(cur, user_id, "correct_intake", supply_number, None, None, delta,
             "correction",
             details={"old_total": old_total, "new_total": new_total,
                      "reason": reason})
        return delta

    return run_mutation(conn, work)


def reverse_entry(conn, user_id, ref_log_id, supply_number, quantity,
                  to_state, reason):
    """Admin: compensating entry that returns units from archive to active stock.

    Example: "consumed 5, really 2" returns 3 to lab. Writes a negative
    archive row (causale='correction') so reconciliation nets it out, plus a
    master_log 'correction' linked to the original via ref_log_id. Both the
    original entry and its correction remain visible (QA traceability).
    """
    location = "lab" if to_state == "lab" else "inventory"

    def work(cur):
        # Referenced corrections must target an existing log row for this batch.
        ref = cur.execute(
            "SELECT supply_number, quantity, details FROM master_log WHERE id = ?",
            (ref_log_id,),
        ).fetchone()
        if ref is None or ref["supply_number"] != supply_number:
            raise StockError(
                "la voce di registro indicata non esiste o non appartiene a "
                "questo lotto"
            )

        # The archive row this entry created (if any). Both reversals and
        # restore-page restorations record details.ref_archive_id, so capping
        # against this row counts returns made through either path.
        ref_archive_id = None
        if ref["details"]:
            try:
                ref_archive_id = json.loads(ref["details"]).get("archive_id")
            except (ValueError, TypeError):
                ref_archive_id = None

        # Per-entry cap: return at most the original quantity, minus what has
        # already been returned for the same archived event (by either path).
        if ref["quantity"] is not None:
            if ref_archive_id is not None:
                already_returned = cur.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM master_log "
                    "WHERE json_extract(details, '$.ref_archive_id') = ?",
                    (ref_archive_id,),
                ).fetchone()[0]
            else:
                already_returned = cur.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM master_log WHERE ref_log_id = ?",
                    (ref_log_id,),
                ).fetchone()[0]
            entry_remaining = ref["quantity"] - already_returned
            if quantity > entry_remaining:
                raise InsufficientStock(
                    f"questa voce può restituire al massimo {entry_remaining} unità"
                )

        # Batch-level guard against returning more units than are net archived.
        net_archived = cur.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM archive WHERE supply_number = ?",
            (supply_number,),
        ).fetchone()[0]
        if quantity > net_archived:
            raise InsufficientStock(
                f"impossibile restituire {quantity} unità: solo {net_archived} "
                f"in archivio per il lotto {supply_number}"
            )

        # Preserve archive-row linkage for net archive and restore calculations.
        details = {"reason": reason}
        if ref_archive_id is not None:
            details["ref_archive_id"] = ref_archive_id

        _increment(cur, supply_number, to_state, location, quantity)
        _archive(cur, supply_number, -quantity, "correction", user_id)
        _log(cur, user_id, "correction", supply_number, "archive", to_state,
             quantity, "correction", ref_log_id=ref_log_id, details=details)

    return run_mutation(conn, work)


def restored_so_far(conn, archive_id):
    """How many units have already been restored FROM this archive row.

    A restore logs a master_log 'correction' with details.ref_archive_id pointing
    back at the row, so summing those gives the amount already brought back."""
    return conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM master_log "
        "WHERE json_extract(details, '$.ref_archive_id') = ?",
        (archive_id,),
    ).fetchone()[0]


def restore_to_inventory(conn, user_id, archive_id, quantity):
    """Admin: bring `quantity` units that were archived BY MISTAKE back into
    inventory (a partial restore is allowed).

    Bounds: 1 <= quantity <= (this row's units not yet restored), and never more
    than the batch's net archived amount — so stock is never invented and nothing
    goes below zero. Records a compensating archive row (causale='correction',
    negative) plus a master_log entry, so conservation and the audit trail stay
    intact; the original (mistaken) row is never deleted. Returns units restored.
    """
    def work(cur):
        row = cur.execute(
            "SELECT supply_number, quantity FROM archive WHERE id = ?",
            (archive_id,),
        ).fetchone()
        if row is None:
            raise StockError("voce di archivio inesistente")
        if row["quantity"] <= 0:
            raise StockError("questa voce non è ripristinabile")
        if quantity < 1:
            raise StockError("quantità non valida")
        supply_number = row["supply_number"]
        already = cur.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM master_log "
            "WHERE json_extract(details, '$.ref_archive_id') = ?",
            (archive_id,),
        ).fetchone()[0]
        remaining = row["quantity"] - already
        if quantity > remaining:
            raise StockError(
                f"puoi ripristinare al massimo {remaining} unità da questa voce")
        # Batch-level guard against restoring more units than are net archived.
        net_archived = cur.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM archive WHERE supply_number = ?",
            (supply_number,),
        ).fetchone()[0]
        if quantity > net_archived:
            raise InsufficientStock(
                f"impossibile ripristinare {quantity}: solo {net_archived} unità "
                f"in archivio per il lotto {supply_number}")
        _increment(cur, supply_number, "inventory", "inventory", quantity)
        _archive(cur, supply_number, -quantity, "correction", user_id)
        _log(cur, user_id, "correction", supply_number, "archive", "inventory",
             quantity, "correction",
             details={"reason": "ripristino in magazzino", "ref_archive_id": archive_id})
        return quantity

    return run_mutation(conn, work)


def reconcile(conn):
    """Return one row per batch with the conservation identity checked, plus two
    extra invariants the identity alone cannot see.

    received_total == inventory_qty + lab_qty + archived_qty
    where archived_qty sums the SIGNED archive.quantity (net of corrections).

    Because that sum is signed, a symmetric corruption (+x into active stock and
    -x into archive) nets to zero and would still pass the identity, so it also
    requires archived_qty >= 0 and inventory + lab <= received_total. These
    two extra checks only catch the ASYMMETRIC / over-archival case (they fire
    when x > current net archived); a same-size +stock/-archive edit with
    x <= net archived is itself conservation-preserving (it is exactly what a
    legitimate restore/reversal does) and is not, and need not be, flagged here.
    Each row is a dict including ok=True/False and a `reason` string naming any
    failed check.
    """
    sql = (
        "SELECT b.supply_number, b.product_name, b.received_total, "
        "  COALESCE((SELECT SUM(quantity) FROM stock "
        "            WHERE supply_number = b.supply_number AND state = 'inventory'), 0) AS inventory_qty, "
        "  COALESCE((SELECT SUM(quantity) FROM stock "
        "            WHERE supply_number = b.supply_number AND state = 'lab'), 0) AS lab_qty, "
        "  COALESCE((SELECT SUM(quantity) FROM archive "
        "            WHERE supply_number = b.supply_number), 0) AS archived_qty "
        "FROM batches b ORDER BY b.supply_number"
    )
    results = []
    for row in conn.execute(sql).fetchall():
        accounted = row["inventory_qty"] + row["lab_qty"] + row["archived_qty"]
        item = dict(row)
        item["accounted"] = accounted

        problems = []
        if accounted != row["received_total"]:
            problems.append("identity")
        if row["archived_qty"] < 0:
            problems.append("archived<0")
        if row["inventory_qty"] + row["lab_qty"] > row["received_total"]:
            problems.append("active>received")

        item["ok"] = not problems
        item["reason"] = ", ".join(problems)
        results.append(item)
    return results


def list_users(conn):
    """Return every account (id, username, role, active) for the admin UI."""
    rows = conn.execute(
        "SELECT id, username, role, active FROM users ORDER BY username"
    ).fetchall()
    return [dict(r) for r in rows]


def _active_admin_count(cur):
    """How many active admins exist right now (call inside the transaction)."""
    return cur.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
    ).fetchone()[0]


def _log_user_admin(cur, actor_id, kind, target_username, extra=None):
    """Append one 'user_admin' audit row. Passwords are not stored in details."""
    details = {"kind": kind, "target": target_username}
    if extra:
        details.update(extra)
    _log(cur, actor_id, "user_admin", None, None, None, None, None, details=details)


def admin_create_user(conn, actor_id, username, password, role):
    """Admin-initiated account creation (audited). Returns the new user id."""
    if role not in ("admin", "user"):
        raise StockError(f"invalid role: {role!r}")

    def work(cur):
        # Username uniqueness is checked inside the write transaction.
        if cur.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise StockError(f"nome utente «{username}» già esistente")
        cur.execute(
            "INSERT INTO users (username, password_hash, role, active) "
            "VALUES (?, ?, ?, 1)",
            (username, generate_password_hash(password), role),
        )
        new_id = cur.lastrowid
        _log_user_admin(cur, actor_id, "create", username, {"role": role})
        return new_id

    return run_mutation(conn, work)


def set_user_active(conn, actor_id, target_id, active):
    """Activate (active=1) or deactivate (active=0) an account. Audited.

    An admin may not deactivate another admin's account (prevents admins
    from locking each other out); deactivating regular users is still allowed,
    as is the last-admin guard on one's own account.
    """
    def work(cur):
        row = cur.execute(
            "SELECT username, role, active FROM users WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise StockError("utente inesistente")
        if active == 0 and row["role"] == "admin" and target_id != actor_id:
            raise StockError("non puoi disattivare l'account di un altro amministratore")
        if active == 0 and row["role"] == "admin" and row["active"] == 1:
            if _active_admin_count(cur) <= 1:
                raise StockError("non puoi disattivare l'ultimo amministratore attivo")
        cur.execute("UPDATE users SET active = ? WHERE id = ?", (active, target_id))
        _log_user_admin(cur, actor_id,
                        "reactivate" if active == 1 else "deactivate",
                        row["username"])

    return run_mutation(conn, work)


def set_user_role(conn, actor_id, target_id, role):
    """Change an account's role (admin/user). Audited.

    An admin may not change another admin's role, the same protection as the
    deactivate / password-reset paths. Without it, admin A could demote admin B
    to 'user' and then deactivate or reset B, bypassing those guards. Also
    refuses to demote the last active admin.
    """
    if role not in ("admin", "user"):
        raise StockError(f"invalid role: {role!r}")

    def work(cur):
        row = cur.execute(
            "SELECT username, role, active FROM users WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise StockError("utente inesistente")
        # In-app role changes cannot promote regular accounts to admin.
        if role == "admin" and row["role"] != "admin":
            raise StockError(
                "la promozione ad amministratore è consentita solo da riga di comando")
        if row["role"] == "admin" and target_id != actor_id:
            raise StockError("non puoi cambiare il ruolo di un altro amministratore")
        if role == "user" and row["role"] == "admin" and row["active"] == 1:
            if _active_admin_count(cur) <= 1:
                raise StockError("non puoi declassare l'ultimo amministratore attivo")
        cur.execute("UPDATE users SET role = ? WHERE id = ?", (role, target_id))
        _log_user_admin(cur, actor_id, "role", row["username"],
                        {"old_role": row["role"], "new_role": role})

    return run_mutation(conn, work)


def reset_password(conn, actor_id, target_id, new_password):
    """Set a new password for an account (audited; the password is not logged).

    An admin may not reset another admin's password (prevents admin takeover);
    regular users and one's own account are still allowed.
    """
    def work(cur):
        row = cur.execute(
            "SELECT username, role FROM users WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise StockError("utente inesistente")
        if row["role"] == "admin" and target_id != actor_id:
            raise StockError("non puoi reimpostare la password di un altro amministratore")
        cur.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password), target_id))
        _log_user_admin(cur, actor_id, "reset_password", row["username"])

    return run_mutation(conn, work)


def autocomplete_options(conn):
    """Distinct existing values offered as typeahead suggestions in the forms.

    Read-only. Supplies the <datalist> dropdowns so users can pick an existing
    product, manufacturer, location or batch code instead of retyping it.
    """
    def values(query):
        return [row[0] for row in conn.execute(query).fetchall() if row[0]]

    return {
        "products": values(
            "SELECT DISTINCT product_name FROM batches "
            "WHERE product_name IS NOT NULL AND product_name <> '' "
            "ORDER BY product_name"),
        "manufacturers": values(
            "SELECT DISTINCT manufacturer FROM batches "
            "WHERE manufacturer IS NOT NULL AND manufacturer <> '' "
            "ORDER BY manufacturer"),
        "locations": values(
            "SELECT DISTINCT location FROM stock "
            "WHERE location IS NOT NULL AND location <> '' "
            "ORDER BY location"),
        "batch_codes": values(
            "SELECT DISTINCT batch_code FROM batches "
            "WHERE batch_code IS NOT NULL AND batch_code <> '' "
            "ORDER BY batch_code"),
    }


def add_feedback(conn, user_id, message, page):
    """Store one feedback message from a logged-in user."""
    conn.execute(
        "INSERT INTO feedback (user_id, message, page, timestamp) "
        "VALUES (?, ?, ?, ?)",
        (user_id, message, page, _now()),
    )


def list_feedback(conn):
    """Return all feedback, newest first, with the author's username."""
    return conn.execute(
        "SELECT f.id, f.message, f.page, f.timestamp, u.username "
        "FROM feedback f LEFT JOIN users u ON u.id = f.user_id "
        "ORDER BY f.id DESC"
    ).fetchall()


def list_products_with_stock_and_thresholds(conn):
    """For the admin thresholds page: every distinct product with its current
    inventory quantity and its low-stock threshold (None if not set)."""
    return conn.execute(
        "SELECT b.product_name, "
        "  COALESCE(SUM(CASE WHEN s.state = 'inventory' THEN s.quantity ELSE 0 END), 0) AS inv_qty, "
        "  t.min_qty AS min_qty "
        "FROM batches b "
        "LEFT JOIN stock s ON s.supply_number = b.supply_number "
        "LEFT JOIN product_thresholds t ON t.product_name = b.product_name "
        "GROUP BY b.product_name ORDER BY b.product_name"
    ).fetchall()


def set_product_threshold(conn, product_name, min_qty):
    """Upsert a product's low-stock threshold, or remove it when min_qty is None."""
    if min_qty is None:
        conn.execute("DELETE FROM product_thresholds WHERE product_name = ?",
                     (product_name,))
    else:
        conn.execute(
            "INSERT INTO product_thresholds (product_name, min_qty) VALUES (?, ?) "
            "ON CONFLICT (product_name) DO UPDATE SET min_qty = excluded.min_qty",
            (product_name, min_qty),
        )


def low_stock(conn):
    """Products whose current inventory quantity is at or below their set
    threshold (the 'in esaurimento' list on the Avvisi page).

    Derived from list_products_with_stock_and_thresholds so the inventory-sum
    query lives in exactly one place: keeps only monitored products (a threshold
    is set) that are at or below it, sorted by inventory quantity ascending."""
    rows = [r for r in list_products_with_stock_and_thresholds(conn)
            if r["min_qty"] is not None and r["inv_qty"] <= r["min_qty"]]
    return sorted(rows, key=lambda r: r["inv_qty"])
