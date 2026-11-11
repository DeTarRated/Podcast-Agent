"""真实 PostgreSQL 任务队列集成测试；仅使用本地 podcast_agent_test 数据库。

模拟场景：
- 幂等入队与 JSONB payload
- 多 worker 领取互斥、完成与错误 worker 拒绝
- 租约续约、过期接管、旧 worker 不得提交
- 可重试失败指数退避、非重试与 unknown 不自动重试
- 尝试次数耗尽的过期 running 任务转 parked
- 队列状态统计

默认跳过；设置 PODCAST_TEST_POSTGRES=1 且 PODCAST_POSTGRES_DSN 指向本地测试库才运行。
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from state.postgres import db, job, migrations

pytestmark = pytest.mark.skipif(
    os.environ.get("PODCAST_TEST_POSTGRES") != "1",
    reason="需要显式启用本地 PostgreSQL 测试库",
)

START = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    connection = db.connect()
    migrations.migrate(connection)
    connection.execute("TRUNCATE job_attempt, job, episode RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    connection.close()


def _enqueue(conn, key="job-1", max_attempts=3):
    return job.enqueue_job(
        conn,
        kind="transcribe",
        episode_id=None,
        payload={"input_sha256": "abc", "version": 1},
        idempotency_key=key,
        available_at=START,
        max_attempts=max_attempts,
    )["job"]


def test_enqueue_is_idempotent_and_payload_is_jsonb(conn):
    first = job.enqueue_job(
        conn, kind="transcribe", payload={"hash": "abc"}, idempotency_key="same"
    )
    second = job.enqueue_job(
        conn, kind="transcribe", payload={"hash": "abc"}, idempotency_key="same"
    )
    assert first["reused"] is False
    assert second["reused"] is True
    assert first["job"]["id"] == second["job"]["id"]
    assert first["job"]["payload"] == {"hash": "abc"}
    assert job.queue_stats(conn) == {"ready": 1}


def test_only_one_worker_claims_and_completes(conn):
    queued = _enqueue(conn)
    claimed = job.claim_job(conn, worker_id="worker-a", now=START)
    assert claimed["job"]["id"] == queued["id"]
    assert claimed["attempt_no"] == 1
    assert claimed["job"]["attempts"] == 1
    assert job.claim_job(conn, worker_id="worker-b", now=START) is None
    completed = job.complete_job(
        conn, job_id=queued["id"], worker_id="worker-a",
        result={"artifact_id": 1}, usage={"tokens": 10}, now=START,
    )
    assert completed["status"] == "completed"
    attempt = conn.execute("SELECT * FROM job_attempt WHERE job_id=%s", (queued["id"],)).fetchone()
    assert attempt["status"] == "succeeded"
    assert attempt["result"] == {"artifact_id": 1}
    with pytest.raises(job.JobError):
        job.complete_job(conn, job_id=queued["id"], worker_id="worker-b", result={}, now=START)


def test_lease_heartbeat_and_expired_takeover(conn):
    queued = _enqueue(conn)
    first = job.claim_job(conn, worker_id="a", now=START, lease_seconds=60)
    assert job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=30)) is None
    assert job.heartbeat_job(
        conn, job_id=queued["id"], worker_id="a",
        now=START + timedelta(seconds=30), lease_seconds=120,
    ) is True
    assert job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=61)) is None
    second = job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=151), lease_seconds=60)
    assert second["attempt_no"] == 2
    old_attempt = conn.execute(
        "SELECT * FROM job_attempt WHERE job_id=%s AND attempt_no=1", (queued["id"],)
    ).fetchone()
    assert old_attempt["status"] == "failed"
    assert old_attempt["error"] == "lease_expired"
    with pytest.raises(job.JobError):
        job.complete_job(conn, job_id=queued["id"], worker_id="a", result={}, now=START + timedelta(seconds=151))


def test_retryable_failure_uses_backoff(conn):
    queued = _enqueue(conn)
    claimed = job.claim_job(conn, worker_id="a", now=START)
    failed = job.fail_job(
        conn, job_id=queued["id"], worker_id="a", error="temporary",
        retryable=True, now=START,
    )
    assert failed["status"] == "retry"
    assert failed["available_at"] == START + timedelta(seconds=1)
    assert job.claim_job(conn, worker_id="b", now=START) is None
    retried = job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=1))
    assert retried["attempt_no"] == 2


def test_nonretryable_and_unknown_do_not_retry(conn):
    nonretry = _enqueue(conn, key="nonretry")
    claimed = job.claim_job(conn, worker_id="a", now=START)
    failed = job.fail_job(
        conn, job_id=nonretry["id"], worker_id="a", error="bad", retryable=False, now=START
    )
    assert failed["status"] == "failed"
    assert job.claim_job(conn, worker_id="b", now=START) is None

    unknown = _enqueue(conn, key="unknown")
    claimed_unknown = job.claim_job(conn, worker_id="a", now=START)
    failed_unknown = job.fail_job(
        conn, job_id=unknown["id"], worker_id="a", error="unknown paid result",
        retryable=True, unknown=True, now=START,
    )
    assert failed_unknown["status"] == "failed"
    assert job.claim_job(conn, worker_id="b", now=START) is None
    attempt = conn.execute("SELECT status FROM job_attempt WHERE job_id=%s", (unknown["id"],)).fetchone()
    assert attempt["status"] == "unknown"


def test_concurrent_workers_never_claim_same_job(conn):
    from concurrent.futures import ThreadPoolExecutor

    for index in range(20):
        _enqueue(conn, key=f"concurrent-{index}")

    def worker(worker_id):
        connection = db.connect()
        claimed = []
        try:
            while True:
                result = job.claim_job(connection, worker_id=worker_id, now=START)
                if result is None:
                    break
                claimed.append(result["job"]["id"])
        finally:
            connection.close()
        return claimed

    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(worker, ["a", "b", "c", "d"]))
    flattened = [job_id for values in claimed for job_id in values]
    assert len(flattened) == 20
    assert len(set(flattened)) == 20


def test_expired_exhausted_job_is_parked_not_reclaimed(conn):
    queued = _enqueue(conn, key="exhausted", max_attempts=1)
    job.claim_job(conn, worker_id="a", now=START, lease_seconds=1)
    result = job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=2))
    assert result is None
    current = job.get_job(conn, queued["id"])
    assert current["status"] == "parked"
    assert current["last_error"] == "lease_exhausted"
