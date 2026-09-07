"""批量异常检测（PRD §6.4）：与单笔审查互补，周期性统计扫描。

A1 费用趋势：全司/部门/类型/员工四层月度序列，同比/环比 + 3 个月滚动均值
   ±2σ 双判据，超阈值 → WARN，超严重阈值或连续 2 期触发 → ALERT；
   附贡献度下钻（环比增量主因）与单据引用。
A2 单价偏离：同 SKU+供应商 90 天滚动均价，偏离 ±warn_pct 双向检测
   （高于均价=溢价嫌疑，低于均价=质量/假票嫌疑），采购场景就绪。
方法边界：基线不足 min_baseline_months 不启用；统计异常≠违规，只标注+给证据。

CLI: python3 -m boss_secretary.anomaly data/secretary.db [--config config/anomaly.yaml]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from boss_secretary.core import router as R

WATCH, WARN, ALERT = "WATCH", "WARN", "ALERT"

TYPE_TREND = "费用趋势"
TYPE_PRICE = "单价偏离"

COUNTED_STATUSES = {R.APPROVED, R.AUTO_APPROVED, R.SUBMITTED, R.ESCALATED}

DEFAULT_CFG = {
    "a1": {"enabled": True, "min_baseline_months": 3, "baseline_months": 3,
           "warn_pct": 50.0, "alert_pct": 100.0,
           "levels": ["全司", "部门", "类型", "员工"]},
    "a2": {"enabled": True, "lookback_days": 90, "min_baseline_n": 1,
           "warn_pct": 20.0, "alert_pct": 50.0},
}


@dataclass(frozen=True)
class AnomalyEvent:
    type: str
    level: str
    subject: str
    period: str
    baseline: float
    observed: float
    deviation_pct: float
    severity: str
    evidence: str
    ticket_refs: tuple[str, ...] = ()


def load_config(path: str | Path | None = None) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULT_CFG.items()}
    if path and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for k, v in data.items():
            if k in cfg and isinstance(v, dict):
                cfg[k].update(v)
    return cfg


def month_of(t: Mapping) -> str | None:
    for k in ("occurred_at", "created_at"):
        v = str(t.get(k) or "")
        if len(v) >= 7 and v[4] == "-":
            return v[:7]
    return None


def prev_month(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def last_completed_period(today: dt.date) -> str:
    first = today.replace(day=1)
    return (first - dt.timedelta(days=1)).strftime("%Y-%m")


def monthly_sums(tickets: Sequence[Mapping], key_fn: Callable[[Mapping], str | None],
                 statuses: set[str] = COUNTED_STATUSES) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for t in tickets:
        if t.get("status") not in statuses:
            continue
        month = month_of(t)
        if not month:
            continue
        subject = key_fn(t)
        if subject is None:
            continue
        out.setdefault(subject, {})[month] = \
            out.setdefault(subject, {}).get(month, 0) + float(t.get("amount") or 0)
    return out


def _severity(dev_pct: float, params: dict, consecutive: bool) -> str | None:
    if dev_pct > float(params.get("alert_pct", 100)) or consecutive:
        return ALERT
    if dev_pct > float(params.get("warn_pct", 50)):
        return WARN
    return None


def _sigma_hit(base_vals: Sequence[float], cur: float, mean: float) -> bool:
    if len(base_vals) < 2:
        return False
    sigma = statistics.pstdev(base_vals)
    if sigma <= 0:
        return False
    return cur > mean + 2 * sigma


def _ticket_refs(tickets: Sequence[Mapping], match: Callable[[Mapping], bool],
                 period: str, top: int = 5) -> tuple[str, ...]:
    refs = [(float(t.get("amount") or 0), t.get("ticket_id", "?")) for t in tickets
            if match(t) and month_of(t) == period]
    return tuple(tid for _, tid in sorted(refs, reverse=True)[:top])


def _contribution(valid: Sequence[Mapping], level: str, subject: str,
                  period: str) -> str:
    sub_fn = {"全司": lambda t: f"部门:{t.get('dept_id')}",
              "部门": lambda t: f"员工:{t.get('employee_id')}",
              "类型": lambda t: f"部门:{t.get('dept_id')}"}.get(level)
    if sub_fn is None:
        return ""
    filt = {"全司": lambda t: True,
            "部门": lambda t: f"部门:{t.get('dept_id')}" == subject,
            "类型": lambda t: f"类型:{t.get('expense_type')}" == subject}[level]
    deltas: dict[str, float] = {}
    pm = prev_month(period)
    for t in valid:
        if not filt(t):
            continue
        m = month_of(t)
        if m not in (period, pm):
            continue
        key = sub_fn(t)
        d = float(t.get("amount") or 0)
        if m == period:
            deltas[key] = deltas.get(key, 0) + d
        else:
            deltas[key] = deltas.get(key, 0) - d
    top = sorted(deltas.items(), key=lambda x: -x[1])[:3]
    top = [(k, v) for k, v in top if v > 0]
    if not top:
        return ""
    total = sum(v for _, v in top)
    parts = [f"{k} +{v:.0f}元(占增量{v / total * 100:.0f}%)" for k, v in top]
    return "环比增量主因: " + "; ".join(parts)


def detect_a1(tickets: Sequence[Mapping], cfg: dict | None = None,
              period: str | None = None,
              prev_events: Sequence[Mapping] = ()) -> list[AnomalyEvent]:
    cfg = cfg or load_config()
    a1 = cfg["a1"]
    if not a1.get("enabled", True):
        return []
    period = period or last_completed_period(dt.date.today())
    valid = [t for t in tickets if t.get("status") in COUNTED_STATUSES and month_of(t)]
    if not valid:
        return []
    level_fns: dict[str, Callable[[Mapping], str | None]] = {
        "全司": lambda t: "全司",
        "部门": lambda t: f"部门:{t.get('dept_id')}",
        "类型": lambda t: f"类型:{t.get('expense_type')}",
        "员工": lambda t: f"员工:{t.get('employee_id')}",
    }
    events: list[AnomalyEvent] = []
    for level in a1.get("levels") or list(level_fns):
        fn = level_fns[level]
        grouped = monthly_sums(valid, fn)
        for subject, series in sorted(grouped.items()):
            months_before = sorted(m for m in series if m < period)
            need = int(a1.get("min_baseline_months", 3))
            if len(months_before) < need:
                continue
            n_base = int(a1.get("baseline_months", 3))
            base_months = months_before[-n_base:]
            base_vals = [series[m] for m in base_months]
            mean = sum(base_vals) / len(base_vals)
            cur = series.get(period, 0.0)
            if mean <= 0 or cur <= 0:
                continue
            dev = (cur - mean) / mean * 100
            consecutive = any(
                str(e.get("type")) == TYPE_TREND and str(e.get("subject")) == subject
                and str(e.get("period")) == prev_month(period)
                and str(e.get("severity")) in (WARN, ALERT)
                for e in prev_events)
            sev = _severity(dev, a1, consecutive)
            if sev is None and _sigma_hit(base_vals, cur, mean):
                sev = WARN
            if sev is None:
                continue
            ev_text = (f"基线({','.join(base_months)})均值 {mean:.0f} 元, "
                       f"本期 {cur:.0f} 元, 偏离 +{dev:.0f}%"
                       + (", 连续2期触发" if consecutive else ""))
            contrib = _contribution(valid, level, subject, period)
            if contrib:
                ev_text += " | " + contrib
            refs = _ticket_refs(valid, lambda t: fn(t) == subject, period)
            events.append(AnomalyEvent(
                type=TYPE_TREND, level=level, subject=subject, period=period,
                baseline=round(mean, 2), observed=round(cur, 2),
                deviation_pct=round(dev, 1), severity=sev, evidence=ev_text,
                ticket_refs=refs))
    return events


def check_price(records: Sequence[Mapping], cfg: dict | None = None) -> list[AnomalyEvent]:
    cfg = cfg or load_config()
    a2 = cfg["a2"]
    if not a2.get("enabled", True):
        return []
    lookback = int(a2.get("lookback_days", 90))
    min_n = int(a2.get("min_baseline_n", 1))
    groups: dict[tuple[str, str], list[Mapping]] = {}
    for rec in records:
        if rec.get("sku") is None or rec.get("unit_price") is None:
            continue
        groups.setdefault((str(rec["sku"]), str(rec.get("supplier", "-"))), []).append(rec)
    events: list[AnomalyEvent] = []
    for (sku, sup), items in sorted(groups.items()):
        items = sorted(items, key=lambda x: str(x.get("date") or ""))

        def d_of(x: Mapping) -> dt.date | None:
            return C_date(x.get("date"))
        for i, rec in enumerate(items):
            rd = d_of(rec)
            if rd is None:
                continue
            base = [float(x["unit_price"]) for x in items[:i]
                    if d_of(x) is not None
                    and 0 <= (rd - d_of(x)).days <= lookback]
            if len(base) < min_n:
                continue
            mean = sum(base) / len(base)
            if mean <= 0:
                continue
            price = float(rec["unit_price"])
            dev = (price - mean) / mean * 100
            ad = abs(dev)
            if ad > float(a2.get("alert_pct", 50)):
                sev = ALERT
            elif ad > float(a2.get("warn_pct", 20)):
                sev = WARN
            else:
                continue
            direction = "高于" if dev > 0 else "低于"
            events.append(AnomalyEvent(
                type=TYPE_PRICE, level="SKU", subject=f"SKU:{sku}@{sup}",
                period=str(rec.get("date") or ""), baseline=round(mean, 2),
                observed=round(price, 2), deviation_pct=round(dev, 1), severity=sev,
                evidence=(f"近{lookback}天{len(base)}笔均价 {mean:.2f}, 本次 {price:.2f}"
                          f", {direction}均价 {ad:.0f}%"),
                ticket_refs=(str(rec.get("ticket_id") or "?"),)))
    return events


def C_date(v: Any) -> dt.date | None:
    if v in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def sample_purchases(records: Sequence[Mapping], standard_skus: Sequence[str],
                     top_pct: float = 0.2, random_pct: float = 0.1,
                     seed: int = 0) -> list[Mapping]:
    std = {str(s) for s in standard_skus}
    standard = [r for r in records if str(r.get("sku")) in std]
    nonstandard = [r for r in records if str(r.get("sku")) not in std]
    ranked = sorted(nonstandard, key=lambda r: -(float(r.get("unit_price") or 0)
                                                 * float(r.get("qty") or 1)))
    n_top = int(len(ranked) * top_pct)
    rest = ranked[n_top:]
    rng = random.Random(seed)
    n_rand = int(len(rest) * random_pct)
    picked = standard + ranked[:n_top] + rng.sample(rest, min(n_rand, len(rest)))
    return picked


def save_events(conn, events: Sequence[AnomalyEvent]) -> int:
    for e in events:
        conn.execute(
            "INSERT INTO anomalies(type, subject, period, baseline, observed,"
            " deviation_pct, severity, evidence, ticket_refs)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (e.type, e.subject, e.period, e.baseline, e.observed, e.deviation_pct,
             e.severity, e.evidence, json.dumps(list(e.ticket_refs), ensure_ascii=False)))
    conn.commit()
    return len(events)


def load_events(conn, type: str | None = None, period: str | None = None,
                severity: Sequence[str] = ()) -> list[dict]:
    sql = "SELECT type, subject, period, baseline, observed, deviation_pct," \
          " severity, evidence, ticket_refs FROM anomalies WHERE 1=1"
    args: list[Any] = []
    if type:
        sql += " AND type=?"
        args.append(type)
    if period:
        sql += " AND period=?"
        args.append(period)
    if severity:
        sql += f" AND severity IN ({','.join('?' * len(severity))})"
        args.extend(severity)
    out = []
    for row in conn.execute(sql, args).fetchall():
        rec = dict(zip(("type", "subject", "period", "baseline", "observed",
                        "deviation_pct", "severity", "evidence", "ticket_refs"), row))
        rec["ticket_refs"] = json.loads(rec["ticket_refs"] or "[]")
        out.append(rec)
    return out


def sweep(store, cfg_path: str | None = "config/anomaly.yaml",
          purchases: Sequence[Mapping] = (), today: dt.date | None = None,
          save: bool = True) -> list[AnomalyEvent]:
    cfg = load_config(cfg_path)
    today = today or dt.date.today()
    period = last_completed_period(today)
    conn = store.conn if hasattr(store, "conn") else store
    tickets = []
    for row in conn.execute("SELECT ticket_id, employee_id, dept_id, status,"
                            " amount, expense_type, occurred_at, created_at,"
                            " sensitivity FROM tickets").fetchall():
        tickets.append(dict(zip(("ticket_id", "employee_id", "dept_id", "status",
                                 "amount", "expense_type", "occurred_at",
                                 "created_at", "sensitivity"), row)))
    prev = load_events(conn, type=TYPE_TREND, period=prev_month(period),
                       severity=(WARN, ALERT))
    events = detect_a1(tickets, cfg, period=period, prev_events=prev)
    if purchases:
        events += check_price(purchases, cfg)
    if save and events:
        save_events(conn, events)
    return events


def to_table(events: Sequence[AnomalyEvent]) -> str:
    if not events:
        return "（无异常事件）"
    lines = []
    for e in events:
        lines.append(f"[{e.severity:<5}] {e.type} {e.subject} @{e.period} "
                     f"基线{e.baseline}→{e.observed} (+{e.deviation_pct}%)")
        lines.append(f"        {e.evidence}")
        if e.ticket_refs:
            lines.append(f"        单据: {','.join(e.ticket_refs)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="boss_secretary.anomaly")
    p.add_argument("db_path")
    p.add_argument("--config", default="config/anomaly.yaml")
    p.add_argument("--today", default=None, help="ISO 日期（默认今天）")
    args = p.parse_args(argv)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    class _S:
        conn = None

    s = _S()
    import sqlite3
    s.conn = sqlite3.connect(args.db_path)
    events = sweep(s, cfg_path=args.config, today=today, save=True)
    print(f"周期: {last_completed_period(today)}")
    print(to_table(events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
