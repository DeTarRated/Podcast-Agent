"""PostgreSQL 持久化任务队列：幂等入队、SKIP LOCKED、租约、尝试与恢复。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from state.postgres.budget import _amount, _estimated, check_budget
from state.postgres.time import _now


def _json_dumps(value) -> str:
    return json.dumps(value, default=str)


class JobError(RuntimeError):
    """任务状态、租约或输入不合法。"""


BUDGET_ADVISORY_LOCK = 7_401_125_903

# 仅明确识别的 safe 类型允许过期租约被其他 worker 接管；
# 其余类型（含未来新增）一律视为可能已发出付费调用：过期 running 转 parked/unknown 防重放，
# 等待人工凭账本与在途证据对账恢复。
SAFE_KINDS = frozenset({"download"})


def _require_idle_connection(conn, operation: str) -> None:
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise JobError(f"{operation} 要求独占事务边界，调用前请提交或回滚当前事务")


def enqueue_job(
    conn,
    *,
    kind: str,
    payload: dict,
    idempotency_key: str,
    episode_id: Optional[int] = None,
    user_id: Optional[int] = None,
    priority: int = 0,
    available_at: Optional[str | datetime] = None,
    max_attempts: int = 3,
    budget=None,
) -> dict:
    if not idempotency_key.strip() or max_attempts <= 0:
        raise JobError("idempotency_key 必填，max_attempts 必须为正数")
    if budget is not None:
        budget = _amount(budget, "job budget")
    estimate = _estimated({"payload": payload})
    basis = (payload or {}).get("estimate_basis")
    available = _now(available_at)
    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO job (
                episode_id, user_id, kind, payload, idempotency_key, priority,
                available_at, max_attempts, budget
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING *
            """,
            (
                episode_id, user_id, kind, Jsonb(payload, dumps=_json_dumps), idempotency_key, priority,
                available, max_attempts, budget,
            ),
        ).fetchone()
        reused = row is None
        if reused:
            row = conn.execute(
                "SELECT * FROM job WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            existing_estimate = _estimated(row)
            existing_basis = (row["payload"] or {}).get("estimate_basis")
            legacy_without_estimate = (
                existing_estimate is None
                and existing_basis is None
                and estimate is not None
                and row["status"] in ("ready", "failed", "retry")
                and row["attempts"] == 0
            )
            if legacy_without_estimate:
                # 旧版无估算 job 且从未被领取：允许以正确估算一次性重写，避免 Episode 被永久卡死。
                # 任何已领取（attempts>0）或 running 行不允许改写，仍失败关闭。
                merged_payload = {**(row["payload"] or {}), **(payload or {})}
                row = conn.execute(
                    """
                    UPDATE job SET payload = %s, available_at = %s, status = 'ready',
                        locked_by = NULL, locked_until = NULL, last_error = NULL, updated_at = %s
                    WHERE id = %s RETURNING *
                    """,
                    (
                        Jsonb(merged_payload, dumps=_json_dumps), available,
                        _now(None), row["id"],
                    ),
                ).fetchone()
                # 处于 transaction() 上下文内，提交由上下文负责，此处不得显式 commit。
                return {"job": row, "reused": True, "upgraded_estimate": True}
            structurally_same = (
                row["kind"] == kind
                and row["episode_id"] == episode_id
                and row["budget"] == budget
            )
            estimate_changed = existing_estimate != estimate or existing_basis != basis
            if structurally_same and estimate_changed:
                # 期数推进后时长已知、或按剩余额度重算：估算依据会变化。
                # 规则：非 running 时采纳最新估算并记录差异；不改状态（不由队列自动重试付费任务）。
                if row["status"] == "running":
                    raise JobError("running job 的估算不可改写")
                if estimate is None:
                    raise JobError("新请求缺少估算，不得覆盖已有预留")
                merged = {**(row["payload"] or {}), **(payload or {})}
                updated_row = conn.execute(
                    """
                    UPDATE job SET payload = %s, updated_at = %s
                    WHERE id = %s RETURNING *
                    """,
                    (Jsonb(merged, dumps=_json_dumps), _now(None), row["id"]),
                ).fetchone()
                return {
                    "job": updated_row, "reused": True, "estimate_updated": True,
                    "previous_estimate": None if existing_estimate is None else str(existing_estimate),
                    "requested_estimate": str(estimate),
                }
            if not structurally_same:
                raise JobError("幂等 job 的 kind/episode/budget 与新请求不一致")
    conn.commit()
    return {"job": row, "reused": reused}


def claim_job(
    conn,
    *,
    worker_id: str,
    now: Optional[str | datetime] = None,
    lease_seconds: int = 300,
    kinds: Optional[list[str]] = None,
    allowed_episode_ids: Optional[list[int]] = None,
    allowed_job_ids: Optional[list[int]] = None,
    ledger_conn=None,
) -> Optional[dict]:
    if not worker_id.strip() or lease_seconds <= 0:
        raise JobError("worker_id 必填，lease_seconds 必须为正数")
    current = _now(now)
    expires = current + timedelta(seconds=lease_seconds)
    _require_idle_connection(conn, "claim_job")
    with conn.transaction():
        # 全局预算锁序列化金额检查、running 预留与状态更新，避免不同 job 并发超额领取。
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_ADVISORY_LOCK,))
        # 过期 running 的非 safe 任务可能已发出付费调用：park 并在 unknown 保留占用，
        # 绝不交给另一个 worker 自动重放；恢复由人工凭账本与在途证据决定。
        # 维护清理同样尊重白名单范围，不修改范围外记录。
        unsafe_expired = conn.execute(
            """
            UPDATE job SET status = 'parked', locked_by = NULL, locked_until = NULL,
                last_error = 'unsafe_lease_expired', updated_at = %s
            WHERE status = 'running' AND locked_until <= %s AND kind <> ALL(%s::text[])
              AND (%s::int[] IS NULL OR episode_id = ANY(%s::int[]))
              AND (%s::int[] IS NULL OR id = ANY(%s::int[]))
            RETURNING id, attempts
            """,
            (
                current, current, list(SAFE_KINDS),
                allowed_episode_ids, allowed_episode_ids,
                allowed_job_ids, allowed_job_ids,
            ),
        ).fetchall()
        for item in unsafe_expired:
            conn.execute(
                """
                UPDATE job_attempt SET status = 'unknown', finished_at = %s, error = 'unsafe_lease_expired'
                WHERE job_id = %s AND attempt_no = %s AND status = 'running'
                """,
                (current, item["id"], item["attempts"]),
            )
        # 已耗尽次数的过期 running 任务不再运行，转为 parked 并留下尝试错误。
        exhausted = conn.execute(
            """
            UPDATE job SET status = 'parked', locked_by = NULL, locked_until = NULL,
                last_error = 'lease_exhausted', updated_at = %s
            WHERE status = 'running' AND locked_until <= %s AND attempts >= max_attempts
              AND (%s::int[] IS NULL OR episode_id = ANY(%s::int[]))
              AND (%s::int[] IS NULL OR id = ANY(%s::int[]))
            RETURNING id, attempts
            """,
            (
                current, current,
                allowed_episode_ids, allowed_episode_ids,
                allowed_job_ids, allowed_job_ids,
            ),
        ).fetchall()
        for item in exhausted:
            conn.execute(
                """
                UPDATE job_attempt SET status = 'failed', finished_at = %s, error = 'lease_exhausted'
                WHERE job_id = %s AND attempt_no = %s AND status = 'running'
                """,
                (current, item["id"], item["attempts"]),
            )
        row = conn.execute(
            """
            SELECT * FROM job
            WHERE (
                (status IN ('ready','retry') AND available_at <= %s)
                OR (status = 'running' AND locked_until <= %s)
            ) AND attempts < max_attempts
              AND (%s::text[] IS NULL OR kind = ANY(%s::text[]))
              AND (%s::int[] IS NULL OR episode_id = ANY(%s::int[]))
              AND (%s::int[] IS NULL OR id = ANY(%s::int[]))
            ORDER BY priority DESC, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (
                current, current, kinds, kinds,
                allowed_episode_ids, allowed_episode_ids,
                allowed_job_ids, allowed_job_ids,
            ),
        ).fetchone()
        if row is None:
            return None
        budget = check_budget(conn, row, ledger_conn=ledger_conn, now=current)
        if not budget.allowed:
            status = "retry" if budget.reason == "max_running" else "failed"
            available = budget.retry_at or current
            conn.execute(
                """
                UPDATE job SET status = %s, available_at = %s, last_error = %s, updated_at = %s
                WHERE id = %s
                """,
                (status, available, budget.reason, current, row["id"]),
            )
            return None
        attempt_no = row["attempts"] + 1
        locked = conn.execute(
            """
            UPDATE job SET status = 'running', locked_by = %s, locked_until = %s,
                attempts = %s, updated_at = %s, last_error = NULL
            WHERE id = %s
            RETURNING *
            """,
            (worker_id, expires, attempt_no, current, row["id"]),
        ).fetchone()
        conn.execute(
            """
            UPDATE job_attempt SET status = 'failed', finished_at = %s, error = 'lease_expired'
            WHERE job_id = %s AND status = 'running'
            """,
            (current, row["id"]),
        )
        attempt = conn.execute(
            """
            INSERT INTO job_attempt (job_id, attempt_no, worker_id, started_at)
            VALUES (%s,%s,%s,%s) RETURNING id
            """,
            (row["id"], attempt_no, worker_id, current),
        ).fetchone()
    conn.commit()
    return {"job": locked, "attempt_id": attempt["id"], "attempt_no": attempt_no}


def heartbeat_job(
    conn,
    *,
    job_id: int,
    worker_id: str,
    now: Optional[str | datetime] = None,
    lease_seconds: int = 300,
) -> bool:
    current = _now(now)
    expires = current + timedelta(seconds=lease_seconds)
    with conn.transaction():
        row = conn.execute(
            """
            UPDATE job SET locked_until = %s, updated_at = %s
            WHERE id = %s AND status = 'running' AND locked_by = %s AND locked_until > %s
            RETURNING id
            """,
            (expires, current, job_id, worker_id, current),
        ).fetchone()
    conn.commit()
    return row is not None


def _running_for_update(conn, job_id: int, worker_id: str, current: datetime) -> dict:
    row = conn.execute(
        "SELECT * FROM job WHERE id = %s FOR UPDATE", (job_id,)
    ).fetchone()
    if (
        row is None
        or row["status"] != "running"
        or row["locked_by"] != worker_id
        or row["locked_until"] <= current
    ):
        raise JobError("任务不存在、非 running、worker 不匹配或租约已过期")
    return row


def complete_job(
    conn,
    *,
    job_id: int,
    worker_id: str,
    result: dict,
    usage: Optional[dict] = None,
    now: Optional[str | datetime] = None,
) -> dict:
    current = _now(now)
    _require_idle_connection(conn, "complete_job")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_ADVISORY_LOCK,))
        row = _running_for_update(conn, job_id, worker_id, current)
        conn.execute(
            """
            UPDATE job_attempt SET status = 'succeeded', finished_at = %s, result = %s, usage = %s
            WHERE job_id = %s AND attempt_no = %s
            """,
            (current, Jsonb(result, dumps=_json_dumps), Jsonb(usage, dumps=_json_dumps) if usage is not None else None, job_id, row["attempts"]),
        )
        completed = conn.execute(
            """
            UPDATE job SET status = 'completed', locked_by = NULL, locked_until = NULL,
                updated_at = %s, last_error = NULL WHERE id = %s RETURNING *
            """,
            (current, job_id),
        ).fetchone()
    conn.commit()
    return completed


def fail_job(
    conn,
    *,
    job_id: int,
    worker_id: str,
    error: str,
    retryable: bool,
    unknown: bool = False,
    now: Optional[str | datetime] = None,
    retry_base_seconds: int = 1,
) -> dict:
    current = _now(now)
    _require_idle_connection(conn, "fail_job")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_ADVISORY_LOCK,))
        row = _running_for_update(conn, job_id, worker_id, current)
        conn.execute(
            """
            UPDATE job_attempt SET status = %s, finished_at = %s, error = %s
            WHERE job_id = %s AND attempt_no = %s
            """,
            ("unknown" if unknown else "failed", current, error[:2000], job_id, row["attempts"]),
        )
        if not unknown and retryable and row["attempts"] < row["max_attempts"]:
            delay = retry_base_seconds * (2 ** max(0, row["attempts"] - 1))
            status = "retry"
            available = current + timedelta(seconds=delay)
            locked_by, locked_until = None, None
        else:
            status = "failed" if not retryable or unknown else "parked"
            available = row["available_at"]
            locked_by, locked_until = None, None
        failed = conn.execute(
            """
            UPDATE job SET status = %s, available_at = %s, locked_by = %s, locked_until = %s,
                last_error = %s, updated_at = %s WHERE id = %s RETURNING *
            """,
            (status, available, locked_by, locked_until, error[:2000], current, job_id),
        ).fetchone()
    conn.commit()
    return failed


def get_job(conn, job_id: int) -> Optional[dict]:
    return conn.execute("SELECT * FROM job WHERE id = %s", (job_id,)).fetchone()


def queue_stats(conn) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) AS count FROM job GROUP BY status").fetchall()
    return {row["status"]: row["count"] for row in rows}
