"""统一审批动作层：审批意见的采集/记录/展示共用件（所有审批类型）。

七类审批共用（报销/采购 ticket、合同、供应商、用印、差旅、借款、额度）：
- record()：审批决定（谁/角色/同意驳回/意见）落 approval_actions 表
- history()/render()：后续审批人与提交人查阅——"带意见同意 → 附送下一级"
- decision_form()：飞书卡片表单（意见输入框 + 提交按钮），
  卡片回调把 form_value 里的 comment 透传给 on_card_action
- 驳回意见经各业务通知回传提交人与链路其他审批人

约束：本模块不依赖任何业务模块与渠道，只依赖 sqlite conn。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

APPROVE = "approve"
REJECT = "reject"

ROLE_LABELS = {
    "manager": "经理", "boss": "老板", "finance_consign": "财务会签",
    "finance": "财务", "legal": "法务", "audit": "审计",
    "allowance": "额度", "employee": "员工",
}
DECISION_LABELS = {APPROVE: "同意", REJECT: "驳回"}

_COMMENT_MAX = 200


def record(conn, *, doc_type: str, doc_id: str, actor: str, role: str,
           decision: str, comment: str = "") -> None:
    conn.execute(
        "INSERT INTO approval_actions(doc_type, doc_id, actor, role, decision, comment)"
        " VALUES(?,?,?,?,?,?)",
        (doc_type, doc_id, actor, role, decision, (comment or "").strip() or None))
    conn.commit()


def history(conn, doc_type: str, doc_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT actor, role, decision, comment, created_at FROM approval_actions"
        " WHERE doc_type=? AND doc_id=? ORDER BY seq", (doc_type, doc_id)).fetchall()
    return [dict(zip(("actor", "role", "decision", "comment", "created_at"), r))
            for r in rows]


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(str(role or ""), str(role or "审批"))


def render(history_: Sequence[Mapping]) -> str:
    lines = []
    for d in history_:
        who = role_label(d.get("role"))
        act = DECISION_LABELS.get(str(d.get("decision")), str(d.get("decision")))
        ts = str(d.get("created_at") or "")[5:16]
        line = f"**{who}** {act}"
        if ts:
            line += f"（{ts}）"
        if d.get("comment"):
            line += f"：{str(d['comment'])[:_COMMENT_MAX]}"
        lines.append(line)
    return "\n".join(lines)


def latest_reject(history_: Sequence[Mapping]) -> Mapping | None:
    for d in reversed(list(history_)):
        if d.get("decision") == REJECT:
            return d
    return None


def comment_input(name: str = "comment",
                  placeholder: str = "审批意见（驳回请填原因）") -> dict:
    return {"tag": "input", "name": name, "required": False,
            "placeholder": {"tag": "plain_text", "content": placeholder}}


def decision_form(buttons: Sequence[Mapping], *, name: str = "comment",
                  placeholder: str = "审批意见（驳回请填原因）",
                  form_name: str = "approval_form") -> dict:
    """把审批按钮组包成带意见输入框的飞书表单（按钮直接放 form 内）。"""
    els: list[dict] = [comment_input(name, placeholder)]
    for i, b in enumerate(buttons):
        b = dict(b)
        b["action_type"] = "form_submit"
        b.setdefault("name", f"btn_{i}")
        els.append(b)
    return {"tag": "form", "name": form_name, "elements": els}


def parse_comment(form_value: Mapping[str, Any] | None,
                  name: str = "comment") -> str:
    if not form_value:
        return ""
    v = form_value.get(name)
    return str(v).strip() if v else ""
