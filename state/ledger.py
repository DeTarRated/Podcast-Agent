"""账本最小读写封装：intent 先落、outcome 后更新（effect sandwich）。"""
import sqlite3
from typing import Optional

OUTCOME_STATUSES = ("success", "failed", "unknown")


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
) -> int:
    """付费调用发出前落 intent 行；call_ref 重复时由 UNIQUE 约束拒绝。"""
    cur = conn.execute(
        """
        INSERT INTO ledger (episode_id, purpose, provider, model,
                            usage_basis, amount_est, call_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (episode_id, purpose, provider, model, usage_basis, amount_est, call_ref),
    )
    conn.commit()
    return int(cur.lastrowid)


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


def reconcile_success(conn: sqlite3.Connection, call_ref: str) -> None:
    """对账核销：仅允许 unknown → success。

    用于人工/半自动对账拿到明确服务端 ID、且同 ID 完整结果已落盘后，
    把「结果未知」的调用核销为成功。intent / failed / success 均拒绝，
    不放宽 record_outcome 的通用语义，不允许终态任意跳转。
    """
    cur = conn.execute(
        "UPDATE ledger SET status = 'success' WHERE call_ref = ? AND status = 'unknown'",
        (call_ref,),
    )
    if cur.rowcount != 1:
        row = get_by_ref(conn, call_ref)
        current = row["status"] if row is not None else None
        conn.rollback()
        raise ValueError(f"reconcile_success 要求 unknown 态，当前为 {current!r}")
    conn.commit()
