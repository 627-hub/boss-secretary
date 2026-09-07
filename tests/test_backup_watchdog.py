import datetime as dt
import json
import zipfile

import pytest

from boss_secretary.core.scheduler import Job, Scheduler

from boss_secretary.core import backup as BK
from boss_secretary.core.router import SQLiteTicketStore
from boss_secretary.core.scheduler import Scheduler
from boss_secretary.core.watchdog import check_stale


def test_backup_zip_and_prune(tmp_path):
    db = tmp_path / "t.db"
    store = SQLiteTicketStore(db, audit_dir=tmp_path / "a")
    store.conn.execute("INSERT INTO tickets(ticket_id, employee_id, status, amount)"
                       " VALUES('T1','e1','APPROVED',100)")
    store.conn.commit()
    (tmp_path / "auditdir").mkdir()
    (tmp_path / "auditdir" / "x.txt").write_text("evidence")
    items = [str(tmp_path / "auditdir")]
    r1 = BK.backup(db, out_dir=tmp_path / "bk", items=items, keep=2,
                   now=dt.datetime(2026, 9, 1, 3, 0))
    assert r1["size"] > 0 and r1["kept"] == 1
    BK.backup(db, out_dir=tmp_path / "bk", items=items, keep=2,
              now=dt.datetime(2026, 9, 2, 3, 0))
    BK.backup(db, out_dir=tmp_path / "bk", items=items, keep=2,
              now=dt.datetime(2026, 9, 3, 3, 0))
    BK.backup(db, out_dir=tmp_path / "bk", items=items, keep=2,
              now=dt.datetime(2026, 9, 4, 3, 0))
    zips = list((tmp_path / "bk").glob("backup_*.zip"))
    assert len(zips) == 2
    with zipfile.ZipFile(zips[-1]) as z:
        names = z.namelist()
        assert "t.db" in names
        assert any("x.txt" in n for n in names)


def test_backup_copy_to_dest(tmp_path):
    db = tmp_path / "t.db"
    SQLiteTicketStore(db, audit_dir=tmp_path / "a")
    dest = tmp_path / "nas"
    BK.backup(db, out_dir=tmp_path / "bk", keep=2, dest_dir=str(dest))
    assert list(dest.glob("backup_*.zip"))


def test_backup_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        BK.backup(tmp_path / "nope.db", out_dir=tmp_path / "bk")


def test_scheduler_touches_heartbeat(tmp_path):
    j = Scheduler([Job("t", "hourly", fn=lambda: "x")],
                  state_path=tmp_path / "s.json",
                  heartbeat_path=tmp_path / "data" / "heartbeat")
    j.tick(now=dt.datetime(2026, 9, 7, 10, 0))
    assert (tmp_path / "data" / "heartbeat").exists()


def test_watchdog_check_stale(tmp_path):
    hb = tmp_path / "heartbeat"
    now = dt.datetime(2026, 9, 7, 12, 0)
    r = check_stale(hb, 60, now=now)
    assert r["stale"] and not r["ever_existed"]
    hb.write_text((now - dt.timedelta(minutes=10)).isoformat())
    assert not check_stale(hb, 60, now=now)["stale"]
    hb.write_text((now - dt.timedelta(minutes=90)).isoformat())
    r2 = check_stale(hb, 60, now=now)
    assert r2["stale"] and r2["age_min"] == 90.0
