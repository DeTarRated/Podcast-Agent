"""PostgreSQL 预算执行：SQLite 权威账本读取、预算预检与运行中额度预留。

拒绝读取过期 PostgreSQL ledger 副本；金额限制开启时，账本或估算不可用必须失败关闭。
"""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from state.ledger_report import _usage_cost


class BudgetDataError(RuntimeError):
    """账本、估算或运行中预留不可信，拒绝付费任务领取。"""


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: Optional[str] = None
    retry_at: Optional[datetime] = None
    current_spend: Decimal = Decimal(0)
    estimated_cost: Optional[Decimal] = None


def open_authoritative_ledger(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """只读打开唯一 SQLite 账本；不创建、不迁移、不回退到 PG 副本。"""
    source = Path(path or os.environ.get("PODCAST_AGENT_DB", "var/podcast_agent.db")).resolve()
    if not source.is_file():
        raise BudgetDataError(f"SQLite 权威账本不存在: {source}")
    uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        from state.migrations import SCHEMA_VERSION
        if version != SCHEMA_VERSION:
            conn.close()
            raise BudgetDataError(f"SQLite 权威账本 schema 不匹配: {version} != {SCHEMA_VERSION}")
        conn.execute("SELECT episode_id,purpose,triggered_at,amount_est,usage_basis,status FROM ledger LIMIT 0")
        return conn
    except (sqlite3.Error, OSError) as exc:
        raise BudgetDataError("SQLite 权威账本不可读，拒绝预算领取") from exc


def _amount(value, label: str) -> Decimal:
    if isinstance(value, bool):
        raise BudgetDataError(f"{label} 必须为有限的非负金额")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BudgetDataError(f"{label} 必须为有限的非负金额") from None
    if not result.is_finite() or result < 0:
        raise BudgetDataError(f"{label} 必须为有限的非负金额")
    try:
        represented = float(result)
    except (OverflowError, ValueError):
        raise BudgetDataError(f"{label} 超出 SQLite 可表示范围") from None
    if not math.isfinite(represented) or (result > 0 and represented == 0):
        raise BudgetDataError(f"{label} 超出 SQLite 可表示范围")
    return result


def _estimated(job: dict) -> Optional[Decimal]:
    payload = job.get("payload") or {}
    value = payload.get("estimated_cost", payload.get("amount_est"))
    return None if value is None else _amount(value, "estimated_cost")


def _period_start(now: datetime, period: str) -> Optional[datetime]:
    if period == "task":
        return None
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise BudgetDataError(f"不支持的预算周期: {period}")


def _parse_timestamp(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise BudgetDataError("SQLite 账本包含非法 triggered_at") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _scope_spend(conn, *, scope: str, period: str, user_id: Optional[int], episode_id: Optional[int], now: datetime) -> Decimal:
    """SQLite 单一付款主体：global/user 覆盖全部账单；episode 按期过滤。"""
    if conn is None:
        raise BudgetDataError("缺少 SQLite 权威账本连接")
    start = _period_start(now, period)
    try:
        rows = conn.execute(
            "SELECT episode_id,triggered_at,amount_est,usage_basis,status FROM ledger"
        ).fetchall()
    except sqlite3.Error as exc:
        raise BudgetDataError("SQLite 权威账本读取失败") from exc
    total = Decimal(0)
    for row in rows:
        if scope == "episode" and row["episode_id"] != episode_id:
            continue
        triggered = _parse_timestamp(row["triggered_at"])
        if start is not None and triggered < start:
            continue
        if row["amount_est"] is not None:
            total += _amount(row["amount_est"], "ledger amount_est")
            continue
        raw_cost = _usage_cost(row["usage_basis"])
        if raw_cost is not None and math.isfinite(raw_cost) and raw_cost >= 0:
            total += Decimal(str(raw_cost))
            continue
        # 无价 intent/unknown/success 不能被当作零费用。
        raise BudgetDataError(f"账本存在未定价记录，status={row['status']}")
    return total


def _running_count(conn, *, scope: str, user_id: Optional[int], episode_id: Optional[int], exclude_job_id: int) -> int:
    conditions = ["status = 'running'", "id <> %s"]
    params: list = [exclude_job_id]
    if scope == "user":
        conditions.append("user_id = %s")
        params.append(user_id)
    elif scope == "episode":
        conditions.append("episode_id = %s")
        params.append(episode_id)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM job WHERE {' AND '.join(conditions)}", params
    ).fetchone()
    return int(row["count"])


def _running_reservations(conn, *, scope: str, user_id: Optional[int], episode_id: Optional[int], exclude_job_id: int) -> Decimal:
    conditions = ["status = 'running'", "id <> %s"]
    params: list = [exclude_job_id]
    if scope == "user":
        conditions.append("user_id = %s")
        params.append(user_id)
    elif scope == "episode":
        conditions.append("episode_id = %s")
        params.append(episode_id)
    rows = conn.execute(
        f"SELECT payload FROM job WHERE {' AND '.join(conditions)}", params
    ).fetchall()
    total = Decimal(0)
    for row in rows:
        payload = row["payload"] or {}
        estimate = payload.get("estimated_cost", payload.get("amount_est"))
        if estimate is None:
            raise BudgetDataError("已有 running job 没有金额预留")
        total += _amount(estimate, "running estimated_cost")
    return total


def check_budget(conn, job: dict, *, ledger_conn=None, now: Optional[datetime] = None) -> BudgetDecision:
    current = now or datetime.now(timezone.utc)
    estimate = _estimated(job)
    unsupported_user_limits = conn.execute(
        "SELECT 1 FROM budget_limit WHERE scope='user' AND enabled=TRUE LIMIT 1"
    ).fetchone()
    if unsupported_user_limits is not None:
        raise BudgetDataError("user scope 预算未接入账本付款主体归属，拒绝领取")
    scopes = [("global", None, None)]
    if job.get("episode_id") is not None:
        user_episode_limit = conn.execute(
            "SELECT 1 FROM budget_limit WHERE scope='episode' AND episode_id=%s "
            "AND user_id IS NOT NULL AND enabled=TRUE LIMIT 1",
            (job["episode_id"],),
        ).fetchone()
        if user_episode_limit is not None:
            raise BudgetDataError("user-specific episode 预算未接入付款主体归属，拒绝领取")
        scopes.append(("episode", None, job["episode_id"]))

    for scope, user_id, episode_id in scopes:
        # 当前 UI 可写多种周期；必须全部执行，不静默忽略月度／task 限额。
        limits = conn.execute(
            "SELECT * FROM budget_limit WHERE scope=%s AND user_id IS NOT DISTINCT FROM %s "
            "AND episode_id IS NOT DISTINCT FROM %s AND enabled=TRUE ORDER BY period",
            (scope, user_id, episode_id),
        ).fetchall()
        for limit in limits:
            period = limit["period"]
            if limit["max_running"] is not None and _running_count(
                conn, scope=scope, user_id=user_id, episode_id=episode_id,
                exclude_job_id=job["id"],
            ) >= limit["max_running"]:
                return BudgetDecision(False, "max_running", current + timedelta(seconds=30))
            if limit["max_amount"] is not None:
                if estimate is None:
                    return BudgetDecision(False, "cost_unknown")
                spend = _scope_spend(
                    ledger_conn, scope=scope, period=period, user_id=user_id,
                    episode_id=episode_id, now=current,
                )
                reserved = _running_reservations(
                    conn, scope=scope, user_id=user_id, episode_id=episode_id,
                    exclude_job_id=job["id"],
                )
                ceiling = _amount(limit["max_amount"], "max_amount")
                if spend + reserved + estimate > ceiling:
                    return BudgetDecision(False, "budget_exceeded", current_spend=spend + reserved, estimated_cost=estimate)

    if job.get("budget") is not None:
        if estimate is None:
            return BudgetDecision(False, "cost_unknown")
        spend = _scope_spend(
            ledger_conn, scope="episode", period="task", user_id=job.get("user_id"),
            episode_id=job.get("episode_id"), now=current,
        )
        reserved = _running_reservations(
            conn, scope="episode", user_id=job.get("user_id"),
            episode_id=job.get("episode_id"), exclude_job_id=job["id"],
        )
        ceiling = _amount(job["budget"], "job budget")
        if spend + reserved + estimate > ceiling:
            return BudgetDecision(False, "budget_exceeded", current_spend=spend + reserved, estimated_cost=estimate)
    return BudgetDecision(True, estimated_cost=estimate)
