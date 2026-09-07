"""日报渲染（PRD §2.1 分版 / F10）：公司版→老板、部门版→经理、财务版→财务。

同一数据源三种权限裁剪；机密单在部门版中只计数不展示明细。
口径（MVP 截面）：今日新增=created_at 当日；通过/驳回=累计状态计数；
待办=SUBMITTED+ESCALATED 按等待时长降序；趋势=近 7 日每日新增。

CLI: python3 -m boss_secretary.report.daily data/secretary.db
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from boss_secretary.core import perm as P
from boss_secretary.core import router as R


def _fmt_amount(v: Any) -> str:
    if v is None:
        return "-"
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:.2f}"


def _waiting_hours(t: Mapping, now: dt.datetime) -> float:
    s = t.get("submitted_at")
    if not s:
        return 0.0
    try:
        return max(0.0, (now - dt.datetime.fromisoformat(s)).total_seconds() / 3600)
    except ValueError:
        return 0.0


def aggregate(tickets: Sequence[Mapping], day: dt.date,
              now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now()
    day_s = day.isoformat()
    totals: dict[str, int] = {}
    for t in tickets:
        totals[t.get("status")] = totals.get(t.get("status"), 0) + 1
    pending = []
    for t in tickets:
        if t.get("status") in (R.SUBMITTED, R.ESCALATED):
            pending.append({**t, "waiting_hours": round(_waiting_hours(t, now), 1)})
    pending.sort(key=lambda x: -x["waiting_hours"])
    trend = []
    for i in range(6, -1, -1):
        d = (day - dt.timedelta(days=i)).isoformat()
        trend.append((d, sum(1 for t in tickets
                             if (t.get("created_at") or "")[:10] == d)))
    return {
        "day": day_s, "now": now.isoformat(timespec="minutes"), "tickets": list(tickets),
        "created_today": sum(1 for t in tickets
                             if (t.get("created_at") or "")[:10] == day_s),
        "totals": totals,
        "approved": totals.get(R.APPROVED, 0) + totals.get(R.AUTO_APPROVED, 0),
        "rejected": totals.get(R.REJECTED, 0),
        "pending": pending,
        "escalated_n": totals.get(R.ESCALATED, 0),
        "confidential_n": sum(1 for t in tickets
                              if t.get("sensitivity") == P.CONFIDENTIAL),
        "trend": trend,
    }


def _pending_lines(pending: Sequence[Mapping], limit: int = 10) -> list[str]:
    lines = []
    for i, t in enumerate(pending[:limit], 1):
        mark = " ⚠超时" if t.get("status") == R.ESCALATED else ""
        lines.append(f"  {i}. {t['ticket_id']} {t.get('expense_type', '-')} "
                     f"{_fmt_amount(t.get('amount'))}元 等待{t['waiting_hours']}h{mark}")
    if not lines:
        lines.append("  （无待办）")
    return lines


def render_company(data: Mapping) -> str:
    lines = [
        f"═══ 秘书日报 · 公司版 · {data['day']} ═══",
        f"今日新增 {data['created_today']} | 累计通过 {data['approved']} | "
        f"累计驳回 {data['rejected']} | 待办 {len(data['pending'])}"
        f"（超时 {data['escalated_n']}）",
    ]
    if data["confidential_n"]:
        lines.append(f"⚠ 机密单 {data['confidential_n']} 笔（明细仅审批链可见）")
    lines.append("待办（按等待时长）:")
    lines += _pending_lines(data["pending"])
    trend_s = " ".join(f"{d[5:]}:{n}" for d, n in data["trend"])
    lines.append(f"近7日新增: {trend_s}")
    lines.append("明细见飞书审批中心；回放包可由审计导出")
    return "\n".join(lines)


def render_department(data: Mapping, dept_id: str) -> str:
    own = [t for t in data["tickets"] if t.get("dept_id") == dept_id]
    sub = aggregate(own, dt.date.fromisoformat(data["day"]),
                    now=dt.datetime.fromisoformat(data["now"]))
    conf_pending = [t for t in sub["pending"]
                    if t.get("sensitivity") == P.CONFIDENTIAL]
    sub["pending"] = [t for t in sub["pending"]
                      if t.get("sensitivity") != P.CONFIDENTIAL]
    conf_all = [t for t in sub["tickets"] if t.get("sensitivity") == P.CONFIDENTIAL]
    total_pending = len(sub["pending"]) + len(conf_pending)
    lines = [
        f"═══ 秘书日报 · 部门版[{dept_id}] · {data['day']} ═══",
        f"今日新增 {sub['created_today']} | 累计通过 {sub['approved']} | "
        f"累计驳回 {sub['rejected']} | 待办 {total_pending}"
        + (f"（含机密 {len(conf_pending)} 笔，明细略）" if conf_pending else ""),
    ]
    if conf_all:
        lines.append(f"⚠ 部门内机密单 {len(conf_all)} 笔（明细仅审批链可见）")
    lines.append("待办（按等待时长）:")
    lines += _pending_lines(sub["pending"])
    return "\n".join(lines)


def render_finance(data: Mapping) -> str:
    approved = sorted([t for t in data["tickets"] if t.get("status") == R.APPROVED],
                      key=lambda t: -(t.get("amount") or 0))
    total = sum(t.get("amount") or 0 for t in approved)
    lines = [f"═══ 财务简报 · 待付清单 · {data['day']} ═══",
             f"共 {len(approved)} 笔，合计 {_fmt_amount(total)} 元（金额降序）"]
    for t in approved:
        lines.append(f"  {t['ticket_id']} {t.get('employee_id')} "
                     f"{t.get('expense_type', '-')} {_fmt_amount(t.get('amount'))}元 "
                     f"「{t.get('reason', '')}」")
    if not approved:
        lines.append("  （今日无待付）")
    return "\n".join(lines)


def render_for(actor: P.Actor, tickets: Sequence[Mapping], day: dt.date,
               now: dt.datetime | None = None,
               company_enabled: bool = False) -> str | None:
    version = P.daily_report_version(actor, company_report_enabled=company_enabled)
    if version is None:
        return None
    data = aggregate(tickets, day, now=now)
    if version == "company":
        return render_company(data)
    if version == "department":
        return render_department(data, actor.dept_id)
    if version == "finance":
        return render_finance(data)
    if version == "audit":
        return render_company(data) + "\n（审计版：全量留痕可导出）"
    return None


def _company_report_enabled(store, user_id: str) -> bool:
    row = store.conn.execute(
        "SELECT enabled FROM daily_report_access WHERE user_id=? AND report_type='company'",
        (user_id,)).fetchone()
    return bool(row and row[0])


def send_all(store, notifier, now: dt.datetime | None = None,
             boss_user_id: str | None = None,
             company_enabled_managers: frozenset[str] = frozenset(),
             finance_user_ids: Sequence[str] = ()) -> list[tuple[str, str]]:
    now = now or dt.datetime.now()
    day = now.date()
    tickets = store.list_all()
    sent: list[tuple[str, str]] = []
    if boss_user_id:
        actor = P.Actor(user_id=boss_user_id, role=P.BOSS)
        body = render_for(actor, tickets, day, now=now)
        notifier.send("daily.report.company", {"ticket_id": "-", "status": "company",
                                               "report_date": day.isoformat(),
                                               "body": body}, [boss_user_id])
        sent.append(("company", boss_user_id))
    for uid, dept in store.conn.execute(
            "SELECT feishu_user_id, dept_id FROM employees WHERE role='MANAGER'"):
        actor = P.Actor(user_id=uid, role=P.MANAGER, dept_id=dept)
        enabled = uid in company_enabled_managers or \
            _company_report_enabled(store, uid)
        body = render_for(actor, tickets, day, now=now, company_enabled=enabled)
        version = "company" if enabled else "department"
        notifier.send(f"daily.report.{version}",
                      {"ticket_id": "-", "status": version,
                       "report_date": day.isoformat(), "body": body}, [uid])
        sent.append((version, uid))
    for uid in finance_user_ids:
        actor = P.Actor(user_id=uid, role=P.FINANCE)
        body = render_for(actor, tickets, day, now=now)
        notifier.send("daily.report.finance", {"ticket_id": "-", "status": "finance",
                                               "report_date": day.isoformat(),
                                               "body": body}, [uid])
        sent.append(("finance", uid))
    return sent


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    from boss_secretary.core.router import SQLiteTicketStore
    store = SQLiteTicketStore(argv[0])
    print(render_company(aggregate(store.list_all(), dt.date.today())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
