"""TelegramAdapter 单元测试：mock requests，验证长轮询→Envelope→业务分发。"""
from __future__ import annotations

import pytest

from boss_secretary.ingress import telegram as TG


class FakeSession:
    def __init__(self, updates=None, file_bytes=b"fake"):
        self.updates = updates or []
        self.sent = []
        self.file_bytes = file_bytes

    def post(self, url, json=None, data=None, files=None, timeout=None):
        self.sent.append(("post", url, json, data))
        if "sendMessage" in url:
            return FakeResp(True, {"ok": True})
        return FakeResp(True, {"ok": True})

    def get(self, url, params=None, timeout=None):
        self.sent.append(("get", url, params))
        if "getUpdates" in url:
            return FakeResp(True, {"ok": True, "result": self.updates})
        if "getFile" in url:
            return FakeResp(True, {"ok": True,
                                   "result": {"file_path": "docs/f.pdf"}})
        return FakeResp(True, {"ok": True})


class FakeResp:
    def __init__(self, ok, data):
        self._ok = ok
        self._data = data

    def json(self):
        return self._data

    @property
    def ok(self):
        return self._ok

    @property
    def content(self):
        import json
        return json.dumps(self._data).encode()


def test_to_envelopes_text():
    upd = {"update_id": 1, "message": {
        "message_id": 5, "text": "报销98",
        "from": {"id": 42}, "chat": {"id": 42}}}
    envs = TG.to_envelopes(upd)
    assert len(envs) == 1
    env, msg = envs[0]
    assert env.channel == "telegram" and env.actor == "42"
    assert env.text == "报销98" and env.attachment is None


def test_to_envelopes_photo_and_document():
    upd = {"update_id": 2, "message": {
        "message_id": 6, "from": {"id": 42}, "chat": {"id": 42},
        "photo": [{"file_id": "small"}, {"file_id": "big"}],
        "caption": "发票"}}
    envs = TG.to_envelopes(upd)
    env, _ = envs[0]
    assert env.attachment.kind == "image" and env.attachment.filename.endswith(".jpg")
    upd2 = {"update_id": 3, "message": {
        "message_id": 7, "from": {"id": 42}, "chat": {"id": 42},
        "document": {"file_id": "d1", "file_name": "合同.pdf"}}}
    env2, _ = TG.to_envelopes(upd2)[0]
    assert env2.attachment.kind == "file"
    assert env2.attachment.filename == "合同.pdf"


def test_parse_qr_payload_untouched():
    from boss_secretary.core.channel import card_to_text
    assert "可选操作" in card_to_text({"elements": [
        {"tag": "action", "actions": [{"text": {"content": "A"}},
                                       {"text": {"content": "B"}}]}]})


def test_adapter_send_and_download(monkeypatch):
    fs = FakeSession()
    ad = TG.TelegramAdapter(bot=None, token="TOK")
    ad.session = fs
    ad.send_text("42", "hello")
    assert fs.sent[-1] == ("post", "https://api.telegram.org/botTOK/sendMessage",
                           {"chat_id": "42", "text": "hello"}, None)
    data = ad.download_media({"file_id": "fid"})
    assert data.startswith(b"{") or data  # getFile→file_path→下载（此处 mock json）
    urls = [x[1] for x in fs.sent if x[0] == "get"]
    assert any("getFile" in u for u in urls)
    assert any("file/botTOK/docs/f.pdf" in u for u in urls)


def test_fetch_updates_offset(monkeypatch):
    fs = FakeSession(updates=[{"update_id": 7, "message": {
        "message_id": 1, "text": "hi", "from": {"id": 1}, "chat": {"id": 1}}}])
    ad = TG.TelegramAdapter(bot=None, token="TOK")
    ad.session = fs
    updates, new_offset = ad.fetch_updates(0, timeout=0)
    assert new_offset == 8
