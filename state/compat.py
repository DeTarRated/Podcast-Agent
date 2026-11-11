"""跨 SQLite / PostgreSQL 的 SQL 兼容工具。"""
from __future__ import annotations

import sqlite3


def id_in(conn, column: str, ids: list[int]):
    """返回 (SQL 片段, 参数)；空列表始终不匹配。"""
    if not ids:
        return "1 = 0", ()
    if isinstance(conn, sqlite3.Connection):
        placeholders = ",".join("?" for _ in ids)
        return f"{column} IN ({placeholders})", tuple(ids)
    return f"{column} = ANY(%s)", (ids,)
