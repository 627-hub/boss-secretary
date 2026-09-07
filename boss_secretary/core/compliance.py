"""规则引擎（PRD §6.1 R1-R7）。纯函数：ctx/history/config 注入，无 IO。

R2/R3 需要的历史单据由调用方（router 查库后）注入，引擎不触库。
CLI:
  python3 -m boss_secretary.compliance eval \
    --ctx '{"amount":300,...}' [--history '[[...]]'] [--today 2026-09-06]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

RULE_NAMES = {
    "R1": "金额合规", "R2": "重复发票", "R3": "疑似重复报销",
    "R4": "费用类型上限", "R5": "日期合理性", "R6": "必填齐备", "R7": "发票抬头",
}

DEFAULT_PARAMS: dict[str, dict] = {
    "R1": {"max_amount": 100000},
    "R2": {"lookback_days": 90},
    "R3": {"window_days": 3},
    "R4": {"limits": {"交通": 200, "餐饮": 200, "住宿": 600, "办公": 1000},
           "per_person_types": ["餐饮"]},
    "R5": {"max_age_days": 90, "max_future_days": 0},
    "R6": {"required": ["amount", "occurred_at", "reason", "expense_type", "invoice_no"]},
    "R7": {"company_keywords": []},
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d")


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    name: str
    verdict: str
    evidence: str


def default_config() -> dict[str, dict]:
    return {rid: {"name": name, "enabled": True, "params": dict(p)}
            for rid, name in RULE_NAMES.items()
            for p in [DEFAULT_PARAMS[rid]]}


def load_config(path: str | Path) -> dict[str, dict]:
    cfg = default_config()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for rid, item in (data.get("rules") or {}).items():
        if rid not in cfg:
            continue
        cfg[rid]["enabled"] = bool(item.get("enabled", True))
        merged = dict(cfg[rid]["params"])
        merged.update(item.get("params") or {})
        cfg[rid]["params"] = merged
        if item.get("name"):
            cfg[rid]["name"] = item["name"]
    return cfg


def _parse_date(v: Any) -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    for f in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _res(rid: str, verdict: str, evidence: str) -> RuleResult:
    return RuleResult(rid, RULE_NAMES[rid], verdict, evidence)


def r1_amount(ctx: Mapping[str, Any], params: dict) -> RuleResult:
    amt = ctx.get("amount")
    if amt is None:
        return _res("R1", SKIP, "金额缺失（R6 追问）")
    amt = float(amt)
    cap = float(params.get("max_amount", 100000))
    if amt <= 0:
        return _res("R1", FAIL, f"金额 {amt} 非正数")
    if amt > cap:
        return _res("R1", FAIL, f"金额 {amt} 超单笔上限 {cap}，需老板特批")
    return _res("R1", PASS, f"金额 {amt} ≤ 上限 {cap}")


def r2_duplicate_invoice(ctx: Mapping[str, Any], history: Sequence[Mapping],
                         params: dict, today: dt.date) -> RuleResult:
    inv = ctx.get("invoice_no")
    if not inv:
        return _res("R2", SKIP, "无发票号")
    lookback = int(params.get("lookback_days", 90))
    base = _parse_date(ctx.get("occurred_at")) or today
    hits = []
    for h in history:
        if str(h.get("invoice_no") or "") != str(inv):
            continue
        if ctx.get("ticket_id") and h.get("ticket_id") == ctx.get("ticket_id"):
            continue
        hd = _parse_date(h.get("occurred_at"))
        if hd is None or abs((base - hd).days) > lookback:
            continue
        hits.append(f"{h.get('ticket_id', '?')}@{hd}")
    if hits:
        return _res("R2", FAIL, f"发票号 {inv} 已存在: {', '.join(hits)}")
    return _res("R2", PASS, f"发票号 {inv} 近{lookback}天无重复")


def r3_duplicate_expense(ctx: Mapping[str, Any], history: Sequence[Mapping],
                         params: dict, today: dt.date) -> RuleResult:
    emp, amt, seller = ctx.get("employee_id"), ctx.get("amount"), ctx.get("invoice_seller")
    if emp is None or amt is None:
        return _res("R3", SKIP, "缺员工/金额")
    if not seller:
        return _res("R3", SKIP, "无开票方（无法比对商户）")
    win = int(params.get("window_days", 3))
    base = _parse_date(ctx.get("occurred_at")) or today
    hits = []
    for h in history:
        if ctx.get("ticket_id") and h.get("ticket_id") == ctx.get("ticket_id"):
            continue
        if h.get("employee_id") != emp or h.get("amount") is None:
            continue
        if float(h["amount"]) != float(amt):
            continue
        hs = h.get("invoice_seller")
        if not hs or str(hs) != str(seller):
            continue
        hd = _parse_date(h.get("occurred_at"))
        if hd is None or abs((base - hd).days) > win:
            continue
        hits.append(f"{h.get('ticket_id', '?')}@{hd}")
    if hits:
        return _res("R3", WARN,
                    f"同员工+同金额{amt}+同商户「{seller}」±{win}天内: {', '.join(hits)}")
    return _res("R3", PASS, "无相似单据")


def r4_type_limit(ctx: Mapping[str, Any], params: dict) -> RuleResult:
    t, amt = ctx.get("expense_type"), ctx.get("amount")
    if not t or amt is None:
        return _res("R4", SKIP, "缺费用类型/金额")
    limits = params.get("limits") or {}
    if t not in limits:
        return _res("R4", PASS, f"类型「{t}」无上限配置")
    limit = float(limits[t])
    eff = float(amt)
    note = ""
    if t in (params.get("per_person_types") or []) and ctx.get("headcount"):
        n = int(ctx["headcount"])
        if n > 0:
            eff = float(amt) / n
            note = f"（{amt}/{n}人）"
    if eff > limit:
        return _res("R4", WARN, f"{t} 折合 {eff:.1f} 元{note} 超上限 {limit} → 升级")
    return _res("R4", PASS, f"{t} 折合 {eff:.1f} 元{note} ≤ 上限 {limit}")


def r5_date_reasonable(ctx: Mapping[str, Any], params: dict, today: dt.date) -> RuleResult:
    d = _parse_date(ctx.get("occurred_at"))
    if d is None:
        v = ctx.get("occurred_at")
        if v in (None, ""):
            return _res("R5", SKIP, "日期缺失（R6 追问）")
        return _res("R5", WARN, f"日期「{v}」无法解析")
    max_age = int(params.get("max_age_days", 90))
    max_future = int(params.get("max_future_days", 0))
    if (d - today).days > max_future:
        return _res("R5", WARN, f"日期 {d} 在未来（今天 {today}）")
    if (today - d).days > max_age:
        return _res("R5", WARN, f"日期 {d} 距今 {(today - d).days} 天，超 {max_age} 天")
    return _res("R5", PASS, f"日期 {d} 合理（距今 {(today - d).days} 天）")


def r6_required(ctx: Mapping[str, Any], params: dict) -> RuleResult:
    required = params.get("required") or []
    missing = [f for f in required if ctx.get(f) in (None, "")]
    if missing:
        return _res("R6", WARN, "缺失: " + ", ".join(missing) + "（触发追问）")
    return _res("R6", PASS, f"{len(required)} 项必填齐备")


def r7_invoice_title(ctx: Mapping[str, Any], params: dict) -> RuleResult:
    seller = ctx.get("invoice_seller")
    if not seller:
        return _res("R7", SKIP, "无开票方信息")
    keywords = params.get("company_keywords") or []
    if not keywords:
        return _res("R7", SKIP, "未配置公司名称关键词")
    hit = [k for k in keywords if str(k) in str(seller)]
    if hit:
        return _res("R7", PASS, f"开票方「{seller}」命中关键词「{hit[0]}」")
    return _res("R7", WARN, f"开票方「{seller}」与公司关键词不符")


def run_rules(ctx: Mapping[str, Any],
              history: Sequence[Mapping] = (),
              config: dict[str, dict] | None = None,
              today: dt.date | None = None) -> list[RuleResult]:
    cfg = config or default_config()
    today = today or dt.date.today()
    out: list[RuleResult] = []
    for rid in ("R1", "R2", "R3", "R4", "R5", "R6", "R7"):
        item = cfg.get(rid) or {}
        if not item.get("enabled", True):
            out.append(_res(rid, SKIP, "已停用"))
            continue
        params = item.get("params") or {}
        if rid == "R1":
            out.append(r1_amount(ctx, params))
        elif rid == "R2":
            out.append(r2_duplicate_invoice(ctx, history, params, today))
        elif rid == "R3":
            out.append(r3_duplicate_expense(ctx, history, params, today))
        elif rid == "R4":
            out.append(r4_type_limit(ctx, params))
        elif rid == "R5":
            out.append(r5_date_reasonable(ctx, params, today))
        elif rid == "R6":
            out.append(r6_required(ctx, params))
        elif rid == "R7":
            out.append(r7_invoice_title(ctx, params))
    return out


def summarize(results: Sequence[RuleResult]) -> dict:
    verdicts = [r.verdict for r in results]
    overall = FAIL if FAIL in verdicts else WARN if WARN in verdicts else PASS
    return {"overall": overall,
            "fail": [r.rule_id for r in results if r.verdict == FAIL],
            "warn": [r.rule_id for r in results if r.verdict == WARN],
            "skip": [r.rule_id for r in results if r.verdict == SKIP]}


def to_table(results: Sequence[RuleResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"[{r.verdict:<4}] {r.rule_id} {r.name}: {r.evidence}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="boss_secretary.compliance")
    sub = p.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("eval")
    ev.add_argument("--ctx", required=True, help="单据上下文 JSON")
    ev.add_argument("--history", default="[]", help="历史单据 JSON 数组")
    ev.add_argument("--config", default="config/rules.yaml")
    ev.add_argument("--today", default=None, help="ISO 日期（默认今天，供回放）")
    args = p.parse_args(argv)
    ctx = json.loads(args.ctx)
    history = json.loads(args.history)
    cfg = load_config(args.config)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    results = run_rules(ctx, history, cfg, today)
    print(to_table(results))
    print(json.dumps(summarize(results), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
