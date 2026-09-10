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

CREATE TABLE IF NOT EXISTS contracts(
  contract_id TEXT PRIMARY KEY,
  employee_id TEXT NOT NULL,
  dept_id TEXT,
  title TEXT NOT NULL,
  supplier TEXT,
  amount REAL,
  start_date TEXT,
  end_date TEXT,
  payment_terms TEXT,
  plan TEXT,
  status TEXT DEFAULT 'pending_review',
  procurement_id TEXT,
  ai_review TEXT,
  evidence_file TEXT,
  approvers TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS payments(
  payment_id TEXT PRIMARY KEY,
  contract_id TEXT,
  procurement_id TEXT,
  amount REAL NOT NULL,
  seq TEXT,
  status TEXT DEFAULT 'pending',
  employee_id TEXT,
  note TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  paid_at TEXT
);

CREATE TABLE IF NOT EXISTS seals(
  seal_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  custodian TEXT,
  status TEXT DEFAULT 'active',
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS seal_requests(
  request_id TEXT PRIMARY KEY,
  seal_id TEXT NOT NULL,
  seal_name TEXT,
  applicant TEXT NOT NULL,
  doc_title TEXT NOT NULL,
  doc_type TEXT,
  copies INTEGER DEFAULT 1,
  reason TEXT,
  contract_id TEXT,
  status TEXT DEFAULT 'pending',
  approver TEXT,
  self_approved INTEGER DEFAULT 0,
  evidence_file TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  used_at TEXT
);

CREATE TABLE IF NOT EXISTS trips(
  trip_id TEXT PRIMARY KEY,
  employee_id TEXT NOT NULL,
  dept_id TEXT,
  destination TEXT,
  reason TEXT,
  estimate REAL,
  start_date TEXT,
  end_date TEXT,
  status TEXT DEFAULT 'pending',
  approver TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS loans(
  loan_id TEXT PRIMARY KEY,
  employee_id TEXT NOT NULL,
  amount REAL NOT NULL,
  repaid_amount REAL DEFAULT 0,
  reason TEXT,
  status TEXT DEFAULT 'pending',
  approver TEXT,
  paid_out_at TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS suppliers(
  supplier_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL,
  uscc TEXT,
  contact TEXT,
  bank_name TEXT,
  bank_account TEXT,
  status TEXT DEFAULT 'pending_review',
  reason TEXT,
  created_by TEXT,
  approvers TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS supplier_changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  supplier_id TEXT NOT NULL,
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  changed_by TEXT,
  changed_at TEXT DEFAULT (datetime('now','localtime'))
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

CREATE TABLE IF NOT EXISTS approval_actions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_type TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  role TEXT,
  decision TEXT NOT NULL,
  comment TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
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
CREATE INDEX IF NOT EXISTS idx_approval_actions_doc ON approval_actions(doc_type, doc_id);
"""

# 模式版本：新增结构变更时 +1，并在 MIGRATIONS 追加 (版本, 语句)。
# DDL 是"当前最新 schema"（全部 IF NOT EXISTS，新库一次建全）；
# MIGRATIONS 只处理老库的增量结构（如加列），靠 PRAGMA user_version 只跑一次。
SCHEMA_VERSION = 1
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "ALTER TABLE tickets ADD COLUMN allowance_id TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, stmt in MIGRATIONS:
        if version <= current:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # 无版本号的老库可能已手工加过该列
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    _migrate(conn)
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
