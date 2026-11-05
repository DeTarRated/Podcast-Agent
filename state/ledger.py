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
) -> None:
    """调用结束后把 intent 行推进到终态；只允许从 intent 出发。

    status=unknown 用于「调用已发出但结果未知」的可对账状态，
    此时不得自动重跑付费调用。
    """
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"invalid outcome status: {status}")
    if amount_est is None:
        conn.execute(
            "UPDATE ledger SET status = ? WHERE call_ref = ? AND status = 'intent'",
            (status, call_ref),
        )
    else:
        conn.execute(
            "UPDATE ledger SET status = ?, amount_est = ? "
            "WHERE call_ref = ? AND status = 'intent'",
            (status, amount_est, call_ref),
        )
    conn.commit()


def get_by_ref(conn: sqlite3.Connection, call_ref: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ledger WHERE call_ref = ?", (call_ref,)
    ).fetchone()
