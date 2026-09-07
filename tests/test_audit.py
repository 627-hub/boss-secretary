import datetime as dt

import pytest

from boss_secretary.core import audit as AU
from boss_secretary.core.router import SQLiteTicketStore


@pytest.fixture()
def conn(tmp_path):
    store = SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a")
    c = store.conn
    c.execute("INSERT INTO tickets(ticket_id, employee_id, dept_id, type, status,"
              " sensitivity, amount, expense_type, reason, occurred_at, ai_verdict,"
              " invoice_seller, submitted_at)"
              " VALUES('T-BIG','e1','D1','reimburse','APPROVED','normal',9000,"
              "'餐饮','大额','2026-09-05','MANUAL_REVIEW','某某酒庄','2026-09-05T10:00')")
    c.execute("INSERT INTO tickets(ticket_id, employee_id, dept_id, type, status,"
              " sensitivity, amount, expense_type, reason, occurred_at,"
              " invoice_seller, submitted_at)"
              " VALUES('T-OK','e2','D2','reimburse','APPROVED','normal',100,"
              "'交通','打车','2026-09-05','滴滴','2026-09-05T10:00')")
    for i in range(3):
        c.execute("INSERT INTO tickets(ticket_id, employee_id, dept_id, type,"
                  " status, sensitivity, amount, expense_type, reason,"
                  " occurred_at, invoice_seller, submitted_at)"
                  f" VALUES('T-R{i}','e1','D1','reimburse','APPROVED','normal',"
                  f"{200 + i},'餐饮','聚餐','2026-09-06','某某酒庄',"
                  f"'2026-09-06T10:00')")
    c.execute("INSERT INTO anomalies(type, subject, period, baseline, observed,"
              " deviation_pct, severity, status)"
              " VALUES('费用趋势','员工:e1','2026-08',1000,4000,300,'ALERT','confirmed')")
    c.execute("INSERT INTO anomalies(type, subject, period, baseline, observed,"
              " deviation_pct, severity, status)"
              " VALUES('费用趋势','员工:e1','2026-09',1000,5000,400,'ALERT','confirmed')")
    c.commit()
    return c


def test_risk_scores_factors(conn):
    scores = AU.risk_scores(conn, "2026-09")
    big = [s for s in scores if s["ticket_id"] == "T-BIG"][0]
    assert big["score"] >= 60
    reasons = " ".join(big["reasons"])
    assert "均值" in reasons and "人工复核" in reasons and "同商户" in reasons
    assert not any(s["ticket_id"] == "T-OK" for s in scores)  # 低风险不进队列


def test_sampling_queue_order_and_threshold(conn):
    q = AU.sampling_queue(conn, "2026-09", top_n=2)
    assert len(q) <= 2 and q[0]["score"] >= q[-1]["score"]
    assert q[0]["ticket_id"] == "T-BIG"


def test_replay_package(conn, tmp_path):
    conn.execute("INSERT INTO audit_log(ticket_id, actor, action, payload_hash)"
                 " VALUES('T-BIG','boss','status.REVIEWING→SUBMITTED','abc')")
    conn.commit()
    fp = AU.replay_package(conn, "T-BIG", audit_dir=tmp_path)
    text = fp.read_text(encoding="utf-8")
    assert "回放包 T-BIG" in text and "审计轨迹" in text
    assert "status.REVIEWING→SUBMITTED" in text
    assert "REVIEWING→SUBMITTED" in text


def test_anomaly_status_and_risk_list(conn):
    rows = conn.execute("SELECT anomaly_id FROM anomalies").fetchall()
    assert len(rows) == 2  # 种子: 两条 confirmed
    AU.set_anomaly_status(conn, rows[0][0], "false_positive")
    assert AU.risk_list(conn, min_confirmed=2) == []
    c = conn.execute("INSERT INTO anomalies(type, subject, period, baseline,"
                     " observed, deviation_pct, severity, status)"
                     " VALUES('费用趋势','员工:e1','2026-09',1000,3000,200,"
                     "'WARN','confirmed')")
    conn.commit()
    rl = AU.risk_list(conn, min_confirmed=2)
    assert rl and rl[0]["subject"] == "员工:e1" and rl[0]["confirmed"] == 2
    with pytest.raises(ValueError):
        AU.set_anomaly_status(conn, rows[0][0], "bogus")


def test_workbench(conn):
    text = AU.workbench(conn, "2026-09")
    assert "审计工作台" in text and "抽检推荐" in text and "T-BIG" in text


def test_audit_appendix(conn):
    out = AU.audit_appendix(conn, "2026-09")
    assert "异常事件" in out or out == ""
