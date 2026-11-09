"""跨层数据契约·审核修订读写。

只做持久化与当前指针操作；审批、override 锚点和时间窗口规则归 review.service。
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def current_for(conn: sqlite3.Connection, episode_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_revision WHERE episode_id = ? AND is_current = 1",
        (episode_id,),
    ).fetchone()


def get_revision(
    conn: sqlite3.Connection, revision_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_revision WHERE id = ?", (revision_id,)
    ).fetchone()


def create_current_revision(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    base_artifact_id: int,
    rendered_artifact_id: int,
    variant: str,
    reviewing_since: str,
    auto_approve_at: Optional[str],
    auto_approve_paused: bool,
    commit: bool = True,
) -> tuple[int, bool]:
    """创建当前审核修订，返回 (revision_id, reused)；由调用方控制外层事务。"""
    artifacts = conn.execute(
        """
        SELECT id FROM artifact
        WHERE episode_id = ? AND status = 'done' AND id IN (?, ?)
        """,
        (episode_id, base_artifact_id, rendered_artifact_id),
    ).fetchall()
    if {int(row[0]) for row in artifacts} != {base_artifact_id, rendered_artifact_id}:
        raise ValueError("base/rendered artifact 必须属于同一 Episode 且为 done")
    if variant not in ("review", "split"):
        raise ValueError("未知分条版本")
    if auto_approve_paused:
        if auto_approve_at is not None:
            raise ValueError("暂停审核时 auto_approve_at 必须为 NULL")
    elif auto_approve_at is None:
        raise ValueError("未暂停审核时必须提供 auto_approve_at")

    current = current_for(conn, episode_id)
    if (
        current is not None
        and current["status"] == "reviewing"
        and current["base_artifact_id"] == base_artifact_id
        and current["rendered_artifact_id"] == rendered_artifact_id
        and current["variant"] == variant
    ):
        return int(current["id"]), True

    if current is not None:
        updated = conn.execute(
            """
            UPDATE review_revision
            SET is_current = 0, status = 'superseded'
            WHERE id = ? AND is_current = 1
            """,
            (current["id"],),
        )
        if updated.rowcount != 1:
            raise ValueError("当前审核修订已变化")

    cur = conn.execute(
        """
        INSERT INTO review_revision (
            episode_id, base_artifact_id, rendered_artifact_id, variant,
            status, is_current, reviewing_since, auto_approve_at,
            auto_approve_paused, supersedes_revision_id
        ) VALUES (?, ?, ?, ?, 'reviewing', 1, ?, ?, ?, ?)
        """,
        (
            episode_id,
            base_artifact_id,
            rendered_artifact_id,
            variant,
            reviewing_since,
            auto_approve_at,
            int(auto_approve_paused),
            current["id"] if current is not None else None,
        ),
    )
    conn.execute(
        "UPDATE episode SET status = 'reviewing', updated_at = ? WHERE id = ?",
        (reviewing_since, episode_id),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid), False
