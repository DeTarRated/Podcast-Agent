"""账本最小读写封装：intent 先落、outcome 后更新（effect sandwich）。"""
import math
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Optional

OUTCOME_STATUSES = ("success", "failed", "unknown")


def _validated_amount(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("amount_est 必须为有限的非负金额")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("amount_est 必须为有限的非负金额") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount_est 必须为有限的非负金额")
    try:
        converted = float(amount)
    except (OverflowError, ValueError):
        raise ValueError("amount_est 必须为有限的非负金额，且不超出 SQLite 可表示范围") from None
    if not math.isfinite(converted) or (amount > 0 and converted == 0):
        raise ValueError("amount_est 必须为有限的非负金额，且不超出 SQLite 可表示范围")
    return converted


def record_intent(
    conn: sqlite3.Connection,
    *,
    episode_id: Optional[int],
    purpose: str,
    provider: str,
    model: str,
    usage_basis: str,
    amount_est: Optional[float],
    call_ref: str,
    request_started: bool = False,
    commit: bool = True,
) -> int:
    """付费调用发出前落 intent；request_started 记录是否已一次性claim 外部调用。"""
    if type(request_started) is not bool:
        raise ValueError("request_started 必须为 boolean")
    amount_est = _validated_amount(amount_est)
    cur = conn.execute(
        """
        INSERT INTO ledger (episode_id, purpose, provider, model,
                            usage_basis, amount_est, request_started, call_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (episode_id, purpose, provider, model, usage_basis, amount_est, int(request_started), call_ref),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def claim_request(conn: sqlite3.Connection, call_ref: str) -> None:
    """仅一次将已预算 intent claim 为即将发出外部请求，恢复时拒绝二次 claim。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE ledger SET request_started=1 "
            "WHERE call_ref=? AND status='intent' AND amount_est IS NOT NULL AND request_started=0",
            (call_ref,),
        )
        if cur.rowcount != 1:
            row = get_by_ref(conn, call_ref)
            state = None if row is None else (row["status"], row["request_started"], row["amount_est"])
            raise ValueError(f"model request claim 不可用: {state!r}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def record_outcome(
    conn: sqlite3.Connection,
    call_ref: str,
    *,
    status: str,
    amount_est: Optional[float] = None,
    usage_basis: Optional[str] = None,
) -> None:
    """调用结束后把 intent 行推进到终态；只允许从 intent 出发。

    status=unknown 用于「调用已发出但结果未知」的可对账状态，
    此时不得自动重跑付费调用。
    usage_basis 用于在 outcome 阶段回填真实用量（如 token 数）；未提供时保留 intent 原值。
    """
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"invalid outcome status: {status}")
    amount_est = _validated_amount(amount_est)
    sets = ["status = ?"]
    params: list = [status]
    if amount_est is not None:
        sets.append("amount_est = ?")
        params.append(amount_est)
    if usage_basis is not None:
        sets.append("usage_basis = ?")
        params.append(usage_basis)
    params.append(call_ref)
    conn.execute(
        f"UPDATE ledger SET {', '.join(sets)} WHERE call_ref = ? AND status = 'intent'",
        params,
    )
    conn.commit()


def get_by_ref(conn: sqlite3.Connection, call_ref: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ledger WHERE call_ref = ?", (call_ref,)
    ).fetchone()


def reconcile_success(
    conn: sqlite3.Connection,
    call_ref: str,
    *,
    amount_est=None,
    usage_basis: Optional[str] = None,
) -> None:
    """对账核销：仅允许 unknown → success，可回填已确认的用量与金额。"""
    amount_est = _validated_amount(amount_est)
    assignments = ["status = 'success'"]
    params: list = []
    if amount_est is not None:
        assignments.append("amount_est = ?")
        params.append(amount_est)
    if usage_basis is not None:
        assignments.append("usage_basis = ?")
        params.append(usage_basis)
    params.append(call_ref)
    cur = conn.execute(
        f"UPDATE ledger SET {', '.join(assignments)} WHERE call_ref = ? AND status = 'unknown'",
        params,
    )
    if cur.rowcount != 1:
        row = get_by_ref(conn, call_ref)
        current = row["status"] if row is not None else None
        conn.rollback()
        raise ValueError(f"reconcile_success 要求 unknown 态，当前为 {current!r}")
    conn.commit()
