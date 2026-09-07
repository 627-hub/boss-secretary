"""供应商主数据 + 黑名单（付款舞弊第一防线）。

生命周期：准入申请 → 财务+老板会签 → active；
高危变更（银行账户）→ 强制重审批 + 变更台账；
黑名单（audit/boss）→ 三道拦截闸：采购/合同/付款全拒绝，报销开票方命中 → 强制人工。

名称匹配：规范化（去空格/全半角/去括号后缀）+ 精确与包含双向匹配，防"XX公司"vs
"XX有限公司"绕过。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
REJECTED = "rejected"
PENDING = "pending_review"
REVIEWING = "reviewing"
ACTIVE = "active"
SUSPENDED = "suspended"
BLACKLISTED = "blacklisted"

SIGNED_ROLES = ("finance", "boss")
HIGH_RISK_FIELDS = ("bank_name", "bank_account")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _id() -> str:
    return f"S{dt.date.today():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = re.sub(r"[\s（）()\-—_]", "", s)
    s = re.sub(r"(股份)?有限(责任)?公司$|公司$|集团$", "", s)
    return s.lower()


def get(conn, supplier_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM suppliers WHERE supplier_id=?",
                       (supplier_id,)).fetchone()
    if row is None:
        return None
    rec = dict(zip([c[0] for c in conn.execute(
        "SELECT * FROM suppliers LIMIT 1").description], row))
    try:
        rec["approvers"] = json.loads(rec["approvers"]) if rec.get("approvers") else []
    except (TypeError, ValueError):
        rec["approvers"] = []
    return rec


def _log_change(conn, sid: str, field: str, old: Any, new: Any, by: str) -> None:
    conn.execute("INSERT INTO supplier_changes(supplier_id, field, old_value,"
                 " new_value, changed_by) VALUES(?,?,?,?,?)",
                 (sid, field, str(old), str(new), by))


def find_by_name(conn, name: str) -> dict | None:
    norm = normalize_name(name)
    if not norm:
        return None
    row = conn.execute("SELECT supplier_id, name FROM suppliers", ()).fetchall()
    for sid, nm in row:
        n = normalize_name(nm)
        if n == norm or ((min(len(n), len(norm)) >= 2)
                         and (n in norm or norm in n)):
            return get(conn, sid)
    return None


def check_name(conn, name: str) -> dict:
    """三态: {"level": "FAIL"/"WARN"/"PASS", "supplier": {...}|None, "detail": str}"""
    s = find_by_name(conn, name)
    if s is None:
        return {"level": WARN, "supplier": None,
                "detail": f"供应商「{name}」未准入（先走准入申请）"}
    if s["status"] == BLACKLISTED:
        return {"level": FAIL, "supplier": s,
                "detail": f"供应商「{name}」在黑名单（{s.get('reason') or '未说明'}）"}
    if s["status"] in (PENDING, REVIEWING):
        return {"level": WARN, "supplier": s,
                "detail": f"供应商「{name}」准入审批中"}
    if s["status"] == SUSPENDED:
        return {"level": FAIL, "supplier": s, "detail": f"供应商「{name}」已停用"}
    return {"level": PASS, "supplier": s, "detail": f"供应商「{name}」主数据正常"}


def create_request(conn, *, name: str, uscc: str | None = None,
                   contact: str | None = None, bank_name: str | None = None,
                   bank_account: str | None = None, reason: str = "",
                   created_by: str = "") -> dict:
    dup = find_by_name(conn, name)
    if dup and dup["status"] in (ACTIVE, PENDING, REVIEWING):
        raise ValueError(f"供应商「{name}」已存在（{dup['supplier_id']}，{dup['status']}）")
    sid = _id()
    conn.execute(
        "INSERT INTO suppliers(supplier_id, name, name_norm, uscc, contact,"
        " bank_name, bank_account, status, reason, created_by)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid, name, normalize_name(name), uscc, contact, bank_name,
         bank_account, PENDING, reason, created_by))
    conn.commit()
    return get(conn, sid)


def approve(conn, supplier_id: str, actor_id: str, role: str) -> str:
    s = get(conn, supplier_id)
    if s is None:
        raise ValueError(f"供应商不存在: {supplier_id}")
    if s["status"] not in (PENDING, REVIEWING):
        raise ValueError(f"状态 {s['status']} 不可审批")
    approvals = s["approvers"] or []
    if not any(a.get("role") == role for a in approvals):
        approvals.append({"role": role, "user_id": actor_id})
        conn.execute("UPDATE suppliers SET approvers=?, updated_at=?"
                     " WHERE supplier_id=?",
                     (json.dumps(approvals, ensure_ascii=False), _now(), supplier_id))
        conn.commit()
    signed = {a["role"] for a in (get(conn, supplier_id)["approvers"] or [])}
    if set(SIGNED_ROLES) <= signed:
        conn.execute("UPDATE suppliers SET status=?, updated_at=? WHERE supplier_id=?",
                     (ACTIVE, _now(), supplier_id))
        conn.commit()
        return ACTIVE
    conn.execute("UPDATE suppliers SET status=?, updated_at=? WHERE supplier_id=?",
                 (REVIEWING, _now(), supplier_id))
    conn.commit()
    return REVIEWING


def reject(conn, supplier_id: str, actor_id: str) -> None:
    s = get(conn, supplier_id)
    if s is None or s["status"] not in (PENDING, REVIEWING):
        raise ValueError("供应商不存在或状态不可驳回")
    conn.execute("UPDATE suppliers SET status=?, updated_at=? WHERE supplier_id=?",
                 ("rejected", _now(), supplier_id))
    conn.commit()


def update_field(conn, supplier_id: str, field: str, value: str,
                 changed_by: str) -> dict:
    """普通字段直接更新；银行账户类（高危）→ 置回重审批 + 变更台账。"""
    s = get(conn, supplier_id)
    if s is None:
        raise ValueError(f"供应商不存在: {supplier_id}")
    if field not in ("bank_name", "bank_account", "contact", "uscc"):
        raise ValueError(f"字段不可变更: {field}")
    old = s.get(field)
    _log_change(conn, supplier_id, field, old, value, changed_by)
    conn.execute(f"UPDATE suppliers SET {field}=?, updated_at=? WHERE supplier_id=?",
                 (value, _now(), supplier_id))
    re_review = field in HIGH_RISK_FIELDS
    if re_review and s["status"] == ACTIVE:
        conn.execute("UPDATE suppliers SET status=?, approvers='', updated_at=?"
                     " WHERE supplier_id=?", (PENDING, _now(), supplier_id))
    conn.commit()
    return {"supplier_id": supplier_id, "field": field, "old": old, "new": value,
            "re_review": re_review}


def blacklist(conn, supplier_id: str, reason: str, actor_id: str) -> None:
    s = get(conn, supplier_id)
    if s is None:
        raise ValueError(f"供应商不存在: {supplier_id}")
    conn.execute("UPDATE suppliers SET status=?, reason=?, updated_at=?"
                 " WHERE supplier_id=?",
                 (BLACKLISTED, f"{reason}（by {actor_id}）", _now(), supplier_id))
    _log_change(conn, supplier_id, "status", s["status"], BLACKLISTED, actor_id)
    conn.commit()


def unblacklist(conn, supplier_id: str, actor_id: str) -> None:
    s = get(conn, supplier_id)
    if s is None or s["status"] != BLACKLISTED:
        raise ValueError("供应商不在黑名单")
    conn.execute("UPDATE suppliers SET status=?, reason='移出黑名单', updated_at=?"
                 " WHERE supplier_id=?", (ACTIVE, _now(), supplier_id))
    _log_change(conn, supplier_id, "status", BLACKLISTED, ACTIVE, actor_id)
    conn.commit()


def list_all(conn, status: str | None = None, limit: int = 20) -> list[dict]:
    sql = "SELECT supplier_id FROM suppliers"
    args: list = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    return [get(conn, r[0]) for r in conn.execute(sql, args).fetchall()]


def changes(conn, supplier_id: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT field, old_value, new_value, changed_by, changed_at"
        " FROM supplier_changes WHERE supplier_id=? ORDER BY id DESC LIMIT ?",
        (supplier_id, limit)).fetchall()
    return [dict(zip(("field", "old", "new", "by", "at"), r)) for r in rows]


def to_table(rows: Sequence[Mapping]) -> str:
    if not rows:
        return "（无供应商）"
    marks = {ACTIVE: "✓", PENDING: "⏳", REVIEWING: "⏳", BLACKLISTED: "⛔",
             SUSPENDED: "⏸", "rejected": "✗"}
    lines = []
    for s in rows:
        lines.append(f"  {marks.get(s['status'], '?')} {s['supplier_id']} "
                     f"{s['name']} [{s['status']}] "
                     f"银行:{s.get('bank_name') or '-'} "
                     f"尾号:{str(s.get('bank_account') or '')[-4:] or '-'}")
    return "\n".join(lines)
