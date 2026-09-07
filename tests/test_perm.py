import pytest

from boss_secretary.core import perm as P


def actor(uid="u1", role=P.EMPLOYEE, dept="D1", **kw):
    return P.Actor(user_id=uid, role=role, dept_id=dept, **kw)


def ticket(**kw):
    base = dict(ticket_id="T1", employee_id="e1", dept_id="D1",
                status="SUBMITTED", sensitivity=P.NORMAL, amount=300,
                reason="打车", expense_type="交通", invoice_no="12345")
    return P.Ticket(**{**base, **kw})


def test_employee_sees_own_only():
    assert P.can_view_ticket(actor(), ticket(employee_id="u1"))
    assert not P.can_view_ticket(actor(), ticket(employee_id="e2"))
    with pytest.raises(P.PermissionDenied):
        P.view_ticket(actor(), ticket(employee_id="e2"))


def test_manager_dept_scope():
    m = actor(uid="m1", role=P.MANAGER, dept="D1")
    assert P.can_view_ticket(m, ticket(dept_id="D1"))
    assert not P.can_view_ticket(m, ticket(dept_id="D2"))


def test_boss_and_audit_all():
    t = ticket(dept_id="D9")
    assert P.can_view_ticket(actor(uid="b", role=P.BOSS), t)
    assert P.can_view_ticket(actor(uid="a", role=P.AUDIT), t)


def test_finance_sees_approved_only():
    f = actor(uid="f1", role=P.FINANCE)
    assert P.can_view_ticket(f, ticket(status="APPROVED"))
    assert not P.can_view_ticket(f, ticket(status="SUBMITTED"))


def test_confidential_redacted_for_manager():
    t = ticket(sensitivity=P.CONFIDENTIAL, amount=8888, reason="机密事由")
    m = actor(uid="m1", role=P.MANAGER, dept="D1")
    v = P.view_ticket(m, t)
    assert v["redacted"] is True
    assert "amount" not in v and "reason" not in v and "invoice_no" not in v
    assert v["note"].startswith("机密")


def test_confidential_full_for_chain_and_boss():
    t = ticket(sensitivity=P.CONFIDENTIAL, amount=8888,
               approvers=("m1",))
    assert P.view_ticket(actor(uid="m1", role=P.MANAGER, dept="D1"), t)["redacted"] is False
    assert P.view_ticket(actor(uid="b", role=P.BOSS), t)["redacted"] is False
    assert P.view_ticket(actor(uid="a", role=P.AUDIT), t)["redacted"] is False
    assert P.view_ticket(actor(uid="e1"), t)["redacted"] is False


def test_confidential_unknowable_to_outsider():
    t = ticket(sensitivity=P.CONFIDENTIAL, dept_id="D2")
    with pytest.raises(P.PermissionDenied):
        P.view_ticket(actor(uid="m2", role=P.MANAGER, dept="D1"), t)


def test_sql_scope():
    assert P.sql_scope(actor())[0] == "employee_id = ?"
    frag, params = P.sql_scope(actor(uid="m1", role=P.MANAGER, dept="D1"))
    assert frag.startswith("dept_id = ?") and "confidential" in frag
    assert params == ["D1", "m1"]
    assert "APPROVED" in P.sql_scope(actor(uid="f", role=P.FINANCE))[0]
    assert "PAID" in P.sql_scope(actor(uid="f", role=P.FINANCE))[0]
    assert P.sql_scope(actor(uid="b", role=P.BOSS))[0] == "1=1"


def test_daily_report_versions():
    assert P.daily_report_version(actor(uid="b", role=P.BOSS)) == "company"
    m = actor(uid="m1", role=P.MANAGER, dept="D1")
    assert P.daily_report_version(m) == "department"
    assert P.daily_report_version(m, company_report_enabled=True) == "company"
    assert P.daily_report_version(actor(uid="f", role=P.FINANCE)) == "finance"
    assert P.daily_report_version(actor(uid="e", role=P.EMPLOYEE)) is None


def test_scope_of():
    assert P.scope_of(actor()) == P.SCOPE_SELF
    assert P.scope_of(actor(role=P.MANAGER)) == P.SCOPE_DEPT
    assert P.scope_of(actor(role=P.FINANCE)) == P.SCOPE_FINANCE_PENDING
    assert P.scope_of(actor(role=P.BOSS)) == P.SCOPE_ALL
    assert P.scope_of(actor(role=P.AUDIT)) == P.SCOPE_ALL
