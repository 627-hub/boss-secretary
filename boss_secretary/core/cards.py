"""渠道无关卡片构建：飞书卡片 JSON 作为中间表示（IR）。

- 飞书渠道直接发送本结构（ingress/feishu）
- 文本渠道经 core/channel.card_to_text 降级
- Slack 经 ingress/slack.card_to_blocks 转换

审批意见区、意见表单等原语来自 core/approvals；业务模块不再手写卡片 dict。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from boss_secretary.core import approvals as AP


def md(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def button(label: str, value: Mapping, type_: str = "default") -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": label},
            "type": type_, "value": dict(value)}


def action_buttons(buttons: Sequence[Mapping]) -> dict:
    return {"tag": "action", "actions": [dict(b) for b in buttons]}


def card(title: str, template: str, elements: Sequence[Mapping]) -> dict:
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": list(elements)}


def history_block(history: Sequence[Mapping] | None) -> dict | None:
    if not history:
        return None
    return md("**审批意见**\n" + AP.render(history))


def _elements(primary: Mapping, history: Sequence[Mapping] | None,
              actions: Mapping) -> list[dict]:
    out: list[dict] = [dict(primary)]
    blk = history_block(history)
    if blk:
        out.append(blk)
    out.append(dict(actions))
    return out


# ── 报销/采购主流程 ──────────────────────────────────────────

def approval_card(ticket_id: str, amount: Any, reason: Any, role: str,
                  type_: str = "reimburse",
                  history: Sequence[Mapping] | None = None) -> dict:
    is_proc = type_ == "procurement"
    label = "采购标的" if is_proc else "事由"
    reason_s = reason if is_proc else (reason or "-")
    els = _elements(
        md(f"**金额**：{amount if amount is not None else '-'} 元\n"
           f"**{label}**：{reason_s}\n"
           f"**审批角色**：{role}"),
        history,
        AP.decision_form([
            button("同意", {"action": "approve", "ticket_id": ticket_id,
                            "role": role}, "primary"),
            button("驳回", {"action": "reject", "ticket_id": ticket_id,
                            "role": role}, "danger")]))
    return card(f"{'采购' if is_proc else '报销'}审批 {ticket_id}",
                "purple" if is_proc else "orange", els)


def paid_card(ticket_id: str, amount: Any,
              history: Sequence[Mapping] | None = None) -> dict:
    els = _elements(
        md(f"审批已通过，待打款 **{amount if amount is not None else '-'} 元**\n"
           f"确认后本单关闭并通知员工。"),
        history,
        action_buttons([button("确认打款", {"action": "paid", "ticket_id": ticket_id,
                                           "role": "finance"}, "primary")]))
    return card(f"打款确认 {ticket_id}", "green", els)


# ── 合同 / 付款 ──────────────────────────────────────────────

def contract_review_card(cid: str, title: str, supplier: str, amount: Any,
                         risks: list,
                         history: Sequence[Mapping] | None = None) -> dict:
    risk_text = "\n".join(f"[{r.get('level', '中')}] {r.get('clause', '')}: "
                          f"{r.get('note', '')}" for r in risks[:6]) or "无"
    els = _elements(
        md(f"**合同**：{title}\n"
           f"**供应商**：{supplier or '-'}\n"
           f"**金额**：{amount if amount is not None else '-'} 元\n"
           f"**AI 审查**：\n{risk_text}"),
        history,
        AP.decision_form([
            button("批准", {"action": "contract_approve", "contract_id": cid},
                   "primary"),
            button("拒绝", {"action": "contract_reject", "contract_id": cid},
                   "danger")]))
    return card(f"合同审批 {cid}", "purple", els)


def payment_card(pid: str, amount: Any, note: str) -> dict:
    return card(f"付款确认 {pid}", "green", [
        md(f"**金额**：{amount} 元\n**说明**：{note or '-'}"),
        action_buttons([button("确认打款", {"action": "payment_paid",
                                           "payment_id": pid}, "primary")])])


# ── 用印 / 供应商 ────────────────────────────────────────────

def seal_card(rid: str, seal_name: str, doc_title: str, copies: int,
              applicant: str, note: str = "",
              history: Sequence[Mapping] | None = None) -> dict:
    els = _elements(
        md(f"**印章**：{seal_name}\n"
           f"**文件**：{doc_title}\n"
           f"**份数**：{copies}\n"
           f"**申请人**：{applicant}"
           + (f"\n**备注**：{note}" if note else "")),
        history,
        AP.decision_form([
            button("批准用印", {"action": "seal_approve", "request_id": rid},
                   "primary"),
            button("拒绝", {"action": "seal_reject", "request_id": rid},
                   "danger")]))
    return card(f"用印审批 {rid}", "red", els)


def supplier_card(sid: str, name: str, uscc: str | None,
                  history: Sequence[Mapping] | None = None) -> dict:
    els = _elements(
        md(f"**供应商**：{name}\n"
           f"**信用代码**：{uscc or '-'}"
           f"\n（银行账户变更同样走本卡片重审批）"),
        history,
        AP.decision_form([
            button("批准", {"action": "supplier_approve", "supplier_id": sid},
                   "primary"),
            button("拒绝", {"action": "supplier_reject", "supplier_id": sid},
                   "danger")]))
    return card(f"供应商审批 {sid}", "indigo", els)


# ── 差旅 / 借款 / 额度 ───────────────────────────────────────

def trip_card(trip: Mapping, employee_id: str) -> dict:
    return card(f"出差审批 {trip['trip_id']}", "turquoise", [
        md(f"**员工**：{employee_id}\n"
           f"**目的地**：{trip['destination']}\n"
           f"**期间**：{str(trip['start_date'])[:10]}~{str(trip['end_date'])[:10]}\n"
           f"**预估**：{trip['estimate']} 元\n"
           f"**事由**：{trip['reason'] or '-'}"),
        AP.decision_form([
            button("批准", {"action": "trip_approve", "trip_id": trip["trip_id"]},
                   "primary"),
            button("拒绝", {"action": "trip_reject", "trip_id": trip["trip_id"]},
                   "danger")])])


def loan_card(loan: Mapping, employee_id: str) -> dict:
    return card(f"借款审批 {loan['loan_id']}", "red", [
        md(f"**员工**：{employee_id}\n"
           f"**金额**：{loan['amount']} 元\n"
           f"**事由**：{loan['reason'] or '-'}"),
        AP.decision_form([
            button("批准并放款", {"action": "loan_approve",
                                 "loan_id": loan["loan_id"]}, "primary"),
            button("拒绝", {"action": "loan_reject", "loan_id": loan["loan_id"]},
                   "danger")])])


def allowance_card(req: Mapping, employee_id: str) -> dict:
    return card(f"额度审批 {req['allowance_id']}", "purple", [
        md(f"**员工**：{employee_id}\n"
           f"**类型**：{req['category']}\n"
           f"**额度**：{req['amount']} 元\n"
           f"**用途**：{req['reason'] or '-'}\n"
           f"**有效期**：{req['expires_at']}"),
        AP.decision_form([
            button("批准", {"action": "allowance_approve",
                            "allowance_id": req["allowance_id"]}, "primary"),
            button("拒绝", {"action": "allowance_reject",
                            "allowance_id": req["allowance_id"]}, "danger")])])
