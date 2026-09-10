"""Slack 渠道（Socket Mode 长连接，无需公网 IP）。

复用 SecretaryBot 全部业务；Block Kit 原生按钮卡片（交互回调走同一 on_card_action）。

前置（api.slack.com/apps）：
  1. Create App → From scratch
  2. Socket Mode: Enable → 创建 app-level token（xapp-，scope connections:write）
  3. OAuth & Permissions scopes: chat:write, im:history, app_mentions:read,
     im:write, files:read, files:write
  4. Event Subscriptions: 订阅 message.im（私聊）+ app_mentions:read（频道 @机器人）
  5. Install to Workspace → Bot User OAuth Token（xoxb-）

配置（config/settings.yaml）:
  channels:
    slack:
      enabled: true
      bot_token: ""     # xoxb-（建议存 Keychain）
      app_token: ""     # xapp-（建议存 Keychain）

运行: boss-slack
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from boss_secretary.core.channel import (ChannelAdapter, ConversationDispatcher,
                                         Envelope, Attachment)


def card_to_blocks(card: Mapping) -> list[dict]:
    """lark 风格卡片 → Block Kit blocks（按钮 value 编 JSON 供回调解析）。"""
    blocks: list[dict] = []
    title = (card.get("header") or {}).get("title", {}).get("content")
    if title:
        blocks.append({"type": "header",
                       "text": {"type": "plain_text", "text": title}})
    for el in card.get("elements") or []:
        if el.get("tag") == "div":
            t = (el.get("text") or {}).get("content")
            if t:
                blocks.append({"type": "section",
                               "text": {"type": "mrkdwn", "text": t[:3000]}})
        elif el.get("tag") == "action":
            btns = []
            for a in el.get("actions") or []:
                label = (a.get("text") or {}).get("content", "")
                btns.append({"type": "button", "text": {"type": "plain_text",
                                                        "text": label},
                             "value": json.dumps(a.get("value") or {},
                                                 ensure_ascii=False)})
            if btns:
                blocks.append({"type": "actions", "elements": btns})
    return blocks


class SlackAdapter(ChannelAdapter):
    channel = "slack"

    def __init__(self, bot, bot_token: str, app_token: str,
                 web_client=None, socket_client=None):
        self.bot = bot
        self.bot_token = bot_token
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        self.web = web_client or WebClient(token=bot_token)
        self.socket = socket_client or SocketModeClient(
            app_token=app_token, web_client=self.web)
        self.dispatcher = ConversationDispatcher(bot, self)

    # ── 发送 ─────────────────────────────────────────────────
    def send_text(self, actor: str, text: str) -> None:
        self.web.chat_postMessage(channel=actor, text=text[:4000])

    def send_card(self, actor: str, card: Mapping) -> None:
        self.web.chat_postMessage(channel=actor, blocks=card_to_blocks(card),
                                  text=str((card.get("header") or {})
                                           .get("title", {}).get("content", "")))

    def send_file(self, actor: str, path: str, file_type: str = "docx") -> None:
        self.web.files_upload_v2(channel=actor, file=str(path),
                                 filename=Path(path).name)

    # ── Socket Mode 请求处理（可测试入口）────────────────────
    def handle_socket_request(self, req: Any) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse
        self.socket.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id, payload={"ok": True}))
        if req.type == "events_api":
            event = (req.payload or {}).get("event") or {}
            envs = self.event_to_envelopes(event)
            for env in envs:
                self.dispatcher.spawn(env)
        elif req.type == "interactive":
            payload = req.payload or {}
            actions = (payload.get("actions") or [])
            if not actions:
                return
            user = (payload.get("user") or {}).get("id", "")
            try:
                value = json.loads((actions[0] or {}).get("value") or "{}")
            except ValueError:
                value = {}
            threading.Thread(
                target=lambda: self.bot.on_card_action(user, value),
                daemon=True).start()

    # ── 事件 → Envelope ──────────────────────────────────────
    def event_to_envelopes(self, event: Mapping) -> list[Envelope]:
        etype = event.get("type", "")
        actor = event.get("user") or event.get("bot_id") or ""
        if event.get("subtype") or event.get("bot_id"):   # 机器人回显/编辑等
            return []
        if etype == "message" and event.get("channel_type") != "im":
            return []                                     # MVP: 仅私聊收单
        text = event.get("text") or ""
        if etype == "app_mention":
            text = re.sub(r"<@[UW][A-Z0-9]+>\s*", "", text).strip()
        files = event.get("files") or []
        if files:
            f0 = files[0]
            url = f0.get("url_private_download") or f0.get("url_private")
            if url:
                data = self._download(url)
                if data is not None:
                    kind = "image" if (f0.get("mimetype") or "").startswith(
                        "image/") else "file"
                    return [Envelope(channel="slack", actor=actor, text=text,
                                     attachment=Attachment(
                                         kind=kind, data=data,
                                         filename=f0.get("name", "file")))]
        if text:
            return [Envelope(channel="slack", actor=actor, text=text)]
        return []

    def _download(self, url: str) -> bytes | None:
        r = self.session_get(url)
        return r.content if getattr(r, "ok", False) else None

    def session_get(self, url: str):
        import requests
        return requests.get(url, headers={
            "Authorization": f"Bearer {self.bot_token}"}, timeout=60)

    # ── 运行 ─────────────────────────────────────────────────
    def start(self) -> None:
        self.dispatcher = ConversationDispatcher(self.bot, self)
        self.socket.socket_mode_request_listeners.append(
            self.handle_socket_request)
        self.socket.connect()
        print("[slack] Socket Mode 已连接（长连接，无需公网 IP）")
        while True:
            time.sleep(1)


def run(settings_path: str = "config/settings.yaml") -> None:
    settings = load_settings(settings_path)
    cfg = ((settings.get("channels") or {}).get("slack") or {})
    if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("app_token"):
        print("[slack] 未启用（channels.slack.enabled/bot_token/app_token）")
        return
    from boss_secretary.ingress.feishu import SecretaryBot
    bot = SecretaryBot(settings_path=settings_path)
    adapter = SlackAdapter(bot, cfg["bot_token"], cfg["app_token"])
    adapter.start()


def main(argv: list[str] | None = None) -> int:
    import sys
    run(settings_path=(argv[0] if argv else "config/settings.yaml"))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
