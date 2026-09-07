import datetime as dt

import pytest

from boss_secretary.core import supplier as S
from boss_secretary.core.router import SQLiteTicketStore


@pytest.fixture()
def conn(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a").conn


def test_normalize_name_variants():
    n = S.normalize_name(" 北京 XX 科技 有限公司 ")
    assert "有限公司" not in n and " " not in n
    assert S.normalize_name("XX科技（北京）股份有限公司") == \
           S.normalize_name("xx科技北京")
    assert "yy" in S.normalize_name("ＹＹ公司")


def test_lifecycle_create_approve_cosign(conn):
    s = S.create_request(conn, name="YY科技有限公司", uscc="91110000XXXXXXXXXX",
                         contact="张三", reason="新供应商", created_by="e1")
    assert s["status"] == S.PENDING
    with pytest.raises(ValueError):
        S.create_request(conn, name="YY科技有限公司")
    st = S.approve(conn, s["supplier_id"], "f1", "finance")
    assert st == S.REVIEWING
    st = S.approve(conn, s["supplier_id"], "b1", "boss")
    assert st == S.ACTIVE
    assert S.check_name(conn, "YY科技有限公司")["level"] == "PASS"
    assert S.check_name(conn, "YY公司")["level"] == "PASS"


def test_duplicate_active_request_rejected(conn):
    S.create_request(conn, name="重复供应商", created_by="e1")
    with pytest.raises(ValueError, match="已存在"):
        S.create_request(conn, name="重复供应商")


def test_fuzzy_match_variants(conn):
    S.create_request(conn, name="某某科技有限公司", created_by="e1")
    hit = S.find_by_name(conn, "某某科技")
    assert hit and hit["name"] == "某某科技有限公司"


def test_bank_change_requires_rereview_and_logs(conn):
    s = S.create_request(conn, name="银行测试公司", created_by="e1")
    S.approve(conn, s["supplier_id"], "f1", "finance")
    S.approve(conn, s["supplier_id"], "b1", "boss")
    r = S.update_field(conn, s["supplier_id"], "bank_account", "6222020200",
                       changed_by="f1")
    assert r["re_review"] is True
    assert S.get(conn, s["supplier_id"])["status"] == S.PENDING
    chg = S.changes(conn, s["supplier_id"])
    assert chg[0]["field"] == "bank_account" and chg[0]["new"] == "6222020200"
    S.approve(conn, s["supplier_id"], "f1", "finance")
    S.approve(conn, s["supplier_id"], "b1", "boss")
    assert S.get(conn, s["supplier_id"])["status"] == S.ACTIVE


def test_blacklist_gate_states(conn):
    s = S.create_request(conn, name="黑名单测试公司", created_by="e1")
    S.blacklist(conn, s["supplier_id"], "虚假报价", actor_id="audit1")
    r = S.check_name(conn, "黑名单测试公司")
    assert r["level"] == "FAIL" and "黑名单" in r["detail"]
    S.unblacklist(conn, s["supplier_id"], "audit1")
    assert S.check_name(conn, "黑名单测试公司")["level"] == "PASS"
    with pytest.raises(ValueError):
        S.unblacklist(conn, s["supplier_id"], "audit1")


def test_unapproved_supplier_warns(conn):
    r = S.check_name(conn, "从没登记过的公司")
    assert r["level"] == "WARN" and "未准入" in r["detail"]
