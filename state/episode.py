"""Episode 最小读写封装。"""
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(
    conn: sqlite3.Connection,
    *,
    guid: str,
    feed: str,
    feed_snapshot: Optional[str] = None,
    title: Optional[str] = None,
    published_at: Optional[str] = None,
    duration_secs: Optional[float] = None,
    enclosure: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO episode (guid, feed, feed_snapshot, title, published_at,
                             duration_secs, enclosure)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (guid, feed, feed_snapshot, title, published_at, duration_secs, enclosure),
    )
    conn.commit()
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, episode_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM episode WHERE id = ?", (episode_id,)
    ).fetchone()


def get_by_guid_feed(conn: sqlite3.Connection, guid: str, feed: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM episode WHERE guid = ? AND feed = ?", (guid, feed)
    ).fetchone()


def update_status(
    conn: sqlite3.Connection,
    episode_id: int,
    status: str,
    *,
    commit: bool = True,
) -> None:
    """状态推进；领域事务传 commit=False，由调用方统一提交。"""
    conn.execute(
        "UPDATE episode SET status = ?, updated_at = ? WHERE id = ?",
        (status, utcnow_iso(), episode_id),
    )
    if commit:
        conn.commit()


def set_audio(
    conn: sqlite3.Connection,
    episode_id: int,
    sha256: str,
    duration_secs: Optional[float] = None,
) -> None:
    """下载固化后写入音频 hash（首次版本）与时长。"""
    conn.execute(
        "UPDATE episode SET audio_sha256 = ?, duration_secs = ?, updated_at = ? WHERE id = ?",
        (sha256, duration_secs, utcnow_iso(), episode_id),
    )
    conn.commit()
