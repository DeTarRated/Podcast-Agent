"""审核层·审核箱、详情与本地媒体只读查询入口。"""
from __future__ import annotations

import sqlite3

from review import service


def inbox(conn: sqlite3.Connection) -> dict:
    return service.inbox(conn)


def detail(conn: sqlite3.Connection, episode_id: int) -> dict:
    return service.detail(conn, episode_id=episode_id)


def media_info(conn: sqlite3.Connection, episode_id: int) -> dict:
    return service.media_info(conn, episode_id=episode_id)
