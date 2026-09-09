"""企业微信之外的第一个第二渠道：Telegram（长轮询，无需公网 IP）。

验证 core/channel.py 抽象的试金石：复用 SecretaryBot 全部业务逻辑
（含权限密级/预算/审计/卡片降级为文本）。

配置（config/settings.yaml）:
  channels:
    telegram:
      enabled: true
      bot_token: ""          # @BotFather 创建，建议存 Keychain

运行: boss-telegram
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import requests

from boss_secretary.core.channel import (ChannelAdapter, ConversationDispatcher,
                                         poll_loop)
from boss_secretary.core.llm import load_settings


class TelegramAdapter(ChannelAdapter):
    channel = "telegram"

    def __init__(self, bot, token: str):
        self.bot = bot
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    # ── 发送 ─────────────────────────────────────────────────
    def send_text(self, actor: str, text: str) -> None:
        r = self.session.post(f"{self.api}/sendMessage",
                              json={"chat_id": actor, "text": text[:4000]},
                              timeout=30)
        if not r.ok or not r.json().get("ok"):
            print(f"[telegram] 发送失败: {r.text[:200]}")
        else:
            print(f"[telegram] 已回复 {actor}: {text[:60]!r}")

    def send_file(self, actor: str, path: str, file_type: str = "docx") -> None:
        with open(path, "rb") as f:
            r = self.session.post(f"{self.api}/sendDocument",
                                  data={"chat_id": actor},
                                  files={"document": (Path(path).name, f)},
                                  timeout=60)
        if not r.ok or not r.json().get("ok"):
            print(f"[telegram] 文件发送失败: {r.text[:200]}")
        else:
            print(f"[telegram] 文件已发送 {actor}: {Path(path).name}")

    # ── 媒体下载 ─────────────────────────────────────────────
    def download_media(self, attachment: Mapping) -> bytes | None:
        file_id = attachment.get("file_id")
        if not file_id:
            return None
        r = self.session.get(f"{self.api}/getFile", params={"file_id": file_id},
                             timeout=30)
        fp = (r.json().get("result") or {}).get("file_path")
        if not fp:
            return None
        r2 = self.session.get(f"https://api.telegram.org/file/bot{self.token}/{fp}",
                              timeout=60)
        return r2.content if r2.ok else None

    # ── 拉取（长轮询）────────────────────────────────────────
    def fetch_updates(self, offset: int, timeout: int = 50):
        r = self.session.get(f"{self.api}/getUpdates",
                             params={"offset": offset, "timeout": timeout},
                             timeout=timeout + 10)
        data = r.json()
        updates = data.get("result") or []
        new_offset = offset
        for u in updates:
            new_offset = max(new_offset, u["update_id"] + 1)
        return updates, new_offset


def to_envelopes(update: Mapping) -> list[Any]:
    """update → (Envelope, message) 列表；photo/document 多附件各一条。"""
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return []
    chat_id = str((msg.get("chat") or {}).get("id")
                  or (msg.get("from") or {}).get("id"))
    from_id = str((msg.get("from") or {}).get("id") or chat_id)
    out = []
    from boss_secretary.core.channel import Envelope, Attachment
    print(f"[dbg to_envelopes] text={bool(msg.get('text'))} photo={bool(msg.get('photo'))} doc={bool(msg.get('document'))}")
    if msg.get("text"):
        out.append((Envelope(channel="telegram", actor=from_id,
                             text=msg.get("text")), msg))
    elif msg.get("photo"):
        biggest = msg["photo"][-1]
        att = Attachment(kind="image", data=b"",
                         filename=f"tg_{biggest['file_id'][:10]}.jpg")
        env = Envelope(channel="telegram", actor=from_id,
                       text=msg.get("caption"), attachment=att, raw=msg)
        out.append((env, msg))
    elif msg.get("document"):
        doc = msg["document"]
        att = Attachment(kind="file", data=b"", filename=doc.get("file_name", "file"))
        env = Envelope(channel="telegram", actor=from_id,
                       text=msg.get("caption"), attachment=att, raw=msg)
        out.append((env, msg))
    return out


def run(settings_path: str = "config/settings.yaml") -> None:
    settings = load_settings(settings_path)
    tg = ((settings.get("channels") or {}).get("telegram") or {})
    if not tg.get("enabled") or not tg.get("bot_token"):
        print("[telegram] 未启用（channels.telegram.enabled/bot_token）")
        return
    from boss_secretary.ingress.feishu import SecretaryBot
    bot = SecretaryBot(settings_path=settings_path)
    adapter = TelegramAdapter(bot, tg["bot_token"])
    dispatcher = ConversationDispatcher(bot, adapter)
    print(f"[telegram] 长轮询启动…")
    offset = 0

    def fetch():
        nonlocal offset
        updates, offset = adapter.fetch_updates(offset, timeout=50)
        envs = []
        for u in updates:
            for env, msg in to_envelopes(u):
                # 图片/文件：先补齐附件字节再投递
                if env.attachment is not None and env.attachment.data == b"":
                    att = msg.get("photo")[-1] if msg.get("photo") else msg.get("document")
                    data = adapter.download_media({"file_id": att["file_id"]})
                    if data is None:
                        adapter.send_text(env.actor, "媒体下载失败，请重发")
                        continue
                    from boss_secretary.core.channel import Envelope, Attachment
                    env = Envelope(channel="telegram", actor=env.actor,
                                   text=env.text,
                                   attachment=Attachment(kind=env.attachment.kind,
                                                         data=data,
                                                         filename=env.attachment.filename),
                                   raw=msg)
                envs.append(env)
        return envs

    poll_loop(adapter, dispatcher, fetch, interval=1.0)


def main(argv: list[str] | None = None) -> int:
    import sys
    run(settings_path=(argv[0] if argv else "config/settings.yaml"))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
