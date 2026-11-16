"""账本只读管理报表测试；不修改任何账目。

模拟场景：
- 混合账本估算、模型 usage 成本与未对账记录的汇总
- 按用途、状态、供应商和 Episode 筛选
- 无金额不记零、非法模型成本／预算金额拒绝、SQLite 权威账本读取与未知费用失败关闭
- 异常状态计数、明细 limit 与汇总分离
"""
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from state import db, episode, ledger, ledger_report
from state.postgres.budget import (
    BudgetDataError, _estimated, _scope_spend, open_authoritative_ledger,
)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "usage.db")
    db.init_schema(connection)
    episode_id = episode.create(connection, guid="g1", feed="feed", title="Episode")
    for purpose, status, amount, usage in [
        ("transcribe", "success", 0.13744, "audio_duration_secs=2249"),
        ("translate", "unknown", None, "phase=intent"),
        ("highlights", "success", None, "input_tokens=100;output_tokens=20;cost_usd=0.03773748"),
    ]:
        call_ref = f"call:{purpose}"
        ledger.record_intent(
            connection,
            episode_id=episode_id,
            purpose=purpose,
            provider="elevenlabs" if purpose == "transcribe" else "deepseek",
            model="mock",
            usage_basis="phase=intent",
            amount_est=amount,
            call_ref=call_ref,
        )
        ledger.record_outcome(connection, call_ref, status=status, usage_basis=usage)
    yield connection
    connection.close()


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "NaN", "Infinity", Decimal("1e-10000")])
def test_budget_estimate_rejects_invalid_amount(value):
    with pytest.raises(BudgetDataError, match="有限的非负金额|可表示范围"):
        _estimated({"payload": {"estimated_cost": value}})


def test_authoritative_ledger_is_read_only_and_counts_priced_rows(tmp_path):
    path = tmp_path / "authoritative.db"
    writer = db.connect(path)
    db.init_schema(writer)
    writer.execute(
        "INSERT INTO ledger (purpose,amount_est,call_ref,status,triggered_at) VALUES (?,?,?,?,?)",
        ("transcribe", 0.25, "priced", "success", "2026-11-15T10:00:00+00:00"),
    )
    writer.commit(); writer.close()
    source = open_authoritative_ledger(path)
    try:
        total = _scope_spend(
            source, scope="global", period="day", user_id=None, episode_id=None,
            now=datetime(2026, 11, 15, 12, tzinfo=timezone.utc),
        )
        assert total == 0.25
        with pytest.raises(sqlite3.OperationalError):
            source.execute("INSERT INTO ledger (purpose,call_ref) VALUES ('x','write')")
    finally:
        source.close()


def test_authoritative_unpriced_unknown_fails_closed(tmp_path):
    path = tmp_path / "authoritative.db"
    source = db.connect(path); db.init_schema(source)
    source.execute(
        "INSERT INTO ledger (purpose,call_ref,status,triggered_at) VALUES (?,?,?,?)",
        ("translate", "unknown", "unknown", "2026-11-15T10:00:00+00:00"),
    )
    source.commit(); source.close()
    reader = open_authoritative_ledger(path)
    try:
        with pytest.raises(BudgetDataError, match="未定价"):
            _scope_spend(
                reader, scope="global", period="day", user_id=None, episode_id=None,
                now=datetime(2026, 11, 15, 12, tzinfo=timezone.utc),
            )
    finally:
        reader.close()


def test_summary_distinguishes_cost_sources_and_unpriced(conn):
    report = ledger_report.usage_report(conn)
    assert report["summary"]["call_count"] == 3
    assert report["summary"]["episode_count"] == 1
    assert report["summary"]["total_amount"] == 0.175177
    assert report["summary"]["unknown_count"] == 1
    assert report["summary"]["unpriced_count"] == 1
    costs = {item["purpose"]: item for item in report["items"]}
    assert costs["transcribe"]["cost_source"] == "ledger_estimate"
    assert costs["highlights"]["cost_source"] == "model_usage"
    assert costs["translate"]["cost_amount"] is None


@pytest.mark.parametrize("value", ["cost_usd=NaN", "cost_usd=Infinity", "cost_usd=-0.1", "cost_usd=not-a-number"])
def test_invalid_model_cost_is_unpriced_not_zero_or_negative(value):
    assert ledger_report._usage_cost(value) is None


def test_filters_and_limit_do_not_change_summary(conn):
    filtered = ledger_report.usage_report(conn, purpose="highlights", status="success", provider="deepseek")
    assert filtered["summary"]["call_count"] == 1
    assert filtered["summary"]["total_amount"] == 0.037737
    assert filtered["items"][0]["purpose"] == "highlights"
    unknown = ledger_report.usage_report(conn, status="unknown")
    assert unknown["summary"]["call_count"] == 1
    assert unknown["summary"]["total_amount"] == 0
    limited = ledger_report.usage_report(conn, limit=1)
    assert len(limited["items"]) == 1
    assert limited["truncated"] is True
    assert limited["summary"]["call_count"] == 3
    with pytest.raises(ValueError):
        ledger_report.usage_report(conn, limit=0)
