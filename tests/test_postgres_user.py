"""真实 PostgreSQL 多用户账号、会话、授权与预算测试。

模拟场景：
- Argon2 密码哈希与错误密码、重复 email
- 会话校验、错误 token、过期与撤销
- owner/admin 放行、普通用户按 Episode 授权
- 预算 upsert 与查询、审计 JSONB 追加

默认跳过；设置 PODCAST_TEST_POSTGRES=1 且本地测试库可用时运行。
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.errors import UniqueViolation

from state.postgres import db, migrations
from state.postgres.user import (
    audit, authenticate, budget_for, can_access, create_session,
    create_user, grant_permission, revoke_session, set_budget, validate_session,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("PODCAST_TEST_POSTGRES") != "1",
    reason="需要显式启用本地 PostgreSQL 测试库",
)

START = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    connection = db.connect()
    migrations.migrate(connection)
    connection.execute("TRUNCATE session, resource_grant, audit_log, budget_limit, user_account CASCADE")
    connection.commit()
    yield connection
    connection.close()


def test_password_hash_and_authentication(conn):
    user = create_user(conn, email="Owner@Example.com", password="correct-horse", role="owner")
    assert user["email"] == "owner@example.com"
    stored = conn.execute("SELECT password_hash FROM user_account WHERE id=%s", (user["id"],)).fetchone()
    assert stored["password_hash"] != "correct-horse"
    assert stored["password_hash"].startswith("$argon2id$")
    assert authenticate(conn, email="OWNER@example.com", password="correct-horse")["id"] == user["id"]
    assert authenticate(conn, email="owner@example.com", password="wrong-password") is None
    with pytest.raises(UniqueViolation):
        create_user(conn, email="owner@example.com", password="different-password", role="viewer")


def test_session_expiry_and_revoke(conn):
    user = create_user(conn, email="viewer@example.com", password="viewer-password", role="viewer")
    session = create_session(conn, user_id=user["id"], now=START)
    assert "token_hash" not in session["token"]
    assert validate_session(conn, token=session["token"], now=START)["id"] == user["id"]
    assert validate_session(conn, token="invalid-token", now=START) is None
    assert validate_session(conn, token=session["token"], now=START + timedelta(days=8)) is None
    assert revoke_session(conn, token=session["token"]) is True
    assert validate_session(conn, token=session["token"], now=START) is None


def test_resource_grant_and_role_access(conn):
    owner = create_user(conn, email="owner@example.com", password="owner-password", role="owner")
    viewer = create_user(conn, email="viewer@example.com", password="viewer-password", role="viewer")
    assert can_access(conn, user_id=owner["id"], resource_type="episode", resource_id=99) is True
    assert can_access(conn, user_id=viewer["id"], resource_type="episode", resource_id=99) is False
    grant_permission(conn, user_id=viewer["id"], resource_type="episode", resource_id=99, permission="read")
    assert can_access(conn, user_id=viewer["id"], resource_type="episode", resource_id=99) is True
    assert can_access(conn, user_id=viewer["id"], resource_type="episode", resource_id=100) is False


def test_budget_and_audit(conn):
    user = create_user(conn, email="viewer@example.com", password="viewer-password", role="viewer")
    budget = set_budget(
        conn, scope="user", period="day", user_id=user["id"], max_amount=2.5, max_running=2,
    )
    assert budget["max_running"] == 2
    assert budget_for(conn, scope="user", period="day", user_id=user["id"])["id"] == budget["id"]
    set_budget(conn, scope="user", period="day", user_id=user["id"], max_amount=3.0, max_running=1)
    updated = budget_for(conn, scope="user", period="day", user_id=user["id"])
    assert updated["max_running"] == 1
    audit(
        conn, user_id=user["id"], action="approve", resource_type="episode",
        resource_id=99, version="abc123", result="success", details={"method": "manual"},
    )
    row = conn.execute("SELECT * FROM audit_log WHERE resource_id=99").fetchone()
    assert row["details"] == {"method": "manual"}
