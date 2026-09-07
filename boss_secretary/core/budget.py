"""预算检查（PRD F14）：部门/全司月度预算，额度审批与报销提交两个检查点。

口径：消费归属月 = occurred_at 的月份；已用 = 该月非驳回单据金额合计
（APPROVED/PAID/AUTO_APPROVED/SUBMITTED/ESCALATED 在途计入）。
部门未设预算时不检查；dept_id 为空的员工归入全司预算（dept_id='*'）。
超预算默认**提示不阻断**（settings.budgets.block_over=true 时拒绝提交）。

CLI: 无（经 boss-feishu 命令 `预算`）。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

COUNTED = ("APPROVED", "PAID", "AUTO_APPROVED", "SUBMITTED", "ESCALATED")
ALL_DEPT = "*"


def set_budget(conn, dept_id: str, month: str, amount: float,
               created_by: str = "") -> None:
    conn.execute(
        "INSERT INTO budgets(dept_id, month, amount, created_by)"
        " VALUES(?,?,?,?) ON CONFLICT(dept_id, month)"
        " DO UPDATE SET amount=excluded.amount, created_by=excluded.created_by,"
        " updated_at=datetime('now','localtime')",
        (dept_id, month, float(amount), created_by))
    conn.commit()


def get_budget(conn, dept_id: str, month: str) -> float | None:
    row = conn.execute("SELECT amount FROM budgets WHERE dept_id=? AND month=?",
                       (dept_id, month)).fetchone()
    return float(row[0]) if row else None


def used(conn, dept_id: str, month: str) -> float:
    if dept_id == ALL_DEPT:
        cond, args = "(dept_id = '*' OR dept_id IS NULL)", []
    else:
        cond, args = "dept_id = ?", [dept_id]
    row = conn.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM tickets"
        f" WHERE {cond} AND substr(occurred_at,1,7)=? AND status IN"
        f" ('APPROVED','PAID','AUTO_APPROVED','SUBMITTED','ESCALATED')",
        [*args, month]).fetchone()
    return float(row[0] or 0)


def check(conn, dept_id: str | None, month: str, extra: float = 0.0,
          settings: Mapping | None = None) -> dict:
    dept = dept_id or ALL_DEPT
    budget = get_budget(conn, dept, month)
    out = {"dept": dept, "month": month, "budget": budget, "used": None,
           "remaining": None, "ok": True, "over": 0.0, "checked": budget is not None,
           "block": False}
    if budget is None:
        return out
    u = used(conn, dept, month)
    rem = budget - u
    out.update(used=u, remaining=rem)
    if extra > rem:
        out["ok"] = False
        out["over"] = round(extra - rem, 2)
        out["block"] = bool((settings or {}).get("budgets", {}).get("block_over", False))
    return out


def overview(conn, month: str) -> list[dict]:
    rows = conn.execute("SELECT dept_id, amount FROM budgets WHERE month=?"
                        " ORDER BY dept_id", (month,)).fetchall()
    out = []
    for dept, amount in rows:
        u = used(conn, dept, month)
        out.append({"dept": dept, "month": month, "budget": float(amount),
                    "used": u, "remaining": float(amount) - u})
    return out


def to_table(rows: Sequence[Mapping], month: str) -> str:
    if not rows:
        return f"（{month} 未设置任何预算。设置：`预算 部门ID {month} 金额`，全司用 * ）"
    lines = [f"预算概览 {month}:"]
    for r in rows:
        pct = (r["used"] / r["budget"] * 100) if r["budget"] else 0
        mark = " ⚠超支" if r["remaining"] < 0 else (" ⚠接近上限" if pct > 80 else "")
        lines.append(f"  {r['dept']:<6} 预算 {r['budget']:.0f} | 已用 {r['used']:.0f} | "
                     f"剩余 {r['remaining']:.0f} ({pct:.0f}%){mark}")
    return "\n".join(lines)


def parse_budget_text(text: str) -> dict | None:
    """`预算 D1 2026-09 50000` 设值；`预算 D1 2026-09` 查询；`预算` 总览。"""
    import re
    parts = text.split()
    if not parts or parts[0] != "预算":
        return None
    m = re.match(r"^预算\s+(\S+)\s+(\d{4}-\d{2})(?:\s+(\d+(?:\.\d+)?))?$", text)
    if m:
        dept, month = m.group(1), m.group(2)
        if m.group(3):
            return {"action": "set", "dept": dept, "month": month,
                    "amount": float(m.group(3))}
        return {"action": "query", "dept": dept, "month": month}
    return {"action": "overview", "month": None}
