"""审核层·审核箱、详情与本地媒体只读查询入口。"""
from __future__ import annotations

import sqlite3

from review import service


def inbox(conn: sqlite3.Connection, *, allowed_ids=None) -> dict:
    result = service.inbox(conn)
    if allowed_ids is not None:
        allowed = set(allowed_ids)
        result["items"] = [item for item in result["items"] if item["id"] in allowed]
        result["count"] = len(result["items"])
    return result


def detail(conn: sqlite3.Connection, episode_id: int, *, allowed_ids=None) -> dict:
    if allowed_ids is not None and episode_id not in set(allowed_ids):
        raise service.ReviewError("not_found", "节目不存在或无权访问")
    return service.detail(conn, episode_id=episode_id)


def media_info(conn: sqlite3.Connection, episode_id: int) -> dict:
    return service.media_info(conn, episode_id=episode_id)
