"""产物清单最小读写封装。"""
import sqlite3
from typing import Optional


def add(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    step: str,
    kind: str,
    path: str,
    version: int = 1,
    sha256: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO artifact (episode_id, step, kind, path, version, sha256)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (episode_id, step, kind, path, version, sha256),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_for(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM artifact WHERE episode_id = ? ORDER BY id",
        (episode_id,),
    ).fetchall()


def finish(
    conn: sqlite3.Connection,
    artifact_id: int,
    *,
    sha256: Optional[str] = None,
) -> None:
    """标记产物完成，可顺带补校验值。"""
    if sha256 is None:
        conn.execute(
            "UPDATE artifact SET status = 'done' WHERE id = ?", (artifact_id,)
        )
    else:
        conn.execute(
            "UPDATE artifact SET status = 'done', sha256 = ? WHERE id = ?",
            (sha256, artifact_id),
        )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, artifact_id: int) -> None:
    conn.execute(
        "UPDATE artifact SET status = 'failed' WHERE id = ?", (artifact_id,)
    )
    conn.commit()
