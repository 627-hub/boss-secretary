import json
import os

import pytest
import yaml

from boss_secretary.core import matrix as M
from boss_secretary.core import matrix_import as MI

CTX = {"expense_type": "交通", "amount": 300,
       "rule_result": "PASS", "llm_verdict": "PASS"}


@pytest.fixture(scope="module")
def base_yaml():
    return "config/matrix/reimburse_v1.yaml"


def test_roundtrip_semantics(base_yaml, tmp_path):
    m1 = M.load(base_yaml)
    xlsx = tmp_path / "rt.xlsx"
    MI.export_to_xlsx(m1, xlsx)
    px = MI.read_xlsx(xlsx)
    m2 = M.from_dict(MI.xlsx_to_matrix_dict(px))
    for ctx in [CTX, {**CTX, "amount": 80000}, {**CTX, "expense_type": "餐饮",
                 "amount": 1500}, {**CTX, "rule_result": "FAIL"}]:
        d1, d2 = M.evaluate(m1, ctx), M.evaluate(m2, ctx)
        assert (d1.hit_rule_id, d1.action) == (d2.hit_rule_id, d2.action)
    assert m2.hit_policy == "FIRST"
    assert not m2.inputs["amount"]["header"]


def test_le_symbol_normalized():
    cell = M.parse_cell("≤500", "number")
    assert cell.matches(300) and not cell.matches(600)
    assert M.parse_cell("≥5000", "number").matches(5001)
    assert M.parse_cell("＞50000", "number").matches(80000)
    assert M.parse_cell("５００", "number").matches(500)


def test_validate_action_enum_error(tmp_path):
    md = yaml.safe_load(open("config/matrix/reimburse_v1.yaml", encoding="utf-8"))
    md["rules"][0]["then"]["action"] = "NOT_EXIST"
    xlsx = tmp_path / "bad.xlsx"
    m = M.from_dict(md)
    MI.export_to_xlsx(m, xlsx)
    px = MI.read_xlsx(xlsx)
    _, errors, _, _ = MI.validate(px)
    assert any("动作" in e and "枚举" in e for e in errors)


def test_grid_dry_run(base_yaml, capsys):
    m = M.load(base_yaml)
    ctxs = MI.boundary_contexts(m)
    assert 0 < len(ctxs) <= 5000
    report = MI.dry_run(m, m, ctxs)
    assert "命中分布" in report and "M01" in report
    assert "NO_MATCH" not in report or m.rules[-1].cells


def test_commit_lifecycle(base_yaml, tmp_path):
    db = tmp_path / "t.db"
    v1_xlsx = tmp_path / "v1.xlsx"
    m = M.load(base_yaml)
    MI.export_to_xlsx(m, v1_xlsx, changelog=[(1, "2026-09-06", "boss", "初始")])
    assert MI.commit(v1_xlsx, db, operator="boss") == 1

    import sqlite3
    conn = sqlite3.connect(db)
    prev_yaml = conn.execute("SELECT compiled_yaml FROM matrix_versions"
                             " WHERE status='active'").fetchone()[0]
    conn.close()

    md = yaml.safe_load(prev_yaml)
    md["version"] = 2
    md["rules"][0]["then"]["action"] = "MANAGER_BOSS"
    v2_xlsx = tmp_path / "v2.xlsx"
    MI.export_to_xlsx(M.from_dict(md), v2_xlsx, changelog=[(2, "2026-09-06", "boss", "直批收紧")])
    assert MI.commit(v2_xlsx, db, operator="boss") == 2

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT version, status FROM matrix_versions"
                        " ORDER BY version").fetchall()
    assert rows == [(1, "retired"), (2, "active")]
    audit = conn.execute("SELECT actor, action, payload_file FROM audit_log").fetchall()
    assert len(audit) == 2 and audit[1][0] == "boss" and audit[1][1] == "matrix.import"
    diff = open(audit[1][2], encoding="utf-8").read()
    assert "-version: 1" in diff and "+version: 2" in diff
    conn.close()


def test_commit_version_must_increase(base_yaml, tmp_path):
    import sqlite3
    db = tmp_path / "t2.db"
    xlsx = tmp_path / "x.xlsx"
    MI.export_to_xlsx(M.load(base_yaml), xlsx)
    MI.commit(xlsx, db, operator="boss")
    with pytest.raises(M.MatrixLintError, match="版本必须递增"):
        MI.commit(xlsx, db, operator="boss")


def test_cli_import_dry(base_yaml, tmp_path):
    import subprocess, sys
    xlsx = tmp_path / "cli.xlsx"
    MI.export_to_xlsx(M.load(base_yaml), xlsx)
    r = subprocess.run([sys.executable, "-m", "boss_secretary.matrix", "import",
                        str(xlsx), "--grid"],
                       capture_output=True, text=True,
                       cwd=".", env={**os.environ, "PYTHONPATH": "."})
    out = r.stdout + r.stderr
    assert r.returncode == 0
    assert "重叠" in out and "干跑样本" in out and "[DRY]" in out
