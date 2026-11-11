"""PostgreSQL 预算执行：在领取付费任务前检查额度、周期和 running 背压。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from state.ledger_report import _usage_cost
from state.postgres.user import budget_for


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: Optional[str] = None
    retry_at: Optional[datetime] = None
    current_spend: Decimal = Decimal(0)
    estimated_cost: Optional[Decimal] = None


def _estimated(job: dict) -> Optional[Decimal]:
    payload = job.get("payload") or {}
    value = payload.get("estimated_cost", payload.get("amount_est"))
    if value is None:
        return None
    return Decimal(str(value))


def _period_start(now: datetime, period: str) -> Optional[datetime]:
    if period == "task":
        return None
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _scope_spend(conn, *, scope: str, period: str, user_id: Optional[int], episode_id: Optional[int], now: datetime) -> Decimal:
    conditions = ["TRUE"]
    params: list = []
    if scope == "episode":
        conditions.append("l.episode_id = %s")
        params.append(episode_id)
    elif scope == "user":
        conditions.append("""
            l.episode_id IN (
                SELECT resource_id FROM resource_grant
                WHERE user_id = %s AND resource_type = 'episode'
            )
        """)
        params.append(user_id)
    start = _period_start(now, period)
    if start is not None:
        conditions.append("l.triggered_at >= %s")
        params.append(start)
    rows = conn.execute(
        f"""
        SELECT l.amount_est, l.usage_basis FROM ledger l
        WHERE {' AND '.join(conditions)}
        """,
        params,
    ).fetchall()
    total = Decimal(0)
    for row in rows:
        if row["amount_est"] is not None:
            total += Decimal(str(row["amount_est"]))
        else:
            model_cost = _usage_cost(row["usage_basis"])
            if model_cost is not None:
                total += Decimal(str(model_cost))
    return total


def _running_count(conn, *, scope: str, user_id: Optional[int], episode_id: Optional[int]) -> int:
    conditions = ["status = 'running'"]
    params: list = []
    if scope == "user":
        conditions.append("user_id = %s")
        params.append(user_id)
    elif scope == "episode":
        conditions.append("episode_id = %s")
        params.append(episode_id)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM job WHERE {' AND '.join(conditions)}",
        params,
    ).fetchone()
    return int(row["count"])


def check_budget(conn, job: dict, *, now: Optional[datetime] = None) -> BudgetDecision:
    current = now or datetime.now(timezone.utc)
    estimate = _estimated(job)
    scopes = [("global", None, None)]
    if job.get("user_id") is not None:
        scopes.append(("user", job["user_id"], None))
    if job.get("episode_id") is not None:
        scopes.append(("episode", job["user_id"], job["episode_id"]))

    for scope, user_id, episode_id in scopes:
        period = "day" if scope != "episode" else "task"
        limit = budget_for(
            conn, scope=scope, period=period, user_id=user_id, episode_id=episode_id
        )
        if limit is None:
            continue
        if limit["max_running"] is not None and _running_count(
            conn, scope=scope, user_id=user_id, episode_id=episode_id
        ) >= limit["max_running"]:
            return BudgetDecision(False, "max_running", current + timedelta(seconds=30))
        if limit["max_amount"] is not None:
            if estimate is None:
                return BudgetDecision(False, "cost_unknown")
            spend = _scope_spend(
                conn, scope=scope, period=period, user_id=user_id,
                episode_id=episode_id, now=current,
            )
            if spend + estimate > Decimal(str(limit["max_amount"])):
                return BudgetDecision(False, "budget_exceeded", current_spend=spend, estimated_cost=estimate)

    if job.get("budget") is not None:
        if estimate is None:
            return BudgetDecision(False, "cost_unknown")
        spend = _scope_spend(
            conn, scope="episode", period="task", user_id=job.get("user_id"),
            episode_id=job.get("episode_id"), now=current,
        )
        if spend + estimate > Decimal(str(job["budget"])):
            return BudgetDecision(False, "budget_exceeded", current_spend=spend, estimated_cost=estimate)
    return BudgetDecision(True, estimated_cost=estimate)
