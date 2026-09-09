"""企微加解密 + adapter 单元测试（mock 网络）。"""
from __future__ import annotations

import pytest

from boss_secretary.core import wecom_crypto as WC
from boss_secretary.ingress import wecom as WG

AES_KEY_43 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"  # 43 位
RECV = "wwcorp"


def test_crypto_roundtrip():
    key = WC.derive_key(AES_KEY_43)
    plain = "<xml><Content>你好报销98</Content></xml>"
    enc = WC.encrypt_msg(plain, key, RECV)
    assert WC.decrypt_msg(enc, key, RECV) == plain


def test_decrypt_rejects_wrong_receiveid():
    key = WC.derive_key(AES_KEY_43)
    enc = WC.encrypt_msg("<xml/>", key, "other_corp")
    with pytest.raises(WC.WeComCryptoError, match="receiveid"):
        WC.decrypt_msg(enc, key, RECV)


def test_signature_verify_url():
    echo = WC.encrypt_msg("echo_plain_123", WC.derive_key(AES_KEY_43), RECV)
    sig = WC.signature("mytoken", "1700000000", "nonce1", echo)
    out = WC.verify_url("mytoken", AES_KEY_43, RECV, sig, "1700000000",
                        "nonce1", echo)
    assert out == "echo_plain_123"
    with pytest.raises(WC.WeComCryptoError, match="签名不匹配"):
        WC.verify_url("mytoken", AES_KEY_43, RECV, "badsig", "1700000000",
                      "nonce1", echo)


def test_parse_xml():
    d = WC.parse_xml("<xml><FromUserName><![CDATA[e1]]></FromUserName>"
                     "<MsgType><![CDATA[text]]></MsgType>"
                     "<Content><![CDATA[报销98]]></Content></xml>")
    assert d["FromUserName"] == "e1" and d["Content"] == "报销98"


def test_adapter_envelopes_text():
    adapter = WG.WeComAdapter(bot=None, cfg={
        "corp_id": RECV, "token": "t", "encoding_aes_key": AES_KEY_43,
        "secret": "s", "agent_id": 1000002})
    envs = adapter.to_envelopes("<xml/>", {
        "FromUserName": "e1", "MsgType": "text", "Content": "报销98"})
    assert len(envs) == 1
    env, _ = envs[0]
    assert env.channel == "wecom" and env.actor == "e1" and env.text == "报销98"


def test_adapter_envelopes_image():
    adapter = WG.WeComAdapter(bot=None, cfg={
        "corp_id": RECV, "token": "t", "encoding_aes_key": AES_KEY_43,
        "secret": "s", "agent_id": 1000002})
    envs = adapter.to_envelopes("<xml/>", {
        "FromUserName": "e1", "MsgType": "image", "MediaId": "MEDIA1",
        "PicUrl": "http://x"})
    env, _ = envs[0]
    assert env.attachment.kind == "image" and env.attachment.data == b""


def test_access_token_cached(monkeypatch):
    adapter = WG.WeComAdapter(bot=None, cfg={
        "corp_id": RECV, "token": "t", "encoding_aes_key": AES_KEY_43,
        "secret": "s", "agent_id": 1000002})

    calls = []

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            calls.append(url)
            return FakeResp({"errcode": 0, "access_token": f"TOK{len(calls)}"})

        def post(self, url, timeout=None, json=None, files=None):
            return FakeResp({"errcode": 0})

    class FakeResp:
        def __init__(self, d):
            self._d = d

        def json(self):
            return self._d

    adapter.session = FakeSession()
    t1 = adapter.access_token()
    t2 = adapter.access_token()
    assert t1 == t2 == "TOK1" and len(calls) == 1
