"""权限层：角色 × 数据范围 × 单据密级（PRD §2.1）。

查询/日报/导出所有入口统一经此判定，先于任何 LLM 上下文注入——
LLM 不是可信边界，无权数据根本不进 prompt。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EMPLOYEE = "EMPLOYEE"
MANAGER = "MANAGER"
FINANCE = "FINANCE"
AUDIT = "AUDIT"
BOSS = "BOSS"

NORMAL = "normal"
CONFIDENTIAL = "confidential"

APPROVED = "APPROVED"

SCOPE_ALL = "ALL"
SCOPE_DEPT = "DEPT"
SCOPE_SELF = "SELF"
SCOPE_FINANCE_PENDING = "FINANCE_PENDING"

_FULL_FIELDS = ("ticket_id", "employee_id", "dept_id", "type", "status", "sensitivity",
                "amount", "currency", "expense_type", "occurred_at", "reason",
                "invoice_code", "invoice_no", "invoice_seller", "invoice_amount",
                "risk_score", "ai_verdict")


class PermissionDenied(Exception):
    pass


@dataclass(frozen=True)
class Actor:
    user_id: str
    name: str = ""
    role: str = EMPLOYEE
    dept_id: str | None = None
    manager_user_id: str | None = None


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    employee_id: str
    dept_id: str | None
    status: str
    sensitivity: str = NORMAL
    type: str = "reimburse"
    amount: float | None = None
    currency: str = "CNY"
    expense_type: str | None = None
    occurred_at: str | None = None
    reason: str | None = None
    invoice_code: str | None = None
    invoice_no: str | None = None
    invoice_seller: str | None = None
    invoice_amount: float | None = None
    risk_score: float | None = None
    ai_verdict: str | None = None
    approvers: tuple[str, ...] = ()


def scope_of(actor: Actor) -> str:
    if actor.role in (BOSS, AUDIT):
        return SCOPE_ALL
    if actor.role == FINANCE:
        return SCOPE_FINANCE_PENDING
    if actor.role == MANAGER:
        return SCOPE_DEPT
    return SCOPE_SELF


def can_view_ticket(actor: Actor, t: Ticket) -> bool:
    if actor.user_id == t.employee_id:
        return True
    if actor.role in (BOSS, AUDIT):
        return True
    if actor.role == MANAGER:
        return t.dept_id is not None and t.dept_id == actor.dept_id
    if actor.role == FINANCE:
        return t.status == APPROVED
    return False


def _confidential_restricted(actor: Actor, t: Ticket) -> bool:
    if t.sensitivity != CONFIDENTIAL:
        return False
    if actor.user_id == t.employee_id:
        return False
    if actor.role in (BOSS, AUDIT):
        return False
    return actor.user_id not in (t.approvers or ())


def view_ticket(actor: Actor, t: Ticket) -> dict[str, Any]:
    if not can_view_ticket(actor, t):
        raise PermissionDenied(f"{actor.user_id}({actor.role}) 无权查看 {t.ticket_id}")
    base = {"ticket_id": t.ticket_id, "employee_id": t.employee_id,
            "dept_id": t.dept_id, "type": t.type, "status": t.status,
            "sensitivity": t.sensitivity}
    if _confidential_restricted(actor, t):
        return {**base, "redacted": True,
                "note": "机密单据：明细仅审批链与老板可见"}
    full = {**base, "redacted": False}
    for f in _FULL_FIELDS[6:]:
        full[f] = getattr(t, f)
    return full


def sql_scope(actor: Actor) -> tuple[str, list[Any]]:
    s = scope_of(actor)
    if s == SCOPE_SELF:
        return "employee_id = ?", [actor.user_id]
    if s == SCOPE_DEPT:
        return "dept_id = ? AND (sensitivity != 'confidential' OR employee_id = ?)", \
               [actor.dept_id, actor.user_id]
    if s == SCOPE_FINANCE_PENDING:
        return "status = 'APPROVED'", []
    return "1=1", []


def daily_report_version(actor: Actor, company_report_enabled: bool = False) -> str | None:
    if actor.role == BOSS:
        return "company"
    if actor.role == AUDIT:
        return "audit"
    if actor.role == FINANCE:
        return "finance"
    if actor.role == MANAGER:
        return "company" if company_report_enabled else "department"
    return None
