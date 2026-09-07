import csv
import datetime as dt

import pytest

from boss_secretary.core.router import SQLiteTicketStore
from boss_secretary.finance import export as F


@pytest.fixture()
def conn(tmp_path):
    store = SQLiteTicketStore(tmp_path / "t.db", audit_dir=tmp_path / "a")
    c = store.conn
    for tid, dept, st, amt, et, reason, occ in [
        ("T-PAID", "D1", "PAID", 469, "餐饮", "工作餐", "2026-09-05"),
        ("T-APP", "D2", "APPROVED", 300, "交通", "打车", "2026-09-06"),
        ("T-REJ", "D1", "REJECTED", 999, "办公", "不该入", "2026-09-06"),
    ]:
        c.execute("INSERT INTO tickets(ticket_id, employee_id, dept_id, type, status,"
                  " sensitivity, amount, currency, expense_type, reason, occurred_at,"
                  " submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, "e1", dept, "reimburse", st, "normal", amt, "CNY", et,
                   reason, occ, "2026-09-06T10:00"))
    c.commit()
    return store


def test_export_paid_and_approved(conn, tmp_path):
    out = tmp_path / "v.csv"
    result = F.export_csv(conn, out, month="2026-09", operator="finance")
    assert result["vouchers"] == 2 and result["rows"] == 6
    with open(out, encoding="gbk") as f:
        rows = list(csv.reader(f))
    assert rows[0][:4] == ["会计期间", "凭证日期", "凭证字", "凭证号"]
    paid = [r for r in rows if r[11] == "T-PAID"]
    app = [r for r in rows if r[11] == "T-APP"]
    assert len(paid) == 4 and len(app) == 2
    assert "REJECTED" not in [r for r in rows[1:] and [x[11] for x in rows[1:]]]
    # 计提行: 借费用 贷应付
    assert paid[0][5] == "660107" and float(paid[0][8]) == 469 and float(paid[0][9]) == 0
    assert paid[1][5] == "2241" and float(paid[1][9]) == 469
    # 打款行: 借应付 贷银行
    assert paid[2][5] == "2241" and float(paid[2][8]) == 469
    assert paid[3][5] == "1002" and float(paid[3][9]) == 469


def test_debit_credit_balance(conn, tmp_path):
    out = tmp_path / "v2.csv"
    F.export_csv(conn, out, month="2026-09")
    with open(out, encoding="gbk") as f:
        rows = list(csv.reader(f))[1:]
    debit = sum(float(r[8]) for r in rows)
    credit = sum(float(r[9]) for r in rows)
    assert abs(debit - credit) < 0.01


def test_subject_and_dept_mapping(conn, tmp_path):
    settings = {"kingdee": {"subject_map": {"餐饮": "660201"},
                            "dept_map": {"D1": "BM001"}, "encoding": "utf-8"}}
    out = tmp_path / "v3.csv"
    F.export_csv(conn, out, month="2026-09", settings=settings)
    with open(out, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    paid = [r for r in rows if r[11] == "T-PAID"]
    assert paid[0][5] == "660201" and paid[0][6] == "BM001"


def test_month_filter(conn, tmp_path):
    out = tmp_path / "v4.csv"
    result = F.export_csv(conn, out, month="2099-01")
    assert result["vouchers"] == 0


def test_cli_export(conn, tmp_path):
    import subprocess, sys, os
    db = tmp_path / "t.db"
    r = subprocess.run([sys.executable, "-m", "boss_secretary.voucher", "export",
                        "--db", str(db), "--month", "2026-09",
                        "--out", str(tmp_path / "cli.csv")],
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": "."}, cwd=".")
    assert "凭证导出完成" in r.stdout
