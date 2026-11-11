"""PostgreSQL 多用户账号、会话、授权、审计与预算实现。"""
from __future__ import annotations

import hashlib
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from psycopg.types.json import Jsonb

from state.postgres.time import _now

_PASSWORD = PasswordHasher()
ROLES = {"owner", "admin", "reviewer", "viewer"}
PERMISSIONS = {"read", "review", "approve", "publish", "admin"}
RESOURCE_TYPES = {"feed", "episode", "delivery", "ledger"}


class AuthError(RuntimeError):
    """账号、会话或授权输入不合法。"""


def _execute(conn, statement: str, params=()):
    if not isinstance(conn, sqlite3.Connection):
        return conn.execute(statement, params)
    # 账号、会话与授权归 PostgreSQL；Episode 业务查询仍归 SQLite。
    auth_conn = __import__("state.postgres.db", fromlist=["connect"]).connect()
    try:
        result = auth_conn.execute(statement, params)
        if statement.lstrip().upper().startswith("SELECT"):
            return result
        auth_conn.commit()
        return result
    finally:
        auth_conn.close()


def _email(value: str) -> str:
    email = value.strip().lower()
    if not email or len(email) > 320 or "@" not in email:
        raise AuthError("email 格式非法")
    return email


def _password(value: str) -> str:
    if not isinstance(value, str) or not 10 <= len(value) <= 200:
        raise AuthError("密码长度必须在 10..200 字符之间")
    return value


def _public_user(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "status": row["status"],
    }


def create_user(conn, *, email: str, password: str, role: str = "viewer") -> dict:
    email = _email(email)
    if role not in ROLES:
        raise AuthError("未知角色")
    password_hash = _PASSWORD.hash(_password(password))
    with conn.transaction():
        row = _execute(conn,
            """
            INSERT INTO user_account (email, password_hash, role)
            VALUES (%s,%s,%s)
            RETURNING id,email,role,status
            """,
            (email, password_hash, role),
        ).fetchone()
    conn.commit()
    return _public_user(row)


def authenticate(conn, *, email: str, password: str) -> Optional[dict]:
    try:
        normalized = _email(email)
    except AuthError:
        return None
    row = _execute(conn,
        "SELECT * FROM user_account WHERE email = %s", (normalized,)
    ).fetchone()
    if row is None or row["status"] != "active":
        return None
    try:
        _PASSWORD.verify(row["password_hash"], password)
    except VerifyMismatchError:
        return None
    return _public_user(row)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    conn,
    *,
    user_id: int,
    now: Optional[str | datetime] = None,
    ttl_seconds: int = 7 * 24 * 3600,
) -> dict:
    current = _now(now)
    if ttl_seconds <= 0:
        raise AuthError("会话 TTL 必须为正数")
    user = _execute(conn,
        "SELECT id,email,role,status FROM user_account WHERE id = %s", (user_id,)
    ).fetchone()
    if user is None or user["status"] != "active":
        raise AuthError("用户不存在或已禁用")
    token = secrets.token_urlsafe(32)
    expires = current + timedelta(seconds=ttl_seconds)
    _execute(conn,
        """
        INSERT INTO session (user_id, token_hash, expires_at, last_used_at)
        VALUES (%s,%s,%s,%s)
        """,
        (user_id, _token_hash(token), expires, current),
    )
    conn.commit()
    return {"token": token, "expires_at": expires, "user": _public_user(user)}


def validate_session(
    conn, *, token: str, now: Optional[str | datetime] = None
) -> Optional[dict]:
    current = _now(now)
    row = _execute(conn,
        """
        SELECT s.id AS session_id, u.id, u.email, u.role, u.status
        FROM session s JOIN user_account u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > %s
        """,
        (_token_hash(token), current),
    ).fetchone()
    if row is None or row["status"] != "active":
        return None
    with conn.transaction():
        _execute(conn,
            "UPDATE session SET last_used_at = %s WHERE id = %s",
            (current, row["session_id"]),
        )
    conn.commit()
    return _public_user(row)


def revoke_session(conn, *, token: str) -> bool:
    with conn.transaction():
        row = _execute(conn,
            "DELETE FROM session WHERE token_hash = %s RETURNING id", (_token_hash(token),)
        ).fetchone()
    conn.commit()
    return row is not None


def grant_permission(
    conn,
    *,
    user_id: int,
    resource_type: str,
    resource_id: int,
    permission: str,
) -> dict:
    if resource_type not in RESOURCE_TYPES or permission not in PERMISSIONS:
        raise AuthError("资源类型或权限非法")
    row = _execute(conn,
        """
        INSERT INTO resource_grant (user_id, resource_type, resource_id, permission)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (user_id, resource_type, resource_id, permission)
            DO UPDATE SET created_at = resource_grant.created_at
            RETURNING *
            """,
            (user_id, resource_type, resource_id, permission),
        ).fetchone()
    conn.commit()
    return row


def allowed_resource_ids(
    conn,
    *,
    user_id: int,
    resource_type: str,
    permission: str = "read",
) -> Optional[list[int]]:
    """返回列表过滤用资源 id；owner/admin 返回 None 表示不过滤。"""
    user = _execute(conn,
        "SELECT role,status FROM user_account WHERE id = %s", (user_id,)
    ).fetchone()
    if user is None or user["status"] != "active":
        return []
    if user["role"] in ("owner", "admin"):
        return None
    rows = _execute(conn,
        """
        SELECT resource_id FROM resource_grant
        WHERE user_id = %s AND resource_type = %s
          AND permission IN (%s, 'admin')
        ORDER BY resource_id
        """,
        (user_id, resource_type, permission),
    ).fetchall()
    return [int(row["resource_id"]) for row in rows]


def can_access(
    conn,
    *,
    user_id: int,
    resource_type: str,
    resource_id: int,
    permission: str = "read",
) -> bool:
    user = _execute(conn,
        "SELECT role,status FROM user_account WHERE id = %s", (user_id,)
    ).fetchone()
    if user is None or user["status"] != "active":
        return False
    if user["role"] in ("owner", "admin"):
        return True
    row = _execute(conn,
        """
        SELECT 1 FROM resource_grant
        WHERE user_id = %s AND resource_type = %s AND resource_id = %s
          AND permission IN (%s, 'admin') LIMIT 1
        """,
        (user_id, resource_type, resource_id, permission),
    ).fetchone()
    return row is not None


def audit(
    conn,
    *,
    user_id: Optional[int],
    action: str,
    resource_type: str,
    resource_id: int,
    result: str,
    version: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    if result not in {"success", "failed", "rejected"}:
        raise AuthError("审计结果非法")
    with conn.transaction():
        _execute(conn,
            """
            INSERT INTO audit_log (user_id, action, resource_type, resource_id, version, result, details)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (user_id, action, resource_type, resource_id, version, result, Jsonb(details) if details is not None else None),
        )
    conn.commit()


def set_budget(
    conn,
    *,
    scope: str,
    period: str,
    user_id: Optional[int] = None,
    episode_id: Optional[int] = None,
    max_amount=None,
    max_running: Optional[int] = None,
) -> dict:
    if scope not in {"global", "user", "episode"} or period not in {"task", "day", "month"}:
        raise AuthError("预算 scope 或 period 非法")
    if max_amount is None and max_running is None:
        raise AuthError("预算至少限制金额或 running 数量之一")
    if max_running is not None and max_running <= 0:
        raise AuthError("max_running 必须为正数")
    with conn.transaction():
        row = _execute(conn,
            """
            INSERT INTO budget_limit (scope, user_id, episode_id, period, max_amount, max_running)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (scope, COALESCE(user_id, 0), COALESCE(episode_id, 0), period)
            DO UPDATE SET max_amount = EXCLUDED.max_amount, max_running = EXCLUDED.max_running,
                          updated_at = now()
            RETURNING *
            """,
            (scope, user_id, episode_id, period, max_amount, max_running),
        ).fetchone()
    conn.commit()
    return row


def budget_for(
    conn,
    *,
    scope: str,
    period: str,
    user_id: Optional[int] = None,
    episode_id: Optional[int] = None,
) -> Optional[dict]:
    return _execute(conn,
        """
        SELECT * FROM budget_limit
        WHERE scope = %s AND period = %s
          AND user_id IS NOT DISTINCT FROM %s
          AND episode_id IS NOT DISTINCT FROM %s
          AND enabled = TRUE LIMIT 1
        """,
        (scope, period, user_id, episode_id),
    ).fetchone()
