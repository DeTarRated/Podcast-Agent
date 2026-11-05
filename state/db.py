"""SQLite 连接与 schema 管理。

垂直切片采用 stdlib sqlite3 最小封装；SQLite 驱动正式选型
（stdlib sqlite3 / SQLAlchemy）按 TECH_STACK 排在自动化主链定稿。
"""
import os
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | os.PathLike) -> sqlite3.Connection:
    """打开连接并应用本项目约定：WAL、外键开启、锁等待。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等建表（IF NOT EXISTS）。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def default_db_path() -> str:
    """环境变量 PODCAST_AGENT_DB 优先，默认 var/podcast_agent.db（var/ 不入 git）。"""
    return os.environ.get("PODCAST_AGENT_DB", "var/podcast_agent.db")


def open_default() -> sqlite3.Connection:
    path = Path(default_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    init_schema(conn)
    return conn
