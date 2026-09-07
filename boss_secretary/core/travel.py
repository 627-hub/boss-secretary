"""差旅事前申请 + 员工借款台账（内控闭环最后两块支出侧拼图）。

差旅：申请（目的地/期间/预估）→ 经理审批 → 生效期内报销自动关联
（报销日期落在出差期间 → 单据记 trip_id，超预估 20% 进确认门）。
借款：申请 → boss 审批 → 财务打款(paid_out) → 报销冲销或手工核销 → 余额≤0 关闭；
open 借款超 60 天 → 月度提醒。
"""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

TRIP_PENDING = "pending"
TRIP_ACTIVE = "active"
TRIP_REJECTED = "rejected"
TRIP_CLOSED = "closed"

LOAN_PENDING = "pending"
LOAN_PAID_OUT = "paid_out"
LOAN_OPEN = "open"
LOAN_CLOSED = "closed"
LOAN_REJECTED = "rejected"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}{dt.date.today():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


# ── 差旅事前申请 ──────────────────────────────────────────────

def trip_request(conn, *, employee_id: str, dept_id: str | None, destination: str,
                 reason: str, estimate: float, start_date: str,
                 end_date: str) -> dict:
    tid = _id("TR")
    conn.execute(
        "INSERT INTO trips(trip_id, employee_id, dept_id, destination, reason,"
        " estimate, start_date, end_date) VALUES(?,?,?,?,?,?,?,?)",
        (tid, employee_id, dept_id, destination, reason, float(estimate),
         start_date, end_date))
    conn.commit()
    return get_trip(conn, tid)


def get_trip(conn, trip_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in conn.execute("SELECT * FROM trips LIMIT 1")
                     .description], row))


def decide_trip(conn, trip_id: str, approver: str, approve: bool) -> dict | None:
    t = get_trip(conn, trip_id)
    if t is None or t["status"] != TRIP_PENDING:
        return None
    new = TRIP_ACTIVE if approve else TRIP_REJECTED
    conn.execute("UPDATE trips SET status=?, approver=? WHERE trip_id=?",
                 (new, approver, trip_id))
    conn.commit()
    return get_trip(conn, trip_id)


def trip_for(conn, employee_id: str, occurred_at: str) -> dict | None:
    """报销发生日期落在某 active 出差期间 → 返回该出差单。"""
    row = conn.execute(
        "SELECT trip_id FROM trips WHERE employee_id=? AND status=?"
        " AND ? BETWEEN start_date AND end_date ORDER BY created_at DESC LIMIT 1",
        (employee_id, TRIP_ACTIVE, str(occurred_at)[:10])).fetchone()
    return get_trip(conn, row[0]) if row else None


def trip_list(conn, employee_id: str | None = None, limit: int = 10) -> list[dict]:
    sql = "SELECT trip_id FROM trips"
    args: list = []
    if employee_id:
        sql += " WHERE employee_id=?"
        args.append(employee_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [get_trip(conn, r[0]) for r in conn.execute(sql, args).fetchall()]


def trip_table(rows: Sequence[Mapping]) -> str:
    if not rows:
        return "（无出差申请）"
    marks = {TRIP_PENDING: "⏳", TRIP_ACTIVE: "✓在途", TRIP_REJECTED: "✗",
             TRIP_CLOSED: "✓已结"}
    lines = []
    for t in rows:
        lines.append(f"  {marks.get(t['status'], '?')} {t['trip_id']} "
                     f"{t.get('destination')} {str(t.get('start_date'))[:10]}~"
                     f"{str(t.get('end_date'))[:10]} 预估{t.get('estimate') or 0:.0f}元 "
                     f"「{t.get('reason') or ''}」")
    return "\n".join(lines)


# ── 员工借款台账 ──────────────────────────────────────────────

def loan_request(conn, *, employee_id: str, amount: float, reason: str = "",
                 approver: str | None = None) -> dict:
    lid = _id("L")
    conn.execute(
        "INSERT INTO loans(loan_id, employee_id, amount, reason, approver)"
        " VALUES(?,?,?,?,?)",
        (lid, employee_id, float(amount), reason, approver))
    conn.commit()
    return get_loan(conn, lid)


def get_loan(conn, loan_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM loans WHERE loan_id=?", (loan_id,)).fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in conn.execute("SELECT * FROM loans LIMIT 1")
                     .description], row))


def remaining(loan: Mapping) -> float:
    return max(0.0, float(loan["amount"]) - float(loan.get("repaid_amount") or 0))


def loan_decide(conn, loan_id: str, approver: str, approve: bool) -> dict | None:
    l = get_loan(conn, loan_id)
    if l is None or l["status"] != LOAN_PENDING:
        return None
    if not approve:
        conn.execute("UPDATE loans SET status=? WHERE loan_id=?",
                     (LOAN_REJECTED, loan_id))
        conn.commit()
        return get_loan(conn, loan_id)
    conn.execute("UPDATE loans SET status=?, approver=? WHERE loan_id=?",
                 (LOAN_PAID_OUT, approver, loan_id))
    conn.commit()
    return get_loan(conn, loan_id)  # PAID_OUT = 已放款，进入 open 台账


def loan_offset(conn, loan_id: str, amount: float, by: str = "") -> float:
    """核销（报销冲销或工资抵扣）。余额≤0 → closed。"""
    l = get_loan(conn, loan_id)
    if l is None or l["status"] not in (LOAN_PAID_OUT, LOAN_OPEN):
        raise ValueError(f"借款 {loan_id} 状态 {l['status'] if l else '缺失'} 不可核销")
    new_repaid = min(float(l["amount"]), float(l.get("repaid_amount") or 0) + amount)
    status = LOAN_CLOSED if new_repaid >= float(l["amount"]) else LOAN_OPEN
    conn.execute("UPDATE loans SET repaid_amount=?, status=? WHERE loan_id=?",
                 (new_repaid, status, loan_id))
    conn.commit()
    return max(0.0, float(l["amount"]) - new_repaid)


def overdue_loans(conn, days: int = 60, now: dt.datetime | None = None) -> list[dict]:
    now = now or dt.datetime.now()
    out = []
    cutoff = (now - dt.timedelta(days=days)).isoformat()
    for lid, in conn.execute("SELECT loan_id FROM loans WHERE status=?"
                             " AND created_at < ?", (LOAN_PAID_OUT, cutoff)).fetchall():
        l = get_loan(conn, lid)
        if l and remaining(l) > 0:
            out.append({**l, "overdue_days": (now - dt.datetime.fromisoformat(
                str(l["created_at"])[:19])).days})
    return out


def loan_list(conn, employee_id: str | None = None) -> list[dict]:
    sql = "SELECT loan_id FROM loans"
    args: list = []
    if employee_id:
        sql += " WHERE employee_id=?"
        args.append(employee_id)
    sql += " ORDER BY created_at DESC LIMIT 10"
    return [get_loan(conn, r[0]) for r in conn.execute(sql, args).fetchall()]


def loan_table(rows: Sequence[Mapping]) -> str:
    if not rows:
        return "（无借款记录）"
    marks = {LOAN_PENDING: "⏳", LOAN_PAID_OUT: "💸已放款", LOAN_OPEN: "🧾未结",
             LOAN_CLOSED: "✅已结", LOAN_REJECTED: "✗"}
    lines = []
    for l in rows:
        lines.append(f"  {marks.get(l['status'], '?')} {l['loan_id']} "
                     f"{l['employee_id']} 借{l['amount']:.0f}元 "
                     f"余{remaining(l):.0f} 「{l.get('reason') or ''}」")
    return "\n".join(lines)
