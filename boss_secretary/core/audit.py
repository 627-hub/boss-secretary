"""内部审计（与异常检测合并，PRD 审计角色 + P2 抽检队列）。

工作台: 本月概览（单据/金额/异常/待抽检）
风险评分: 多因子加权（金额倍数/人工复核单/额度核销/重复商户/主体异常/周末发生）
         → 抽检推荐队列（TOP N）
回放包: 单据全生命周期证据（audit_log + ctx 快照 + 规则/LLM 痕迹）→ replay.md
闭环: 异常 误报(false_positive)/属实(confirmed) 反馈；confirmed≥2 的主体进风险名单
      （risk_flag 前置，PRD §7.3 预留位）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

COUNTED = ("APPROVED", "PAID", "AUTO_APPROVED", "SUBMITTED", "ESCALATED")

DEFAULT_WEIGHTS = {
    "amount_x2": 15, "amount_x5": 30, "manual_review": 20,
    "allowance": 12, "repeat_merchant": 25, "subject_anomaly": 30,
    "weekend": 8, "suggest_threshold": 40,
}


def load_weights(settings: Mapping | None = None) -> dict:
    w = dict(DEFAULT_WEIGHTS)
    w.update((settings or {}).get("audit", {}).get("weights") or {})
    return w


def _month_tickets(conn, month: str) -> list[dict]:
    cols = ("ticket_id", "employee_id", "dept_id", "status", "amount", "type",
            "expense_type", "reason", "occurred_at", "ai_verdict", "allowance_id",
            "invoice_seller")
    rows = conn.execute(f"SELECT {','.join(cols)} FROM tickets"
                        " WHERE substr(occurred_at,1,7)=? AND status IN"
                        " ('APPROVED','PAID','AUTO_APPROVED')", (month,)).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def _confirmed_subjects(conn) -> set[str]:
    out = set()
    for subject, in conn.execute(
            "SELECT subject FROM anomalies WHERE status='confirmed'"):
        out.add(str(subject))
    return out


def _merchant_counts(tickets: Sequence[Mapping]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for t in tickets:
        seller = str(t.get("invoice_seller") or "")
        if seller:
            key = (str(t.get("employee_id")), seller)
            counts[key] = counts.get(key, 0) + 1
    return counts


def risk_scores(conn, month: str, settings: Mapping | None = None,
                now: dt.date | None = None) -> list[dict]:
    w = load_weights(settings)
    now = now or dt.date.today()
    tickets = _month_tickets(conn, month)
    amounts = [float(t.get("amount") or 0) for t in tickets]
    mean_amt = (sum(amounts) / len(amounts)) if amounts else 0
    merch = _merchant_counts(tickets)
    confirmed = _confirmed_subjects(conn)
    out = []
    for t in tickets:
        score, reasons = 0, []
        amt = float(t.get("amount") or 0)
        if mean_amt > 0:
            if amt >= mean_amt * 5:
                score += w["amount_x5"]
                reasons.append(f"金额{amt:.0f}为均值{mean_amt:.0f}的5倍+")
            elif amt >= mean_amt * 2:
                score += w["amount_x2"]
                reasons.append(f"金额{amt:.0f}为均值{mean_amt:.0f}的2倍+")
        if (t.get("ai_verdict") or "") in ("MANUAL_REVIEW", "ALLOWANCE") \
                and t.get("ai_verdict") == "MANUAL_REVIEW":
            score += w["manual_review"]
            reasons.append("曾转人工复核")
        if t.get("allowance_id"):
            score += w["allowance"]
            reasons.append("额度核销单")
        key = (str(t.get("employee_id")), str(t.get("invoice_seller") or ""))
        if key[1] and merch.get(key, 0) >= 3:
            score += w["repeat_merchant"]
            reasons.append(f"同商户本月{merch[key]}笔")
        for subj in confirmed:
            if f"员工:{t.get('employee_id')}" == subj or \
                    (t.get("dept_id") and f"部门:{t['dept_id']}" == subj):
                score += w["subject_anomaly"]
                reasons.append(f"主体在异常属实名单({subj})")
                break
        occ = str(t.get("occurred_at") or "")
        try:
            if dt.date.fromisoformat(occ[:10]).weekday() >= 5:
                score += w["weekend"]
                reasons.append("周末发生")
        except ValueError:
            pass
        if score >= w["suggest_threshold"]:
            out.append({"ticket_id": t["ticket_id"], "employee_id": t["employee_id"],
                        "amount": amt, "score": score, "reasons": reasons,
                        "type": t.get("type")})
    out.sort(key=lambda x: -x["score"])
    return out


def sampling_queue(conn, month: str, top_n: int = 10,
                   settings: Mapping | None = None) -> list[dict]:
    return risk_scores(conn, month, settings)[:top_n]


def queue_table(queue: Sequence[Mapping]) -> str:
    if not queue:
        return "（本月无达抽检阈值单据）"
    lines = ["抽检推荐（按风险分）:"]
    for i, q in enumerate(queue, 1):
        lines.append(f"  {i}. {q['ticket_id']} {q.get('type')} {q['employee_id']} "
                     f"{q['amount']:.0f}元 风险分{q['score']} | "
                     f"{'; '.join(q['reasons'][:3])}")
    return "\n".join(lines)


def replay_package(conn, ticket_id: str, audit_dir: str | Path = "data/audit"
                   ) -> Path | None:
    row = conn.execute("SELECT ticket_id, employee_id, dept_id, type, status,"
                       " amount, expense_type, reason, occurred_at, ai_verdict,"
                       " ai_evidence, matrix_version, allowance_id"
                       " FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if row is None:
        return None
    t = dict(zip(("ticket_id", "employee_id", "dept_id", "type", "status", "amount",
                  "expense_type", "reason", "occurred_at", "ai_verdict",
                  "ai_evidence", "matrix_version", "allowance_id"), row))
    lines = [f"# 回放包 {ticket_id}", "",
             f"生成: {dt.datetime.now():%Y-%m-%d %H:%M} | "
             f"状态: {t['status']} | 金额: {t['amount']} | "
             f"矩阵版本: v{t['matrix_version']} | AI 判定: {t['ai_verdict']}", "",
             "## 单据要素", ""]
    for k in ("employee_id", "dept_id", "type", "expense_type", "reason",
              "occurred_at", "allowance_id"):
        if t.get(k):
            lines.append(f"- {k}: {t[k]}")
    if t.get("ai_evidence"):
        ctx_file = str(t["ai_evidence"]).split("|")[0]
        lines += ["", "## 抽取上下文快照", "", "```json"]
        try:
            lines.append(Path(ctx_file).read_text(encoding="utf-8"))
        except OSError:
            lines.append(f"(快照文件缺失: {ctx_file})")
        lines.append("```")
        lines.append(f"- 快照哈希: {str(t['ai_evidence']).split('|')[-1]}")
    logs = conn.execute("SELECT ts, actor, action, payload_hash, payload_file"
                        " FROM audit_log WHERE ticket_id=? ORDER BY seq",
                        (ticket_id,)).fetchall()
    lines += ["", "## 审计轨迹（append-only）", "",
              "| 时间 | 操作者 | 动作 | 载荷哈希 |", "|---|---|---|---|"]
    for ts, actor, action, ph, pf in logs:
        lines.append(f"| {ts} | {actor} | {action} | {(ph or '')[:12]} |")
    lines += ["", "> 本回放包由 audit_log（append-only）生成，"
              "哈希链可用于完整性核验；不构成审计意见。"]
    out = Path(audit_dir) / "replays"
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"replay_{ticket_id}.md"
    fp.write_text("\n".join(lines), encoding="utf-8")
    return fp


def set_anomaly_status(conn, anomaly_id: int, status: str) -> dict | None:
    if status not in ("confirmed", "false_positive", "open"):
        raise ValueError(f"非法状态: {status}")
    row = conn.execute("SELECT subject FROM anomalies WHERE anomaly_id=?",
                       (anomaly_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE anomalies SET status=? WHERE anomaly_id=?",
                 (status, anomaly_id))
    conn.commit()
    return {"anomaly_id": anomaly_id, "subject": row[0], "status": status}


def risk_list(conn, min_confirmed: int = 2) -> list[dict]:
    rows = conn.execute(
        "SELECT subject, count(*) n FROM anomalies WHERE status='confirmed'"
        " GROUP BY subject HAVING n>=? ORDER BY n DESC", (min_confirmed,)).fetchall()
    return [{"subject": s, "confirmed": n} for s, n in rows]


def workbench(conn, month: str) -> str:
    n_tickets = conn.execute(
        "SELECT count(*), COALESCE(SUM(amount),0) FROM tickets"
        " WHERE substr(occurred_at,1,7)=? AND status IN"
        " ('APPROVED','PAID','AUTO_APPROVED')", (month,)).fetchone()
    n_open = conn.execute("SELECT count(*) FROM anomalies WHERE status='open'"
                          ).fetchone()[0]
    n_conf = conn.execute("SELECT count(*) FROM anomalies WHERE status='confirmed'"
                          ).fetchone()[0]
    queue = sampling_queue(conn, month)
    lines = [f"═══ 审计工作台 · {month} ═══",
             f"已核销单据 {n_tickets[0]} 张 / {n_tickets[1]:.0f} 元 | "
             f"异常事件: open {n_open} / confirmed {n_conf} | "
             f"风险名单 {len(risk_list(conn))} 主体",
             queue_table(queue)]
    return "\n".join(lines)


def audit_appendix(conn, month: str) -> str:
    rows = conn.execute("SELECT severity, type, subject, evidence FROM anomalies"
                        " WHERE period=? AND severity IN ('WARN','ALERT')"
                        " ORDER BY anomaly_id DESC LIMIT 10",
                        (month,)).fetchall()
    if not rows:
        return ""
    lines = [f"### 审计附录 · 异常事件（{month}）", ""]
    for sev, typ, subj, ev in rows:
        lines.append(f"- [{sev}] {typ} {subj}：{str(ev)[:80]}")
    return "\n".join(lines)
