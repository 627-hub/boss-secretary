"""SQLite schema（PRD §5.3）。audit_log append-only 由触发器强制。

CLI: python3 -m boss_secretary.models data/secretary.db
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS employees(
  feishu_user_id TEXT PRIMARY KEY,
  name TEXT,
  dept_id TEXT,
  dept_name TEXT,
  manager_user_id TEXT,
  role TEXT NOT NULL DEFAULT 'EMPLOYEE'
);

CREATE TABLE IF NOT EXISTS daily_report_access(
  user_id TEXT NOT NULL,
  report_type TEXT NOT NULL,
  dept_id TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT,
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY(user_id, report_type)
);

CREATE TABLE IF NOT EXISTS tickets(
  ticket_id TEXT PRIMARY KEY,
  feishu_instance_id TEXT,
  employee_id TEXT NOT NULL,
  dept_id TEXT,
  type TEXT NOT NULL DEFAULT 'reimburse',
  status TEXT NOT NULL,
  matrix_version INTEGER,
  sensitivity TEXT NOT NULL DEFAULT 'normal',
  amount REAL,
  currency TEXT DEFAULT 'CNY',
  expense_type TEXT,
  occurred_at TEXT,
  reason TEXT,
  invoice_code TEXT,
  invoice_no TEXT,
  invoice_seller TEXT,
  invoice_amount REAL,
  invoice_img_file_id TEXT,
  risk_score REAL,
  ai_verdict TEXT,
  ai_evidence TEXT,
  approvers TEXT,
  approvals TEXT,
  submitted_at TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  withdrawn_from_ticket_id TEXT
);

CREATE TABLE IF NOT EXISTS rules(
  rule_id TEXT PRIMARY KEY,
  name TEXT,
  enabled INTEGER DEFAULT 1,
  params TEXT,
  version INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS matrix_versions(
  matrix_id INTEGER PRIMARY KEY AUTOINCREMENT,
  matrix_name TEXT NOT NULL,
  version INTEGER NOT NULL,
  source_xlsx TEXT,
  checksum TEXT,
  compiled_yaml TEXT,
  hit_policy TEXT DEFAULT 'FIRST',
  status TEXT DEFAULT 'draft',
  created_by TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(matrix_name, version)
);

CREATE TABLE IF NOT EXISTS anomalies(
  anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  subject TEXT,
  period TEXT,
  baseline REAL,
  observed REAL,
  deviation_pct REAL,
  severity TEXT NOT NULL,
  evidence TEXT,
  evidence_file TEXT,
  ticket_refs TEXT,
  status TEXT DEFAULT 'open',
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS allowances(
  allowance_id TEXT PRIMARY KEY,
  employee_id TEXT NOT NULL,
  created_by TEXT,
  approver_role TEXT,
  category TEXT,
  total_amount REAL NOT NULL,
  used_amount REAL DEFAULT 0,
  expense_types TEXT,
  max_single REAL,
  reason TEXT,
  status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  expires_at TEXT
);

CREATE TABLE IF NOT EXISTS budgets(
  dept_id TEXT NOT NULL,
  month TEXT NOT NULL,
  amount REAL NOT NULL,
  created_by TEXT,
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY(dept_id, month)
);

CREATE TABLE IF NOT EXISTS audit_log(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  ticket_id TEXT,
  actor TEXT,
  action TEXT NOT NULL,
  payload_hash TEXT,
  payload_file TEXT
);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE INDEX IF NOT EXISTS idx_tickets_employee ON tickets(employee_id);
CREATE INDEX IF NOT EXISTS idx_tickets_dept ON tickets(dept_id);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_log(ticket_id);
"""


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    for stmt in ("ALTER TABLE tickets ADD COLUMN allowance_id TEXT",):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def append_audit(conn: sqlite3.Connection, *, ticket_id: str | None, actor: str,
                 action: str, payload_hash: str | None = None,
                 payload_file: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log(ticket_id, actor, action, payload_hash, payload_file)"
        " VALUES(?,?,?,?,?)",
        (ticket_id, actor, action, payload_hash, payload_file))
    conn.commit()
    return cur.lastrowid


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    conn = init_db(argv[0])
    n = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    print(f"initialized {argv[0]}: {n} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
