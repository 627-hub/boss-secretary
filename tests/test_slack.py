"""Slack adapter 单元测试：Socket Mode 请求处理 + 卡片转 Block Kit。"""
from __future__ import annotations

import json
import time

from boss_secretary.ingress import slack as SL


class FakeWeb:
    def __init__(self):
        self.calls = []

    def chat_postMessage(self, channel=None, text=None, blocks=None, **kw):
        self.calls.append({"channel": channel, "text": text, "blocks": blocks})

    def files_upload_v2(self, channel=None, file=None, filename=None, **kw):
        self.calls.append({"channel": channel, "file": file})


class FakeSocket:
    def __init__(self):
        self.responses = []

    def send_socket_mode_response(self, resp):
        self.responses.append(resp)


class RecordingBot:
    def __init__(self):
        self.card_actions = []
        self.handled = []

    def on_card_action(self, user, value):
        self.card_actions.append((user, value))
        return "ok"

    def handle_text(self, actor, text):
        self.handled.append((actor, text))
        return "收到"

    def handle_image_bytes(self, actor, data):
        self.handled.append(("img", actor))
        return "图片收到"

    def handle_file_bytes(self, actor, data, filename):
        self.handled.append(("file", actor, filename))
        return f"文件已处理: {filename}"


def build_adapter():
    bot = RecordingBot()
    ad = SL.SlackAdapter(bot, "xoxb-test", "xapp-test",
                         web_client=FakeWeb(), socket_client=FakeSocket())
    return ad, bot


class FakeReq:
    def __init__(self, rtype, payload):
        self.type = rtype
        self.payload = payload
        self.envelope_id = "env1"


def card():
    return {"header": {"template": "orange",
                       "title": {"tag": "plain_text", "content": "报销审批 T1"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": "金额: 300元"}},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "同意"},
                     "value": {"action": "approve", "ticket_id": "T1"}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "驳回"},
                     "value": {"action": "reject", "ticket_id": "T1"}}]}]}


def test_card_to_blocks():
    blocks = SL.card_to_blocks(card())
    assert blocks[0]["type"] == "header"
    assert "报销审批 T1" in blocks[0]["text"]["text"]
    assert "300元" in blocks[1]["text"]["text"]
    actions = blocks[2]
    assert actions["type"] == "actions"
    assert json.loads(actions["elements"][0]["value"]) == {
        "action": "approve", "ticket_id": "T1"}


def test_card_to_blocks_form():
    card = {"header": {"title": {"tag": "plain_text", "content": "报销审批 T1"}},
            "elements": [{"tag": "form", "name": "f", "elements": [
                {"tag": "input", "name": "comment",
                 "placeholder": {"tag": "plain_text", "content": "审批意见"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "同意"},
                 "value": {"action": "approve", "ticket_id": "T1"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "驳回"},
                 "value": {"action": "reject", "ticket_id": "T1"}}]}]}
    blocks = SL.card_to_blocks(card)
    actions = [b for b in blocks if b["type"] == "actions"]
    assert actions and len(actions[0]["elements"]) == 2
    assert json.loads(actions[0]["elements"][0]["value"])["action"] == "approve"
    assert any(b["type"] == "context" for b in blocks)


def test_handle_socket_events_message():
    ad, bot = build_adapter()
    req = FakeReq("events_api", {"event": {
        "type": "message", "channel_type": "im", "user": "U1",
        "text": "报销98", "channel": "D1"}})
    ad.handle_socket_request(req)
    time.sleep(0.3)
    assert bot.handled == [("U1", "报销98")]
    assert len(ad.socket.responses) == 1


def test_event_to_envelopes_filters():
    ad, _ = build_adapter()
    assert ad.event_to_envelopes({"type": "message", "bot_id": "B1",
                                  "channel_type": "im", "text": "回显"}) == []
    assert ad.event_to_envelopes({"type": "message", "user": "U1",
                                  "text": "群消息"}) == []
    envs = ad.event_to_envelopes({"type": "message", "channel_type": "im",
                                  "user": "U1", "text": "进度"})
    assert envs[0].text == "进度" and envs[0].actor == "U1"


def test_block_actions_route_to_card_action():
    ad, bot = build_adapter()
    req = FakeReq("interactive", {"actions": [
        {"value": json.dumps({"action": "approve", "ticket_id": "T1"})}],
        "user": {"id": "U1"}})
    ad.handle_socket_request(req)
    time.sleep(0.3)
    assert bot.card_actions == [("U1", {"action": "approve", "ticket_id": "T1"})]


def test_app_mention_strips_mention():
    ad, _ = build_adapter()
    envs = ad.event_to_envelopes({"type": "app_mention", "user": "U1",
                                  "text": "<@U888> 报销98"})
    assert envs[0].text == "报销98"


def test_send_text_and_card_via_web():
    ad, _ = build_adapter()
    ad.send_text("U1", "hello")
    ad.send_card("U1", card())
    assert ad.web.calls[0] == {"channel": "U1", "text": "hello", "blocks": None}
    card_call = ad.web.calls[1]
    assert card_call["blocks"][0]["text"]["text"] == "报销审批 T1"


def test_file_event_downloads_and_routes(monkeypatch):
    ad, bot = build_adapter()
    monkeypatch.setattr(ad, "_download", lambda url: b"pdfbytes")
    envs = ad.event_to_envelopes({"type": "message", "channel_type": "im",
                                  "user": "U1",
                                  "files": [{"url_private_download":
                                             "https://x/f.pdf",
                                             "mimetype": "application/pdf",
                                             "name": "报价单.pdf"}],
                                  "text": "采购附件"})
    assert envs[0].attachment.kind == "file"
    assert envs[0].attachment.filename == "报价单.pdf"
    assert envs[0].attachment.data == b"pdfbytes"
