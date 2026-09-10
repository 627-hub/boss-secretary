"""数据访问共用件：时间戳 / ID / 行解码 / 取行 / 字段更新。

各业务模块此前各自实现 _now/_id 与 dict(zip(description, row)) 行解码，
UPDATE ... SET 也逐处手写；这里收敛为唯一实现，避免复制与格式漂移。

安全：表名/列名只接受标识符（内部常量），非法即抛 ValueError，杜绝拼接注入。
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def now() -> str:
    """本地时间 ISO 秒级字符串（各模块 updated_at/created_at 统一格式）。"""
    return dt.datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """业务单号：前缀 + 日期 + 6 位随机十六进制，如 C20260910-A1B2C3。"""
    return f"{prefix}{dt.date.today():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def row_to_dict(cursor, row) -> dict:
    return dict(zip([c[0] for c in cursor.description], row))


def fetch_one(conn, table: str, id_col: str, id_val: Any) -> dict | None:
    """按主键取一行并解码为 dict；不存在返回 None。"""
    table, id_col = _ident(table), _ident(id_col)
    cur = conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (id_val,))
    row = cur.fetchone()
    return row_to_dict(cur, row) if row is not None else None


def update_fields(conn, table: str, id_col: str, id_val: Any, *,
                  commit: bool = True, **fields: Any) -> None:
    """按主键更新指定字段；无字段时为空操作。"""
    if not fields:
        return
    table, id_col = _ident(table), _ident(id_col)
    cols = list(fields)
    for c in cols:
        _ident(c)
    sets = ", ".join(f"{c}=?" for c in cols)
    conn.execute(f"UPDATE {table} SET {sets} WHERE {id_col}=?",
                 (*[fields[c] for c in cols], id_val))
    if commit:
        conn.commit()


def _ident(name: str) -> str:
    if not _IDENT.match(str(name)):
        raise ValueError(f"非法标识符: {name!r}")
    return name
