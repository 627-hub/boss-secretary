"""命令注册表测试：注册顺序、命中、未命中回落。"""
from types import SimpleNamespace

import pytest

from boss_secretary.core.router import SQLiteTicketStore
from boss_secretary.ingress import commands as CMD


@pytest.fixture()
def bot(tmp_path):
    store = SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a")
    b = SimpleNamespace(
        store=SimpleNamespace(conn=store.conn),
        roles={"boss": "ou_b", "finance": "ou_f", "audit": "ou_a"},
        settings={},
        _pending={},
        sent=[],
    )
    b.get_or_create_employee = lambda oid: {"user_id": oid, "dept_id": "D1"}
    b.send_card = lambda oid, card: b.sent.append((oid, card))
    b._supplier_gate = lambda name: None
    return b


def test_registry_order_is_stable():
    names = [n for n, _, _ in CMD.COMMANDS]
    assert names == [
        "预算", "供应商", "用印", "审计", "撤回", "打款", "进度", "我的额度",
        "导出凭证", "额度申请", "登记合同", "续签", "付款", "合同查询", "采购",
        "出差申请", "我的出差", "借款申请", "我的借款", "核销借款", "借款用法",
        "取消"]


def test_free_text_falls_through(bot):
    assert CMD.route(bot, "ou_x", "9月5号打车98块，滴滴发票") is None


def test_progress_route(bot):
    out = CMD.route(bot, "ou_x", "进度")
    assert "暂无报销单" in out


def test_audit_requires_privilege(bot):
    assert "仅审计/老板" in CMD.route(bot, "ou_x", "审计")
    assert "审计工作台" in CMD.route(bot, "ou_a", "审计")


def test_cancel_clears_pending(bot):
    bot._pending["ou_x"] = {"ctx": {}}
    assert "已放弃" in CMD.route(bot, "ou_x", "取消")
    assert "ou_x" not in bot._pending
