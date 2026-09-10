"""卡片动作注册表测试：覆盖、分发、异常语义。"""
from types import SimpleNamespace

from boss_secretary.core import router as R
from boss_secretary.ingress import actions as ACT


class FakeRouter:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []

    def approve(self, *args):
        if self.exc:
            raise self.exc
        self.calls.append(args)
        return R.SUBMITTED


def fake_bot(exc=None):
    return SimpleNamespace(router=FakeRouter(exc), roles={}, store=None,
                           settings={}, send_text=lambda *a: None,
                           send_card=lambda *a: None)


def test_handlers_cover_all_card_actions():
    assert set(ACT.HANDLERS) == {
        "approve", "reject", "paid", "allowance_approve", "allowance_reject",
        "trip_approve", "trip_reject", "loan_approve", "loan_reject",
        "seal_approve", "seal_reject", "supplier_approve", "supplier_reject",
        "contract_approve", "contract_reject", "payment_paid"}


def test_unknown_action():
    assert "未知动作" in ACT.dispatch(fake_bot(), "ou", {"action": "bogus"})


def test_dispatch_wraps_router_error():
    out = ACT.dispatch(fake_bot(exc=R.RouterError("炸了")), "ou",
                       {"action": "approve"})
    assert "操作失败: 炸了" in out


def test_dispatch_approve_passes_comment():
    bot = fake_bot()
    out = ACT.dispatch(bot, "ou_m", {"action": "approve", "ticket_id": "T1",
                                     "role": "manager"}, "同意")
    assert "已附意见" in out
    assert bot.router.calls == [("T1", "ou_m", "manager", "同意")]
