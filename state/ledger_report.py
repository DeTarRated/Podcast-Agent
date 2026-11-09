"""跨层数据契约·账本只读汇总与筛选。

不修改 ledger；金额区分账本估算、模型 usage 中的成本与未对账。
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def _usage_cost(usage_basis: Optional[str]) -> Optional[float]:
    if not usage_basis:
        return None
    for part in usage_basis.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == "cost_usd":
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _cost_fields(row: sqlite3.Row) -> tuple[Optional[float], Optional[float], Optional[float], str]:
    ledger_amount = row["amount_est"]
    model_cost = _usage_cost(row["usage_basis"])
    if ledger_amount is not None:
        return ledger_amount, model_cost, float(ledger_amount), "ledger_estimate"
    if model_cost is not None:
        return None, model_cost, model_cost, "model_usage"
    return None, None, None, "unpriced"


def usage_report(
    conn: sqlite3.Connection,
    *,
    episode_id: Optional[int] = None,
    purpose: Optional[str] = None,
    status: Optional[str] = None,
    provider: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """返回筛选后的全部汇总及前 limit 条明细，汇总不受 limit 截断。"""
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit 必须在 1..500")
    conditions = []
    params: list = []
    if episode_id is not None:
        conditions.append("l.episode_id = ?")
        params.append(episode_id)
    if purpose:
        conditions.append("l.purpose = ?")
        params.append(purpose)
    if status:
        conditions.append("l.status = ?")
        params.append(status)
    if provider:
        conditions.append("l.provider = ?")
        params.append(provider)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"""
        SELECT l.*, e.guid, e.title, e.feed
        FROM ledger l
        LEFT JOIN episode e ON e.id = l.episode_id
        {where}
        ORDER BY l.triggered_at DESC, l.id DESC
        """,
        params,
    ).fetchall()

    items = []
    purpose_totals: dict[str, dict] = {}
    total = 0.0
    unpriced = 0
    for row in rows:
        ledger_amount, model_cost, cost, source = _cost_fields(row)
        if cost is None:
            unpriced += 1
        else:
            total += cost
        item = {
            "id": row["id"],
            "episode_id": row["episode_id"],
            "guid": row["guid"],
            "title": row["title"],
            "feed": row["feed"],
            "purpose": row["purpose"],
            "provider": row["provider"],
            "model": row["model"],
            "triggered_at": row["triggered_at"],
            "usage_basis": row["usage_basis"],
            "amount_est": ledger_amount,
            "model_cost": model_cost,
            "cost_amount": cost,
            "cost_source": source,
            "status": row["status"],
            "call_ref": row["call_ref"],
        }
        items.append(item)
        bucket = purpose_totals.setdefault(
            row["purpose"], {"count": 0, "amount": 0.0, "unpriced": 0}
        )
        bucket["count"] += 1
        if cost is None:
            bucket["unpriced"] += 1
        else:
            bucket["amount"] = round(bucket["amount"] + cost, 6)

    episodes = conn.execute(
        """
        SELECT DISTINCT e.id, e.title, e.guid
        FROM ledger l JOIN episode e ON e.id = l.episode_id
        ORDER BY e.title, e.id
        """
    ).fetchall()
    purposes = [row[0] for row in conn.execute("SELECT DISTINCT purpose FROM ledger ORDER BY purpose")]
    providers = [row[0] for row in conn.execute("SELECT DISTINCT provider FROM ledger WHERE provider IS NOT NULL ORDER BY provider")]
    statuses = [row[0] for row in conn.execute("SELECT DISTINCT status FROM ledger ORDER BY status")]
    return {
        "summary": {
            "call_count": len(rows),
            "episode_count": len({row["episode_id"] for row in rows if row["episode_id"] is not None}),
            "total_amount": round(total, 6),
            "unpriced_count": unpriced,
            "unknown_count": sum(row["status"] == "unknown" for row in rows),
            "intent_count": sum(row["status"] == "intent" for row in rows),
            "failed_count": sum(row["status"] == "failed" for row in rows),
            "currency": "USD-estimated",
        },
        "by_purpose": purpose_totals,
        "filters": {
            "episodes": [dict(row) for row in episodes],
            "purposes": purposes,
            "providers": providers,
            "statuses": statuses,
        },
        "items": items[:limit],
        "truncated": len(rows) > limit,
        "r2_note": "R2 存储与 A/B 类操作费用未纳入项目账本，请在 Cloudflare 控制台核对。",
    }
