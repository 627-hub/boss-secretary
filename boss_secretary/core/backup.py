"""数据备份（PRD 可靠性）：SQLite 在线 backup API + 审计/附件/报表目录打 zip。

策略：每日调度（默认 03:00）→ data/backups/backup_YYYYMMDD_HHMM.zip；
保留最近 keep 份，其余自动清理。可选第二副本目录（dest_dir，如 NAS/外置盘）。
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import zipfile
from pathlib import Path

DEFAULT = {"keep": 14, "dest_dir": "", "items": ["data/audit", "data/attachments",
                                                 "data/reports"]}


def backup(db_path: str | Path, out_dir: str | Path = "data/backups",
           items: Sequence[str] = (), keep: int = 14, dest_dir: str | None = None,
           now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"backup_{stamp}.zip"
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"数据库不存在: {src}")
    # SQLite 在线备份（不锁库、含 WAL 已提交数据）
    tmp_sql = out_dir / f"{src.stem}_{stamp}.db"
    dst = sqlite3.connect(str(tmp_sql))
    conn = sqlite3.connect(str(src))
    conn.backup(dst)
    conn.close()
    dst.close()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp_sql, arcname=f"{src.stem}.db")
        tmp_sql.unlink()
        for item in items:
            p = Path(item)
            if not p.exists():
                continue
            if p.is_file():
                z.write(p, arcname=str(p))
            else:
                for f in p.rglob("*"):
                    if f.is_file() and "__pycache__" not in str(f):
                        z.write(f, arcname=str(f))
    prune(out_dir, keep)
    kept = len(list(out_dir.glob("backup_*.zip")))
    copied_to = None
    if dest_dir:
        d = Path(dest_dir)
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(zip_path, d / zip_path.name)
        copied_to = str(d / zip_path.name)
    return {"zip": str(zip_path), "size": zip_path.stat().st_size,
            "kept": kept, "copied_to": copied_to}


def prune(out_dir: str | Path, keep: int) -> int:
    zips = sorted(Path(out_dir).glob("backup_*.zip"), key=lambda p: p.name)
    removed = 0
    for old in zips[:-keep] if keep > 0 else zips:
        old.unlink()
        removed += 1
    return removed


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="boss_secretary.backup")
    p.add_argument("--db", default="data/secretary.db")
    p.add_argument("--out", default="data/backups")
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--dest", default=None, help="第二副本目录（NAS/外置盘）")
    args = p.parse_args(argv)
    r = backup(args.db, out_dir=args.out, keep=args.keep, dest_dir=args.dest)
    print(f"备份完成: {r['zip']} ({r['size'] / 1024:.0f} KB) | "
          f"保留 {r['kept']} 份" + (f" | 副本 {r['copied_to']}" if r["copied_to"] else ""))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
