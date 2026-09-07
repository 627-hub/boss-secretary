import datetime as dt

import pytest

from boss_secretary.core import contract as CT
from boss_secretary.core import seal as SL
from boss_secretary.core.router import SQLiteTicketStore


@pytest.fixture()
def conn(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a").conn


def test_create_seal_and_request_routing(conn):
    seal = SL.create_seal(conn, "公司公章", custodian="boss1")
    r = SL.request(conn, seal_id=seal["seal_id"], applicant="e1",
                   doc_title="XX销售合同", doc_type="合同", copies=2,
                   contract_id=None)
    assert r["approver_role"] == "boss"
    fin = SL.create_seal(conn, "财务专用章", custodian="f1")
    r2 = SL.request(conn, seal_id=fin["seal_id"], applicant="e1",
                    doc_title="银行文件", copies=1)
    assert r2["approver_role"] == "finance"


def test_blank_document_rejected(conn):
    seal = SL.create_seal(conn, "公司公章", custodian="boss1")
    with pytest.raises(ValueError, match="空白"):
        SL.request(conn, seal_id=seal["seal_id"], applicant="e1",
                   doc_title="空白合同", copies=1)


def test_contract_seal_requires_active_contract(conn):
    seal = SL.create_seal(conn, "合同专用章", custodian="boss1")
    with pytest.raises(ValueError, match="不存在"):
        SL.request(conn, seal_id=seal["seal_id"], applicant="e1",
                   doc_title="XX合同", contract_id="C20990101-AAAAAA")
    cid = CT.create(conn, employee_id="e1", dept_id="D1", title="合同A",
                    supplier="Y", amount=100, start_date="2026-01-01",
                    end_date="2026-12-31")
    # 未生效 → 拒绝
    with pytest.raises(ValueError, match="未生效"):
        SL.request(conn, seal_id=seal["seal_id"], applicant="e1",
                   doc_title="合同A", contract_id=cid)
    # legal+boss 会签生效 → 可用印申请
    CT.approve(conn, cid, "l1", "legal")
    CT.approve(conn, cid, "b1", "boss")
    r = SL.request(conn, seal_id=seal["seal_id"], applicant="e1",
                   doc_title="合同A", contract_id=cid)
    assert r["status"] == SL.PENDING


def test_lifecycle_approve_mark_used_self_flag(conn):
    seal = SL.create_seal(conn, "公司公章", custodian="boss1")
    r = SL.request(conn, seal_id=seal["seal_id"], applicant="e1",
                   doc_title="证明文件", copies=1)
    rr = SL.decide(conn, r["request_id"], "boss1", approve=True)
    assert rr["status"] == SL.APPROVED and not rr["self_approved"]
    SL.mark_used(conn, r["request_id"], "boss1")
    assert SL.get_request(conn, r["request_id"])["status"] == SL.USED
    with pytest.raises(ValueError):
        SL.decide(conn, r["request_id"], "boss1", approve=True)


def test_self_approve_flagged(conn):
    seal = SL.create_seal(conn, "公司公章", custodian="boss1")
    r = SL.request(conn, seal_id=seal["seal_id"], applicant="boss1",
                   doc_title="自批文件", copies=1)
    rr = SL.decide(conn, r["request_id"], "boss1", approve=True)
    assert rr["self_approved"] == 1


def test_void(conn):
    seal = SL.create_seal(conn, "公司公章", custodian="boss1")
    r = SL.request(conn, seal_id=seal["seal_id"], applicant="e1",
                   doc_title="x", copies=1)
    SL.void_request(conn, r["request_id"], "boss1")
    assert SL.get_request(conn, r["request_id"])["status"] == SL.VOIDED
    with pytest.raises(ValueError):
        SL.mark_used(conn, r["request_id"], "boss1")
