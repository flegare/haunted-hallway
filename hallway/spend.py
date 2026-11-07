"""SpendGuard: the only module that writes the spend ledger.

Budget is reserved BEFORE every paid call. A call without a valid Reservation cannot
happen: providers require one and raise TypeError otherwise. The guard fails closed:
a missing, tampered or inconsistent ledger refuses to reserve.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db

_MINT = object()


class BudgetExceeded(Exception):
    pass


class LedgerCorrupt(Exception):
    pass


@dataclass(frozen=True)
class Reservation:
    id: int
    ref: str
    micro: int
    kind: str
    _token: object = None

    def is_valid(self) -> bool:
        return self._token is _MINT


def require(res: object) -> "Reservation":
    if not isinstance(res, Reservation) or not res.is_valid():
        raise TypeError("a paid call needs a Reservation from SpendGuard.reserve()")
    return res


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SpendGuard:
    def __init__(self, db_path: Path, cap_micro: int, daily_micro: int):
        self.db_path = db_path
        self.cap_micro = cap_micro
        self.daily_micro = daily_micro

    def _open(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise LedgerCorrupt("ledger database is missing")
        return db.connect(self.db_path)

    def _checked_sums(self, conn: sqlite3.Connection) -> tuple[int, int]:
        """Return (total, today) after verifying the ledger. Raises LedgerCorrupt."""
        try:
            total = conn.execute("SELECT COALESCE(SUM(micro), 0) FROM spend WHERE state != 'released'").fetchone()[0]
            control = conn.execute("SELECT value FROM settings WHERE key='spend_control'").fetchone()
            ledger_id = conn.execute("SELECT value FROM settings WHERE key='ledger_id'").fetchone()
            day_start = _now().strftime("%Y-%m-%dT00:00:00")
            today = conn.execute(
                "SELECT COALESCE(SUM(micro), 0) FROM spend WHERE state != 'released' AND ts >= ?", (day_start,)
            ).fetchone()[0]
        except sqlite3.Error as exc:
            raise LedgerCorrupt(f"ledger unreadable: {exc}") from exc
        witness = db.witness_path(self.db_path)
        if control is None or ledger_id is None or not witness.is_file():
            raise LedgerCorrupt("ledger control data missing")
        if witness.read_text().strip() != ledger_id[0]:
            raise LedgerCorrupt("ledger does not match its witness file")
        if control[0] != str(total):
            raise LedgerCorrupt("ledger sum does not match its control value")
        return int(total), int(today)

    def _set_control(self, conn: sqlite3.Connection) -> None:
        total = conn.execute("SELECT COALESCE(SUM(micro), 0) FROM spend WHERE state != 'released'").fetchone()[0]
        conn.execute("UPDATE settings SET value=? WHERE key='spend_control'", (str(total),))

    def reserve(self, ref: str, micro: int, kind: str) -> Reservation:
        if not isinstance(micro, int) or micro < 0:
            raise ValueError("micro must be a non-negative int")
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                total, today = self._checked_sums(conn)
                if total + micro > self.cap_micro:
                    raise BudgetExceeded(f"lifetime cap {self.cap_micro} micro-USD would be exceeded")
                if today + micro > self.daily_micro:
                    raise BudgetExceeded(f"daily cap {self.daily_micro} micro-USD would be exceeded")
                cur = conn.execute(
                    "INSERT INTO spend (ref, micro, kind, state, ts) VALUES (?, ?, ?, 'reserved', ?)",
                    (ref, micro, kind, _now().strftime("%Y-%m-%dT%H:%M:%S")),
                )
                self._set_control(conn)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return Reservation(cur.lastrowid, ref, micro, kind, _MINT)
        finally:
            conn.close()

    def settle(self, res: Reservation, measured_micro: int | None = None) -> None:
        """Note the measured cost. Never adds spend; the reservation stays the booked amount."""
        require(res)
        conn = self._open()
        try:
            conn.execute(
                "UPDATE spend SET state='settled', measured_micro=? WHERE id=? AND state='reserved'",
                (measured_micro, res.id),
            )
        finally:
            conn.close()

    def release(self, res: Reservation) -> None:
        """Give budget back. Only when the call is proven never to have left the machine."""
        require(res)
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE spend SET state='released' WHERE id=? AND state='reserved'", (res.id,))
            self._set_control(conn)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def status(self) -> dict:
        conn = self._open()
        try:
            total, today = self._checked_sums(conn)
        finally:
            conn.close()
        return {
            "spent_micro": total,
            "cap_micro": self.cap_micro,
            "remaining_micro": max(0, self.cap_micro - total),
            "today_micro": today,
            "daily_cap_micro": self.daily_micro,
            "estimated": True,
        }
