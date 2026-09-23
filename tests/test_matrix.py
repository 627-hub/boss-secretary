import copy
import pytest

from boss_secretary.core import matrix as M

BASE_CTX = {"expense_type": "交通", "amount": 300,
            "rule_result": "PASS", "llm_verdict": "PASS"}


@pytest.fixture(scope="module")
def mat():
    return M.load("config/matrix/reimburse_v1.yaml")


def ev(m, ctx):
    return M.evaluate(m, ctx)


def test_load_and_shape(mat):
    assert mat.name == "reimburse"
    assert mat.version == 1
    assert mat.hit_policy == M.FIRST
    assert len(mat.rules) == 7


def test_first_ordering(mat):
    assert ev(mat, BASE_CTX).action == "AUTO_APPROVE"
    d = ev(mat, {"expense_type": "餐饮", "amount": 1500,
                 "rule_result": "PASS", "llm_verdict": "PASS"})
    assert (d.hit_rule_id, d.action) == ("M02", "MANAGER")


def test_amount_tiers(mat):
    cases = [(500, "M01"), (500.01, "M03"), (5000, "M03"), (5001, "M04"),
             (50000, "M04"), (50001, "M05"), (80000, "M05")]
    for amt, rid in cases:
        ctx = {**BASE_CTX, "amount": amt}
        if amt > 2000:
            ctx["expense_type"] = "其他"
        assert ev(mat, ctx).hit_rule_id == rid, amt


def test_warn_stays_in_amount_tier(mat):
    d = ev(mat, {**BASE_CTX, "amount": 300, "rule_result": "WARN"})
    assert (d.hit_rule_id, d.action) == ("M03", "MANAGER")
    d = ev(mat, {"expense_type": "其他", "amount": 80000,
                 "rule_result": "WARN", "llm_verdict": "WARN"})
    assert d.hit_rule_id == "M05"


def test_fail_goes_manual_review_before_tiers(mat):
    assert ev(mat, {**BASE_CTX, "rule_result": "FAIL"}).action == "MANUAL_REVIEW"
    d = ev(mat, {"expense_type": "其他", "amount": 80000,
                 "rule_result": "FAIL", "llm_verdict": "PASS"})
    assert (d.hit_rule_id, d.action) == ("M07", "MANUAL_REVIEW")


def test_catchall_row(mat):
    assert ev(mat, {"expense_type": "其他"}).hit_rule_id == "M99"


def test_no_match_raises(mat):
    m = M.from_dict({"matrix": "t", "version": 1,
                     "inputs": [{"name": "x", "type": "number"}],
                     "rules": [{"id": "R1", "cells": {"x": ">100"},
                                "then": {"action": "A"}}]})
    with pytest.raises(M.NoMatchError):
        ev(m, {"x": 50})


def test_decision_carries_version_and_notify(mat):
    d = ev(mat, {**BASE_CTX, "amount": 80000, "expense_type": "其他"})
    assert d.matrix_version == 1
    assert d.notify == ("finance",)
    assert d.matched_rows == ("M05",)


def test_unique_policy_violation():
    m = M.from_dict({
        "matrix": "u", "version": 2, "hit_policy": "UNIQUE",
        "inputs": [{"name": "x", "type": "number"}],
        "rules": [
            {"id": "A", "cells": {"x": "<=100"}, "then": {"action": "X"}},
            {"id": "B", "cells": {"x": "50-150"}, "then": {"action": "Y"}},
        ]})
    with pytest.raises(M.UniqueViolationError):
        ev(m, {"x": 80})


def test_lint_duplicate_cells_error(mat):
    data = {"matrix": "d", "version": 1,
            "inputs": [{"name": "x", "type": "number"}],
            "rules": [
                {"id": "A", "cells": {"x": ">10"}, "then": {"action": "X"}},
                {"id": "B", "cells": {"x": ">10"}, "then": {"action": "Y"}},
                {"id": "C", "cells": {"x": "*"}, "then": {"action": "Z"}}]}
    errors, warnings, overlaps = M.lint(M.from_dict(data))
    assert any("条件完全相同" in e for e in errors)
    assert any("兜底" in w for w in warnings) is False
    assert ("A", "B") in overlaps


def test_lint_enum_violation():
    data = {"matrix": "e", "version": 1,
            "inputs": [{"name": "t", "type": "string", "enum": ["a", "b"]}],
            "rules": [{"id": "R", "cells": {"t": "c"}, "then": {"action": "X"}},
                      {"id": "R2", "cells": {"t": "*"}, "then": {"action": "Y"}}]}
    errors, _, _ = M.lint(M.from_dict(data))
    assert any("枚举" in e for e in errors)


def test_cells_overlap_semantics():
    assert M._cells_overlap(M.parse_cell("<=500", "number"),
                            M.parse_cell("5001-50000", "number")) is False
    assert M._cells_overlap(M.parse_cell("<=500", "number"),
                            M.parse_cell("400-600", "number")) is True
    assert M._cells_overlap(M.parse_cell(">50000", "number"),
                            M.parse_cell("5001-50000", "number")) is False
    assert M._cells_overlap(M.parse_cell("in (交通, 餐饮)"),
                            M.parse_cell("交通")) is True
    assert M._cells_overlap(M.parse_cell("in (交通, 餐饮)"),
                            M.parse_cell("住宿")) is False


def test_to_table_contains_rules(mat):
    table = M.to_table(mat)
    for rid in ("M01", "M05", "M99"):
        assert rid in table
