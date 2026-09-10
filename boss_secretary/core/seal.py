"""用印管理：印章台账 + 用印申请/审批/使用确认（民企高发风险区）。

硬规则：
  - 空白文件用印 → 直接拒绝（最高危）
  - 合同专用章 → 必须关联合同且状态 active（法务+老板已会签）
  - 审批路由：公章→boss；合同专用章→boss；财务/发票专用章→finance
  - 申请人=审批人 → 台账标注 self_approved（内控提示）
台账永久留痕（含已作废/已用印记录）。
"""
from __future__ import annotations

import re
from typing import Mapping, Sequence

from boss_secretary.core import approvals as AP
from boss_secretary.core import db as DB
from boss_secretary.core import status as ST

PENDING = ST.Doc.PENDING
APPROVED = ST.Doc.APPROVED
REJECTED = ST.Doc.REJECTED
USED = ST.Doc.USED
VOIDED = ST.Doc.VOIDED

SEAL_ACTIVE = ST.Doc.ACTIVE
SEAL_FROZEN = ST.Doc.FROZEN
SEAL_VOIDED = ST.Doc.VOIDED

BLANK_PAT = re.compile(r"空白")


# ── 印章台账 ──────────────────────────────────────────────────

def create_seal(conn, name: str, custodian: str) -> dict:
    sid = DB.new_id("SE")
    conn.execute("INSERT INTO seals(seal_id, name, custodian) VALUES(?,?,?)",
                 (sid, name, custodian))
    conn.commit()
    return get_seal(conn, sid)


def get_seal(conn, seal_id: str) -> dict | None:
    return DB.fetch_one(conn, "seals", "seal_id", seal_id)


def find_seal_by_name(conn, name: str) -> dict | None:
    row = conn.execute("SELECT seal_id FROM seals WHERE name=? AND status=?",
                       (name, SEAL_ACTIVE)).fetchone()
    return get_seal(conn, row[0]) if row else None


def seal_list(conn) -> list[dict]:
    return [get_seal(conn, r[0]) for r in
            conn.execute("SELECT seal_id FROM seals ORDER BY created_at").fetchall()]


# ── 用印申请 ──────────────────────────────────────────────────

def approver_role_for(seal_name: str) -> str:
    if "财务" in seal_name or "发票" in seal_name:
        return "finance"
    return "boss"          # 公章/合同专用章/其他 → boss


def request(conn, *, seal_id: str, applicant: str, doc_title: str,
            doc_type: str = "其他", copies: int = 1, reason: str = "",
            contract_id: str | None = None,
            evidence_file: str | None = None) -> dict:
    seal = get_seal(conn, seal_id)
    if seal is None or seal["status"] != SEAL_ACTIVE:
        raise ValueError(f"印章不可用: {seal_id}")
    if BLANK_PAT and BLANK_PAT.search(doc_title or ""):
        raise ValueError("空白文件用印为高风险操作，禁止受理")
    if "合同" in seal["name"] and contract_id:
        from boss_secretary.core import contract as CT
        c = CT.get(conn, contract_id)
        if c is None:
            raise ValueError(f"关联合同不存在: {contract_id}")
        if c["status"] != CT.ACTIVE:
            raise ValueError(f"关联合同 {contract_id} 状态为 {c['status']}，"
                             f"未生效不得用合同专用章")
    rid = DB.new_id("Y")
    conn.execute(
        "INSERT INTO seal_requests(request_id, seal_id, seal_name, applicant,"
        " doc_title, doc_type, copies, reason, contract_id, status, evidence_file)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (rid, seal_id, seal["name"], applicant, doc_title, doc_type, copies,
         reason, contract_id, PENDING, evidence_file))
    conn.commit()
    r = get_request(conn, rid)
    r["approver_role"] = approver_role_for(seal["name"])
    return r


def get_request(conn, request_id: str) -> dict | None:
    return DB.fetch_one(conn, "seal_requests", "request_id", request_id)


def decide(conn, request_id: str, approver: str, approve: bool,
           note: str = "") -> dict:
    r = get_request(conn, request_id)
    if r is None:
        raise ValueError(f"用印申请不存在: {request_id}")
    if r["status"] != PENDING:
        raise ValueError(f"状态 {r['status']} 不可审批")
    self_flag = 1 if r["applicant"] == approver else 0
    new = APPROVED if approve else REJECTED
    role = approver_role_for(r["seal_name"])
    AP.record(conn, doc_type="seal", doc_id=request_id, actor=approver,
              role=role, decision=AP.APPROVE if approve else AP.REJECT,
              comment=note)
    DB.update_fields(conn, "seal_requests", "request_id", request_id,
                     status=new,
                     approver=f"{approver}({note})" if note else approver,
                     self_approved=self_flag,
                     used_at=DB.now() if new == USED else None)
    r = get_request(conn, request_id)
    r["approver_role"] = approver_role_for(r["seal_name"])
    return r


def mark_used(conn, request_id: str, actor: str) -> None:
    r = get_request(conn, request_id)
    if r is None:
        raise ValueError(f"用印申请不存在: {request_id}")
    if r["status"] != APPROVED:
        raise ValueError(f"状态 {r['status']} 不可确认用印（先审批）")
    DB.update_fields(conn, "seal_requests", "request_id", request_id,
                     status=USED, used_at=DB.now())


def void_request(conn, request_id: str, actor: str) -> None:
    r = get_request(conn, request_id)
    if r is None or r["status"] in (USED, VOIDED):
        raise ValueError("状态不可作废")
    DB.update_fields(conn, "seal_requests", "request_id", request_id,
                     status=VOIDED)


def list_requests(conn, applicant: str | None = None, limit: int = 15) -> list[dict]:
    sql = "SELECT request_id FROM seal_requests"
    args: list = []
    if applicant:
        sql += " WHERE applicant=?"
        args.append(applicant)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [get_request(conn, r[0]) for r in conn.execute(sql, args).fetchall()]


def to_table(rows: Sequence[Mapping]) -> str:
    if not rows:
        return "（无用印记录）"
    marks = {PENDING: "⏳", APPROVED: "✓待用", USED: "✓已用", REJECTED: "✗",
             VOIDED: "✗作废"}
    lines = []
    for r in rows:
        self_mark = " ⚠自批" if r.get("self_approved") else ""
        lines.append(f"  {marks.get(r['status'], '?')} {r['request_id']} "
                     f"[{r['seal_name']}] {r['doc_title']} ×{r.get('copies') or 1}"
                     f" 申请人{r['applicant']}{self_mark}"
                     + (f" 关联{r['contract_id']}" if r.get("contract_id") else ""))
    return "\n".join(lines)
