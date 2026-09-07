import json

import pytest

from boss_secretary.core import extract as E
from boss_secretary.core import router as R
from boss_secretary.ingress import feishu as F


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    settings = {
        "feishu": {"app_id": "x", "app_secret": "y",
                   "roles": {"manager": "ou_m1", "boss": "ou_b1", "finance": "ou_f1"}},
        "storage": {"db_path": str(tmp_path / "s.db"),
                    "audit_dir": str(tmp_path / "audit")},
        "matrix": {"active": {"reimburse": "config/matrix/reimburse_v1.yaml"}},
        "rules_config": "config/rules.yaml",
        "flow_config": "config/flows/reimburse.yaml",
        "llm": {"provider": "cloud", "cloud": {"base_url": "http://x", "api_key": "k"}},
    }
    b = F.SecretaryBot(settings=settings)

    class Rec:
        def __init__(self):
            self.texts = []
            self.cards = []

        def send_text(self, oid, text):
            self.texts.append((oid, text))

        def send_card(self, oid, card):
            self.cards.append((oid, card))

    rec = Rec()
    b.send_text, b.send_card = rec.send_text, rec.send_card
    b.rec = rec

    def fake_extract(text, **kw):
        return {"amount": 300, "currency": "CNY", "expense_type": "交通",
                "occurred_at": "2026-09-05", "reason": "打车", "headcount": None,
                "invoice_no": "5001", "invoice_seller": "滴滴", "invoice_amount": 300,
                "sensitivity": "normal"}

    def fake_review(ctx, rr, **kw):
        return {"verdict": "WARN", "confidence": 0.8, "evidence": ["小额"],
                "suggestions": []}

    monkeypatch.setattr(F.E, "extract_ticket", fake_extract)
    monkeypatch.setattr(F.E, "review_with_llm", fake_review)
    return b


def test_approval_card_shape():
    card = F.approval_card("T1", 300, "打车", "manager")
    assert card["header"]["title"]["content"] == "报销审批 T1"
    buttons = card["elements"][-1]["actions"]
    assert buttons[0]["value"]["action"] == "approve"
    assert buttons[1]["value"]["role"] == "manager"


def test_submit_flow_creates_and_routes_card(bot):
    reply = bot.handle_text("ou_emp1", "9月5号打车300块")
    assert reply.startswith("已受理 T")
    assert "manager" in reply
    tid = F.TICKET_ID_RE.search(reply).group(0)
    t = bot.store.get(tid)
    assert t["status"] == R.SUBMITTED
    assert t["employee_id"] == "ou_emp1"
    assert t["ai_verdict"] == "MANAGER"
    assert len(bot.rec.cards) == 1
    oid, card = bot.rec.cards[0]
    assert oid == "ou_m1"
    assert card["elements"][-1]["actions"][0]["value"]["ticket_id"] == tid


def test_card_action_approve_completes_cosign(bot):
    bot.handle_text("ou_emp1", "9月5号打车300块")
    oid, card = bot.rec.cards[0]
    tid = card["elements"][-1]["actions"][0]["value"]["ticket_id"]
    out = bot.on_card_action("ou_m1", {"action": "approve", "ticket_id": tid,
                                       "role": "manager"})
    assert "会签完成" in out
    assert bot.store.get(tid)["status"] == R.APPROVED


def test_card_action_reject(bot):
    bot.handle_text("ou_emp1", "9月5号打车300块")
    _, card = bot.rec.cards[0]
    tid = card["elements"][-1]["actions"][0]["value"]["ticket_id"]
    out = bot.on_card_action("ou_b1", {"action": "reject", "ticket_id": tid,
                                       "role": "manager"})
    assert "已驳回" in out
    assert bot.store.get(tid)["status"] == R.REJECTED


def test_card_action_invalid_role(bot):
    bot.handle_text("ou_emp1", "9月5号打车300块")
    _, card = bot.rec.cards[0]
    tid = card["elements"][-1]["actions"][0]["value"]["ticket_id"]
    out = bot.on_card_action("ou_x", {"action": "approve", "ticket_id": tid,
                                      "role": "finance_consign"})
    assert "操作失败" in out


def test_withdraw_command(bot):
    reply = bot.handle_text("ou_emp1", "9月5号打车300块")
    tid = F.TICKET_ID_RE.search(reply).group(0)
    reply = bot.handle_text("ou_emp1", f"撤回 {tid}")
    assert "已撤回" in reply
    assert bot.store.get(tid)["status"] == R.WITHDRAWN
    reply2 = bot.handle_text("ou_emp2", f"撤回 {tid}")
    assert "撤回失败" in reply2


def test_progress_command(bot):
    assert "暂无" in bot.handle_text("ou_emp1", "进度")
    bot.handle_text("ou_emp1", "9月5号打车300块")
    out = bot.handle_text("ou_emp1", "我的报销")
    assert "你的报销单" in out and "SUBMITTED" in out


def test_missing_fields_asks_back(bot, monkeypatch):
    monkeypatch.setattr(F.E, "extract_ticket",
                        lambda text, **kw: {"amount": None, "occurred_at": None,
                                            "reason": None, "expense_type": None,
                                            "invoice_no": None})
    out = bot.handle_text("ou_emp1", "帮我报一下聚餐")
    assert "请补充" in out and "金额" in out and "发票号" in out
    assert not bot.rec.cards


def test_employee_persisted_once(bot):
    bot.handle_text("ou_emp9", "9月5号打车300块")
    bot.handle_text("ou_emp9", "进度")
    n = bot.store.conn.execute(
        "SELECT count(*) FROM employees WHERE feishu_user_id='ou_emp9'").fetchone()[0]
    assert n == 1


def test_mark_paid_closes_ticket(bot):
    reply = bot.handle_text("ou_emp1", "9月5号打车300块")
    tid = F.TICKET_ID_RE.search(reply).group(0)
    bot.on_card_action("ou_m1", {"action": "approve", "ticket_id": tid, "role": "manager"})
    assert bot.store.get(tid)["status"] == R.APPROVED
    out = bot.handle_text("ou_f1", f"打款 {tid}")
    assert "已确认打款" in out
    assert bot.store.get(tid)["status"] == "PAID"
    out2 = bot.handle_text("ou_f1", f"打款 {tid}")
    assert "失败" in out2
    paid_events = [c for _, c in bot.rec.cards if c["header"]["title"]["content"].startswith("打款确认")]
    assert paid_events


def test_invoice_image_merge_flow(bot, monkeypatch):
    monkeypatch.setattr(bot, "download_image", lambda key, mid="": b"fake")
    def fake_vision(image_b64, **kw):
        return {"invoice_no": "62589335", "invoice_amount": 469,
                "invoice_date": "2026-09-05", "invoice_seller": "星巴克",
                "expense_type": "餐饮"}
    monkeypatch.setattr(F.E, "extract_invoice_image", fake_vision)
    monkeypatch.setattr(F.E, "extract_ticket",
                        lambda text, **kw: {"amount": None, "currency": "CNY",
                                            "expense_type": None,
                                            "occurred_at": "2026-09-05",
                                            "reason": "和客户在星巴克的工作餐",
                                            "headcount": None, "invoice_no": None,
                                            "invoice_seller": None,
                                            "invoice_amount": None,
                                            "sensitivity": "normal"})
    out = bot.handle_image("ou_emp1", "img_key_x", "om_x")
    assert "发票要素已识别" in out and "请补充" in out
    reply = bot.handle_text("ou_emp1", "9月5号和客户在星巴克的工作餐")
    assert "已受理" in reply or "自动通过" in reply
    tickets = bot.store.conn.execute("SELECT status, amount FROM tickets").fetchall()
    assert tickets and tickets[-1][1] == 469


def test_invoice_crosscheck_mismatch_guards(bot, monkeypatch):
    monkeypatch.setattr(bot, "download_image", lambda key, mid="": b"fake")
    def fake_vision(image_b64, **kw):
        return {"invoice_no": "X1", "invoice_amount": 999,
                "invoice_date": "2026-09-05", "expense_type": "交通"}
    monkeypatch.setattr(F.E, "extract_invoice_image", fake_vision)
    monkeypatch.setattr(F.E, "extract_ticket",
                        lambda text, **kw: {"amount": 300, "currency": "CNY",
                                            "expense_type": "交通",
                                            "occurred_at": "2026-09-05",
                                            "reason": "打车", "headcount": None,
                                            "invoice_no": "X1", "invoice_seller": None,
                                            "invoice_amount": None,
                                            "sensitivity": "normal"})
    bot.handle_image("ou_emp1", "img_k", "om_k")
    reply = bot.handle_text("ou_emp1", "9月5号打车300块，发票X1")
    assert "不一致" in reply and "按此提交" in reply
    ok = bot.handle_text("ou_emp1", "按此提交")
    assert "已受理" in ok or "自动通过" in ok


def test_pdf_signature_detection(bot, monkeypatch):
    monkeypatch.setattr(bot, "download_file", lambda key, mid="": b"fake")
    monkeypatch.setattr(F.E, "extract_invoice_pdf",
                        lambda data, **kw: {"invoice_no": "P1", "invoice_amount": 300,
                                            "invoice_date": "2026-09-05",
                                            "e_signature": True})
    out = bot.handle_file("ou_emp1", "fk", "om_f", "发票.pdf")
    assert "电子签章" in out
