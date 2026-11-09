"""跨层数据契约·交付与当前发布只读辅助。"""
from __future__ import annotations

import sqlite3
from typing import Optional


def current_for(conn: sqlite3.Connection, episode_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM delivery WHERE episode_id = ? AND is_current = 1 AND status = 'delivered'",
        (episode_id,),
    ).fetchone()
