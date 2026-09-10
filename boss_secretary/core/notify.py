"""通知发送出口：异常隔离 + 去重，业务层不直接触碰渠道发送。

所有审批/系统通知经此发送：单条发送失败只记日志，不打断已落库的决策主流程。
target 只需实现 send_text/send_card（SecretaryBot 或任意渠道 adapter）。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


def send_text(target: Any, uid: str | None, text: str) -> bool:
    if not uid:
        return False
    try:
        target.send_text(uid, text)
        return True
    except Exception as e:
        print(f"[notify] 文本发送失败 uid={uid}: {type(e).__name__}: {e}")
        return False


def send_card(target: Any, uid: str | None, card: Mapping) -> bool:
    if not uid:
        return False
    try:
        target.send_card(uid, card)
        return True
    except Exception as e:
        print(f"[notify] 卡片发送失败 uid={uid}: {type(e).__name__}: {e}")
        return False


def send_texts(target: Any, uids: Iterable[str], text: str) -> int:
    """向多个 uid 发送同一文本；去重保序，返回成功条数。"""
    n = 0
    for uid in dict.fromkeys(uids):
        if send_text(target, uid, text):
            n += 1
    return n
