"""渠道抽象层（抄 Hermes/OpenClaw 网关模式）：统一 Envelope 契约 + ChannelAdapter。

设计：业务层（SecretaryBot.handle_text/handle_image_bytes/handle_file_bytes）渠道无关；
每个渠道一个 adapter 文件实现本 ABC；ConversationDispatcher 把 Envelope 路由给业务层，
回复经 adapter 送出。审批/回复是确定性路径，不过 LLM。
"""
from __future__ import annotations

import threading
import traceback
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class Attachment:
    kind: str            # image | file
    data: bytes
    filename: str


@dataclass(frozen=True)
class Envelope:
    channel: str         # feishu | telegram | ...
    actor: str           # 渠道用户唯一标识（open_id / tg_id）
    text: str | None = None
    attachment: Attachment | None = None
    raw: Any = None      # 渠道原始对象（message_id 等，用于资源下载）


class ChannelAdapter(ABC):
    """每渠道一个 adapter 文件实现。poll 长循环由 adapter 自驱。"""

    channel: str = "base"

    @abstractmethod
    def send_text(self, actor: str, text: str) -> None: ...

    def send_card(self, actor: str, card: Mapping) -> None:
        """无卡片能力的渠道降级为文本。默认：提取标题与正文行。"""
        self.send_text(actor, card_to_text(card))

    def send_file(self, actor: str, path: str, file_type: str = "docx") -> None:
        raise NotImplementedError(f"{self.channel} 不支持文件发送")

    def download_media(self, attachment: Mapping) -> bytes | None:
        raise NotImplementedError(f"{self.channel} 不支持资源下载")


def card_to_text(card: Mapping) -> str:
    lines = []
    header = (card.get("header") or {}).get("title") or {}
    if header.get("content"):
        lines.append(str(header["content"]))
    for el in card.get("elements") or []:
        if el.get("tag") == "div":
            t = (el.get("text") or {}).get("content")
            if t:
                lines.append(str(t))
        elif el.get("tag") == "action":
            acts = [a.get("text", {}).get("content", "") for a in el.get("actions") or []]
            if acts:
                lines.append("可选操作: " + " / ".join(filter(None, acts)))
    return "\n".join(lines)


class ConversationDispatcher:
    """Envelope → 业务层（bot）→ 回复经 adapter。异常兜底为文本提示。"""

    def __init__(self, bot: Any, adapter: ChannelAdapter):
        self.bot = bot
        self.adapter = adapter

    def process(self, env: Envelope) -> None:
        try:
            reply: str | None = None
            if env.attachment is not None:
                att = env.attachment
                if att.kind == "image":
                    reply = self.bot.handle_image_bytes(env.actor, att.data)
                else:
                    reply = self.bot.handle_file_bytes(env.actor, att.data,
                                                       att.filename)
            elif env.text is not None:
                reply = self.bot.handle_text(env.actor, env.text)
            if reply:
                self.adapter.send_text(env.actor, reply)
        except Exception as e:
            print(f"[{env.channel}] 处理异常: {type(e).__name__}: {e}")
            try:
                self.adapter.send_text(env.actor,
                                       f"处理出错: {type(e).__name__}，请稍后重试")
            except Exception:
                pass

    def spawn(self, env: Envelope) -> None:
        threading.Thread(target=self.process, args=(env,), daemon=True).start()


def poll_loop(adapter: ChannelAdapter, dispatcher: ConversationDispatcher,
              fetch: Callable[[], Sequence[Envelope]], interval: float = 2.0,
              stop: threading.Event | None = None) -> None:
    """通用轮询驱动：adapter 提供发送，fetch 提供新 Envelope（自带去重/offset）。"""
    while not (stop and stop.is_set()):
        try:
            for env in fetch():
                dispatcher.spawn(env)
        except Exception as e:
            print(f"[{adapter.channel}] poll 异常: {type(e).__name__}: {e}")
            time.sleep(min(float(interval), 30))
        time.sleep(float(interval))
