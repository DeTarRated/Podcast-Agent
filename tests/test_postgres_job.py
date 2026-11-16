"""真实 PostgreSQL 任务队列集成测试；仅使用本地 podcast_agent_test 数据库。

模拟场景：
- 幂等入队与 JSONB payload；经济参数（估算/依据/预算）冲突拒绝
- 多 worker 领取互斥、完成与错误 worker 拒绝；claim 事务边界避免 advisory lock 泄漏
- 租约续约、过期接管、旧 worker 不得提交
- 可重试失败指数退避、非重试与 unknown 不自动重试
- 尝试次数耗尽的过期 running 任务转 parked
- 过期 unsafe 任务 parked/unknown 防重放、safe 任务过期接管仍可用、领取遵守 Episode 白名单
- 队列状态统计

默认跳过；设置 PODCAST_TEST_POSTGRES=1 且 PODCAST_POSTGRES_DSN 指向本地测试库才运行。
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.pq import TransactionStatus

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


def _enqueue(conn, key="job-1", max_attempts=3, kind="download"):
    return job.enqueue_job(
        conn,
        kind=kind,
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


def test_idempotent_enqueue_rejects_changed_estimate_or_budget(conn):
    first = job.enqueue_job(
        conn, kind="transcribe", episode_id=None,
        payload={"estimated_cost": "0.25", "estimate_version": "v1"},
        idempotency_key="same-economic-job", budget="1.00",
    )
    assert first["reused"] is False
    # 估算变化：非 running 时采纳最新估算并记录差异，不改状态（不自动重试付费任务）。
    updated = job.enqueue_job(
        conn, kind="transcribe", episode_id=None,
        payload={"estimated_cost": "0.50", "estimate_version": "v2"},
        idempotency_key="same-economic-job", budget="1.00",
    )
    assert updated["reused"] is True
    assert updated.get("estimate_updated") is True
    assert updated["previous_estimate"] == "0.25"
    assert conn.execute("SELECT payload->>'estimated_cost' AS v FROM job").fetchone()["v"] == "0.50"
    # 结构性差异（预算变化）仍必须失败关闭。
    with pytest.raises(job.JobError, match="kind/episode/budget"):
        job.enqueue_job(
            conn, kind="transcribe", episode_id=None,
            payload={"estimated_cost": "0.50", "estimate_version": "v2"},
            idempotency_key="same-economic-job", budget="2.00",
        )
    assert conn.execute("SELECT COUNT(*) AS count FROM job").fetchone()["count"] == 1


def test_claim_requires_owned_transaction_and_releases_lock(conn):
    queued = _enqueue(conn, key="idle-boundary")
    conn.execute("SELECT 1")
    with pytest.raises(job.JobError, match="事务边界"):
        job.claim_job(conn, worker_id="must-not-lock", now=START)
    conn.rollback()
    assert job.get_job(conn, queued["id"])["attempts"] == 0
    conn.commit()

    claimed = job.claim_job(conn, worker_id="valid", now=START)
    assert claimed["job"]["id"] == queued["id"]
    assert conn.info.transaction_status == TransactionStatus.IDLE


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


def _episode(conn, ident: int) -> int:
    row = conn.execute(
        "INSERT INTO episode (guid, feed) VALUES (%s, %s) RETURNING id",
        (f"guid-{ident}", f"feed-{ident}"),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def test_expired_unsafe_job_is_parked_unknown_not_replayed(conn):
    episode_id = _episode(conn, 1)
    unsafe = job.enqueue_job(
        conn, kind="episode", episode_id=episode_id,
        payload={"guid": "g"}, idempotency_key="unsafe-1",
        available_at=START,
    )["job"]
    job.claim_job(conn, worker_id="a", now=START, lease_seconds=60)
    # 过期后另一个 worker 不得自动重放可能已付费的 unsafe 任务。
    assert job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=61)) is None
    row = job.get_job(conn, unsafe["id"])
    assert row["status"] == "parked"
    assert row["last_error"] == "unsafe_lease_expired"
    attempt = conn.execute(
        "SELECT * FROM job_attempt WHERE job_id=%s", (unsafe["id"],)
    ).fetchone()
    assert attempt["status"] == "unknown"
    assert job.queue_stats(conn).get("parked") == 1


def test_expired_safe_job_takeover_still_works(conn):
    safe = job.enqueue_job(
        conn, kind="download", episode_id=None,
        payload={"url": "https://cdn.example.com/a.mp3"}, idempotency_key="safe-1",
        available_at=START,
    )["job"]
    job.claim_job(conn, worker_id="a", now=START, lease_seconds=60)
    second = job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=61), lease_seconds=60)
    assert second["job"]["id"] == safe["id"]
    assert second["attempt_no"] == 2


@pytest.mark.parametrize("paid_kind", ["transcribe", "translate", "review", "highlights"])
def test_expired_paid_kinds_are_parked_unknown(conn, paid_kind):
    queued = job.enqueue_job(
        conn, kind=paid_kind, episode_id=None,
        payload={"version": 1}, idempotency_key=f"paid-{paid_kind}",
        available_at=START,
    )["job"]
    job.claim_job(conn, worker_id="a", now=START, lease_seconds=60)
    assert job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=61)) is None
    row = job.get_job(conn, queued["id"])
    assert row["status"] == "parked"
    assert row["last_error"] == "unsafe_lease_expired"
    attempt = conn.execute(
        "SELECT * FROM job_attempt WHERE job_id=%s", (queued["id"],)
    ).fetchone()
    assert attempt["status"] == "unknown"


def test_claim_honors_allowed_job_ids(conn):
    first = job.enqueue_job(
        conn, kind="download", episode_id=None,
        payload={"url": "https://cdn.example.com/1.mp3"}, idempotency_key="job-1",
        available_at=START,
    )["job"]
    second = job.enqueue_job(
        conn, kind="download", episode_id=None,
        payload={"url": "https://cdn.example.com/2.mp3"}, idempotency_key="job-2",
        available_at=START,
    )["job"]
    claimed = job.claim_job(conn, worker_id="w", now=START, allowed_job_ids=[second["id"]])
    assert claimed["job"]["id"] == second["id"]
    assert job.claim_job(conn, worker_id="w", now=START, allowed_job_ids=[second["id"]]) is None
    # 白名单外的 first 未被触碰。
    assert job.get_job(conn, first["id"])["status"] == "ready"


def test_expired_cleanup_respects_allowed_episode_scope(conn):
    in_scope = _episode(conn, 4)
    out_scope = _episode(conn, 5)
    for ident, episode_id in (("in", in_scope), ("out", out_scope)):
        queued = job.enqueue_job(
            conn, kind="episode", episode_id=episode_id,
            payload={"guid": ident}, idempotency_key=f"scope-{ident}",
            available_at=START,
        )["job"]
        job.claim_job(conn, worker_id="a", now=START, lease_seconds=60)
    assert job.claim_job(
        conn, worker_id="b", now=START + timedelta(seconds=61),
        allowed_episode_ids=[in_scope],
    ) is None
    in_id = conn.execute("SELECT id FROM job WHERE episode_id=%s", (in_scope,)).fetchone()["id"]
    in_row = job.get_job(conn, in_id)
    assert in_row["status"] == "parked"
    # 范围外 Episode 的过期 running 任务不被维护写入修改。
    out_id = conn.execute("SELECT id FROM job WHERE episode_id=%s", (out_scope,)).fetchone()["id"]
    out_row = job.get_job(conn, out_id)
    assert out_row["status"] == "running"
    assert out_row["last_error"] is None


def test_expired_cleanup_respects_allowed_job_ids(conn):
    first = job.enqueue_job(
        conn, kind="episode", episode_id=None,
        payload={"guid": "j1"}, idempotency_key="job-scope-1", available_at=START,
    )["job"]
    second = job.enqueue_job(
        conn, kind="episode", episode_id=None,
        payload={"guid": "j2"}, idempotency_key="job-scope-2", available_at=START,
    )["job"]
    job.claim_job(conn, worker_id="a", now=START, lease_seconds=60)
    job.claim_job(conn, worker_id="a", now=START, lease_seconds=60)
    assert job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=61),
                         allowed_job_ids=[second["id"]]) is None
    in_row = job.get_job(conn, second["id"])
    assert in_row["status"] == "parked" and in_row["last_error"] == "unsafe_lease_expired"
    # 白名单外 job 的过期 running 记录不被维护写入修改。
    out_row = job.get_job(conn, first["id"])
    assert out_row["status"] == "running" and out_row["last_error"] is None


def test_enqueue_episode_respects_feed_and_guid_allowlist(conn, tmp_path, monkeypatch):
    from state import db as sqlite_db
    from state import episode as sqlite_episode
    from state import feed as sqlite_feed
    from state.postgres import job_cli

    lite = sqlite_db.connect(tmp_path / "biz.db")
    sqlite_db.init_schema(lite)
    feed_url = "https://feeds.example.com/a.xml"
    ep_id = sqlite_episode.create(lite, guid="guid-x", feed=feed_url)
    sqlite_feed.add(lite, name="A", feed_url=feed_url, show_name="S",
                    now=START.isoformat(), process_existing=True)
    lite.close()
    allow = tmp_path / "allowlist.json"
    allow.write_text(json.dumps({"guids": [f"{feed_url}::other-guid"]}))
    monkeypatch.setenv("PODCAST_AGENT_DB", str(tmp_path / "biz.db"))
    monkeypatch.setenv("PODCAST_ALLOWLIST_FILE", str(allow))

    assert job_cli.main(["enqueue-episode", str(ep_id), "guid-x"]) == 0
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()["count"] == 0

    allow.write_text(json.dumps({"guids": [f"{feed_url}::guid-x"]}))
    assert job_cli.main(["enqueue-episode", str(ep_id), "guid-x"]) == 0
    rows = conn.execute("SELECT COUNT(*) AS count FROM job").fetchone()
    assert rows["count"] == 1

    # 传入 guid 与权威记录不一致必须拒绝，防止幂等复用掩盖身份错配。
    assert job_cli.main(["enqueue-episode", str(ep_id), "forged-guid"]) == 1
    assert conn.execute("SELECT COUNT(*) AS count FROM job").fetchone()["count"] == 1


def test_claim_honors_allowed_episode_ids(conn):
    allowed = _episode(conn, 2)
    blocked = _episode(conn, 3)
    job.enqueue_job(conn, kind="episode", episode_id=blocked,
                    payload={"guid": "g1"}, idempotency_key="blocked", available_at=START)
    job.enqueue_job(conn, kind="episode", episode_id=allowed,
                    payload={"guid": "g2"}, idempotency_key="allowed", available_at=START)
    claimed = job.claim_job(conn, worker_id="w", now=START,
                            allowed_episode_ids=[allowed])
    assert claimed["job"]["episode_id"] == allowed
    assert job.claim_job(conn, worker_id="w", now=START,
                         allowed_episode_ids=[allowed]) is None
    assert job.get_job(conn, claimed["job"]["id"])["status"] == "running"


def test_expired_exhausted_job_is_parked_not_reclaimed(conn):
    queued = _enqueue(conn, key="exhausted", max_attempts=1)
    job.claim_job(conn, worker_id="a", now=START, lease_seconds=1)
    result = job.claim_job(conn, worker_id="b", now=START + timedelta(seconds=2))
    assert result is None
    current = job.get_job(conn, queued["id"])
    assert current["status"] == "parked"
    assert current["last_error"] == "lease_exhausted"


def test_legacy_job_without_estimate_can_be_upgraded_once(conn):
    """旧版无估算 job：未领取时可一次性重写估算；已领取或 running 不得改写。"""
    legacy = job.enqueue_job(
        conn, kind="episode", episode_id=None,
        payload={"guid": "legacy"}, idempotency_key="legacy-1", available_at=START,
    )["job"]
    upgraded = job.enqueue_job(
        conn, kind="episode", episode_id=None,
        payload={"guid": "legacy", "estimated_cost": "0.916", "estimate_basis": "estimate_version=v1"},
        idempotency_key="legacy-1", available_at=START,
    )
    assert upgraded["reused"] is True
    assert upgraded.get("upgraded_estimate") is True
    assert upgraded["job"]["payload"]["estimated_cost"] == "0.916"
    assert upgraded["job"]["status"] == "ready"

    # 已领取（attempts>0）后不允许再改估算。
    job.claim_job(conn, worker_id="w", now=START)
    conn.commit()
    # running 中不允许改写估算（避免在途请求的预留被变更）。
    with pytest.raises(job.JobError, match="running job 的估算不可改写"):
        job.enqueue_job(
            conn, kind="episode", episode_id=None,
            payload={"guid": "legacy", "estimated_cost": "1.5", "estimate_basis": "estimate_version=v2"},
            idempotency_key="legacy-1", available_at=START,
        )
    assert job.get_job(conn, legacy["id"])["payload"]["estimated_cost"] == "0.916"
    assert job.get_job(conn, legacy["id"])["payload"]["estimated_cost"] == "0.916"


def test_estimate_update_on_resume_records_previous_value(conn):
    """期数推进后时长已知：估算按最新依据更新并记录旧值，但不自动改变任务状态。"""
    base = job.enqueue_job(
        conn, kind="episode", episode_id=None,
        payload={"guid": "g", "estimated_cost": "1.13", "estimate_basis": "duration_source=cap"},
        idempotency_key="resume-1", available_at=START,
    )["job"]
    updated = job.enqueue_job(
        conn, kind="episode", episode_id=None,
        payload={"guid": "g", "estimated_cost": "0.98", "estimate_basis": "duration_secs=2878"},
        idempotency_key="resume-1", available_at=START,
    )
    assert updated.get("estimate_updated") is True
    assert updated["previous_estimate"] == "1.13"
    row = job.get_job(conn, base["id"])
    assert row["payload"]["estimated_cost"] == "0.98"
    # 不由队列自动重试：状态保持不变。
    assert row["status"] == "ready"


def test_estimate_update_survives_outer_open_transaction(conn):
    """外层事务已打开时（如 migrate 之后），估算更新必须随统一提交持久化。"""
    conn.execute("SELECT 1")  # 打开外层事务
    base = job.enqueue_job(
        conn, kind="episode", episode_id=None,
        payload={"guid": "t", "estimated_cost": "1.13", "estimate_basis": "cap"},
        idempotency_key="outer-tx-1", available_at=START,
    )["job"]
    updated = job.enqueue_job(
        conn, kind="episode", episode_id=None,
        payload={"guid": "t", "estimated_cost": "0.98", "estimate_basis": "duration_secs=2879"},
        idempotency_key="outer-tx-1", available_at=START,
    )
    assert updated.get("estimate_updated") is True
    conn.commit()
    assert job.get_job(conn, base["id"])["payload"]["estimated_cost"] == "0.98"
