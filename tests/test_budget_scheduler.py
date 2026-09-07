import datetime as dt

import pytest

from boss_secretary.core import budget as BG
from boss_secretary.core.router import SQLiteTicketStore
from boss_secretary.core.scheduler import Job, Scheduler


@pytest.fixture()
def conn(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a").conn


def tk(conn, tid, dept, month, amount, status):
    conn.execute("INSERT INTO tickets(ticket_id, employee_id, dept_id, type, status,"
                 " amount, expense_type, occurred_at, reason)"
                 " VALUES(?,?,?,?,?,?,?,?,?)",
                 (tid, "e1", dept, "reimburse", status, amount, "交通",
                  f"{month}-15", "x"))
    conn.commit()


def test_set_get_used_check(conn):
    BG.set_budget(conn, "D1", "2026-09", 1000)
    assert BG.get_budget(conn, "D1", "2026-09") == 1000
    tk(conn, "T1", "D1", "2026-09", 300, "APPROVED")
    tk(conn, "T2", "D1", "2026-09", 200, "SUBMITTED")
    tk(conn, "T3", "D1", "2026-09", 999, "REJECTED")
    tk(conn, "T4", "D2", "2026-09", 500, "APPROVED")
    assert BG.used(conn, "D1", "2026-09") == 500
    b = BG.check(conn, "D1", "2026-09", extra=400)
    assert b["ok"] and b["remaining"] == 500
    b2 = BG.check(conn, "D1", "2026-09", extra=600)
    assert not b2["ok"] and b2["over"] == 100 and not b2["block"]


def test_check_no_budget_ok(conn):
    b = BG.check(conn, "D9", "2026-09", extra=99999)
    assert b["checked"] is False and b["ok"]


def test_block_over_setting(conn):
    BG.set_budget(conn, "D1", "2026-09", 100)
    b = BG.check(conn, "D1", "2026-09", extra=500, settings={"budgets": {"block_over": True}})
    assert b["block"] is True


def test_all_dept_bucket(conn):
    BG.set_budget(conn, "*", "2026-09", 10000)
    tk(conn, "T5", None, "2026-09", 400, "APPROVED")
    b = BG.check(conn, None, "2026-09", extra=100)
    assert b["checked"] and b["used"] == 400 and b["ok"]
    b2 = BG.check(conn, None, "2026-09", extra=9700)
    assert not b2["ok"]


def test_parse_budget_text():
    assert BG.parse_budget_text("预算 D1 2026-09 50000") == {
        "action": "set", "dept": "D1", "month": "2026-09", "amount": 50000.0}
    assert BG.parse_budget_text("预算 D1 2026-09")["action"] == "query"
    assert BG.parse_budget_text("预算")["action"] == "overview"
    assert BG.parse_budget_text("报销 300") is None


def test_overview_table(conn):
    BG.set_budget(conn, "D1", "2026-09", 1000)
    tk(conn, "T6", "D1", "2026-09", 900, "APPROVED")
    out = BG.to_table(BG.overview(conn, "2026-09"), "2026-09")
    assert "D1" in out and "90%" in out and "⚠接近上限" in out


# ── 调度器 ────────────────────────────────────────────────

def test_scheduler_daily_due_and_state(tmp_path):
    ran = []
    j = Job("daily_report", "daily", at="18:00", fn=lambda: "ran")
    sched = Scheduler([j], state_path=tmp_path / "state.json")
    before = dt.datetime(2026, 9, 7, 17, 59)
    assert sched.tick(now=before) == []
    at_time = dt.datetime(2026, 9, 7, 18, 1)
    ran1 = sched.tick(now=at_time)
    assert any("daily_report" in x for x in ran1)
    assert sched.tick(now=dt.datetime(2026, 9, 7, 19, 0)) == []
    assert sched.tick(now=dt.datetime(2026, 9, 8, 18, 1))


def test_scheduler_monthly_and_state_persist(tmp_path):
    ran = []
    j1 = Job("anomaly", "monthly", at="09:00", day=1, fn=lambda: "m")
    j2 = Job("hourly", "hourly", fn=lambda: "h")
    sched = Scheduler([j1, j2], state_path=tmp_path / "state.json")
    out1 = sched.tick(now=dt.datetime(2026, 9, 1, 9, 5))
    assert any("anomaly" in x for x in out1)
    out2 = sched.tick(now=dt.datetime(2026, 9, 1, 10, 0))
    assert not any("anomaly" in x for x in out2)
    assert any("hourly" in x for x in out2)
    sched2 = Scheduler([j1, j2], state_path=tmp_path / "state.json")
    out3 = sched2.tick(now=dt.datetime(2026, 9, 1, 11, 0))
    assert not any("anomaly" in x for x in out3)
    assert sched2.tick(now=dt.datetime(2026, 9, 2, 11, 0))


def test_scheduler_error_isolated(tmp_path):
    def boom():
        raise RuntimeError("x")
    j = Job("bad", "daily", at="00:00", fn=boom)
    sched = Scheduler([j], state_path=tmp_path / "s.json")
    out = sched.tick(now=dt.datetime(2026, 9, 7, 10, 0))
    assert any("ERROR" in x for x in out)
