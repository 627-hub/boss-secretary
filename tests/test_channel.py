import json

import pytest

from boss_secretary.core.channel import (Attachment, ChannelAdapter,
                                         ConversationDispatcher, Envelope,
                                         card_to_text)


class FakeBot:
    def __init__(self):
        self.calls = []

    def handle_text(self, actor, text):
        self.calls.append(("text", actor, text))
        return f"收到: {text}"

    def handle_image_bytes(self, actor, data):
        self.calls.append(("image", actor, data))
        return "图片已处理"

    def handle_file_bytes(self, actor, data, filename):
        self.calls.append(("file", actor, filename))
        return f"文件已处理: {filename}"


class FakeAdapter(ChannelAdapter):
    channel = "fake"

    def __init__(self):
        self.sent = []

    def send_text(self, actor, text):
        self.sent.append(("text", actor, text))


def test_dispatcher_routes_text():
    bot, ad = FakeBot(), FakeAdapter()
    d = ConversationDispatcher(bot, ad)
    d.process(Envelope(channel="fake", actor="u1", text="报销98"))
    assert bot.calls == [("text", "u1", "报销98")]
    assert ad.sent == [("text", "u1", "收到: 报销98")]


def test_dispatcher_routes_image_and_file():
    bot, ad = FakeBot(), FakeAdapter()
    d = ConversationDispatcher(bot, ad)
    d.process(Envelope(channel="fake", actor="u1",
                       attachment=Attachment("image", b"img", "a.jpg")))
    d.process(Envelope(channel="fake", actor="u1",
                       attachment=Attachment("file", b"pdf", "合同.pdf")))
    assert ("image", "u1", b"img") in bot.calls
    assert ("file", "u1", "合同.pdf") in bot.calls


def test_dispatcher_exception_fallback():
    class BoomBot(FakeBot):
        def handle_text(self, actor, text):
            raise RuntimeError("炸了")
    bot, ad = BoomBot(), FakeAdapter()
    d = ConversationDispatcher(bot, ad)
    d.process(Envelope(channel="fake", actor="u1", text="x"))
    assert "处理出错" in ad.sent[0][2]


def test_card_to_text_fallback():
    card = {"header": {"title": {"content": "报销审批 T1"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": "金额: 300元"}},
                {"tag": "action", "actions": [
                    {"text": {"content": "同意"}},
                    {"text": {"content": "驳回"}}]}]}
    text = card_to_text(card)
    assert "报销审批 T1" in text and "金额: 300元" in text
    assert "同意 / 驳回" in text


def test_card_to_text_form_fallback():
    card = {"header": {"title": {"content": "报销审批 T1"}},
            "elements": [
                {"tag": "form", "name": "f", "elements": [
                    {"tag": "input", "name": "comment",
                     "placeholder": {"tag": "plain_text", "content": "审批意见"}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "同意"}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "驳回"}}]}]}
    text = card_to_text(card)
    assert "审批意见" in text and "同意 / 驳回" in text


def test_channel_adapter_abc():
    with pytest.raises(TypeError):
        ChannelAdapter()
