"""真实 PostgreSQL 预算执行与背压测试；零外部付费调用。

模拟场景：
- 用户日预算内允许，超限任务失败关闭且不增加 attempts
- 最大 running 数量触发 retry 背压，释放后可继续
- 未知预估成本失败关闭，任务级预算超限拒绝
- 预算拦截不创建 job_attempt

默认跳过；需 PODCAST_TEST_POSTGRES=1 与本地测试库。
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from state.postgres import db, job, migrations
from state.postgres.user import create_user, grant_permission, set_budget

pytestmark = pytest.mark.skipif(
    os.environ.get("PODCAST_TEST_POSTGRES") != "1",
    reason="需要显式启用本地 PostgreSQL 测试库",
)

START = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    connection = db.connect()
    migrations.migrate(connection)
    connection.execute("TRUNCATE job_attempt, job, ledger, resource_grant, budget_limit, user_account, episode CASCADE")
    connection.commit()
    yield connection
    connection.close()


def _setup(conn):
    user = create_user(conn, email="budget@example.com", password="budget-password", role="viewer")
    episode_id = conn.execute(
        "INSERT INTO episode (guid,feed) VALUES ('budget-guid','feed') RETURNING id"
    ).fetchone()["id"]
    grant_permission(conn, user_id=user["id"], resource_type="episode", resource_id=episode_id, permission="read")
    return user, episode_id


def _job(conn, user, episode_id, key, estimate=None, budget=None):
    return job.enqueue_job(
        conn,
        kind="transcribe",
        episode_id=episode_id,
        user_id=user["id"],
        payload={} if estimate is None else {"estimated_cost": estimate},
        idempotency_key=key,
        available_at=START,
        budget=budget,
    )["job"]


def test_budget_exceeded_blocks_without_consuming_attempt(conn):
    user, episode_id = _setup(conn)
    set_budget(conn, scope="user", period="day", user_id=user["id"], max_amount=Decimal("0.5"))
    conn.execute(
        """
        INSERT INTO ledger (episode_id,purpose,amount_est,call_ref,status,triggered_at)
        VALUES (%s,'transcribe',%s,'existing','success',%s)
        """,
        (episode_id, Decimal("0.2"), START),
    )
    conn.commit()
    allowed = _job(conn, user, episode_id, "allowed", estimate=Decimal("0.2"))
    assert job.claim_job(conn, worker_id="a", now=START)["job"]["id"] == allowed["id"]
    blocked = _job(conn, user, episode_id, "blocked", estimate=Decimal("0.4"))
    assert job.claim_job(conn, worker_id="b", now=START) is None
    current = job.get_job(conn, blocked["id"])
    assert current["status"] == "failed"
    assert current["last_error"] == "budget_exceeded"
    assert current["attempts"] == 0
    assert conn.execute("SELECT COUNT(*) AS count FROM job_attempt WHERE job_id=%s", (blocked["id"],)).fetchone()["count"] == 0


def test_max_running_backpressure_releases_after_completion(conn):
    user, episode_id = _setup(conn)
    set_budget(conn, scope="user", period="day", user_id=user["id"], max_running=1)
    first = _job(conn, user, episode_id, "first", estimate=Decimal("0.1"))
    second = _job(conn, user, episode_id, "second", estimate=Decimal("0.1"))
    assert job.claim_job(conn, worker_id="a", now=START)["job"]["id"] == first["id"]
    assert job.claim_job(conn, worker_id="b", now=START) is None
    current = job.get_job(conn, second["id"])
    assert current["status"] == "retry"
    assert current["last_error"] == "max_running"
    assert current["attempts"] == 0
    job.complete_job(conn, job_id=first["id"], worker_id="a", result={}, now=START)
    assert job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=30))["job"]["id"] == second["id"]


def test_unknown_estimate_and_job_budget_fail_closed(conn):
    user, episode_id = _setup(conn)
    set_budget(conn, scope="user", period="day", user_id=user["id"], max_amount=Decimal("1"))
    unknown = _job(conn, user, episode_id, "unknown", estimate=None)
    assert job.claim_job(conn, worker_id="a", now=START) is None
    assert job.get_job(conn, unknown["id"])["last_error"] == "cost_unknown"

    too_expensive = _job(conn, user, episode_id, "too-expensive", estimate=Decimal("0.2"), budget=Decimal("0.1"))
    assert job.claim_job(conn, worker_id="a", now=START) is None
    assert job.get_job(conn, too_expensive["id"])["last_error"] == "budget_exceeded"
