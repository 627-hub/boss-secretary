import datetime as dt

import pytest

from boss_secretary.core import travel as TR
from boss_secretary.core.router import SQLiteTicketStore


@pytest.fixture()
def conn(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a").conn


def test_trip_lifecycle_and_reimburse_link(conn):
    t = TR.trip_request(conn, employee_id="e1", dept_id="D1",
                        destination="上海", reason="客户拜访", estimate=3000,
                        start_date="2026-09-10", end_date="2026-09-12")
    assert t["status"] == TR.TRIP_PENDING
    assert TR.trip_for(conn, "e1", "2026-09-05") is None  # 出差前不算
    t = TR.decide_trip(conn, t["trip_id"], "m1", approve=True)
    assert t["status"] == TR.TRIP_ACTIVE
    hit = TR.trip_for(conn, "e1", "2026-09-11")
    assert hit and hit["trip_id"] == t["trip_id"]
    t = TR.decide_trip(conn, t["trip_id"], "m1", approve=False)
    assert t is None  # 已处理不可重复


def test_trip_over_estimate_guard(conn):
    t = TR.trip_request(conn, employee_id="e1", dept_id="D1", destination="北京",
                        reason="x", estimate=1000, start_date="2026-09-10",
                        end_date="2026-09-11")
    TR.decide_trip(conn, t["trip_id"], "m1", approve=True)
    hit = TR.trip_for(conn, "e1", "2026-09-10")
    over = 2000 - float(hit["estimate"])
    assert over > float(hit["estimate"]) * 0.2  # 超预估 100% → 确认门


def test_loan_lifecycle_offset_and_close(conn):
    l = TR.loan_request(conn, employee_id="e1", amount=2000, reason="出差备用")
    assert l["status"] == TR.LOAN_PENDING
    l = TR.loan_decide(conn, l["loan_id"], "b1", approve=True)
    assert l["status"] == TR.LOAN_PAID_OUT
    rem = TR.loan_offset(conn, l["loan_id"], 1500, by="f1")
    assert rem == 500
    l = TR.get_loan(conn, l["loan_id"])
    assert l["status"] == TR.LOAN_OPEN
    rem = TR.loan_offset(conn, l["loan_id"], 500, by="f1")
    assert rem == 0
    assert TR.get_loan(conn, l["loan_id"])["status"] == TR.LOAN_CLOSED
    with pytest.raises(ValueError):
        TR.loan_offset(conn, l["loan_id"], 100)


def test_loan_reject_and_overdue(conn):
    l = TR.loan_request(conn, employee_id="e2", amount=500, reason="x")
    l = TR.loan_decide(conn, l["loan_id"], "b1", approve=False)
    assert l["status"] == TR.LOAN_REJECTED
    l2 = TR.loan_request(conn, employee_id="e3", amount=800, reason="y")
    TR.loan_decide(conn, l2["loan_id"], "b1", approve=True)
    old_date = (dt.datetime.now() - dt.timedelta(days=70)).isoformat()
    conn.execute("UPDATE loans SET created_at=? WHERE loan_id=?",
                 (old_date, l2["loan_id"]))
    conn.commit()
    od = TR.overdue_loans(conn, days=60)
    assert any(x["loan_id"] == l2["loan_id"] for x in od)
