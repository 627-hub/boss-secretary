import datetime as dt
import sqlite3

import pytest

from boss_secretary.core import budget as BG
from boss_secretary.core.router import SQLiteTicketStore


@pytest.fixture()
def conn(tmp_path):
    return SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a").conn


def test_template_import_roundtrip(conn, tmp_path):
    xlsx = tmp_path / "b.xlsx"
    BG.export_template(xlsx)
    result = BG.import_budgets(conn, xlsx, operator="boss")
    assert result["imported"] == 3
    assert BG.get_budget(conn, "D1", "2026-09") == 50000
    assert BG.get_budget(conn, "*", "2026-09") == 100000
    # upsert 覆盖
    BG.import_budgets(conn, xlsx, operator="boss")
    assert BG.get_budget(conn, "D1", "2026-09") == 50000


def test_import_custom_rows(conn, tmp_path):
    xlsx = tmp_path / "b2.xlsx"
    BG.export_template(xlsx, samples=[{"dept": "D9", "month": "2026-10", "amount": 800}])
    result = BG.import_budgets(conn, xlsx)
    assert result["imported"] == 1
    assert BG.get_budget(conn, "D9", "2026-10") == 800


def test_import_validation_errors(conn, tmp_path):
    from openpyxl import Workbook
    xlsx = tmp_path / "bad.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "预算"
    ws.append(["部门", "月份", "预算金额(元)"])
    ws.append(["D1", "2026/09", 100])
    ws.append(["D1", "2026-09", -5])
    ws.append(["D1", "2026-09", "abc"])
    ws.append(["", "2026-09", 100])
    ws.append(["D2", "2026-10", 300])
    wb.save(xlsx)
    with pytest.raises(ValueError) as e:
        BG.read_xlsx(xlsx)
    assert "月份格式" in str(e.value) and "正数" in str(e.value) and "不是数字" in str(e.value)
    assert "字段不全" in str(e.value)
    with pytest.raises(ValueError):
        BG.import_budgets(conn, xlsx)
    assert BG.get_budget(conn, "D2", "2026-10") is None


def test_used_and_check_after_import(conn, tmp_path):
    xlsx = tmp_path / "b3.xlsx"
    BG.export_template(xlsx, samples=[{"dept": "D1", "month": "2026-09", "amount": 400}])
    BG.import_budgets(conn, xlsx)
    conn.execute("INSERT INTO tickets(ticket_id, employee_id, dept_id, status, amount,"
                 " occurred_at) VALUES('T1','e1','D1','APPROVED',300,'2026-09-15')")
    conn.commit()
    b = BG.check(conn, "D1", "2026-09", extra=200)
    assert b["checked"] and b["used"] == 300 and not b["ok"] and b["over"] == 100


def test_cli_template_and_import(tmp_path):
    import subprocess, sys, os
    env = {**os.environ, "PYTHONPATH": "."}
    xlsx = tmp_path / "cli.xlsx"
    r1 = subprocess.run([sys.executable, "-m", "boss_secretary.budget", "template",
                         "--out", str(xlsx)], capture_output=True, text=True, env=env,
                        cwd=".")
    assert r1.returncode == 0 and "模板已生成" in r1.stdout
    db = tmp_path / "t.db"
    from boss_secretary.core.router import SQLiteTicketStore
    SQLiteTicketStore(db, audit_dir=tmp_path / "a")
    r2 = subprocess.run([sys.executable, "-m", "boss_secretary.budget", "import",
                         str(xlsx), "--db", str(db)], capture_output=True, text=True,
                        env=env, cwd=".")
    assert r2.returncode == 0 and "导入 3 条" in r2.stdout
