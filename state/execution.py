"""跨层数据契约·有界调度的 Episode 执行租约与待处理查询。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

DEFAULT_LEASE_SECONDS = 6 * 3600


def _time(value: Optional[str]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def acquire(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    owner: str,
    now: Optional[str] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict:
    if not owner.strip() or lease_seconds <= 0:
        raise ValueError("owner 与 lease_seconds 必须有效")
    current = _time(now)
    acquired_at = _iso(current)
    expires_at = _iso(current + timedelta(seconds=lease_seconds))
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM execution_lock WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO execution_lock (episode_id, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                (episode_id, owner, acquired_at, expires_at),
            )
            acquired, reused = True, False
        elif _time(row["expires_at"]) <= current:
            conn.execute(
                "UPDATE execution_lock SET owner = ?, acquired_at = ?, expires_at = ? WHERE episode_id = ?",
                (owner, acquired_at, expires_at, episode_id),
            )
            acquired, reused = True, False
        elif row["owner"] == owner:
            conn.execute(
                "UPDATE execution_lock SET expires_at = ? WHERE episode_id = ? AND owner = ?",
                (expires_at, episode_id, owner),
            )
            acquired, reused = True, True
        else:
            acquired, reused = False, False
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "episode_id": episode_id,
        "acquired": acquired,
        "reused": reused,
        "owner": owner,
        "acquired_at": acquired_at,
        "expires_at": expires_at,
    }


def heartbeat(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    owner: str,
    now: Optional[str] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    current = _time(now)
    expires = _iso(current + timedelta(seconds=lease_seconds))
    cur = conn.execute(
        "UPDATE execution_lock SET expires_at = ? WHERE episode_id = ? AND owner = ? AND expires_at > ?",
        (expires, episode_id, owner, _iso(current)),
    )
    conn.commit()
    return cur.rowcount == 1


def release(conn: sqlite3.Connection, *, episode_id: int, owner: str) -> bool:
    cur = conn.execute(
        "DELETE FROM execution_lock WHERE episode_id = ? AND owner = ?",
        (episode_id, owner),
    )
    conn.commit()
    return cur.rowcount == 1


def pending_episodes(conn: sqlite3.Connection, *, limit: int = 10) -> list[sqlite3.Row]:
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1..100")
    return conn.execute(
        """
        SELECT id, guid, feed, title, published_at, enclosure
        FROM episode WHERE status = 'discovered'
        ORDER BY published_at IS NULL, published_at ASC, id ASC LIMIT ?
        """,
        (limit,),
    ).fetchall()
