import sqlite3
import threading

import pytest

from hallway import db
from hallway.config import DEFAULT_CAP_USD, parse_cap
from hallway.providers.mock import MockProvider
from hallway.spend import BudgetExceeded, LedgerCorrupt, SpendGuard
from tests.conftest import ffmpeg_bin, make_services, make_settings


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-1", "0", "1e9", "", "abc", None, "1001"])
def test_bad_cap_falls_back_to_default(raw):
    assert parse_cap(raw, DEFAULT_CAP_USD) == DEFAULT_CAP_USD


def test_good_cap_is_kept():
    assert parse_cap("0.5", 5.0) == 0.5


def test_concurrent_reservations_never_exceed_cap(tmp_path):
    svc = make_services(make_settings(tmp_path, SPEND_CAP_USD="1", SPEND_CAP_DAILY_USD="1"))
    ok, refused = [], []

    def worker(i):
        try:
            svc.guard.reserve(f"t{i}", 100_000, "image")
            ok.append(i)
        except BudgetExceeded:
            refused.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 10 and len(refused) == 10
    assert svc.guard.status()["spent_micro"] == 1_000_000


def test_tampered_ledger_refuses(tmp_path):
    svc = make_services(make_settings(tmp_path))
    svc.guard.reserve("a", 1_000, "text")
    conn = sqlite3.connect(svc.settings.db_path)
    conn.execute("UPDATE spend SET micro = 1")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerCorrupt):
        svc.guard.reserve("b", 1_000, "text")


def test_deleted_database_refuses(tmp_path):
    svc = make_services(make_settings(tmp_path))
    svc.guard.reserve("a", 1_000, "text")
    for suffix in ("", "-wal", "-shm"):
        p = svc.settings.db_path.parent / (svc.settings.db_path.name + suffix)
        p.unlink(missing_ok=True)
    with pytest.raises(LedgerCorrupt):
        svc.guard.reserve("b", 1_000, "text")
    with pytest.raises(db.LedgerMissing):
        db.init(svc.settings.db_path)


def test_paid_call_without_reservation_is_a_type_error_and_makes_no_call(tmp_path):
    provider = MockProvider(ffmpeg_bin())
    with pytest.raises(TypeError):
        provider.make_video([b"x"], "p", None)
    with pytest.raises(TypeError):
        provider.make_video([b"x"], "p", object())
    assert provider.calls["video"] == 0


def test_daily_cap(tmp_path):
    svc = make_services(make_settings(tmp_path, SPEND_CAP_USD="10", SPEND_CAP_DAILY_USD="0.3"))
    svc.guard.reserve("a", 200_000, "image")
    with pytest.raises(BudgetExceeded):
        svc.guard.reserve("b", 200_000, "image")
