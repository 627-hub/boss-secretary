"""预算检查（PRD F14）：部门/全司月度预算，额度审批与报销提交两个检查点。

口径：消费归属月 = occurred_at 的月份；已用 = 该月非驳回单据金额合计
（APPROVED/PAID/AUTO_APPROVED/SUBMITTED/ESCALATED 在途计入）。
部门未设预算时不检查；dept_id 为空的员工归入全司预算（dept_id='*'）。
超预算默认**提示不阻断**（settings.budgets.block_over=true 时拒绝提交）。

CLI: 无（经 boss-feishu 命令 `预算`）。
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

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


# ─────────────────────── Excel 批量导入 ───────────────────────

SHEET_NAME = "预算"
_HEADERS = ("部门", "月份", "预算金额(元)")
_TITLE = "预算批量导入（每月一行；部门 * = 全司；月份 YYYY-MM）"


def export_template(path, samples: Sequence[Mapping] | None = None) -> Path:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws["A1"] = _TITLE
    ws.append(list(_HEADERS))
    for row in (samples or [
        {"dept": "D1", "month": "2026-09", "amount": 50000},
        {"dept": "D2", "month": "2026-09", "amount": 30000},
        {"dept": "*", "month": "2026-09", "amount": 100000},
    ]):
        ws.append([row["dept"], row["month"], row["amount"]])
    for col, w in (("A", 14), ("B", 14), ("C", 18)):
        ws.column_dimensions[col].width = w
    wb.save(path)
    return Path(path)


def read_xlsx(path) -> list[dict]:
    from openpyxl import load_workbook
    p = Path(path)
    if not p.exists():
        raise ValueError(f"文件不存在: {p}")
    wb = load_workbook(p, data_only=True, read_only=True)
    if SHEET_NAME not in wb.sheetnames:
        raise ValueError(f"缺少工作表「{SHEET_NAME}」（用 template 生成模板）")
    ws = wb[SHEET_NAME]
    rows, errors = [], []
    header_seen = False
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        vals = [str(v).strip() if v is not None else "" for v in row] if row else []
        if not any(vals):
            continue
        if not header_seen and vals[:3] == list(_HEADERS):
            header_seen = True
            continue
        if vals[0].startswith("预算批量导入"):
            continue
        if len(vals) < 3 or not vals[0] or not vals[1] or not vals[2]:
            errors.append(f"第 {i} 行字段不全（部门/月份/金额）")
            continue
        dept, month, amt_s = vals[0], vals[1], vals[2].replace(",", "")
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            errors.append(f"第 {i} 行月份格式错误：{month}（应 YYYY-MM）")
            continue
        try:
            amount = float(amt_s)
        except ValueError:
            errors.append(f"第 {i} 行金额不是数字：{amt_s}")
            continue
        if amount <= 0:
            errors.append(f"第 {i} 行金额须为正数：{amount}")
            continue
        rows.append({"dept": dept, "month": month, "amount": amount, "row": i})
    if not header_seen and not rows:
        raise ValueError("未找到表头行（部门/月份/预算金额(元)）")
    return rows + [] if not errors else (_ for _ in ()).throw(ValueError("；".join(errors)))


def import_budgets(conn, path, operator: str = "") -> dict:
    rows = read_xlsx(path)
    n_set = 0
    for r in rows:
        set_budget(conn, r["dept"], r["month"], r["amount"], created_by=operator)
        n_set += 1
    return {"imported": n_set, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="boss_secretary.budget")
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="xlsx 批量导入（upsert）")
    imp.add_argument("xlsx_path")
    imp.add_argument("--db", default="data/secretary.db")
    imp.add_argument("--operator", default="boss")
    tpl = sub.add_parser("template", help="生成 Excel 模板")
    tpl.add_argument("--out", default="config/budgets.xlsx")
    args = p.parse_args(argv)
    if args.cmd == "template":
        print(f"模板已生成: {export_template(args.out)}")
        return 0
    import sqlite3
    conn = sqlite3.connect(args.db, check_same_thread=False)
    result = import_budgets(conn, args.xlsx_path, operator=args.operator)
    print(f"导入 {result['imported']} 条：")
    for r in result["rows"]:
        print(f"  {r['dept']:<6} {r['month']} {r['amount']:.0f} 元")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
