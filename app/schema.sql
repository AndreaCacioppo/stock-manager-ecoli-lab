-- schema.sql: the stock-manager data model (migration 1).
--
-- Six tables: orders, batches, stock, archive, master_log, users.
-- (Migrations 2-3 in app/db.py add two more: feedback and product_thresholds,
--  so a fully upgraded database has eight tables.)
-- CREATE ... IF NOT EXISTS keeps this script re-runnable: if a first init fails
-- partway, re-running init-db can finish it instead of erroring on an existing
-- table.
-- Conservation and audit are enforced in the database (constraints + triggers),
-- not just in application code.

-- ----------------------------------------------------------------------------
-- users: real accounts (the audit trail records who did what). Never deleted,
-- soft-deactivated via active=0 so master_log.user_id / archive.user_id never
-- orphan. Defined first because other tables reference it.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- ----------------------------------------------------------------------------
-- orders: one delivery. Contains many batches.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    order_number INTEGER PRIMARY KEY AUTOINCREMENT,
    order_date   TEXT,                       -- ISO YYYY-MM-DD
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL,              -- ISO timestamp
    notes        TEXT
);

-- ----------------------------------------------------------------------------
-- batches: N identical items from one order, sharing all attributes.
-- supply_number is the progressive, never-reused identifier the audit trail
-- depends on. AUTOINCREMENT (not plain rowid) guarantees it is never reused
-- even after the top row is archived.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS batches (
    supply_number  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number   INTEGER NOT NULL REFERENCES orders(order_number),
    type           TEXT NOT NULL CHECK (type IN ('microbiology', 'primers', 'other')),
    product_name   TEXT NOT NULL,
    manufacturer   TEXT,
    batch_code     TEXT,                     -- manufacturer's lot string
    expiring_date  TEXT,                     -- ISO YYYY-MM-DD, NULL = never expires
    shipment_date  TEXT,
    quality_flag   INTEGER NOT NULL DEFAULT 0 CHECK (quality_flag IN (0, 1)),
    received_total INTEGER NOT NULL CHECK (received_total >= 0),  -- conservation anchor
    created_at     TEXT NOT NULL
);

-- ----------------------------------------------------------------------------
-- stock: how much of a given batch is currently in a given ACTIVE state, and
-- where it physically is. Holds only state ∈ (inventory, lab).
-- UNIQUE(supply_number, state, location) stops a batch-state quantity from
-- fragmenting across rows (which would silently break the resolver and the
-- atomic decrement). Additions use upsert (INSERT ... ON CONFLICT DO UPDATE).
-- CHECK(quantity >= 0) means the DB itself refuses to go negative.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_number INTEGER NOT NULL REFERENCES batches(supply_number),
    state         TEXT NOT NULL CHECK (state IN ('inventory', 'lab')),
    quantity      INTEGER NOT NULL CHECK (quantity >= 0),
    location      TEXT NOT NULL,
    UNIQUE (supply_number, state, location)
);

-- ----------------------------------------------------------------------------
-- archive: append-only history of units that left active use.
-- quantity is SIGNED: out-of-stock causali are positive; a 'correction' that
-- returns units to active stock is negative, so reconciliation can sum this
-- column directly (net of correction).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS archive (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_number INTEGER NOT NULL REFERENCES batches(supply_number),
    quantity      INTEGER NOT NULL,          -- signed; see note above
    causale       TEXT NOT NULL CHECK (causale IN
                      ('consumed', 'expired', 'ineligible', 'removed', 'correction')),
    timestamp     TEXT NOT NULL,
    user_id       INTEGER REFERENCES users(id)
);

-- ----------------------------------------------------------------------------
-- master_log: append-only audit trail. Written in the SAME transaction as the
-- stock move, so audit and state are atomically consistent, always.
-- ref_log_id links a 'correction' entry to the original entry it reverses.
-- details is JSON-in-TEXT, validated by json_valid (NULL allowed).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS master_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    user_id       INTEGER REFERENCES users(id),
    action        TEXT NOT NULL,             -- e.g. 'move', 'consume', 'order', 'remove'
    supply_number INTEGER,
    from_state    TEXT,
    to_state      TEXT,
    quantity      INTEGER,
    causale       TEXT,
    ref_log_id    INTEGER REFERENCES master_log(id),
    details       TEXT CHECK (details IS NULL OR json_valid(details))
);

-- ----------------------------------------------------------------------------
-- Append-only enforcement: archive and master_log may only ever be INSERTed.
-- Any UPDATE or DELETE aborts the transaction, enforcing that no unit is
-- unaccounted.
-- ----------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS archive_no_update BEFORE UPDATE ON archive
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TRIGGER IF NOT EXISTS archive_no_delete BEFORE DELETE ON archive
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TRIGGER IF NOT EXISTS master_log_no_update BEFORE UPDATE ON master_log
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TRIGGER IF NOT EXISTS master_log_no_delete BEFORE DELETE ON master_log
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- Indexes for the resolver and the views.
CREATE INDEX IF NOT EXISTS idx_stock_lookup  ON stock (supply_number, state);
CREATE INDEX IF NOT EXISTS idx_archive_batch ON archive (supply_number);
CREATE INDEX IF NOT EXISTS idx_batches_order ON batches (order_number);
