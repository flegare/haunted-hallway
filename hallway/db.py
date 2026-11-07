"""SQLite schema and connections."""
from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path

MIGRATIONS = [
    # version 1
    """
    CREATE TABLE drawings (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        consent INTEGER NOT NULL,
        ok_online INTEGER NOT NULL DEFAULT 0,
        original_file TEXT NOT NULL
    );
    CREATE TABLE characters (
        id TEXT PRIMARY KEY,
        drawing_id TEXT NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
        idx INTEGER NOT NULL,
        description TEXT NOT NULL,
        file_id TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE clips (
        id TEXT PRIMARY KEY,
        drawing_id TEXT NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
        file_id TEXT NOT NULL,
        cost_micro INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE publications (
        drawing_id TEXT PRIMARY KEY REFERENCES drawings(id) ON DELETE CASCADE,
        clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
        channel TEXT NOT NULL CHECK (channel IN ('preview', 'public')),
        updated_at TEXT NOT NULL
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        drawing_id TEXT NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'error')),
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE spend (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT NOT NULL,
        micro INTEGER NOT NULL CHECK (micro >= 0),
        kind TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('reserved', 'settled', 'released')),
        measured_micro INTEGER,
        ts TEXT NOT NULL
    );
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT
    );
    """,
    # version 2
    """
    ALTER TABLE characters ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE clips ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE clips ADD COLUMN prompt TEXT NOT NULL DEFAULT '';
    ALTER TABLE jobs ADD COLUMN prompt TEXT NOT NULL DEFAULT '';
    CREATE UNIQUE INDEX one_active_job_per_drawing ON jobs(drawing_id) WHERE state IN ('queued', 'running');
    """,
]


class LedgerMissing(Exception):
    """The database is gone but its witness file is still there: refuse to start from zero."""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def witness_path(db_path: Path) -> Path:
    return db_path.parent / ".ledger_id"


def init(db_path: Path) -> None:
    """Create or migrate the database. Never silently restart the spend ledger."""
    witness = witness_path(db_path)
    if not db_path.exists() and witness.exists():
        raise LedgerMissing(
            "database is missing but .ledger_id exists; the spend history would reset to zero. "
            "Restore the database, or delete .ledger_id yourself if you really want a fresh ledger."
        )
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        for number, script in enumerate(MIGRATIONS, start=1):
            if number <= current:
                continue
            conn.executescript("BEGIN;" + script + f"INSERT INTO schema_version VALUES ({number});COMMIT;")
        if current == 0:
            ledger_id = secrets.token_hex(16)
            conn.execute("INSERT INTO settings VALUES ('ledger_id', ?)", (ledger_id,))
            conn.execute("INSERT INTO settings VALUES ('spend_control', '0')")
            witness.write_text(ledger_id)
            witness.chmod(0o600)
    finally:
        conn.close()
