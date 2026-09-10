"""审批决策通知策略：谁收到什么，一处定义。

- 通过：提交人收到类型正文（detail）+ 统一意见后缀
- 驳回：提交人与链路其他审批人收到统一文案（含意见与角色）
业务 handler 只提供 label/detail/submitter/others，不再各自拼文案、遍历发送。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from boss_secretary.core import approvals as AP
from boss_secretary.core import notify


@dataclass(frozen=True)
class DecisionNotice:
    label: str                 # 单据中文标签：合同 / 供应商 XX 准入 / 用印申请…
    doc_id: str
    submitter: str = ""        # 提交人 uid（可空）
    role: str = ""             # 审批角色（驳回文案用）
    approve: bool = True
    comment: str = ""
    detail: str = ""           # 通过时给提交人的类型正文（不含意见后缀）
    others: Sequence[str] = ()  # 链路其他审批人 uid（驳回时同步状态）


def comment_suffix(comment: str) -> str:
    return f"；审批意见：{comment}" if comment else ""


def reject_text(label: str, doc_id: str, comment: str = "") -> str:
    return f"{label} {doc_id} 被拒绝" + (f"：{comment}" if comment else "")


def chain_text(label: str, doc_id: str, role: str, comment: str = "") -> str:
    return (f"{label} {doc_id} 已被{AP.role_label(role)}拒绝"
            + (f"：{comment}" if comment else ""))


def emit(target: Any, n: DecisionNotice) -> None:
    if n.approve:
        if n.detail:
            notify.send_text(target, n.submitter, n.detail + comment_suffix(n.comment))
        return
    notify.send_text(target, n.submitter, reject_text(n.label, n.doc_id, n.comment))
    notify.send_texts(target, n.others,
                      chain_text(n.label, n.doc_id, n.role, n.comment))
