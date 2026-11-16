"""真实 PostgreSQL 预算执行与背压测试；零外部付费调用。

模拟场景：
- 用户日预算内允许，超限任务失败关闭且不增加 attempts
- 最大 running 数量触发 retry 背压，释放后可继续；跨连接并发预算／running 领取串行
- 未知预估成本或权威账本未定价失败关闭，任务级预算超限拒绝；陈旧 PG ledger 不参与计算
- 预算拦截不创建 job_attempt；逐请求预算读取全局／Episode限额并拒绝不可归属限额

默认跳过；需 PODCAST_TEST_POSTGRES=1 与本地测试库。
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from state.postgres import db, job, migrations
from state import db as sqlite_db
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


@pytest.fixture()
def ledger_conn(tmp_path):
    connection = sqlite_db.connect(tmp_path / "authoritative.db")
    sqlite_db.init_schema(connection)
    yield connection
    connection.close()


def _setup(conn):
    user = create_user(conn, email="budget@example.com", password="budget-password", role="viewer")
    episode_id = conn.execute(
        "INSERT INTO episode (guid,feed) VALUES ('budget-guid','feed') RETURNING id"
    ).fetchone()["id"]
    grant_permission(conn, user_id=user["id"], resource_type="episode", resource_id=episode_id, permission="read")
    return user, episode_id


def _seed_sqlite_episode(ledger_conn, episode_id):
    ledger_conn.execute(
        "INSERT INTO episode (id,guid,feed) VALUES (?,?,?)",
        (episode_id, f"sqlite-guid-{episode_id}", "budget-feed"),
    )
    ledger_conn.commit()


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


def test_cost_guard_loads_global_and_episode_limits_and_rejects_user_scope(conn):
    from pipeline.cost_guard import load_limits
    from state.postgres.budget import BudgetDataError

    user, episode_id = _setup(conn)
    set_budget(conn, scope="global", period="day", max_amount=Decimal("2"))
    set_budget(conn, scope="episode", period="task", episode_id=episode_id, max_amount=Decimal("1"))
    limits = load_limits(episode_id)
    assert {(row["scope"], row["period"]) for row in limits} == {
        ("global", "day"), ("episode", "task"),
    }
    set_budget(conn, scope="user", period="day", user_id=user["id"], max_amount=Decimal("1"))
    with pytest.raises(BudgetDataError, match="user scope"):
        load_limits(episode_id)


def test_budget_exceeded_blocks_without_consuming_attempt(conn, ledger_conn):
    user, episode_id = _setup(conn)
    set_budget(conn, scope="global", period="day", max_amount=Decimal("0.5"))
    _seed_sqlite_episode(ledger_conn, episode_id)
    conn.execute(
        """
        INSERT INTO ledger (episode_id,purpose,amount_est,call_ref,status,triggered_at)
        VALUES (%s,'transcribe',%s,'stale-pg-copy','success',%s)
        """,
        (episode_id, Decimal("0.0"), START),
    )
    conn.commit()
    ledger_conn.execute(
        "INSERT INTO ledger (episode_id,purpose,provider,model,usage_basis,amount_est,call_ref,status,triggered_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (episode_id, "transcribe", "fake", "fake", "priced", 0.2,
         "sqlite-authority", "success", START.isoformat()),
    )
    ledger_conn.commit()
    allowed = _job(conn, user, episode_id, "allowed", estimate=Decimal("0.2"))
    assert job.claim_job(conn, worker_id="a", now=START, ledger_conn=ledger_conn)["job"]["id"] == allowed["id"]
    blocked = _job(conn, user, episode_id, "blocked", estimate=Decimal("0.4"))
    assert job.claim_job(conn, worker_id="b", now=START, ledger_conn=ledger_conn) is None
    current = job.get_job(conn, blocked["id"])
    assert current["status"] == "failed"
    assert current["last_error"] == "budget_exceeded"
    assert current["attempts"] == 0
    assert conn.execute("SELECT COUNT(*) AS count FROM job_attempt WHERE job_id=%s", (blocked["id"],)).fetchone()["count"] == 0


def test_concurrent_amount_claims_share_reservation_lock(conn, ledger_conn):
    from concurrent.futures import ThreadPoolExecutor
    from state.postgres.budget import open_authoritative_ledger

    user, episode_id = _setup(conn)
    set_budget(conn, scope="global", period="day", max_amount=Decimal("0.5"))
    first = _job(conn, user, episode_id, "parallel-a", estimate=Decimal("0.4"))
    second = _job(conn, user, episode_id, "parallel-b", estimate=Decimal("0.4"))
    sqlite_path = ledger_conn.execute("PRAGMA database_list").fetchone()[2]

    def claim(worker_id):
        pg = db.connect()
        sqlite = open_authoritative_ledger(sqlite_path)
        try:
            return job.claim_job(pg, worker_id=worker_id, now=START, ledger_conn=sqlite)
        finally:
            sqlite.close(); pg.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ["budget-a", "budget-b"]))
    assert sum(result is not None for result in claimed) == 1
    statuses = {job.get_job(conn, item["id"])["status"] for item in (first, second)}
    assert statuses == {"running", "failed"}
    assert conn.execute("SELECT COUNT(*) AS count FROM job_attempt").fetchone()["count"] == 1


def test_concurrent_max_running_claims_are_serialized(conn, ledger_conn):
    from concurrent.futures import ThreadPoolExecutor
    from state.postgres.budget import open_authoritative_ledger

    user, episode_id = _setup(conn)
    set_budget(conn, scope="global", period="day", max_running=1)
    first = _job(conn, user, episode_id, "parallel-running-a", estimate=Decimal("0.1"))
    second = _job(conn, user, episode_id, "parallel-running-b", estimate=Decimal("0.1"))
    sqlite_path = ledger_conn.execute("PRAGMA database_list").fetchone()[2]

    def claim(worker_id):
        pg = db.connect()
        sqlite = open_authoritative_ledger(sqlite_path)
        try:
            return job.claim_job(pg, worker_id=worker_id, now=START, ledger_conn=sqlite)
        finally:
            sqlite.close(); pg.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ["running-a", "running-b"]))
    assert sum(result is not None for result in claimed) == 1
    statuses = {job.get_job(conn, item["id"])["status"] for item in (first, second)}
    assert statuses == {"running", "retry"}
    assert conn.execute("SELECT COUNT(*) AS count FROM job_attempt").fetchone()["count"] == 1


def test_max_running_backpressure_releases_after_completion(conn):
    user, episode_id = _setup(conn)
    set_budget(conn, scope="global", period="day", max_running=1)
    first = _job(conn, user, episode_id, "first", estimate=Decimal("0.1"))
    second = _job(conn, user, episode_id, "second", estimate=Decimal("0.1"))
    assert job.claim_job(conn, worker_id="a", now=START)["job"]["id"] == first["id"]
    assert job.claim_job(conn, worker_id="b", now=START) is None
    current = job.get_job(conn, second["id"])
    assert current["status"] == "retry"
    assert current["last_error"] == "max_running"
    assert current["attempts"] == 0
    conn.commit()
    job.complete_job(conn, job_id=first["id"], worker_id="a", result={}, now=START)
    assert job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=30))["job"]["id"] == second["id"]


def test_unpriced_authoritative_ledger_fails_closed_without_attempt(conn, ledger_conn):
    from state.postgres.budget import BudgetDataError

    user, episode_id = _setup(conn)
    set_budget(conn, scope="global", period="day", max_amount=Decimal("1"))
    _seed_sqlite_episode(ledger_conn, episode_id)
    ledger_conn.execute(
        "INSERT INTO ledger (episode_id,purpose,provider,model,usage_basis,amount_est,call_ref,status,triggered_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (episode_id, "translate", "fake", "fake", "phase=intent", None,
         "unknown-unpriced", "unknown", START.isoformat()),
    )
    ledger_conn.commit()
    queued = _job(conn, user, episode_id, "blocked-unpriced", estimate=Decimal("0.1"))
    with pytest.raises(BudgetDataError, match="未定价"):
        job.claim_job(conn, worker_id="a", now=START, ledger_conn=ledger_conn)
    current = job.get_job(conn, queued["id"])
    assert current["status"] == "ready"
    assert current["attempts"] == 0
    assert conn.execute("SELECT COUNT(*) AS count FROM job_attempt WHERE job_id=%s", (queued["id"],)).fetchone()["count"] == 0


def test_user_specific_episode_budget_fails_closed(conn, ledger_conn):
    from state.postgres.budget import BudgetDataError

    user, episode_id = _setup(conn)
    set_budget(
        conn, scope="episode", period="task", user_id=user["id"],
        episode_id=episode_id, max_amount=Decimal("1"),
    )
    queued = _job(conn, user, episode_id, "user-episode-budget", estimate=Decimal("0.1"))
    with pytest.raises(BudgetDataError, match="user-specific episode"):
        job.claim_job(conn, worker_id="a", now=START, ledger_conn=ledger_conn)
    assert job.get_job(conn, queued["id"])["status"] == "ready"
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM job_attempt WHERE job_id=%s", (queued["id"],)
    ).fetchone()["count"] == 0


def test_unknown_estimate_and_job_budget_fail_closed(conn, ledger_conn):
    user, episode_id = _setup(conn)
    set_budget(conn, scope="global", period="day", max_amount=Decimal("1"))
    unknown = _job(conn, user, episode_id, "unknown", estimate=None)
    assert job.claim_job(conn, worker_id="a", now=START, ledger_conn=ledger_conn) is None
    assert job.get_job(conn, unknown["id"])["last_error"] == "cost_unknown"

    too_expensive = _job(conn, user, episode_id, "too-expensive", estimate=Decimal("0.2"), budget=Decimal("0.1"))
    assert job.claim_job(conn, worker_id="a", now=START, ledger_conn=ledger_conn) is None
    assert job.get_job(conn, too_expensive["id"])["last_error"] == "budget_exceeded"
