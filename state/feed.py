"""跨层数据契约·RSS feed 配置读写；禁用不影响在途 Episode。"""
from __future__ import annotations

import sqlite3
from typing import Optional
from urllib.parse import urlparse

from state.episode import utcnow_iso


def _validate_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
        raise ValueError("feedUrl 必须是不含内嵌凭据的 HTTP(S) URL")
    return url


def add(
    conn: sqlite3.Connection,
    *,
    name: str,
    feed_url: str,
    show_name: Optional[str] = None,
    now: Optional[str] = None,
    process_existing: bool = False,
) -> int:
    if not name or not name.strip():
        raise ValueError("feed 名称不能为空")
    timestamp = now or utcnow_iso()
    cur = conn.execute(
        """
        INSERT INTO feed (name, feed_url, show_name, bootstrap_pending, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name.strip(), _validate_url(feed_url), show_name, int(not process_existing), timestamp),
    )
    conn.commit()
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, feed_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM feed WHERE id = ?", (feed_id,)).fetchone()


def list_feeds(
    conn: sqlite3.Connection, *, include_disabled: bool = True
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM feed"
    if not include_disabled:
        sql += " WHERE enabled = 1"
    return conn.execute(sql + " ORDER BY id").fetchall()


def disable(conn: sqlite3.Connection, feed_id: int, *, now: Optional[str] = None) -> None:
    timestamp = now or utcnow_iso()
    conn.execute(
        "UPDATE feed SET enabled = 0, updated_at = ? WHERE id = ?",
        (timestamp, feed_id),
    )
    conn.commit()


def record_success(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    etag: Optional[str],
    last_modified: Optional[str],
    now: str,
) -> None:
    conn.execute(
        """
        UPDATE feed SET etag = ?, last_modified = ?, last_checked_at = ?,
                        bootstrap_pending = 0, last_error = NULL, updated_at = ?
        WHERE id = ?
        """,
        (etag, last_modified, now, now, feed_id),
    )
    conn.commit()


def record_error(conn: sqlite3.Connection, feed_id: int, error: str, now: str) -> None:
    conn.execute(
        "UPDATE feed SET last_checked_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
        (now, error[:1000], now, feed_id),
    )
    conn.commit()
