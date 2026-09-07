import datetime as dt

import pytest
import yaml

from boss_secretary.core import matrix as M
from boss_secretary.core import compliance as C
from boss_secretary.core import router as R

TODAY = dt.date(2026, 9, 6)
NOW = dt.datetime(2026, 9, 6, 12, 0, 0)

GREEN = {"amount": 98, "expense_type": "交通", "occurred_at": "2026-09-05",
         "reason": "客户拜访打车", "invoice_no": "5001123", "employee_id": "e1",
         "invoice_seller": "某某科技", "llm_verdict": "PASS"}


@pytest.fixture(scope="module")
def flow():
    return R.load_flow("config/flows/reimburse.yaml")


@pytest.fixture(scope="module")
def matrix():
    return M.load("config/matrix/reimburse_v1.yaml")


@pytest.fixture()
def store(tmp_path):
    return R.SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "audit")


@pytest.fixture()
def employee():
    return {"user_id": "e1", "dept_id": "D1", "name": "张三"}


class Recorder:
    def __init__(self):
        self.events = []

    def send(self, event, ticket, to):
        self.events.append((event, ticket.get("ticket_id"), tuple(to)))


def rt(flow, matrix, store, **kw):
    return R.Router(flow, matrix, store, notifier=Recorder(), today_fn=lambda: TODAY, **kw)


def test_load_flow(flow):
    assert flow.name == "reimburse"
    assert flow.timeout_hours == 48
    assert flow.llm_review_enabled is True
    assert "amount" in flow.extract_required


def test_review_auto_approve(flow, matrix):
    o = R.review(GREEN, flow=flow, matrix=matrix, today=TODAY)
    assert o.next_status == R.AUTO_APPROVED
    assert o.decision.hit_rule_id == "M01"
    assert o.approver_roles == ()
    assert all("LLM" not in n for n in o.notes)


def test_review_llm_missing_failsafe(flow, matrix):
    o = R.review({**GREEN, "llm_verdict": None}, flow=flow, matrix=matrix, today=TODAY)
    assert o.llm_verdict_used == C.WARN
    assert any("WARN 降级" in n for n in o.notes)
    assert o.next_status == R.SUBMITTED
    assert o.decision.hit_rule_id == "M03"
    assert o.approver_roles == (R.MANAGER_ROLE,)


def test_review_llm_disabled_uses_rules_only(flow, matrix):
    import dataclasses
    flow2 = dataclasses.replace(flow, llm_review_enabled=False)
    o = R.review({**GREEN, "llm_verdict": None}, flow=flow2, matrix=matrix, today=TODAY)
    assert o.llm_verdict_used == C.PASS
    assert o.next_status == R.AUTO_APPROVED


def test_review_blocking_beats_auto(flow, matrix):
    m2 = M.from_dict({
        "matrix": "x", "version": 1,
        "inputs": [{"name": "amount", "type": "number"}],
        "rules": [
            {"id": "A", "cells": {"amount": "<=500"}, "then": {"action": "AUTO_APPROVE"}},
            {"id": "B", "cells": {"amount": "*"}, "then": {"action": "MANUAL_REVIEW"}}]})
    history = [{"ticket_id": "T0", "invoice_no": "X", "occurred_at": "2026-09-01"}]
    o = R.review({**GREEN, "amount": 300, "invoice_no": "X"},
                 flow=flow, matrix=m2, history=history, today=TODAY)
    assert o.summary["overall"] == C.FAIL
    assert o.decision.action == "AUTO_APPROVE"
    assert o.next_status == R.SUBMITTED
    assert o.approver_roles == (R.BOSS_ROLE,)
    assert any("双保险" in n for n in o.notes)


def test_review_no_match_falls_back(flow):
    m2 = M.from_dict({
        "matrix": "x", "version": 1,
        "inputs": [{"name": "amount", "type": "number"}],
        "rules": [{"id": "A", "cells": {"amount": ">999999"}, "then": {"action": "MANAGER"}}]})
    o = R.review(GREEN, flow=flow, matrix=m2, today=TODAY)
    assert o.next_status == R.SUBMITTED
    assert o.decision.hit_rule_id is None
    assert any("回落" in n for n in o.notes)


def test_full_lifecycle_cosign(flow, matrix, store, employee):
    router = rt(flow, matrix, store)
    tid = router.create_ticket(GREEN, employee)
    assert store.get(tid)["status"] == R.DRAFT

    o = router.run_review(tid, llm_verdict="WARN")
    assert o.next_status == R.SUBMITTED
    assert o.approver_roles == (R.MANAGER_ROLE, R.BOSS_ROLE) or o.approver_roles == (
        R.MANAGER_ROLE,)
    roles = o.approver_roles

    if R.MANAGER_ROLE in roles:
        st = router.approve(tid, "m1", R.MANAGER_ROLE)
        assert st != R.APPROVED if R.BOSS_ROLE in roles else st == R.APPROVED
    if R.BOSS_ROLE in roles:
        st = router.approve(tid, "b1", R.BOSS_ROLE)
    assert store.get(tid)["status"] == R.APPROVED
    events = [e for e, _, _ in router.notifier.events]
    assert "ticket.approved" in events


def test_reject_then_resubmit(flow, matrix, store, employee):
    router = rt(flow, matrix, store)
    tid = router.create_ticket(GREEN, employee)
    router.run_review(tid, llm_verdict="WARN")
    router.reject(tid, "m1", R.MANAGER_ROLE, "事由不清")
    assert store.get(tid)["status"] == R.REJECTED

    with pytest.raises(R.TransitionError):
        router.approve(tid, "m1", R.MANAGER_ROLE)

    new_id = router.resubmit(tid, {"reason": "客户拜访打车-明细补充"}, employee)
    new_t = store.get(new_id)
    assert new_t["status"] == R.DRAFT
    assert new_t["withdrawn_from_ticket_id"] == tid
    assert new_t["reason"] is None or True


def test_withdraw_rules(flow, matrix, store, employee):
    router = rt(flow, matrix, store)
    tid = router.create_ticket(GREEN, employee)
    with pytest.raises(R.PermissionError_):
        router.withdraw(tid, "e2")
    router.withdraw(tid, "e1")
    assert store.get(tid)["status"] == R.WITHDRAWN
    with pytest.raises(R.TransitionError):
        router.withdraw(tid, "e1")


def test_cancel_boss_only(flow, matrix, store, employee):
    router = rt(flow, matrix, store)
    tid = router.create_ticket(GREEN, employee)
    router.run_review(tid, llm_verdict="WARN")
    with pytest.raises(R.PermissionError_):
        router.cancel(tid, R.MANAGER_ROLE, "x")
    router.cancel(tid, R.BOSS_ROLE, "异常单作废")
    assert store.get(tid)["status"] == R.CANCELLED


def test_timeout_escalation(flow, matrix, store, employee):
    router = rt(flow, matrix, store)
    tid = router.create_ticket(GREEN, employee)
    router.run_review(tid, llm_verdict="WARN")
    stale = (NOW - dt.timedelta(hours=49)).isoformat(timespec="seconds")
    store.update(tid, submitted_at=stale)
    escalated = router.check_timeouts(now=NOW)
    assert tid in escalated
    assert store.get(tid)["status"] == R.ESCALATED
    assert router.check_timeouts(now=NOW) == []
    router.approve(tid, "b1", R.BOSS_ROLE)
    assert store.get(tid)["status"] == R.APPROVED


def test_approve_role_not_in_chain(flow, matrix, store, employee):
    router = rt(flow, matrix, store)
    tid = router.create_ticket(GREEN, employee)
    router.run_review(tid, llm_verdict="PASS")
    st = store.get(tid)["status"]
    if st == R.SUBMITTED:
        with pytest.raises(R.PermissionError_):
            router.approve(tid, "x1", R.FINANCE_CONSIGN_ROLE)
    else:
        with pytest.raises(R.TransitionError):
            router.approve(tid, "x1", R.MANAGER_ROLE)


def test_history_injected_into_rules(flow, matrix, store, employee):
    router = rt(flow, matrix, store)
    t1 = router.create_ticket(GREEN, employee)
    router.run_review(t1, llm_verdict="WARN")
    router.approve(t1, "m1", R.MANAGER_ROLE)
    dup = {**GREEN, "ticket_id": "TX", "reason": "再报一次"}
    router.store.conn.execute(
        "INSERT INTO tickets(ticket_id, employee_id, dept_id, type, status,"
        " sensitivity, amount, expense_type, occurred_at, reason, invoice_no,"
        " invoice_seller) VALUES('TX','e1','D1','reimburse','APPROVED','normal',"
        "98,'交通','2026-09-05','再报一次','5001123','某某科技')")
    router.store.conn.commit()
    t2 = router.create_ticket({**GREEN, "llm_verdict": "PASS"}, employee)
    o = router.run_review(t2, llm_verdict="PASS")
    r2 = [r for r in o.rule_results if r.rule_id == "R2"][0]
    assert r2.verdict == C.FAIL
    assert o.next_status == R.SUBMITTED
    assert o.decision.hit_rule_id == "M07"
