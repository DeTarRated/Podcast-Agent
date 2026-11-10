"""跨层数据契约·发现候选去重与 Episode 原子入队。"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from state import episode


def get_candidate(
    conn: sqlite3.Connection, feed_id: int, guid: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM discovery_candidate WHERE feed_id = ? AND guid = ?",
        (feed_id, guid),
    ).fetchone()


def insert_candidate(
    conn: sqlite3.Connection,
    *,
    feed_id: int,
    guid: str,
    title: Optional[str],
    published_at: Optional[str],
    enclosure: Optional[str],
    summary: Optional[str],
    now: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO discovery_candidate (
            feed_id, guid, title, published_at, enclosure, summary, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (feed_id, guid, title, published_at, enclosure, summary, now),
    )
    return int(cur.lastrowid)


def mark_candidate(
    conn: sqlite3.Connection, candidate_id: int, *, status: str, reason: Optional[str], now: str
) -> None:
    conn.execute(
        "UPDATE discovery_candidate SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
        (status, reason, now, candidate_id),
    )


def enqueue_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    feed: sqlite3.Row,
    now: str,
    commit: bool = True,
) -> tuple[int, bool]:
    """返回 (episode_id, reused)；调用方可在外层事务中保持候选与 Episode 原子。"""
    candidate = conn.execute(
        "SELECT * FROM discovery_candidate WHERE id = ?", (candidate_id,)
    ).fetchone()
    if candidate is None:
        raise ValueError("候选不存在")
    if candidate["episode_id"] is not None and candidate["status"] == "enqueued":
        return int(candidate["episode_id"]), True

    snapshot = json.dumps(
        {
            "id": feed["id"],
            "name": feed["name"],
            "feed_url": feed["feed_url"],
            "show_name": feed["show_name"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    existing = episode.get_by_guid_feed(conn, candidate["guid"], feed["feed_url"])
    if existing is not None:
        episode_id = int(existing["id"])
        reused = True
    else:
        episode_id = episode.create(
            conn,
            guid=candidate["guid"],
            feed=feed["feed_url"],
            feed_snapshot=snapshot,
            title=candidate["title"],
            published_at=candidate["published_at"],
            enclosure=candidate["enclosure"],
            commit=False,
        )
        reused = False
    conn.execute(
        """
        UPDATE discovery_candidate
        SET status = 'enqueued', reason = NULL, episode_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (episode_id, now, candidate_id),
    )
    if commit:
        conn.commit()
    return episode_id, reused
