"""长驻 Python 领域 RPC 测试；使用临时 SQLite，零外部付费调用。

模拟场景：
- 启动时加载配置并只执行一次迁移，health 可访问
- RPC 必须有内部令牌，未知 command 返回 not_found
- 合法 inbox 复用现有查询并返回 success
- 领域错误保持 JSON code，而非进程退出
"""
import os
from pathlib import Path

from fastapi.testclient import TestClient


def test_domain_rpc_auth_health_and_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("PODCAST_DOMAIN_TOKEN", "internal-test-token")
    monkeypatch.setenv("PODCAST_AGENT_DB", str(tmp_path / "domain.db"))
    from review.api import app

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        denied = client.post("/rpc/review/inbox", json={})
        assert denied.status_code == 401
        response = client.post(
            "/rpc/review/inbox", json={}, headers={"X-Domain-Token": "internal-test-token"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["data"]["count"] == 0
        unknown = client.post(
            "/rpc/review/not-a-command", json={}, headers={"X-Domain-Token": "internal-test-token"}
        )
        assert unknown.json()["error"]["code"] == "not_found"
        assert Path(os.environ["PODCAST_AGENT_DB"]).exists()
