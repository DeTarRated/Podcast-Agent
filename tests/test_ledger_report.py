"""账本只读管理报表测试；不修改任何账目。

模拟场景：
- 混合账本估算、模型 usage 成本与未对账记录的汇总
- 按用途、状态、供应商和 Episode 筛选
- 无金额不记零、异常状态计数、明细 limit 与汇总分离
"""
import pytest

from state import db, episode, ledger, ledger_report


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
