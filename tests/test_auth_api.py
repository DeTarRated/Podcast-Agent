"""多用户登录、会话与授权 RPC 测试；PostgreSQL 独立库 + fake 登录限流。

模拟场景：
- 正确账号登录返回一次性 token，错误密码拒绝
- 会话查询、退出后失效
- 内部 grant-check 必须携带领域令牌

默认跳过；需 PODCAST_TEST_POSTGRES=1 和本地 PostgreSQL 测试库。
"""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from state.postgres import db, migrations
from state.postgres.user import create_user, grant_permission

pytestmark = pytest.mark.skipif(
    os.environ.get("PODCAST_TEST_POSTGRES") != "1",
    reason="需要显式启用本地 PostgreSQL 测试库",
)


def test_login_session_logout_and_grant_check(tmp_path, monkeypatch):
    monkeypatch.setenv("PODCAST_DOMAIN_TOKEN", "internal-token")
    monkeypatch.setenv("PODCAST_AGENT_DB", str(tmp_path / "domain.db")
    )
    conn = db.connect()
    migrations.migrate(conn)
    conn.execute("TRUNCATE session, resource_grant, audit_log, budget_limit, user_account CASCADE")
    user = create_user(conn, email="viewer@example.com", password="viewer-password", role="viewer")
    grant_permission(conn, user_id=user["id"], resource_type="episode", resource_id=99, permission="read")
    conn.close()

    class FakeGate:
        def acquire(self, provider, **kwargs):
            return SimpleNamespace(allowed=True, retry_after_ms=0)

    from review import auth_api
    monkeypatch.setattr(auth_api.ProviderGate, "from_env", classmethod(lambda cls: FakeGate()))
    from review.api import app

    with TestClient(app) as client:
        bad = client.post("/auth/login", json={"email": "viewer@example.com", "password": "bad"})
        assert bad.json()["error"]["code"] == "unauthorized"
        response = client.post(
            "/auth/login", json={"email": "viewer@example.com", "password": "viewer-password"}
        )
        assert response.status_code == 200
        token = response.json()["data"]["token"]
        session = client.post("/auth/session", json={"token": token})
        assert session.json()["data"]["email"] == "viewer@example.com"
        checked = client.post(
            "/auth/grant-check",
            json={"user_id": user["id"], "resource_type": "episode", "resource_id": 99},
            headers={"X-Domain-Token": "internal-token"},
        )
        assert checked.json()["data"]["allowed"] is True
        client.post("/auth/logout", json={"token": token})
        assert client.post("/auth/session", json={"token": token}).json()["status"] == "failed"
