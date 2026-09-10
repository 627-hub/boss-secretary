"""通知出口测试：异常隔离 / 去重 / 空 uid。"""
from boss_secretary.core import notify


class Target:
    def __init__(self, fail=False):
        self.texts = []
        self.cards = []
        self.fail = fail

    def send_text(self, uid, text):
        if self.fail:
            raise RuntimeError("掉线")
        self.texts.append((uid, text))

    def send_card(self, uid, card):
        if self.fail:
            raise RuntimeError("掉线")
        self.cards.append((uid, card))


def test_send_text_isolates_exception(capsys):
    t = Target(fail=True)
    assert notify.send_text(t, "u1", "x") is False
    assert "发送失败" in capsys.readouterr().out


def test_send_text_skips_empty_uid():
    t = Target()
    assert notify.send_text(t, None, "x") is False
    assert notify.send_text(t, "", "x") is False
    assert t.texts == []


def test_send_texts_dedup_and_order():
    t = Target()
    n = notify.send_texts(t, ["a", "b", "a", None, "c"], "hi")
    assert n == 3
    assert [u for u, _ in t.texts] == ["a", "b", "c"]


def test_send_card_ok_and_isolated():
    t = Target()
    assert notify.send_card(t, "u", {"x": 1}) is True
    assert t.cards == [("u", {"x": 1})]
    assert notify.send_card(Target(fail=True), "u", {}) is False
