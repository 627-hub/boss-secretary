"""流程引擎（PRD §4 状态机 / §7.7 流程行为）：审查编排 + 会签 + 超时升级 + 审计。

- 责任矩阵只回答"谁处理"（DMN），本模块回答"怎么走"（BPMN 语义）：
  状态机、会签（并行网关）、超时升级（定时器）、驳回重提（关联原单）。
- LLM/飞书通过注入解耦：llm_verdict 由上游填入 ctx；通知走 Notifier 协议；
  角色解析走 role_resolvers（manager→直属上级等）。
- fail-safe：LLM 未执行时按 WARN 降级；规则阻断永远压过直批。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import yaml

from boss_secretary.core import compliance as C
from boss_secretary.core import matrix as M
from boss_secretary import models as MD

DRAFT = "DRAFT"
REVIEWING = "REVIEWING"
AUTO_APPROVED = "AUTO_APPROVED"
SUBMITTED = "SUBMITTED"
APPROVED = "APPROVED"
PAID = "PAID"
REJECTED = "REJECTED"
ESCALATED = "ESCALATED"
WITHDRAWN = "WITHDRAWN"
CANCELLED = "CANCELLED"

MANAGER_ROLE = "manager"
BOSS_ROLE = "boss"
FINANCE_CONSIGN_ROLE = "finance_consign"

ACTION_APPROVERS: dict[str, tuple[str, ...]] = {
    "AUTO_APPROVE": (),
    "AUTO_REJECT": (),
    "MANAGER": (MANAGER_ROLE,),
    "MANAGER_BOSS": (MANAGER_ROLE, BOSS_ROLE),
    "MANAGER_BOSS_FINANCE_CONSIGN": (MANAGER_ROLE, BOSS_ROLE, FINANCE_CONSIGN_ROLE),
    "MANUAL_REVIEW": (BOSS_ROLE,),
}


class RouterError(Exception):
    pass


class TransitionError(RouterError):
    pass


class PermissionError_(RouterError):
    pass


TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {REVIEWING, WITHDRAWN},
    REVIEWING: {AUTO_APPROVED, SUBMITTED, REJECTED, DRAFT},
    AUTO_APPROVED: {PAID, CANCELLED},
    SUBMITTED: {APPROVED, REJECTED, ESCALATED, CANCELLED, WITHDRAWN},
    ESCALATED: {APPROVED, REJECTED, CANCELLED},
    APPROVED: {PAID, CANCELLED},
    REJECTED: {CANCELLED},
    WITHDRAWN: set(),
    CANCELLED: set(),
    PAID: set(),
}


@dataclass(frozen=True)
class Flow:
    name: str
    version: int
    matrix_name: str
    rules: tuple[str, ...]
    llm_review_enabled: bool
    llm_review_temperature: float
    timeout_hours: int
    escalate_to: str
    extract_required: tuple[str, ...]
    source: str = ""


def load_flow(path: str | Path) -> Flow:
    d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Flow(name=d["flow"], version=int(d.get("version", 1)),
                matrix_name=d.get("matrix", "reimburse"),
                rules=tuple(d.get("rules") or ()),
                llm_review_enabled=bool((d.get("llm_review") or {}).get("enabled", True)),
                llm_review_temperature=float((d.get("llm_review") or {}).get("temperature", 0.1)),
                timeout_hours=int((d.get("timeout") or {}).get("hours", 48)),
                escalate_to=(d.get("timeout") or {}).get("escalate_to", BOSS_ROLE),
                extract_required=tuple((d.get("extract") or {}).get("required") or ()),
                source=str(path))


def validate_transition(old: str, new: str) -> bool:
    return new in TRANSITIONS.get(old, set())


@dataclass(frozen=True)
class ReviewOutcome:
    rule_results: tuple[C.RuleResult, ...]
    summary: dict
    decision: M.Decision
    next_status: str
    approver_roles: tuple[str, ...]
    llm_verdict_used: str
    notes: tuple[str, ...]


def review(ctx: Mapping[str, Any], *, flow: Flow, matrix: M.Matrix,
           rules_cfg: dict | None = None, history: Sequence[Mapping] = (),
           today: dt.date | None = None) -> ReviewOutcome:
    today = today or dt.date.today()
    results = C.run_rules(ctx, history, rules_cfg, today)
    summary = C.summarize(results)
    notes: list[str] = []
    llm_v = ctx.get("llm_verdict")
    if llm_v not in (C.PASS, C.WARN, C.FAIL):
        if flow.llm_review_enabled:
            llm_v = C.WARN
            notes.append("LLM 审查未执行，按 WARN 降级（fail-safe）")
        else:
            llm_v = C.PASS
            notes.append("LLM 审查未启用，以规则引擎为准")
    mctx = {**ctx, "rule_result": summary["overall"], "llm_verdict": llm_v}
    try:
        decision = M.evaluate(matrix, mctx)
        hit_id, action, notify = decision.hit_rule_id, decision.action, decision.notify
    except M.NoMatchError:
        decision = None
        hit_id, action, notify = None, M.FALLBACK_ACTION, ()
        notes.append("责任矩阵未命中，回落 MANUAL_REVIEW")
    approver_roles = ACTION_APPROVERS.get(action, (BOSS_ROLE,))
    next_status = SUBMITTED
    if action == "AUTO_APPROVE":
        next_status = AUTO_APPROVED
        approver_roles = ()
    elif action == "AUTO_REJECT":
        next_status = REJECTED
        approver_roles = ()
    if action == "AUTO_APPROVE" and summary["overall"] == C.FAIL:
        next_status = SUBMITTED
        approver_roles = (BOSS_ROLE,)
        notes.append("规则阻断压过直批（双保险）")
    return ReviewOutcome(rule_results=tuple(results), summary=summary,
                         decision=M.Decision(
                             matrix_name=matrix.name, matrix_version=matrix.version,
                             hit_rule_id=hit_id, action=action, notify=notify,
                             matched_rows=decision.matched_rows if decision else ()),
                         next_status=next_status, approver_roles=approver_roles,
                         llm_verdict_used=llm_v, notes=tuple(notes))


class Notifier(Protocol):
    def send(self, event: str, ticket: Mapping, to: Sequence[str]) -> None: ...


class PrintNotifier:
    def send(self, event: str, ticket: Mapping, to: Sequence[str]) -> None:
        print(f"[NOTIFY] {event} → {','.join(to) or '-'} | "
              f"{ticket.get('ticket_id')} {ticket.get('status')}")


class SQLiteTicketStore:
    def __init__(self, db_path: str | Path, audit_dir: str | Path = "data/audit"):
        self.conn: sqlite3.Connection = MD.init_db(db_path)
        self.lock = threading.Lock()
        self.audit_dir = Path(audit_dir) / "tickets"
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def create(self, record: Mapping[str, Any], ctx: Mapping[str, Any]) -> str:
        tid = record["ticket_id"]
        ctx_file = self.audit_dir / f"{tid}.json"
        ctx_file.write_text(json.dumps(dict(ctx), ensure_ascii=False, indent=1),
                            encoding="utf-8")
        h = hashlib.sha256(ctx_file.read_bytes()).hexdigest()
        params = {"ticket_id": record["ticket_id"],
                  "feishu_instance_id": record.get("feishu_instance_id"),
                  "employee_id": record["employee_id"],
                  "dept_id": record.get("dept_id"),
                  "type": record.get("type", "reimburse"),
                  "status": record["status"],
                  "matrix_version": record.get("matrix_version"),
                  "sensitivity": record.get("sensitivity", "normal"),
                  "amount": record.get("amount"),
                  "currency": record.get("currency", "CNY"),
                  "expense_type": record.get("expense_type"),
                  "occurred_at": record.get("occurred_at"),
                  "reason": record.get("reason"),
                  "invoice_code": record.get("invoice_code"),
                  "invoice_no": record.get("invoice_no"),
                  "invoice_seller": record.get("invoice_seller"),
                  "invoice_amount": record.get("invoice_amount"),
                  "ai_evidence": f"{ctx_file}|{h}"}
        with self.lock:
            self.conn.execute(
                "INSERT INTO tickets(ticket_id, feishu_instance_id, employee_id, dept_id,"
                " type, status, matrix_version, sensitivity, amount, currency,"
                " expense_type, occurred_at, reason, invoice_code, invoice_no,"
                " invoice_seller, invoice_amount, ai_evidence)"
                " VALUES(:ticket_id,:feishu_instance_id,:employee_id,:dept_id,:type,"
                ":status,:matrix_version,:sensitivity,:amount,:currency,:expense_type,"
                ":occurred_at,:reason,:invoice_code,:invoice_no,:invoice_seller,"
                ":invoice_amount,:ai_evidence)", params)
            self.conn.commit()
        MD.append_audit(self.conn, ticket_id=tid, actor=record.get("employee_id", "?"),
                        action="ticket.create", payload_hash=h,
                        payload_file=str(ctx_file))
        return tid

    def get(self, ticket_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tickets WHERE ticket_id=?",
                                (ticket_id,)).fetchone()
        if row is None:
            return None
        rec = dict(zip([c[0] for c in self.conn.execute(
            "SELECT * FROM tickets LIMIT 1").description], row))
        for k in ("approvers", "approvals"):
            rec[k] = json.loads(rec[k]) if rec.get(k) else []
        return rec

    def update(self, ticket_id: str, **fields: Any) -> None:
        with self.lock:
            for k, v in fields.items():
                if k in ("approvers", "approvals") and not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False)
                self.conn.execute(f"UPDATE tickets SET {k}=? WHERE ticket_id=?",
                                  (v, ticket_id))
            self.conn.commit()

    def list_by_status(self, *statuses: str) -> list[dict]:
        out = []
        for s in statuses:
            for row in self.conn.execute("SELECT ticket_id FROM tickets WHERE status=?",
                                         (s,)).fetchall():
                rec = self.get(row[0])
                if rec:
                    out.append(rec)
        return out

    def list_all(self) -> list[dict]:
        return [self.get(r[0]) for r in
                self.conn.execute("SELECT ticket_id FROM tickets ORDER BY rowid")
                .fetchall()]

    def history(self, ctx: Mapping[str, Any], lookback_days: int = 90) -> list[dict]:
        base = C._parse_date(ctx.get("occurred_at")) or dt.date.today()
        lo = (base - dt.timedelta(days=lookback_days)).isoformat()
        hi = (base + dt.timedelta(days=lookback_days)).isoformat()
        out = []
        for row in self.conn.execute(
                "SELECT ticket_id, employee_id, amount, invoice_no, invoice_seller,"
                " occurred_at FROM tickets WHERE occurred_at BETWEEN ? AND ?",
                (lo, hi)).fetchall():
            out.append(dict(zip(("ticket_id", "employee_id", "amount", "invoice_no",
                                 "invoice_seller", "occurred_at"), row)))
        return out


class Router:
    def __init__(self, flow: Flow, matrix: M.Matrix, store: SQLiteTicketStore,
                 rules_cfg: dict | None = None, notifier: Notifier | None = None,
                 role_resolvers: Mapping[str, Callable[[dict], str | None]] | None = None,
                 today_fn: Callable[[], dt.date] | None = None):
        self.flow = flow
        self.matrix = matrix
        self.store = store
        self.rules_cfg = rules_cfg or C.default_config()
        self.notifier = notifier or PrintNotifier()
        self.role_resolvers = dict(role_resolvers or {})
        self.today_fn = today_fn or dt.date.today

    def _audit(self, ticket_id: str, actor: str, action: str, **extra: Any) -> None:
        MD.append_audit(self.store.conn, ticket_id=ticket_id, actor=actor,
                        action=action,
                        payload_hash=hashlib.sha256(
                            json.dumps(extra, ensure_ascii=False, sort_keys=True,
                                       default=str).encode()).hexdigest())

    def _notify(self, event: str, ticket: Mapping, to_roles: Sequence[str]) -> None:
        to: list[str] = []
        for role in to_roles:
            r = self.role_resolvers.get(role)
            uid = r(ticket) if r else None
            if uid:
                to.append(uid)
        self.notifier.send(event, ticket, to or [ticket.get("employee_id", "?")])

    def _transition(self, ticket_id: str, old: str, new: str, actor: str,
                    reason: str = "") -> None:
        if not validate_transition(old, new):
            raise TransitionError(f"非法状态迁移 {old} → {new}")
        self.store.update(ticket_id, status=new)
        self._audit(ticket_id, actor, f"status.{old}→{new}", reason=reason)

    def create_ticket(self, ctx: Mapping[str, Any], employee: Mapping[str, Any]) -> str:
        tid = f"T{dt.date.today():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        record = {
            "ticket_id": tid, "feishu_instance_id": ctx.get("feishu_instance_id"),
            "employee_id": employee["user_id"], "dept_id": employee.get("dept_id"),
            "type": self.flow.name, "status": DRAFT,
            "matrix_version": self.matrix.version,
            "sensitivity": ctx.get("sensitivity") or "normal",
            "amount": ctx.get("amount"), "currency": ctx.get("currency", "CNY"),
            "expense_type": ctx.get("expense_type"), "occurred_at": ctx.get("occurred_at"),
            "reason": ctx.get("reason"), "invoice_code": ctx.get("invoice_code"),
            "invoice_no": ctx.get("invoice_no"),
            "invoice_seller": ctx.get("invoice_seller"),
            "invoice_amount": ctx.get("invoice_amount"),
        }
        self.store.create(record, {**ctx, "ticket_id": tid})
        self._notify("ticket.created", record, [])
        return tid

    def run_review(self, ticket_id: str, llm_verdict: str | None = None) -> ReviewOutcome:
        t = self.store.get(ticket_id)
        if t is None:
            raise RouterError(f"单据不存在: {ticket_id}")
        if t["status"] not in (DRAFT, REVIEWING):
            raise TransitionError(f"状态 {t['status']} 不可审查")
        self._transition(ticket_id, t["status"], REVIEWING, "system", "开始审查")
        ctx_file = (t.get("ai_evidence") or "").split("|")[0]
        ctx = json.loads(Path(ctx_file).read_text(encoding="utf-8")) if ctx_file else {}
        if llm_verdict:
            ctx = {**ctx, "llm_verdict": llm_verdict}
        history = self.store.history(ctx, lookback_days=int(
            (self.rules_cfg.get("R2") or {}).get("params", {}).get("lookback_days", 90)))
        outcome = review(ctx, flow=self.flow, matrix=self.matrix,
                         rules_cfg=self.rules_cfg, history=history,
                         today=self.today_fn())
        fields: dict[str, Any] = {
            "ai_verdict": outcome.decision.action,
            "approvers": list(outcome.approver_roles), "approvals": [],
        }
        if outcome.next_status == SUBMITTED:
            fields["submitted_at"] = dt.datetime.now().isoformat(timespec="seconds")
        self.store.update(ticket_id, **fields)
        self._transition(ticket_id, REVIEWING, outcome.next_status, "system",
                         f"矩阵 {outcome.decision.hit_rule_id} → {outcome.decision.action}")
        event = {"AUTO_APPROVED": "ticket.auto_approved",
                 "REJECTED": "ticket.rejected"}.get(outcome.next_status,
                                                    "ticket.submitted")
        self._notify(event, self.store.get(ticket_id) or {},
                     outcome.approver_roles)
        return outcome

    def approve(self, ticket_id: str, actor_id: str, actor_role: str) -> str:
        t = self.store.get(ticket_id)
        if t is None:
            raise RouterError(f"单据不存在: {ticket_id}")
        if t["status"] not in (SUBMITTED, ESCALATED):
            raise TransitionError(f"状态 {t['status']} 不可审批")
        roles = t.get("approvers") or []
        if actor_role not in roles:
            raise PermissionError_(f"角色 {actor_role} 不在审批链 {roles} 中")
        if t["status"] == ESCALATED and actor_role == self.flow.escalate_to:
            self.store.update(ticket_id, approvals=[{"role": actor_role,
                                                     "user_id": actor_id}])
            self._transition(ticket_id, ESCALATED, APPROVED, actor_id, "超时升级老板终裁")
            self._notify("ticket.approved", self.store.get(ticket_id) or {}, ["finance"])
            return APPROVED
        approvals = list(t.get("approvals") or [])
        if not any(a.get("role") == actor_role for a in approvals):
            approvals.append({"role": actor_role, "user_id": actor_id})
            self.store.update(ticket_id, approvals=approvals)
            self._audit(ticket_id, actor_id, "approve", role=actor_role)
        done_roles = {a["role"] for a in approvals}
        if set(roles) <= done_roles:
            self._transition(ticket_id, t["status"], APPROVED, actor_id, "会签完成")
            self._notify("ticket.approved", self.store.get(ticket_id) or {},
                         ["finance"])
            return APPROVED
        self._notify("ticket.approval_progress", self.store.get(ticket_id) or [],
                     [r for r in roles if r not in done_roles])
        return t["status"]

    def reject(self, ticket_id: str, actor_id: str, actor_role: str,
               reason: str) -> None:
        t = self.store.get(ticket_id)
        if t is None:
            raise RouterError(f"单据不存在: {ticket_id}")
        if t["status"] not in (SUBMITTED, ESCALATED):
            raise TransitionError(f"状态 {t['status']} 不可驳回")
        if actor_role not in (t.get("approvers") or []):
            raise PermissionError_(f"角色 {actor_role} 不在审批链中")
        self._transition(ticket_id, t["status"], REJECTED, actor_id, reason)
        self._notify("ticket.rejected", self.store.get(ticket_id) or {}, [])

    def withdraw(self, ticket_id: str, actor_id: str) -> None:
        t = self.store.get(ticket_id)
        if t is None:
            raise RouterError(f"单据不存在: {ticket_id}")
        if t["employee_id"] != actor_id:
            raise PermissionError_("仅发起人可撤回")
        if t["status"] not in (DRAFT, SUBMITTED):
            raise TransitionError(f"状态 {t['status']} 不可撤回")
        self._transition(ticket_id, t["status"], WITHDRAWN, actor_id, "员工撤回")
        self._notify("ticket.withdrawn", self.store.get(ticket_id) or {}, [])

    def cancel(self, ticket_id: str, actor_role: str, reason: str) -> None:
        if actor_role != BOSS_ROLE:
            raise PermissionError_("仅老板可作废")
        t = self.store.get(ticket_id)
        if t is None:
            raise RouterError(f"单据不存在: {ticket_id}")
        if t["status"] in (WITHDRAWN, CANCELLED):
            raise TransitionError(f"状态 {t['status']} 已终态")
        self._transition(ticket_id, t["status"], CANCELLED, BOSS_ROLE, reason)
        self._notify("ticket.cancelled", self.store.get(ticket_id) or {}, [])

    def mark_paid(self, ticket_id: str, actor_id: str, actor_role: str) -> None:
        """财务确认打款 → 单据最终关闭（PAID 终态）。"""
        t = self.store.get(ticket_id)
        if t is None:
            raise RouterError(f"单据不存在: {ticket_id}")
        if actor_role != FINANCE_CONSIGN_ROLE and actor_role != "finance":
            raise PermissionError_("仅财务可确认打款")
        if t["status"] not in (APPROVED, AUTO_APPROVED):
            raise TransitionError(f"状态 {t['status']} 不可打款确认（仅已通过单）")
        self._transition(ticket_id, t["status"], PAID, actor_id, "财务确认打款")
        self._notify("ticket.paid", self.store.get(ticket_id) or {}, [])

    def resubmit(self, original_id: str, updates: Mapping[str, Any],
                 employee: Mapping[str, Any]) -> str:
        original = self.store.get(original_id)
        if original is None:
            raise RouterError(f"原单不存在: {original_id}")
        if original["status"] not in (REJECTED, WITHDRAWN):
            raise TransitionError("仅驳回/撤回单可重提")
        ctx_file = (original.get("ai_evidence") or "").split("|")[0]
        base_ctx = json.loads(Path(ctx_file).read_text(encoding="utf-8")) if ctx_file else {}
        ctx = {**base_ctx, **updates}
        ctx.pop("llm_verdict", None)
        new_id = self.create_ticket(ctx, employee)
        self.store.update(new_id, withdrawn_from_ticket_id=original_id)
        self._audit(new_id, employee.get("user_id", "?"), "ticket.resubmit",
                    from_ticket=original_id)
        return new_id

    def check_timeouts(self, now: dt.datetime | None = None) -> list[str]:
        now = now or dt.datetime.now()
        deadline = now - dt.timedelta(hours=self.flow.timeout_hours)
        escalated: list[str] = []
        for t in self.store.list_by_status(SUBMITTED):
            submitted = t.get("submitted_at")
            if not submitted:
                continue
            if dt.datetime.fromisoformat(submitted) < deadline:
                self._transition(t["ticket_id"], SUBMITTED, ESCALATED, "system",
                                 f"超 {self.flow.timeout_hours}h 未审，升级"
                                 f" {self.flow.escalate_to}")
                roles = list(t.get("approvers") or [])
                if self.flow.escalate_to not in roles:
                    roles.append(self.flow.escalate_to)
                self.store.update(t["ticket_id"], approvers=roles)
                self._notify("ticket.escalated", self.store.get(t["ticket_id"]) or {},
                             [self.flow.escalate_to])
                escalated.append(t["ticket_id"])
        return escalated
