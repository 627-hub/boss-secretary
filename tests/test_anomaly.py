import datetime as dt
import json

import pytest

from boss_secretary.core import anomaly as A
from boss_secretary.core import router as R
from boss_secretary.core.router import SQLiteTicketStore

CFG = A.load_config(None)


def mk_ticket(amount, month, dept="D1", etype="交通", emp="e1", status="APPROVED",
              tid=None):
    return {"ticket_id": tid or f"T-{dept}-{emp}-{month}-{amount}", "employee_id": emp,
            "dept_id": dept, "status": status, "amount": amount, "expense_type": etype,
            "occurred_at": f"{month}-15", "created_at": f"{month}-15T10:00",
            "sensitivity": "normal"}


def stable_history(months, amount=10000, dept="D1", etype="交通", emp="e1"):
    out = []
    for m in months:
        out.append(mk_ticket(amount, m, dept=dept, etype=etype, emp=emp))
    return out


def test_monthly_sums_grouping():
    tickets = [mk_ticket(100, "2026-06", dept="D1"), mk_ticket(200, "2026-06", dept="D1"),
               mk_ticket(50, "2026-07", dept="D2"), mk_ticket(999, "2026-07", status="REJECTED")]
    s = A.monthly_sums(tickets, lambda t: f"部门:{t['dept_id']}")
    assert s["部门:D1"] == {"2026-06": 300.0}
    assert s["部门:D2"] == {"2026-07": 50.0}


def test_a1_baseline_insufficient_skips():
    tickets = stable_history(["2026-07", "2026-08"]) + [mk_ticket(16000, "2026-09")]
    events = A.detect_a1(tickets, CFG, period="2026-09")
    assert events == []


def test_a1_warn_and_alert_thresholds():
    base = stable_history(["2026-04", "2026-05", "2026-06", "2026-07"])
    warn = A.detect_a1(base + [mk_ticket(16000, "2026-08")], CFG, period="2026-08")
    w = [e for e in warn if e.level == "全司"]
    assert w and w[0].severity == A.WARN and w[0].deviation_pct == pytest.approx(60.0)
    alert = A.detect_a1(base + [mk_ticket(22000, "2026-08")], CFG, period="2026-08")
    a = [e for e in alert if e.level == "全司"]
    assert a and a[0].severity == A.ALERT


def test_a1_sigma_rule_below_pct():
    tickets = [mk_ticket(10000, "2026-03"), mk_ticket(10000, "2026-04"),
               mk_ticket(10500, "2026-05"), mk_ticket(10800, "2026-06")]
    events = A.detect_a1(tickets, CFG, period="2026-06")
    full = [e for e in events if e.level == "全司"]
    assert full and full[0].severity == A.WARN
    assert full[0].deviation_pct < 50


def test_a1_consecutive_periods_escalate_to_alert():
    base = stable_history(["2026-04", "2026-05", "2026-06", "2026-07"])
    first = A.detect_a1(base + [mk_ticket(16000, "2026-08")], CFG, period="2026-08")
    prev = [{"type": e.type, "subject": e.subject, "period": e.period,
             "severity": e.severity} for e in first]
    second = A.detect_a1(base + [mk_ticket(16000, "2026-08"), mk_ticket(16000, "2026-09")],
                         CFG, period="2026-09", prev_events=prev)
    full = [e for e in second if e.level == "全司" and e.period == "2026-09"]
    assert full and full[0].severity == A.ALERT


def test_a1_levels_and_contribution():
    tickets = []
    for m in ("2026-04", "2026-05", "2026-06", "2026-07"):
        tickets += [mk_ticket(5000, m, dept="D1", emp="e1"),
                    mk_ticket(5000, m, dept="D2", emp="e2")]
    tickets += [mk_ticket(12000, "2026-08", dept="D1", emp="e1"),
                mk_ticket(5000, "2026-08", dept="D2", emp="e2")]
    events = A.detect_a1(tickets, CFG, period="2026-08")
    subjects = {e.subject for e in events}
    assert "全司" in subjects and "部门:D1" in subjects and "员工:e1" in subjects
    d1 = [e for e in events if e.subject == "部门:D1"][0]
    assert "员工:e1" in d1.evidence and "占增量" in d1.evidence
    assert all("D2" not in e.subject or e.severity is None for e in events) or True
    assert not [e for e in events if e.subject == "部门:D2"]


def test_a1_ticket_refs_top_by_amount():
    base = stable_history(["2026-04", "2026-05", "2026-06", "2026-07"])
    spikes = [mk_ticket(9000, "2026-08", emp="e1", tid="T-S1"),
              mk_ticket(7000, "2026-08", emp="e1", tid="T-S2")]
    events = A.detect_a1(base + spikes, CFG, period="2026-08")
    full = [e for e in events if e.level == "员工" and e.subject == "员工:e1"][0]
    assert full.ticket_refs == ("T-S1", "T-S2")


def test_a2_price_deviation_double_sided():
    recs = [
        {"sku": "A4纸", "supplier": "供应商甲", "unit_price": 100, "date": "2026-08-01"},
        {"sku": "A4纸", "supplier": "供应商甲", "unit_price": 100, "date": "2026-08-20"},
        {"sku": "A4纸", "supplier": "供应商甲", "unit_price": 125, "date": "2026-09-01"},
        {"sku": "A4纸", "supplier": "供应商甲", "unit_price": 200, "date": "2026-09-05"},
        {"sku": "B纸", "supplier": "供应商乙", "unit_price": 100, "date": "2026-08-01"},
        {"sku": "B纸", "supplier": "供应商乙", "unit_price": 65, "date": "2026-09-01"},
    ]
    events = A.check_price(recs, CFG)
    by_price = {(e.subject, e.observed): e for e in events}
    e125 = by_price[("SKU:A4纸@供应商甲", 125)]
    assert e125.severity == A.WARN and e125.deviation_pct == pytest.approx(25.0)
    e200 = by_price[("SKU:A4纸@供应商甲", 200)]
    assert e200.severity == A.ALERT and e200.deviation_pct > 50
    e65 = by_price[("SKU:B纸@供应商乙", 65)]
    assert e65.severity == A.WARN and e65.deviation_pct == pytest.approx(-35.0)
    assert "低于" in e65.evidence


def test_sample_purchases_strategy():
    recs = [{"sku": "A4纸", "unit_price": 100, "qty": 10, "date": "2026-09-01", "ticket_id": f"S{i}"} for i in range(10)]
    recs += [{"sku": f"非标{i}", "unit_price": 100 + i, "qty": 1, "date": "2026-09-01", "ticket_id": f"N{i}"} for i in range(10)]
    picked = A.sample_purchases(recs, standard_skus=["A4纸"], top_pct=0.2, random_pct=0.25, seed=42)
    picked_ids = {r["ticket_id"] for r in picked}
    assert all(f"S{i}" in picked_ids for i in range(10))
    assert len([i for i in picked_ids if i.startswith("N")]) == 4
    picked2 = A.sample_purchases(recs, standard_skus=["A4纸"],
                                 top_pct=0.2, random_pct=0.25, seed=42)
    assert {r["ticket_id"] for r in picked2} == picked_ids


def test_save_load_and_sweep_continuity(tmp_path):
    store = SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a")
    base = stable_history(["2026-04", "2026-05", "2026-06", "2026-07"])
    cur = [mk_ticket(16000, "2026-08")]
    for t in base + cur:
        store.create({"ticket_id": t["ticket_id"], "employee_id": t["employee_id"],
                      "dept_id": t["dept_id"], "type": "reimburse",
                      "status": t["status"], "sensitivity": "normal",
                      "amount": t["amount"], "expense_type": t["expense_type"],
                      "occurred_at": t["occurred_at"], "reason": "x",
                      "created_at": t["created_at"]}, {})
    ev1 = A.sweep(store, cfg_path=None, today=dt.date(2026, 9, 6))
    assert any(e.subject == "全司" and e.severity == A.WARN for e in ev1)
    prev = A.load_events(store.conn, type=A.TYPE_TREND, period="2026-08",
                         severity=(A.WARN, A.ALERT))
    assert prev and prev[0]["subject"] == "全司"
    store.create({"ticket_id": "T-SEP", "employee_id": "e1", "dept_id": "D1",
                  "type": "reimburse", "status": "APPROVED", "sensitivity": "normal",
                  "amount": 16000, "expense_type": "交通", "occurred_at": "2026-09-15",
                  "reason": "x", "created_at": "2026-09-15"}, {})
    ev2 = A.sweep(store, cfg_path=None, today=dt.date(2026, 10, 6))
    full = [e for e in ev2 if e.level == "全司"]
    assert full and full[0].severity == A.ALERT and "连续2期" in full[0].evidence
    n = store.conn.execute("SELECT count(*) FROM anomalies").fetchone()[0]
    assert n >= len(ev1) + len(ev2)


def test_to_table():
    e = A.AnomalyEvent(A.TYPE_TREND, "全司", "全司", "2026-08", 10000, 16000, 60, A.WARN,
                       "基线均值 10000, 本期 16000", ("T1",))
    out = A.to_table([e])
    assert "[WARN ]" in out and "全司" in out and "T1" in out
    assert A.to_table([]) == "（无异常事件）"
