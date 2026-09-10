"""卡片动作处理注册表（飞书/Slack 共用的 on_card_action 实现）。

每个 action 名一个处理函数；feishu 回调经 dispatch 统一分发，
异常语义（RouterError→操作失败）集中在此，不再堆在 feishu.py 的 if/elif 里。
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Mapping

from boss_secretary.core import allowance as AL
from boss_secretary.core import approvals as AP
from boss_secretary.core import budget as BG
from boss_secretary.core import contract as CT
from boss_secretary.core import notify
from boss_secretary.core import policy as PL
from boss_secretary.core import router as R
from boss_secretary.core import seal as SL
from boss_secretary.core import supplier as SUP
from boss_secretary.core import travel as TR
from boss_secretary.core.cards import contract_review_card, supplier_card

Handler = Callable[..., str]
HANDLERS: dict[str, Handler] = {}


def action(*names: str):
    """把一个函数注册到若干 action 名上（如 trip_approve/trip_reject 共用）。"""
    def deco(fn: Handler) -> Handler:
        for n in names:
            HANDLERS[n] = fn
        return fn
    return deco


def dispatch(bot, open_id: str, value: Mapping, comment: str = "") -> str:
    handler = HANDLERS.get(str(value.get("action")))
    if handler is None:
        return f"未知动作: {value.get('action')}"
    try:
        return handler(bot, open_id, value, comment)
    except R.RouterError as e:
        return f"操作失败: {e}"


# ── 报销/采购主流程 ──────────────────────────────────────────

@action("approve")
def handle_approve(bot, open_id: str, value: Mapping, comment: str) -> str:
    st = bot.router.approve(value.get("ticket_id"), open_id,
                            value.get("role"), comment)
    tail = "（已附意见）" if comment else ""
    return (f"已同意（当前：{st}）{tail}" if st != R.APPROVED
            else f"✅ 会签完成，单据通过{tail}")


@action("reject")
def handle_reject(bot, open_id: str, value: Mapping, comment: str) -> str:
    bot.router.reject(value.get("ticket_id"), open_id, value.get("role"),
                      comment or "未填写驳回意见")
    return ("已驳回，意见已回传提交人与链路审批人" if comment
            else "已驳回（未填写意见）")


@action("paid")
def handle_paid(bot, open_id: str, value: Mapping, comment: str) -> str:
    if bot.roles.get("finance") and open_id != bot.roles["finance"]:
        return "仅财务可确认打款"
    bot.router.mark_paid(value.get("ticket_id"), open_id, "finance")
    return "✅ 已确认打款，单据关闭"


# ── 额度 ─────────────────────────────────────────────────────

@action("allowance_approve", "allowance_reject")
def handle_allowance(bot, open_id: str, value: Mapping, comment: str) -> str:
    aid = value.get("allowance_id")
    approve = value.get("action") == "allowance_approve"
    if approve:
        a_pre = AL.get(bot.store.conn, aid)
        if a_pre:
            emp_dept = bot.store.conn.execute(
                "SELECT dept_id FROM employees WHERE feishu_user_id=?",
                (a_pre["employee_id"],)).fetchone()
            b = BG.check(bot.store.conn, emp_dept[0] if emp_dept else None,
                         dt.date.today().strftime("%Y-%m"),
                         extra=float(a_pre["total_amount"]),
                         settings=bot.settings)
            if b["checked"] and not b["ok"] and b["block"]:
                return (f"额度批准被预算检查拦截：{b['dept']} {b['month']} "
                        f"剩余 {b['remaining']:.0f} 元，本额度 {a_pre['total_amount']:.0f} 元。"
                        f"请先调整预算（预算 {b['dept']} {b['month']} 金额）")
    a = AL.decide(bot.store.conn, aid, open_id, approve=approve, note=comment)
    if a is None:
        return "额度申请不存在或已处理"
    if a["status"] == AL.ACTIVE:
        PL.emit(bot, PL.DecisionNotice(
            label="额度申请", doc_id=a["allowance_id"],
            submitter=a["employee_id"], approve=True, comment=comment,
            detail=f"✅ 额度已生效 {a['allowance_id']}：{a['category']} "
                   f"{a['total_amount']} 元（至 {str(a['expires_at'])[:10]}）。"
                   f"在此额度内报销免逐单审批"))
        return f"已批准 {a['allowance_id']}"
    PL.emit(bot, PL.DecisionNotice(
        label="额度申请", doc_id=a["allowance_id"], submitter=a["employee_id"],
        role=str(a.get("approver_role") or ""), approve=False, comment=comment))
    return "已拒绝"


# ── 差旅 / 借款 ──────────────────────────────────────────────

@action("trip_approve", "trip_reject")
def handle_trip(bot, open_id: str, value: Mapping, comment: str) -> str:
    approve = value.get("action") == "trip_approve"
    role_t = "boss" if open_id == bot.roles.get("boss") else "manager"
    t = TR.decide_trip(bot.store.conn, value.get("trip_id"), open_id,
                       approve=approve, note=comment, role=role_t)
    if t is None:
        return "出差申请不存在或已处理"
    if t["status"] == TR.TRIP_ACTIVE:
        PL.emit(bot, PL.DecisionNotice(
            label="出差申请", doc_id=t["trip_id"],
            submitter=t.get("employee_id") or "", role=role_t, approve=True,
            comment=comment,
            detail=f"✅ 出差申请已批准 {t['trip_id']}（{t['destination']}，"
                   f"{str(t['start_date'])[:10]}~{str(t['end_date'])[:10]}）。"
                   f"期间内报销将自动关联本次出差"))
        return f"已批准 {t['trip_id']}"
    PL.emit(bot, PL.DecisionNotice(
        label="出差申请", doc_id=t["trip_id"], submitter=t.get("employee_id") or "",
        role=role_t, approve=False, comment=comment))
    return "已拒绝"


@action("loan_approve", "loan_reject")
def handle_loan(bot, open_id: str, value: Mapping, comment: str) -> str:
    approve = value.get("action") == "loan_approve"
    l = TR.loan_decide(bot.store.conn, value.get("loan_id"), open_id,
                       approve=approve, note=comment)
    if l is None:
        return "借款单不存在或已处理"
    if l["status"] == TR.LOAN_PAID_OUT:
        PL.emit(bot, PL.DecisionNotice(
            label="借款申请", doc_id=l["loan_id"],
            submitter=l.get("employee_id") or "", approve=True, comment=comment,
            detail=f"💸 借款 {l['loan_id']} 已放款 {l['amount']} 元，"
                   f"记入未结台账（报销冲销或财务核销）"))
        return f"已批准放款 {l['loan_id']}"
    PL.emit(bot, PL.DecisionNotice(
        label="借款申请", doc_id=l["loan_id"], submitter=l.get("employee_id") or "",
        role="boss", approve=False, comment=comment))
    return "已拒绝"


# ── 用印 ─────────────────────────────────────────────────────

@action("seal_approve", "seal_reject")
def handle_seal(bot, open_id: str, value: Mapping, comment: str) -> str:
    rid = value.get("request_id")
    approve = value.get("action") == "seal_approve"
    r = SL.get_request(bot.store.conn, rid)
    if r is None:
        return f"用印申请不存在: {rid}"
    seal = SL.get_seal(bot.store.conn, r["seal_id"])
    expected = SL.approver_role_for(seal["name"]) if seal else "boss"
    allowed = [bot.roles.get(expected), bot.roles.get("boss"), r["applicant"]]
    if open_id not in [u for u in allowed if u]:
        return f"仅 {expected} 可审批该用印申请"
    note = "自批" if r["applicant"] == open_id else ""
    if note and comment:
        note = f"自批；{comment}"
    elif comment:
        note = comment
    try:
        rr = SL.decide(bot.store.conn, rid, open_id, approve=approve, note=note)
    except ValueError as e:
        return f"审批失败: {e}"
    if rr["status"] == SL.APPROVED:
        PL.emit(bot, PL.DecisionNotice(
            label="用印申请", doc_id=rid, submitter=r["applicant"],
            role=expected, approve=True, comment=comment,
            detail=f"✅ 用印已批准 {rid}（{r['seal_name']}），"
                   f"用印完成后回复「用印 {rid} 已用」登记台账"))
        return f"已批准（用印完成后申请人回复「用印 {rid} 已用」登记）"
    PL.emit(bot, PL.DecisionNotice(
        label="用印申请", doc_id=rid, submitter=r.get("applicant") or "",
        role=expected, approve=False, comment=comment))
    return "已拒绝"


# ── 供应商 ───────────────────────────────────────────────────

@action("supplier_approve", "supplier_reject")
def handle_supplier(bot, open_id: str, value: Mapping, comment: str) -> str:
    sid = value.get("supplier_id")
    if open_id not in (bot.roles.get("finance"), bot.roles.get("boss")):
        return "仅 finance/boss 可审批供应商准入"
    role = "boss" if open_id == bot.roles.get("boss") else "finance"
    if value.get("action") == "supplier_reject":
        try:
            SUP.reject(bot.store.conn, sid, open_id, role=role, reason=comment)
        except ValueError as e:
            return str(e)
        c = SUP.get(bot.store.conn, sid)
        name = (c or {}).get("name") or sid
        others = [uid for uid in (bot.roles.get("finance"), bot.roles.get("boss"))
                  if uid and uid != open_id]
        PL.emit(bot, PL.DecisionNotice(
            label=f"供应商 {name} 准入", doc_id=sid,
            submitter=(c or {}).get("created_by") or "", role=role,
            approve=False, comment=comment, others=others))
        return "准入已拒绝"
    try:
        st = SUP.approve(bot.store.conn, sid, open_id, role, comment)
    except ValueError as e:
        return str(e)
    c = SUP.get(bot.store.conn, sid)
    if st == SUP.ACTIVE:
        PL.emit(bot, PL.DecisionNotice(
            label=f"供应商 {c['name']} 准入", doc_id=sid,
            submitter=c.get("created_by") or "", role=role, approve=True,
            comment=comment, detail=f"✅ 供应商 {c['name']} 已准入生效"))
        return f"✅ 供应商 {sid} 会签完成，已生效"
    other = "boss" if role == "finance" else "finance"
    ouid = bot.roles.get(other)
    if ouid and ouid != open_id:
        notify.send_card(bot, ouid, supplier_card(
            sid, c["name"], c.get("uscc"),
            history=AP.history(bot.store.conn, "supplier", sid)))
    return f"已批准（等待 {other} 会签）"


# ── 合同 / 付款 ──────────────────────────────────────────────

@action("contract_approve", "contract_reject")
def handle_contract(bot, open_id: str, value: Mapping, comment: str) -> str:
    cid = value.get("contract_id")
    if open_id not in (bot.roles.get("legal"), bot.roles.get("boss")):
        return "仅 legal/boss 可审批合同"
    c = CT.get(bot.store.conn, cid)
    if c is None:
        return f"合同不存在: {cid}"
    role = "boss" if open_id == bot.roles.get("boss") else "legal"
    if value.get("action") == "contract_reject":
        CT.reject(bot.store.conn, cid, open_id, role, comment)
        other = "boss" if role == "legal" else "legal"
        ouid = bot.roles.get(other)
        others = [ouid] if ouid and ouid != open_id else []
        PL.emit(bot, PL.DecisionNotice(
            label="合同", doc_id=cid, submitter=c.get("employee_id") or "",
            role=role, approve=False, comment=comment, others=others))
        return "已拒绝"
    st = CT.approve(bot.store.conn, cid, open_id, role, comment)
    if st == CT.ACTIVE:
        PL.emit(bot, PL.DecisionNotice(
            label="合同", doc_id=cid, submitter=c.get("employee_id") or "",
            role=role, approve=True, comment=comment,
            detail=f"✅ 合同 {cid} 已生效（会签完成），"
                   f"可按付款条款发「付款 {cid} 金额」"))
        return f"✅ 合同 {cid} 会签完成，已生效"
    other = "boss" if role == "legal" else "legal"
    ouid = bot.roles.get(other)
    if ouid and ouid != open_id:
        rv = c.get("risks") or {}
        risks = list(rv.get("risks") or [])
        if rv.get("missing"):
            risks.append({"level": "提示", "clause": "缺失条款",
                          "note": "、".join(rv["missing"])})
        notify.send_card(bot, ouid, contract_review_card(
            cid, c["title"], c.get("supplier"), c.get("amount"), risks,
            history=AP.history(bot.store.conn, "contract", cid)))
    return f"已批准（等待 {other} 会签）"


@action("payment_paid")
def handle_payment_paid(bot, open_id: str, value: Mapping, comment: str) -> str:
    pid = value.get("payment_id")
    if open_id != bot.roles.get("finance"):
        return "仅财务可确认付款打款"
    try:
        pm = CT.pay(bot.store.conn, pid, open_id)
    except ValueError as e:
        return f"打款失败: {e}"
    paid = CT.paid_total(bot.store.conn, pm["contract_id"]) \
        if pm.get("contract_id") else None
    if pm.get("contract_id"):
        c = CT.get(bot.store.conn, pm["contract_id"])
        if c and c.get("employee_id"):
            notify.send_text(bot, c["employee_id"],
                             f"✅ 付款 {pid} 已打款"
                             + (f"（合同累计已付 {paid:.0f} 元）"
                                if paid is not None else ""))
    return "✅ 已确认打款"
