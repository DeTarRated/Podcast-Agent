"""执行层·付费调用预算准入：SQLite BEGIN IMMEDIATE 与 intent 同事务，作为金额占用。

PostgreSQL 保存预算配置；SQLite 账本保存权威金额和在途 intent。外部请求必须在此提交成功后发送。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from state import ledger
from state.postgres import db as postgres_db
from decimal import Decimal

from state.postgres.budget import (
    BudgetDataError,
    _amount,
    _scope_spend,
    open_authoritative_ledger,
)


def load_limits(episode_id: int | None) -> list[dict]:
    """读取全局／Episode 金额限额；无法归属的用户限额明确拒绝。"""
    conn = postgres_db.connect()
    try:
        if conn.execute(
            "SELECT 1 FROM budget_limit WHERE scope='user' AND enabled=TRUE LIMIT 1"
        ).fetchone() is not None:
            raise BudgetDataError("user scope 预算未接入付款主体归属，拒绝付费调用")
        rows = conn.execute(
            "SELECT scope,user_id,episode_id,period,max_amount FROM budget_limit "
            "WHERE enabled=TRUE AND scope='episode' AND episode_id=%s AND user_id IS NOT NULL",
            (episode_id,),
        ).fetchall() if episode_id is not None else []
        if rows:
            raise BudgetDataError("user-specific episode 预算未接入付款主体归属，拒绝付费调用")
        query = "SELECT scope,user_id,episode_id,period,max_amount FROM budget_limit WHERE enabled=TRUE AND scope='global'"
        limits = list(conn.execute(query).fetchall())
        if episode_id is not None:
            limits.extend(conn.execute(
                "SELECT scope,user_id,episode_id,period,max_amount FROM budget_limit "
                "WHERE enabled=TRUE AND scope='episode' AND episode_id=%s AND user_id IS NULL",
                (episode_id,),
            ).fetchall())
        return limits
    finally:
        conn.close()


def remaining_allowance(episode_id: int | None) -> Decimal | None:
    """本期在已结算费用后仍可用的额度；无金额限额时返回 None（不限制）。

    入队预留应按剩余额度计算，否则恢复执行会把已结算费用与整期上界重复计入。
    """
    limits = [item for item in load_limits(episode_id) if item.get("max_amount") is not None]
    if not limits:
        return None
    conn = postgres_db.connect()
    try:
        ledger_conn = open_authoritative_ledger()
        try:
            remaining = None
            for limit in limits:
                ceiling = _amount(limit["max_amount"], "max_amount")
                scope = "episode" if limit.get("episode_id") is not None else "global"
                spend = _scope_spend(
                    ledger_conn,
                    scope=scope,
                    period=limit.get("period") or "task",
                    user_id=limit.get("user_id"),
                    episode_id=episode_id,
                    now=datetime.now(timezone.utc),
                )
                headroom = ceiling - spend
                remaining = headroom if remaining is None else min(remaining, headroom)
            return remaining
        finally:
            ledger_conn.close()
    finally:
        conn.close()


def authorize_and_record_intent(
    conn,
    *,
    episode_id: int | None,
    purpose: str,
    provider: str,
    model: str,
    usage_basis: str,
    amount_est,
    call_ref: str,
    limits: list[dict] | None = None,
    now: datetime | None = None,
    request_started: bool = True,
) -> int:
    """在 SQLite 写锁内计算费用并写 intent，避免并发调用同时越过额度。"""
    estimate = _amount(amount_est, "amount_est")
    applicable = load_limits(episode_id) if limits is None else limits
    current = now or datetime.now(timezone.utc)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for limit in applicable:
            if limit["max_amount"] is None:
                continue
            scope = limit["scope"]
            if scope not in {"global", "episode"}:
                raise BudgetDataError(f"不支持的付费预算 scope: {scope}")
            spend = _scope_spend(
                conn, scope=scope, period=limit["period"], user_id=None,
                episode_id=episode_id if scope == "episode" else None, now=current,
            )
            ceiling = _amount(limit["max_amount"], "max_amount")
            if spend + estimate > ceiling:
                raise BudgetDataError(
                    f"预算超限: scope={scope} period={limit['period']} spend={spend} estimate={estimate} limit={ceiling}"
                )
        row_id = ledger.record_intent(
            conn, episode_id=episode_id, purpose=purpose, provider=provider, model=model,
            usage_basis=usage_basis, amount_est=estimate, call_ref=call_ref,
            request_started=request_started, commit=False,
        )
        conn.commit()
        return row_id
    except Exception:
        conn.rollback()
        raise
