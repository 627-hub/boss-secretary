"""合同管理（商务模块）：登记 → AI 审查 → 法务+老板会签 → 生效 → 付款 → 到期/续签。

流程（PRD 商务模块）：
  采购申请(tickets type=procurement, 复用审批流) → 通过
  → 登记合同(pending_review, 附 AI 审查) → legal+boss 卡片会签 → active
  → 付款单(pending → 财务卡片确认 → paid, 合同累计已付更新)
  → 到期提醒(调度器) → 续签(新合同走审批) 或 到期关闭
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Mapping, Sequence

from boss_secretary.core import approvals as AP
from boss_secretary.core import db as DB
from boss_secretary.core import status as ST

PENDING_REVIEW = ST.Doc.PENDING_REVIEW
REVIEWING = ST.Doc.REVIEWING
ACTIVE = ST.Doc.ACTIVE
EXPIRING = ST.Doc.EXPIRING
RENEWED = ST.Doc.RENEWED
CLOSED = ST.Doc.CLOSED
REJECTED = ST.Doc.REJECTED

PAY_PENDING = ST.Payment.PENDING
PAY_PAID = ST.Payment.PAID
PAY_REJECTED = ST.Payment.REJECTED


def get(conn, contract_id: str) -> dict | None:
    rec = DB.fetch_one(conn, "contracts", "contract_id", contract_id)
    if rec is None:
        return None
    try:
        rec["approvers"] = json.loads(rec["approvers"]) if rec.get("approvers") else []
    except (TypeError, ValueError):
        rec["approvers"] = []
    try:
        rec["risks"] = json.loads(rec["ai_review"]) if rec.get("ai_review") else {}
    except (TypeError, ValueError):
        rec["risks"] = {}
    return rec


def _upsert_approvers(conn, contract_id: str, role: str, user_id: str) -> bool:
    c = get(conn, contract_id)
    approvals = c.get("approvers") or []
    if any(a.get("role") == role for a in approvals):
        return False
    approvals.append({"role": role, "user_id": user_id})
    DB.update_fields(conn, "contracts", "contract_id", contract_id,
                     approvers=json.dumps(approvals, ensure_ascii=False),
                     updated_at=DB.now())
    return True


def create(conn, *, employee_id: str, dept_id: str | None, title: str,
           supplier: str | None, amount: float | None, start_date: str | None,
           end_date: str | None, payment_terms: str | None = None,
           procurement_id: str | None = None, ai_review: dict | None = None,
           evidence_file: str | None = None) -> str:
    cid = DB.new_id("C")
    conn.execute(
        "INSERT INTO contracts(contract_id, employee_id, dept_id, title, supplier,"
        " amount, start_date, end_date, payment_terms, status, procurement_id,"
        " ai_review, evidence_file)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, employee_id, dept_id, title, supplier, amount, start_date, end_date,
         payment_terms, PENDING_REVIEW, procurement_id,
         json.dumps(ai_review or {}, ensure_ascii=False), evidence_file))
    conn.commit()
    return cid


def approve(conn, contract_id: str, actor_id: str, role: str,
            comment: str = "") -> str:
    c = get(conn, contract_id)
    if c is None:
        raise ValueError(f"合同不存在: {contract_id}")
    if c["status"] not in (PENDING_REVIEW, REVIEWING):
        raise ValueError(f"状态 {c['status']} 不可审批")
    if _upsert_approvers(conn, contract_id, role, actor_id):
        AP.record(conn, doc_type="contract", doc_id=contract_id, actor=actor_id,
                  role=role, decision=AP.APPROVE, comment=comment)
    c = get(conn, contract_id)
    done = {a["role"] for a in c["approvers"]}
    required = {"legal", "boss"}
    if required <= done:
        DB.update_fields(conn, "contracts", "contract_id", contract_id,
                         status=ACTIVE, updated_at=DB.now())
        return ACTIVE
    DB.update_fields(conn, "contracts", "contract_id", contract_id,
                     status=REVIEWING, updated_at=DB.now())
    return REVIEWING


def reject(conn, contract_id: str, actor_id: str, role: str,
           reason: str = "") -> None:
    c = get(conn, contract_id)
    if c is None:
        raise ValueError(f"合同不存在: {contract_id}")
    if c["status"] not in (PENDING_REVIEW, REVIEWING):
        raise ValueError(f"状态 {c['status']} 不可驳回")
    AP.record(conn, doc_type="contract", doc_id=contract_id, actor=actor_id,
              role=role, decision=AP.REJECT, comment=reason)
    DB.update_fields(conn, "contracts", "contract_id", contract_id,
                     status=REJECTED, updated_at=DB.now())


def renew(conn, old_id: str, new_end_date: str, amount: float | None = None,
          actor_id: str = "") -> str:
    old = get(conn, old_id)
    if old is None:
        raise ValueError(f"合同不存在: {old_id}")
    if old["status"] not in (ACTIVE, EXPIRING):
        raise ValueError(f"状态 {old['status']} 不可续签")
    DB.update_fields(conn, "contracts", "contract_id", old_id,
                     status=RENEWED, updated_at=DB.now())
    return create(conn, employee_id=old["employee_id"], dept_id=old["dept_id"],
                  title=old["title"], supplier=old["supplier"],
                  amount=amount if amount is not None else old["amount"],
                  start_date=dt.date.today().isoformat(), end_date=new_end_date,
                  payment_terms=old["payment_terms"], procurement_id=old["procurement_id"],
                  ai_review=None, evidence_file=None)


def close(conn, contract_id: str, reason: str = "到期关闭") -> None:
    c = get(conn, contract_id)
    if c is None:
        raise ValueError(f"合同不存在: {contract_id}")
    if c["status"] not in (ACTIVE, EXPIRING):
        raise ValueError(f"状态 {c['status']} 不可关闭")
    DB.update_fields(conn, "contracts", "contract_id", contract_id,
                     status=CLOSED, updated_at=DB.now())


def paid_total(conn, contract_id: str) -> float:
    row = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments"
                       " WHERE contract_id=? AND status=?",
                       (contract_id, PAY_PAID)).fetchone()
    return float(row[0] or 0)


def create_payment(conn, *, contract_id: str | None, procurement_id: str | None,
                   amount: float, seq: str = "", employee_id: str = "",
                   note: str = "") -> str:
    pid = DB.new_id("PM")
    conn.execute(
        "INSERT INTO payments(payment_id, contract_id, procurement_id, amount,"
        " seq, status, employee_id, note) VALUES(?,?,?,?,?,?,?,?)",
        (pid, contract_id, procurement_id, float(amount), seq, PAY_PENDING,
         employee_id, note))
    conn.commit()
    return pid


def pay(conn, payment_id: str, actor_id: str = "finance") -> dict:
    row = conn.execute("SELECT status FROM payments WHERE payment_id=?",
                       (payment_id,)).fetchone()
    if row is None:
        raise ValueError(f"付款单不存在: {payment_id}")
    if row[0] != PAY_PENDING:
        raise ValueError(f"状态 {row[0]} 不可打款")
    DB.update_fields(conn, "payments", "payment_id", payment_id,
                     status=PAY_PAID, paid_at=DB.now())
    pm = dict(zip(("payment_id", "contract_id"), conn.execute(
        "SELECT payment_id, contract_id FROM payments WHERE payment_id=?",
        (payment_id,)).fetchone()))
    return pm


def list_for(conn, employee_id: str | None = None, status: str | None = None) -> list[dict]:
    sql, args = "SELECT contract_id FROM contracts WHERE 1=1", []
    if employee_id:
        sql += " AND employee_id=?"
        args.append(employee_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT 10"
    return [get(conn, r[0]) for r in conn.execute(sql, args).fetchall()]


def expiring(conn, days: int = 30, now: dt.date | None = None) -> list[dict]:
    now = now or dt.date.today()
    out = []
    for cid, in conn.execute("SELECT contract_id FROM contracts WHERE status=?",
                             (ACTIVE,)).fetchall():
        c = get(conn, cid)
        if not c or not c.get("end_date"):
            continue
        try:
            end = dt.date.fromisoformat(str(c["end_date"])[:10])
        except ValueError:
            continue
        left = (end - now).days
        if 0 <= left <= days:
            out.append({**c, "days_left": left})
    return out


def to_table(rows: Sequence[Mapping]) -> str:
    if not rows:
        return "（无合同）"
    lines = []
    for c in rows:
        paid = float(c.get("paid_total") or 0)
        lines.append(f"  {c['contract_id']} [{c['status']}] {c['title']} "
                     f"{c.get('supplier') or '-'} "
                     f"{c.get('amount') or '-'}元(已付{paid:.0f}) "
                     f"至 {str(c.get('end_date'))[:10]}")
    return "\n".join(lines)
