import datetime as dt

import pytest

from boss_secretary.core import allowance as AL
from boss_secretary.core import router as R
from boss_secretary.core.router import SQLiteTicketStore

TODAY = dt.date(2026, 9, 7)


@pytest.fixture()
def store(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a")


def test_request_approve_use_exhausted(store):
    cfg = AL.load_config(None)
    req = AL.create_request(store.conn, "e1", "打车", 200, reason="加班打车",
                            expense_types=["交通"], cfg=cfg)
    assert req["required_role"] == "manager"
    a = AL.get(store.conn, req["allowance_id"])
    assert a["status"] == AL.PENDING

    assert AL.decide(store.conn, req["allowance_id"], "m1", approve=True)
    a = AL.get(store.conn, req["allowance_id"])
    assert a["status"] == AL.ACTIVE

    m, over = AL.match(store.conn, "e1", {"expense_type": "交通", "amount": 98})
    assert m and over == 0
    rem = AL.consume(store.conn, m["allowance_id"], 98)
    assert rem == 102
    m2, over2 = AL.match(store.conn, "e1", {"expense_type": "交通", "amount": 150})
    assert m2 is None
    m3, over3 = AL.match(store.conn, "e1", {"expense_type": "交通", "amount": 102})
    assert m3 is not None and over3 == 0


def test_over_limit_escalates_to_boss(store):
    req = AL.create_request(store.conn, "e1", "差旅", 5000, cfg=AL.load_config(None))
    assert req["required_role"] == "boss"


def test_expired_allowance_not_matched(store):
    cfg = AL.load_config(None)
    req = AL.create_request(store.conn, "e1", "打车", 200, cfg=cfg)
    AL.decide(store.conn, req["allowance_id"], "m1", approve=True)
    store.conn.execute("UPDATE allowances SET expires_at='2026-08-01'"
                       " WHERE allowance_id=?", (req["allowance_id"],))
    store.conn.commit()
    m, _ = AL.match(store.conn, "e1", {"expense_type": "交通", "amount": 100})
    assert m is None
    assert AL.expire_sweep(store.conn) >= 1


def test_category_mismatch_not_matched(store):
    req = AL.create_request(store.conn, "e1", "打车", 200, cfg=AL.load_config(None))
    AL.decide(store.conn, req["allowance_id"], "m1", approve=True)
    m, _ = AL.match(store.conn, "e1", {"expense_type": "餐饮", "amount": 100})
    assert m is None


def test_parse_and_extract_allowance_text():
    p = AL.parse_request_text("申请打车额度200元，今晚加班打车用")
    assert p["category"] == "打车" and p["amount"] == 200
    p2 = AL.parse_request_text("出差差旅备用金3000")
    assert p2["category"] == "差旅" and p2["amount"] == 3000
    p3 = AL.extract_allowance("申请额度500", llm_fn=lambda m: {
        "category": "打车", "amount": 500, "reason": "加班",
        "expense_types": ["交通"]})
    assert p3["expense_types"] == ["交通"]


def test_router_allowance_auto_approve(store):
    flow = R.load_flow("config/flows/reimburse.yaml")
    matrix = None
    from boss_secretary.core import matrix as M
    matrix = M.load("config/matrix/reimburse_v1.yaml")
    router = R.Router(flow, matrix, store, today_fn=lambda: TODAY)
    req = AL.create_request(store.conn, "e1", "打车", 200, cfg=AL.load_config(None))
    AL.decide(store.conn, req["allowance_id"], "m1", approve=True)
    router.create_ticket({"amount": 98, "expense_type": "交通",
                          "occurred_at": "2026-09-06", "reason": "加班打车",
                          "invoice_no": "1"},
                         {"user_id": "e1", "dept_id": "D1"})
    tid = store.conn.execute("SELECT ticket_id FROM tickets").fetchone()[0]
    st = router.allowance_auto_approve(tid, req["allowance_id"])
    assert st == R.AUTO_APPROVED
    t = store.get(tid)
    assert t["status"] == R.AUTO_APPROVED and t["ai_verdict"] == "ALLOWANCE"
    with pytest.raises(R.TransitionError):
        router.allowance_auto_approve(tid, req["allowance_id"])
