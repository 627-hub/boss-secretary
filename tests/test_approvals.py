"""统一审批动作层测试：记录/查阅/渲染/表单 + 各审批类型接入。"""
import pytest

from boss_secretary.core import allowance as AL
from boss_secretary.core import approvals as AP
from boss_secretary.core import contract as CT
from boss_secretary.core import supplier as SUP
from boss_secretary.core import travel as TR
from boss_secretary.core.router import SQLiteTicketStore


@pytest.fixture()
def conn(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a").conn


def test_record_history_and_render(conn):
    AP.record(conn, doc_type="contract", doc_id="C1", actor="u1", role="legal",
              decision=AP.APPROVE, comment="条款已核")
    AP.record(conn, doc_type="contract", doc_id="C1", actor="u2", role="boss",
              decision=AP.REJECT, comment="金额超授权")
    hist = AP.history(conn, "contract", "C1")
    assert [h["decision"] for h in hist] == [AP.APPROVE, AP.REJECT]
    assert hist[0]["comment"] == "条款已核"
    text = AP.render(hist)
    assert "法务" in text and "同意" in text and "条款已核" in text
    assert "老板" in text and "驳回" in text and "金额超授权" in text
    rj = AP.latest_reject(hist)
    assert rj and rj["comment"] == "金额超授权"
    assert AP.history(conn, "contract", "C2") == []


def test_decision_form_and_parse_comment():
    form = AP.decision_form([
        {"tag": "button", "text": {"tag": "plain_text", "content": "同意"},
         "type": "primary", "value": {"action": "approve", "ticket_id": "T1"}},
        {"tag": "button", "text": {"tag": "plain_text", "content": "驳回"},
         "type": "danger", "value": {"action": "reject", "ticket_id": "T1"}}])
    assert form["tag"] == "form"
    inputs = [e for e in form["elements"] if e["tag"] == "input"]
    assert inputs[0]["name"] == "comment"
    buttons = [e for e in form["elements"] if e["tag"] == "button"]
    assert all(b["action_type"] == "form_submit" for b in buttons)
    assert buttons[0]["name"] != buttons[1]["name"]
    assert buttons[0]["value"]["action"] == "approve"
    assert AP.parse_comment({"comment": " 同意 "}) == "同意"
    assert AP.parse_comment(None) == ""
    assert AP.parse_comment({"other": "x"}) == ""


def test_contract_decisions_recorded(conn):
    cid = CT.create(conn, employee_id="e1", dept_id="D1", title="XX合同",
                    supplier="YY", amount=50000, start_date="2026-09-01",
                    end_date="2027-08-31")
    CT.approve(conn, cid, "l1", "legal", "条款已核")
    CT.approve(conn, cid, "l1", "legal", "重复提交不应重复记录")
    hist = AP.history(conn, "contract", cid)
    assert len(hist) == 1 and hist[0]["comment"] == "条款已核"
    CT.reject(conn, cid, "b1", "boss", "金额超授权")
    hist = AP.history(conn, "contract", cid)
    assert hist[-1]["decision"] == AP.REJECT
    assert hist[-1]["comment"] == "金额超授权"


def test_supplier_decisions_recorded(conn):
    s = SUP.create_request(conn, name="YY科技有限公司", created_by="e1")
    SUP.approve(conn, s["supplier_id"], "f1", "finance", "资质已核")
    SUP.reject(conn, s["supplier_id"], "b1", "boss", "经营范围不符")
    hist = AP.history(conn, "supplier", s["supplier_id"])
    assert [h["decision"] for h in hist] == [AP.APPROVE, AP.REJECT]
    assert hist[-1]["comment"] == "经营范围不符"


def test_travel_and_allowance_decisions_recorded(conn):
    t = TR.trip_request(conn, employee_id="e1", dept_id="D1",
                        destination="上海", reason="见客户", estimate=3000,
                        start_date="2026-09-10", end_date="2026-09-14")
    TR.decide_trip(conn, t["trip_id"], "m1", True, note="行程合理")
    hist = AP.history(conn, "trip", t["trip_id"])
    assert hist[0]["comment"] == "行程合理" and hist[0]["role"] == "manager"

    l = TR.loan_request(conn, employee_id="e1", amount=2000, reason="备用金")
    TR.loan_decide(conn, l["loan_id"], "b1", False, note="先走报销")
    hist = AP.history(conn, "loan", l["loan_id"])
    assert hist[0]["decision"] == AP.REJECT and hist[0]["comment"] == "先走报销"

    a = AL.create_request(conn, "e1", "打车", 500, reason="加班")
    AL.decide(conn, a["allowance_id"], "m1", True, note="额度合理")
    hist = AP.history(conn, "allowance", a["allowance_id"])
    assert hist[0]["comment"] == "额度合理"
