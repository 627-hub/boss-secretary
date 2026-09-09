"""企业微信 adapter（自建应用 + 回调服务器 + 隧道方案）。

架构（内网方案）：本 adapter 在内网起 HTTP 服务（默认 127.0.0.1:9898），
公网入口由隧道（Cloudflare Tunnel / frp）转发到它。收消息 → Envelope →
复用 SecretaryBot 全部业务；回复走企微主动发消息 API（message/send）。

前置（管理后台）：
  1. 自建应用 → 接收消息 → 设置回调（URL=隧道地址/wecom/callback，填 Token/EncodingAESKey）
  2. 「企业可信IP」加入本机出口 IP（家用宽带 IP 变动需更新——诚实约束）
  3. 权限：发消息（message/send）、媒体文件（media/get）

配置（config/settings.yaml）:
  channels:
    wecom:
      enabled: true
      corp_id: "ww..."
      agent_id: 1000002
      token: "回调Token"
      encoding_aes_key: "43位"        # 建议存 Keychain
      secret: "应用Secret"            # 建议存 Keychain
      port: 9898

运行: boss-wecom
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import requests

from boss_secretary.core.channel import (Attachment, ChannelAdapter,
                                         ConversationDispatcher, Envelope)
from boss_secretary.core import wecom_crypto as WC
from boss_secretary.core.llm import load_settings


class WeComAdapter(ChannelAdapter):
    channel = "wecom"

    def __init__(self, bot, cfg: Mapping):
        self.bot = bot
        self.cfg = cfg
        self.api = "https://qyapi.weixin.qq.com/cgi-bin"
        self.session = requests.Session()
        self._token_cache: tuple[float, str] = (0, "")

    # ── 加解密 ─────────────────────────────────────────────────
    @property
    def aes_key(self) -> bytes:
        return WC.derive_key(self.cfg["encoding_aes_key"])

    def verify_get(self, msg_signature: str, timestamp: str, nonce: str,
                   echostr: str) -> str:
        return WC.verify_url(self.cfg["token"], self.cfg["encoding_aes_key"],
                             self.cfg["corp_id"], msg_signature, timestamp,
                             nonce, echostr)

    def decrypt_push(self, body: bytes) -> tuple[str, dict]:
        root = ET.fromstring(body.decode())
        enc = root.findtext("Encrypt") or ""
        sig = root.findtext("MsgSignature") or ""
        ts = root.findtext("CreateTime") or str(int(time.time()))
        nonce = root.findtext("Nonce") or ""
        if WC.signature(self.cfg["token"], ts, nonce, enc) != sig:
            raise WC.WeComCryptoError("消息签名不匹配")
        xml = WC.decrypt_msg(enc, self.aes_key, self.cfg["corp_id"])
        return xml, WC.parse_xml(xml)

    # ── 发送（主动 API）────────────────────────────────────────
    def access_token(self) -> str:
        ts = time.time()
        if ts - self._token_cache[0] < 7000:
            return self._token_cache[1]
        r = self.session.get(f"{self.api}/gettoken", params={
            "corpid": self.cfg["corp_id"], "corpsecret": self.cfg["secret"]},
            timeout=15)
        d = r.json()
        if d.get("errcode") != 0:
            raise RuntimeError(f"企微 token 获取失败: {d}")
        self._token_cache = (ts, d["access_token"])
        return d["access_token"]

    def send_text(self, actor: str, text: str) -> None:
        r = self.session.post(f"{self.api}/message/send?access_token="
                              f"{self.access_token()}", timeout=15, json={
            "touser": actor, "msgtype": "text",
            "agentid": int(self.cfg["agent_id"]),
            "text": {"content": text[:2000]}})
        d = r.json()
        if d.get("errcode") != 0:
            print(f"[wecom] 发送失败 {d.get('errcode')}: {d.get('errmsg')} → {actor}")
        else:
            print(f"[wecom] 已回复 {actor}: {text[:60]!r}")

    def send_file(self, actor: str, path: str, file_type: str = "docx") -> None:
        media = self.upload_media(path)
        if not media:
            return
        r = self.session.post(f"{self.api}/message/send?access_token="
                              f"{self.access_token()}", timeout=30, json={
            "touser": actor, "msgtype": "file",
            "agentid": int(self.cfg["agent_id"]),
            "file": {"media_id": media}})
        print(f"[wecom] 文件发送 {'成功' if r.json().get('errcode') == 0 else '失败'}")

    def upload_media(self, path: str) -> str | None:
        fp = Path(path)
        ftype = {"docx": "doc", "doc": "doc", "pdf": "pdf",
                 "xlsx": "xls", "xls": "xls"}.get(fp.suffix.lstrip("."), "file")
        with open(fp, "rb") as f:
            r = self.session.post(
                f"{self.api}/media/upload?access_token={self.access_token()}"
                f"&type={ftype}",
                files={"media": (fp.name, f)}, timeout=60)
        d = r.json()
        return d.get("media_id") if d.get("errcode") == 0 else None

    def download_media(self, attachment: Mapping) -> bytes | None:
        media_id = attachment.get("media_id")
        if not media_id:
            return None
        r = self.session.get(
            f"{self.api}/media/get?access_token={self.access_token()}",
            params={"media_id": media_id}, timeout=60)
        return r.content if r.ok else None

    # ── Envelope 组装 ──────────────────────────────────────────
    def to_envelopes(self, xml: str, msg: dict) -> list[tuple[Envelope, dict]]:
        from_type = msg.get("MsgType", "")
        actor = msg.get("FromUserName", "")
        if from_type == "text":
            env = Envelope(channel="wecom", actor=actor,
                           text=msg.get("Content"))
            return [(env, msg)]
        if from_type in ("image", "file"):
            kind = "image" if from_type == "image" else "file"
            filename = msg.get("FileName") or (f"企微图片.{kind}")
            env = Envelope(channel="wecom", actor=actor,
                           text=msg.get("Content") or None,
                           attachment=Attachment(kind=kind, data=b"",
                                                 filename=filename))
            return [(env, msg)]
        return []


class WecomCallbackHandler(BaseHTTPRequestHandler):
    """GET=URL 验证；POST=消息推送（解密→线程处理→立即回 success）。"""

    adapter: WeComAdapter = None       # type: ignore[assignment]
    dispatcher: ConversationDispatcher = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _query(self) -> dict[str, str]:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    def do_GET(self):  # noqa: N802
        try:
            q = self._query()
            plain = self.adapter.verify_get(
                q.get("msg_signature", ""), q.get("timestamp", ""),
                q.get("nonce", ""), q.get("echostr", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(plain.encode())
        except Exception as e:
            print(f"[wecom] GET 验证失败: {type(e).__name__}: {e}")
            self.send_response(400)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        try:
            q = self._query()
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            xml, msg = self.adapter.decrypt_push(body)
            print(f"[wecom] 收到消息 sender={msg.get('FromUserName')} "
                  f"type={msg.get('MsgType')}")
            for env, _msg in self.adapter.to_envelopes(xml, msg):
                if env.attachment is not None and env.attachment.data == b"":
                    def _fetch(env=env, msg=msg):
                        att = msg.get("image") or msg.get("file") or {}
                        data = self.adapter.download_media(
                            {"media_id": att.get("media_id")})
                        if data is None:
                            self.adapter.send_text(env.actor, "媒体下载失败，请重发")
                            return
                        from boss_secretary.core.channel import Envelope, Attachment
                        filename = msg.get("FileName") or "企微图片.jpg"
                        new_env = Envelope(channel="wecom", actor=env.actor,
                                           text=env.text,
                                           attachment=Attachment(
                                               kind=env.attachment.kind,
                                               data=data, filename=filename),
                                           raw=msg)
                        self.dispatcher.spawn(new_env)
                    threading.Thread(target=_fetch, daemon=True).start()
                else:
                    self.dispatcher.spawn(env)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"success")
        except Exception as e:
            print(f"[wecom] POST 处理异常: {type(e).__name__}: {e}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"success")


def run(settings_path: str = "config/settings.yaml") -> None:
    settings = load_settings(settings_path)
    cfg = ((settings.get("channels") or {}).get("wecom") or {})
    if not cfg.get("enabled") or not cfg.get("corp_id"):
        print("[wecom] 未启用（channels.wecom.enabled/corp_id）")
        return
    from boss_secretary.ingress.feishu import SecretaryBot
    bot = SecretaryBot(settings_path=settings_path)
    adapter = WeComAdapter(bot, cfg)
    dispatcher = ConversationDispatcher(bot, adapter)

    handler = type("Handler", (WecomCallbackHandler,),
                   {"adapter": adapter, "dispatcher": dispatcher})
    port = int(cfg.get("port", 9898))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"[wecom] 回调服务启动 127.0.0.1:{port}（隧道 → 公网/wecom/callback）")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    import sys
    run(settings_path=(argv[0] if argv else "config/settings.yaml"))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
