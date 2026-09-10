"""卡片组件层测试：决策卡片必须带意见输入框；打款卡片用按钮。"""
from boss_secretary.core import cards as CD


def _form_inputs(card):
    for el in card["elements"]:
        if el.get("tag") == "form":
            return [e for e in el["elements"] if e.get("tag") == "input"]
    return []


def _actions(card):
    for el in card["elements"]:
        if el.get("tag") == "action":
            return el["actions"]
    return []


def test_decision_cards_have_comment_form():
    cases = [
        CD.approval_card("T1", 300, "打车", "manager"),
        CD.contract_review_card("C1", "XX合同", "YY", 1000, []),
        CD.seal_card("Y1", "公章", "授权书", 1, "ou_e"),
        CD.supplier_card("S1", "XX公司", None),
        CD.trip_card({"trip_id": "TR1", "destination": "上海",
                      "start_date": "2026-09-10", "end_date": "2026-09-14",
                      "estimate": 3000, "reason": "见客户"}, "ou_e"),
        CD.loan_card({"loan_id": "L1", "amount": 2000, "reason": "备用金"}, "ou_e"),
        CD.allowance_card({"allowance_id": "A1", "category": "打车", "amount": 500,
                           "reason": "加班", "expires_at": "2026-10-01"}, "ou_e"),
    ]
    for card in cases:
        inputs = _form_inputs(card)
        assert inputs and inputs[0]["name"] == "comment", card["header"]


def test_paid_and_payment_cards_use_buttons():
    for card in (CD.paid_card("T1", 300),
                 CD.payment_card("PM1", 5000, "第一期")):
        acts = _actions(card)
        assert acts and acts[0]["value"]["action"] in ("paid", "payment_paid")


def test_history_block_rendered():
    hist = [{"role": "manager", "decision": "approve", "comment": "金额属实",
             "created_at": "2026-09-10 10:00:00"}]
    card = CD.approval_card("T1", 300, "打车", "boss", history=hist)
    text = str(card["elements"])
    assert "审批意见" in text and "经理" in text and "金额属实" in text
