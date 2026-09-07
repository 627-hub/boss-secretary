import sqlite3

import pytest

from boss_secretary import models as MD


@pytest.fixture()
def conn(tmp_path):
    return MD.init_db(tmp_path / "t.db")


def test_tables_created(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"employees", "daily_report_access", "tickets", "rules",
            "matrix_versions", "anomalies", "audit_log"} <= names


def test_audit_append_only(conn):
    seq = MD.append_audit(conn, ticket_id="T1", actor="boss",
                          action="matrix.import", payload_hash="abc")
    assert seq == 1
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET action='tampered'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")


def test_matrix_version_unique(conn):
    conn.execute("INSERT INTO matrix_versions(matrix_name, version, status)"
                 " VALUES('reimburse', 1, 'active')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO matrix_versions(matrix_name, version, status)"
                     " VALUES('reimburse', 1, 'retired')")


def test_ticket_defaults(conn):
    conn.execute("INSERT INTO employees VALUES('e1','张三','D1','市场部','m1','EMPLOYEE')")
    conn.execute("INSERT INTO tickets(ticket_id, employee_id, status, amount)"
                 " VALUES('T1','e1','SUBMITTED',300)")
    row = conn.execute("SELECT type, sensitivity, currency FROM tickets"
                       " WHERE ticket_id='T1'").fetchone()
    assert row == ("reimburse", "normal", "CNY")
