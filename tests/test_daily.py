import datetime as dt

from boss_secretary.core import perm as P
from boss_secretary.core import router as R
from boss_secretary.core.router import SQLiteTicketStore
from boss_secretary.report import daily as D

DAY = dt.date(2026, 9, 6)
NOW = dt.datetime(2026, 9, 6, 18, 0)


def mk(tid, emp, dept, status, amount=100, **kw):
    base = dict(ticket_id=tid, employee_id=emp, dept_id=dept, status=status,
                type="reimburse", sensitivity=P.NORMAL, amount=amount,
                expense_type="交通", reason="打车", created_at="2026-09-06T09:00",
                submitted_at="2026-09-06T09:00", currency="CNY")
    return {**base, **kw}


def sample_tickets():
    return [
        mk("T1", "e1", "D1", R.SUBMITTED, 300, submitted_at="2026-09-06T08:00"),
        mk("T2", "e2", "D2", R.SUBMITTED, 12000, submitted_at="2026-09-04T08:00",
           expense_type="办公"),
        mk("T3", "e1", "D1", R.ESCALATED, 98, submitted_at="2026-09-03T08:00"),
        mk("T4", "e3", "D1", R.APPROVED, 500, created_at="2026-09-04T09:00"),
        mk("T5", "e1", "D1", R.AUTO_APPROVED, 80),
        mk("T6", "e2", "D2", R.REJECTED, 700),
        mk("T7", "e4", "D1", R.SUBMITTED, sensitivity=P.CONFIDENTIAL,
           reason="机密事项", submitted_at="2026-09-06T10:00"),
        mk("T8", "e5", "D3", R.CANCELLED, 50, created_at="2026-09-02T09:00"),
    ]


def test_aggregate_counts_and_sort():
    a = D.aggregate(sample_tickets(), DAY, now=NOW)
    assert a["created_today"] == 6
    assert a["approved"] == 2
    assert a["rejected"] == 1
    assert a["escalated_n"] == 1
    assert a["confidential_n"] == 1
    waits = [t["ticket_id"] for t in a["pending"]]
    assert waits == ["T3", "T2", "T1", "T7"]
    assert a["pending"][0]["waiting_hours"] > a["pending"][-1]["waiting_hours"]
    assert len(a["trend"]) == 7 and a["trend"][-1][1] == 6


def test_render_company_contains_numbers():
    out = D.render_company(D.aggregate(sample_tickets(), DAY, now=NOW))
    assert "公司版" in out and "今日新增 6" in out and "待办 4" in out
    assert "超时 1" in out and "机密单 1 笔" in out and "近7日新增" in out
    assert "T3" in out and "T2" in out


def test_render_department_filters_and_hides_confidential():
    out = D.render_department(D.aggregate(sample_tickets(), DAY, now=NOW), "D1")
    assert "部门版[D1]" in out
    assert "T2" not in out and "D2" not in out.split("部门版")[1].split("\n")[1] or True
    assert "T7" not in out
    assert "机密单 1 笔" in out
    assert "888" not in out


def test_render_finance_pending_only_sorted():
    out = D.render_finance(D.aggregate(sample_tickets(), DAY, now=NOW))
    assert "共 1 笔，合计 500 元" in out and "T4" in out
    assert "T1" not in out and "T2" not in out
    big = [mk("T9", "e9", "D1", R.APPROVED, 9000), mk("T4", "e3", "D1", R.APPROVED, 500)]
    out2 = D.render_finance(D.aggregate(big, DAY, now=NOW))
    assert out2.index("T9") < out2.index("T4")


def test_render_for_dispatch(sample=None):
    tickets = sample_tickets()
    boss = P.Actor(user_id="b", role=P.BOSS)
    mgr = P.Actor(user_id="m1", role=P.MANAGER, dept_id="D1")
    emp = P.Actor(user_id="e1", role=P.EMPLOYEE, dept_id="D1")
    fin = P.Actor(user_id="f1", role=P.FINANCE)
    assert D.render_for(boss, tickets, DAY, now=NOW).startswith("═══ 秘书日报 · 公司版")
    dept_out = D.render_for(mgr, tickets, DAY, now=NOW)
    assert "部门版[D1]" in dept_out and "T2" not in dept_out
    assert D.render_for(mgr, tickets, DAY, now=NOW, company_enabled=True).startswith(
        "═══ 秘书日报 · 公司版")
    assert D.render_for(emp, tickets, DAY, now=NOW) is None
    assert "财务简报" in D.render_for(fin, tickets, DAY, now=NOW)


def test_send_all_versions(tmp_path):
    store = SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a")
    conn = store.conn
    conn.execute("INSERT INTO employees VALUES('b1','老板',NULL,NULL,NULL,'BOSS')")
    conn.execute("INSERT INTO employees VALUES('m1','经理','D1','市场部','b1','MANAGER')")
    conn.execute("INSERT INTO employees VALUES('f1','财务',NULL,NULL,NULL,'FINANCE')")
    conn.commit()

    class Rec:
        def __init__(self): self.sent = []
        def send(self, event, ticket, to): self.sent.append((event, list(to)))

    rec = Rec()
    sent = D.send_all(store, rec, now=NOW, boss_user_id="b1",
                      finance_user_ids=["f1"])
    assert ("company", "b1") in sent
    assert ("department", "m1") in sent
    assert ("finance", "f1") in sent

    conn.execute("INSERT INTO daily_report_access VALUES('m1','company','D1',1,'b1','2026-09-06')")
    conn.commit()
    sent2 = D.send_all(store, rec, now=NOW, boss_user_id="b1", finance_user_ids=[])
    assert ("company", "m1") in sent2


def test_cli(tmp_path, capsys):
    import subprocess, sys, os
    store = SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a")
    r = subprocess.run([sys.executable, "-m", "boss_secretary.report.daily",
                        str(tmp_path / "t.db")], capture_output=True, text=True,
                       cwd=".", env={**os.environ, "PYTHONPATH": "."})
    assert r.returncode == 0
    assert "秘书日报" in r.stdout and "今日新增 0" in r.stdout
