"""执行层付费预算准入测试；全程 fake SQLite，不连接供应商或 PostgreSQL。

模拟场景：预算内 intent 原子落账、超限/未知历史费用/非法估算失败关闭、并发预留互斥、Episode 限额隔离、priced unknown与重复call_ref占用。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from state import db
from state.postgres.budget import BudgetDataError
from pipeline.cost_guard import authorize_and_record_intent

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def _seed_episode(conn, episode_id):
    if conn.execute("SELECT 1 FROM episode WHERE id=?", (episode_id,)).fetchone() is None:
        conn.execute(
            "INSERT INTO episode (id,guid,feed) VALUES (?,?,?)",
            (episode_id, f"episode-{episode_id}", "test-feed"),
        )
        conn.commit()


def _intent(conn, *, ref: str, estimate, episode_id=1, limits=None):
    if episode_id is not None:
        _seed_episode(conn, episode_id)
    return authorize_and_record_intent(
        conn, episode_id=episode_id, purpose="translate", provider="fake", model="fake",
        usage_basis="phase=intent", amount_est=estimate, call_ref=ref, limits=limits or [], now=NOW,
    )


def test_authorized_intent_is_atomically_committed_as_cost_reservation(tmp_path):
    conn = db.connect(tmp_path / "ledger.db"); db.init_schema(conn)
    _intent(conn, ref="call:1", estimate=Decimal("0.3"), limits=[{
        "scope": "global", "period": "day", "max_amount": Decimal("0.5"),
    }])
    row = conn.execute("SELECT status,amount_est,request_started FROM ledger WHERE call_ref='call:1'").fetchone()
    assert row["status"] == "intent" and row["amount_est"] == pytest.approx(0.3)
    assert row["request_started"] == 1
    conn.close()


def test_budget_exceeded_writes_no_intent(tmp_path):
    conn = db.connect(tmp_path / "ledger.db"); db.init_schema(conn)
    _intent(conn, ref="call:1", estimate=0.3, limits=[{
        "scope": "global", "period": "day", "max_amount": Decimal("0.5"),
    }])
    with pytest.raises(BudgetDataError, match="预算超限"):
        _intent(conn, ref="call:2", estimate=0.3, limits=[{
            "scope": "global", "period": "day", "max_amount": Decimal("0.5"),
        }])
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
    conn.close()


def test_unpriced_prior_intent_and_unknown_block_new_call(tmp_path):
    conn = db.connect(tmp_path / "ledger.db"); db.init_schema(conn)
    _seed_episode(conn, 1)
    conn.execute(
        "INSERT INTO ledger (episode_id,purpose,call_ref,status,triggered_at) VALUES (1,'review','prior','unknown',?)",
        (NOW.isoformat(),),
    ); conn.commit()
    with pytest.raises(BudgetDataError, match="未定价"):
        _intent(conn, ref="new", estimate=0.1, limits=[{
            "scope": "global", "period": "day", "max_amount": Decimal("1"),
        }])
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
    conn.close()


def test_priced_unknown_remains_in_budget_and_duplicate_ref_rolls_back(tmp_path):
    conn = db.connect(tmp_path / "ledger.db"); db.init_schema(conn)
    _seed_episode(conn, 1)
    conn.execute(
        "INSERT INTO ledger (episode_id,purpose,amount_est,call_ref,status,triggered_at) "
        "VALUES (1,'translate',0.4,'unknown-priced','unknown',?)",
        (NOW.isoformat(),),
    ); conn.commit()
    cap = [{"scope": "global", "period": "day", "max_amount": Decimal("0.5")}]
    with pytest.raises(BudgetDataError, match="预算超限"):
        _intent(conn, ref="new-call", estimate=0.2, limits=cap)
    with pytest.raises(Exception):
        _intent(conn, ref="unknown-priced", estimate=0.1, limits=[])
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
    conn.close()


def test_concurrent_call_reservations_cannot_exceed_limit(tmp_path):
    path = tmp_path / "ledger.db"
    setup = db.connect(path); db.init_schema(setup); _seed_episode(setup, 1); setup.close()
    limits = [{"scope": "global", "period": "day", "max_amount": Decimal("0.5")}]

    def authorize(ref):
        conn = db.connect(path)
        try:
            _intent(conn, ref=ref, estimate=Decimal("0.4"), limits=limits)
            return "allowed"
        except BudgetDataError:
            return "blocked"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(authorize, ["parallel:a", "parallel:b"]))
    assert sorted(results) == ["allowed", "blocked"]
    check = db.connect(path)
    assert check.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
    check.close()


def test_episode_cap_counts_only_that_episode(tmp_path):
    conn = db.connect(tmp_path / "ledger.db"); db.init_schema(conn)
    _seed_episode(conn, 1); _seed_episode(conn, 2)
    conn.execute(
        "INSERT INTO ledger (episode_id,purpose,amount_est,call_ref,status,triggered_at) VALUES (2,'translate',0.45,'other','success',?)",
        (NOW.isoformat(),),
    ); conn.commit()
    _intent(conn, ref="episode-1", estimate=0.4, episode_id=1, limits=[{
        "scope": "episode", "period": "task", "episode_id": 1, "max_amount": Decimal("0.5"),
    }])
    conn.close()
