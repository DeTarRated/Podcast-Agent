"""交付层·CLI 与长驻内部服务共用的命令分发。"""
from __future__ import annotations

from delivery import library, publish
from delivery.errors import DeliveryError
from state.postgres.user import allowed_resource_ids, can_access


def _allowed_ids(conn, payload):
    user_id = payload.get("_user_id")
    if user_id is None:
        return None
    return allowed_resource_ids(conn, user_id=user_id, resource_type="episode", permission="read")


def dispatch(conn, command: str, payload: dict, *, now: str, store=None) -> dict:
    if command == "publish-approved":
        if payload.get("_user_id") is not None and payload.get("_user_role") not in ("owner", "admin"):
            raise DeliveryError("invalid_state", "发布只允许管理员")
        return publish.publish_approved(conn, now=now, store=store)
    if command == "mark-highlights-failed":
        if payload.get("_user_id") is not None and payload.get("_user_role") not in ("owner", "admin"):
            raise DeliveryError("invalid_state", "发布失败处理只允许管理员")
        return publish.mark_highlights_failed(
            conn, approval_id=payload["approval_id"], error=payload["error"], now=now
        )
    if command == "library":
        return library.list_episodes(conn, allowed_ids=_allowed_ids(conn, payload))
    if command == "detail":
        user_id = payload.get("_user_id")
        if user_id is not None and not can_access(
            conn,
            user_id=user_id,
            resource_type="episode",
            resource_id=payload["episode_id"],
            permission="read",
        ):
            raise DeliveryError("not_found", "节目不存在或无权访问")
        return library.episode_detail(
            conn, episode_id=payload["episode_id"], store=store,
            allowed_ids=_allowed_ids(conn, payload),
        )
    raise ValueError(f"未知 delivery command: {command}")
