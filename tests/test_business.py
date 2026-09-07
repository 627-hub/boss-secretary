import datetime as dt

import pytest

from boss_secretary.core import contract as CT
from boss_secretary.core import extract as E
from boss_secretary.core import matrix as M
from boss_secretary.core import router as R
from boss_secretary.core.router import SQLiteTicketStore


@pytest.fixture()
def store(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a")


def test_contract_lifecycle(store):
    cid = CT.create(store.conn, employee_id="e1", dept_id="D1", title="XX采购合同",
                    supplier="YY公司", amount=50000, start_date="2026-09-01",
                    end_date="2027-08-31", payment_terms="分三期")
    c = CT.get(store.conn, cid)
    assert c["status"] == CT.PENDING_REVIEW

    st = CT.approve(store.conn, cid, "legal1", "legal")
    assert st == CT.REVIEWING
    st = CT.approve(store.conn, cid, "boss1", "boss")
    assert st == CT.ACTIVE
    with pytest.raises(ValueError):
        CT.approve(store.conn, cid, "x", "legal")

    CT.reject(store.conn, cid, "boss1", "boss", "x") if False else None
    store.conn.execute("UPDATE contracts SET status='active' WHERE contract_id=?",
                       (cid,))
    store.conn.commit()
    pid = CT.create_payment(store.conn, contract_id=cid, procurement_id=None,
                            amount=20000, seq="第一期", employee_id="e1")
    pm = CT.pay(store.conn, pid, "f1")
    assert pm["contract_id"] == cid
    assert CT.paid_total(store.conn, cid) == 20000
    with pytest.raises(ValueError):
        CT.pay(store.conn, pid, "f1")


def test_renew_marks_old_and_creates_pending(store):
    cid = CT.create(store.conn, employee_id="e1", dept_id=None, title="服务合同",
                    supplier="YY", amount=10000, start_date="2026-01-01",
                    end_date="2026-12-31")
    store.conn.execute("UPDATE contracts SET status=? WHERE contract_id=?",
                       (CT.ACTIVE, cid))
    store.conn.commit()
    new_id = CT.renew(store.conn, cid, "2027-12-31", actor_id="boss1")
    assert CT.get(store.conn, cid)["status"] == CT.RENEWED
    new_c = CT.get(store.conn, new_id)
    assert new_c["status"] == CT.PENDING_REVIEW
    assert new_c["end_date"] == "2027-12-31"
    assert new_c["title"] == "服务合同"


def test_expiring_within_days(store):
    cid = CT.create(store.conn, employee_id="e1", dept_id=None, title="临期合同",
                    supplier="Z", amount=1000, start_date="2026-01-01",
                    end_date=str(dt.date(2026, 9, 20)))
    store.conn.execute("UPDATE contracts SET status=? WHERE contract_id=?",
                       (CT.ACTIVE, cid))
    store.conn.commit()
    out = CT.expiring(store.conn, days=30, now=dt.date(2026, 9, 7))
    assert any(c["contract_id"] == cid and c["days_left"] == 13 for c in out)
    far = CT.create(store.conn, employee_id="e1", dept_id=None, title="远期",
                    supplier="Z", amount=1, start_date="2026-01-01",
                    end_date="2027-09-20")
    store.conn.execute("UPDATE contracts SET status=? WHERE contract_id=?",
                       (CT.ACTIVE, far))
    store.conn.commit()
    assert not any(c["contract_id"] == far for c in
                   CT.expiring(store.conn, days=30, now=dt.date(2026, 9, 7)))


def test_extract_procurement_and_contract():
    fake = lambda msgs: {"title": "测试服务器", "supplier": "XX电脑", "amount": 8000,
                         "ptype": "设备", "reason": "开发测试"}  # noqa: E731
    p = E.extract_procurement("采购测试服务器8000元", llm_fn=fake)
    assert p["amount"] == 8000 and p["ptype"] == "设备"
    c = E.extract_contract("登记合同 XX合同", llm_fn=lambda m: {
        "title": "XX合同", "supplier": "YY", "amount": 50000,
        "start_date": "2026-09-01", "end_date": "2027-08-31",
        "payment_terms": "分三期"})
    assert c["start_date"] == "2026-09-01"
    rv = E.review_contract("合同文本", ["含违约责任"], llm_fn=lambda m: {
        "verdict": "WARN", "risks": [{"level": "中", "clause": "付款", "note": "节点模糊"}],
        "missing": ["验收标准"]})
    assert rv["verdict"] == "WARN" and rv["missing"] == ["验收标准"]


def test_procurement_matrix_routing():
    m = M.load("config/matrix/procure_v1.yaml")
    d1 = M.evaluate(m, {"ptype": "设备", "amount": 3000, "rule_result": "PASS",
                        "llm_verdict": "PASS"})
    assert (d1.hit_rule_id, d1.action) == ("P01", "MANAGER_BOSS")
    d2 = M.evaluate(m, {"ptype": "设备", "amount": 80000, "rule_result": "PASS",
                        "llm_verdict": "PASS"})
    assert d2.action == "MANAGER_BOSS"
    assert d2.notify == ("legal", "finance")


def test_procurement_flow_via_router(store):
    flow = R.load_flow("config/flows/reimburse.yaml")
    router = R.Router(flow, M.load("config/matrix/procure_v1.yaml"), store,
                      today_fn=lambda: dt.date(2026, 9, 7))
    tid = router.create_ticket({"title": "测试服务器", "supplier": "XX电脑",
                                "amount": 8000, "ptype": "设备",
                                "reason": "开发测试", "llm_verdict": "PASS"},
                               {"user_id": "e1", "dept_id": "D1"},
                               type_override="procurement")
    o = router.run_review(tid, llm_verdict="PASS")
    assert o.next_status == R.SUBMITTED
    assert "manager" in o.approver_roles and "boss" in o.approver_roles
    t = store.get(tid)
    assert t["type"] == "procurement"
    router.approve(tid, "m1", "manager")
    router.approve(tid, "b1", "boss")
    assert store.get(tid)["status"] == R.APPROVED
