"""P0 底座测试：core/db.py 共用件 + core/status.py 值稳定性。"""
import re

import pytest

from boss_secretary.core import allowance as AL
from boss_secretary.core import contract as CT
from boss_secretary.core import db as DB
from boss_secretary.core import router as R
from boss_secretary.core import seal as SL
from boss_secretary.core import status as ST
from boss_secretary.core import supplier as SUP
from boss_secretary.core import travel as TR
from boss_secretary.core.router import SQLiteTicketStore


@pytest.fixture()
def conn(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a").conn


def test_new_id_format_and_uniqueness():
    a, b = DB.new_id("C"), DB.new_id("C")
    assert re.fullmatch(r"C\d{8}-[0-9A-F]{6}", a)
    assert a != b
    assert re.fullmatch(r"PM\d{8}-[0-9A-F]{6}", DB.new_id("PM"))


def test_now_is_iso_seconds():
    s = DB.now()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", s)


def test_fetch_one_and_update_fields(conn):
    cid = CT.create(conn, employee_id="e1", dept_id="D1", title="XX合同",
                    supplier="YY", amount=1000, start_date="2026-09-01",
                    end_date="2027-08-31")
    rec = DB.fetch_one(conn, "contracts", "contract_id", cid)
    assert rec["title"] == "XX合同" and rec["status"] == CT.PENDING_REVIEW
    assert DB.fetch_one(conn, "contracts", "contract_id", "NOPE") is None

    DB.update_fields(conn, "contracts", "contract_id", cid,
                     status=CT.ACTIVE, title="改后标题")
    rec = DB.fetch_one(conn, "contracts", "contract_id", cid)
    assert rec["status"] == CT.ACTIVE and rec["title"] == "改后标题"


def test_update_fields_noop_and_identifier_guard(conn):
    DB.update_fields(conn, "contracts", "contract_id", "NOPE")  # 空字段=空操作
    with pytest.raises(ValueError):
        DB.fetch_one(conn, "contracts; DROP TABLE contracts", "contract_id", "x")
    with pytest.raises(ValueError):
        DB.update_fields(conn, "contracts", "contract_id", "x", **{"a=1;--": 2})


def test_status_values_stable():
    """值一旦改动会破坏既有数据库数据，此处锁定。"""
    assert R.DRAFT == "DRAFT" and R.SUBMITTED == "SUBMITTED"
    assert R.ESCALATED == "ESCALATED" and R.CANCELLED == "CANCELLED"
    assert CT.PENDING_REVIEW == "pending_review"
    assert CT.PAY_PAID == "paid"
    assert SUP.PENDING == "pending_review"      # 历史值，不得改
    assert SUP.ACTIVE == "active"
    assert SL.PENDING == "pending"
    assert SL.SEAL_FROZEN == "frozen"
    assert TR.LOAN_PAID_OUT == "paid_out"
    assert AL.EXHAUSTED == "EXHAUSTED"
    assert ST.Doc.REJECTED == "rejected" and ST.Payment.REJECTED == "rejected"


def test_modules_share_status_definitions():
    assert CT.ACTIVE is SUP.ACTIVE is SL.SEAL_ACTIVE is TR.TRIP_ACTIVE
    assert CT.REJECTED is SUP.REJECTED is AL.REJECTED is TR.LOAN_REJECTED
    assert R.APPROVED is ST.Ticket.APPROVED


def test_schema_version_on_fresh_db(tmp_path):
    from boss_secretary import models as MD
    conn = MD.init_db(tmp_path / "fresh.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == MD.SCHEMA_VERSION
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tickets)")]
    assert "allowance_id" in cols
    # 再次 init 幂等
    conn.close()
    conn2 = MD.init_db(tmp_path / "fresh.db")
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == MD.SCHEMA_VERSION


def test_migrates_legacy_db_without_version(tmp_path):
    import sqlite3
    from boss_secretary import models as MD
    db = tmp_path / "legacy.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE tickets(ticket_id TEXT PRIMARY KEY, employee_id TEXT,"
              " dept_id TEXT)")   # 旧库：有索引列、缺后加的 allowance_id
    c.execute("INSERT INTO tickets(ticket_id, employee_id) VALUES('T1','e1')")
    c.commit()
    c.close()
    conn = MD.init_db(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == MD.SCHEMA_VERSION
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tickets)")]
    assert "allowance_id" in cols
    assert conn.execute("SELECT ticket_id FROM tickets").fetchone()[0] == "T1"
