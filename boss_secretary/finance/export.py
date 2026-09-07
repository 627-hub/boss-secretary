"""金蝶凭证导出（PRD F13）：已通过/已打款报销单 → 凭证引入 CSV。

会计口径（每单一张凭证）：
  计提: 借 费用科目(部门/员工核算项目)      贷 其他应付款-员工
  打款: 借 其他应付款-员工                  贷 银行存款            （PAID 才有）
→ PAID 单 4 行，APPROVED 未打款单 2 行，借贷必然平衡。

科目代码/部门映射请按贵司账套调整（settings.kingdee）：
  subject_map: 费用类型→科目代码；payable_code 其他应付款；bank_code 银行存款；
  dept_map: 部门ID→部门核算项目代码；encoding: gbk（金蝶 K3 Windows 导入）

CLI: boss-voucher export --db data/secretary.db --month 2026-09 --out vouchers.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULTS = {
    "subject_map": {"交通": "660106", "餐饮": "660107", "住宿": "660103",
                    "办公": "660101", "其他": "660199"},
    "payable_code": "2241",
    "bank_code": "1002",
    "dept_map": {},
    "encoding": "gbk",
    "voucher_prefix": "JD",
}

HEADERS = ["会计期间", "凭证日期", "凭证字", "凭证号", "摘要", "科目代码",
           "部门核算项目", "员工核算项目", "借方金额", "贷方金额", "制单人", "来源单据"]


def _cfg(settings: Mapping | None) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update((settings or {}).get("kingdee") or {})
    for k in ("subject_map", "dept_map"):
        cfg[k] = {**DEFAULTS[k], **(cfg.get(k) or {})}
    return cfg


def _dept_code(cfg: dict, dept_id: str | None) -> str:
    if not dept_id:
        return ""
    return str((cfg["dept_map"]).get(dept_id, dept_id))


def _subject_code(cfg: dict, expense_type: str | None) -> str:
    return str(cfg["subject_map"].get(expense_type or "其他",
                                      cfg["subject_map"]["其他"]))


def _period(date_s: str | None) -> str:
    return (date_s or dt.date.today().isoformat())[:7].replace("-", "")


def _vouchers_for(tickets: Sequence[Mapping], cfg: dict, operator: str,
                  prefix: str) -> list[tuple[str, list[list[str]]]]:
    """返回 [(凭证号, 行列表)]；每单一张凭证。"""
    out = []
    seq = 0
    for t in sorted(tickets, key=lambda x: x.get("ticket_id") or ""):
        amt = round(float(t.get("amount") or 0), 2)
        if amt <= 0:
            continue
        seq += 1
        vid = f"{prefix}{seq:04d}"
        date = (t.get("submitted_at") or t.get("created_at") or
                dt.date.today().isoformat())[:10]
        period = _period(t.get("occurred_at"))
        emp = t.get("employee_id") or "-"
        dept = _dept_code(cfg, t.get("dept_id"))
        summary = f"{emp} 报销-{(t.get('reason') or '')[:20]}"
        subject = _subject_code(cfg, t.get("expense_type"))
        payable = str(cfg["payable_code"])
        rows = [
            [period, date, "记", vid, summary, subject, dept, emp,
             f"{amt:.2f}", "0.00", operator, t.get("ticket_id", "")],
            [period, date, "记", vid, summary, payable, "", emp,
             "0.00", f"{amt:.2f}", operator, t.get("ticket_id", "")],
        ]
        if t.get("status") == "PAID":
            rows += [
                [period, date, "记", vid, summary + "-打款", payable, "", emp,
                 f"{amt:.2f}", "0.00", operator, t.get("ticket_id", "")],
                [period, date, "记", vid, summary + "-打款", str(cfg["bank_code"]),
                 "", "", "0.00", f"{amt:.2f}", operator, t.get("ticket_id", "")],
            ]
        out.append((vid, rows))
    return out


def export_csv(conn, out_path: str | Path, month: str | None = None,
               status: Sequence[str] = ("APPROVED", "PAID", "AUTO_APPROVED"),
               settings: Mapping | None = None, operator: str = "finance",
               tickets: Sequence[Mapping] | None = None) -> dict:
    cfg = _cfg(settings)
    conn = conn.conn if hasattr(conn, "conn") else conn
    if tickets is None:
        cond = "status IN (%s)" % ",".join("?" * len(status))
        args = list(status)
        if month:
            cond += " AND substr(occurred_at,1,7)=?"
            args.append(month)
        rows = conn.execute(
            f"SELECT ticket_id, employee_id, dept_id, status, amount, expense_type,"
            f" reason, occurred_at, submitted_at, created_at FROM tickets"
            f" WHERE {cond}", args).fetchall()
        cols = ("ticket_id", "employee_id", "dept_id", "status", "amount",
                "expense_type", "reason", "occurred_at", "submitted_at", "created_at")
        tickets = [dict(zip(cols, r)) for r in rows]
    vouchers = _vouchers_for(tickets, cfg, operator,
                             cfg["voucher_prefix"] + (month or dt.date.today().strftime("%Y%m")))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    n_rows = 0
    with open(out, "w", newline="", encoding=cfg["encoding"]) as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        for _, rows in vouchers:
            w.writerows(rows)
            n_rows += len(rows)
    return {"file": str(out), "vouchers": len(vouchers), "rows": n_rows,
            "encoding": cfg["encoding"],
            "tickets": [v for v, _ in [(v, r) for v, r in vouchers]]}


def summary_text(result: Mapping) -> str:
    return (f"凭证导出完成：{result['vouchers']} 张凭证 / {result['rows']} 行，"
            f"文件 {result['file']}（{result['encoding']} 编码，可直接金蝶引入）")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="boss_secretary.voucher")
    sub = p.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--db", default="data/secretary.db")
    exp.add_argument("--out", default=None, help="默认 data/vouchers_月份.csv")
    exp.add_argument("--month", default=None, help="按发生月过滤（YYYY-MM）")
    exp.add_argument("--settings", default="config/settings.yaml")
    args = p.parse_args(argv)
    import sqlite3

    from boss_secretary.core.llm import load_settings
    settings = load_settings(args.settings)
    conn = sqlite3.connect(args.db, check_same_thread=False)
    month = args.month or dt.date.today().strftime("%Y-%m")
    out = args.out or f"data/vouchers_{month}.csv"
    result = export_csv(conn, out, month=month, settings=settings)
    print(summary_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
