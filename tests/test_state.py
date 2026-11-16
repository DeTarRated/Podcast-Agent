"""state 层最小读写封装测试。

模拟场景：
- Episode 创建/查询、guid+feed 唯一约束、同 guid 不同 feed 可共存
- 状态推进与非法状态被 CHECK 约束拒绝
- SQLite旧v3账本迁移时已存在的付费记录默认视为claimed，禁止恢复重发
- ledger CLI authorize 的 deferred intent 在 claim 前不标已发起，claim单次转换；主 Agent authorize直接标已发起
- 音频固化（sha256 + 时长写入）
- 产物清单生命周期与删除 Episode 后的外键级联
- 账本 intent→outcome 两段式、call_ref 重复拒绝、unknown 对账态、非法 outcome/金额拒绝
- reconcile_success 核销：仅 unknown→success，intent/failed/success/不存在均拒绝；外部请求claim仅一次
"""
import sqlite3
from decimal import Decimal

import pytest

from state import db, episode, ledger, manifest


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()


def test_episode_create_get_and_unique(conn):
    ep_id = episode.create(
        conn,
        guid="guid-1",
        feed="https://feeds.example.com/pl",
        title="Test Episode",
        published_at="2026-11-05T10:00:00+00:00",
        duration_secs=1800.0,
        enclosure="https://dax.example.com/a.mp3",
    )
    row = episode.get(conn, ep_id)
    assert row["guid"] == "guid-1"
    assert row["status"] == "discovered"

    # guid + feed 唯一：同 guid 同 feed 重复创建被拒绝
    with pytest.raises(sqlite3.IntegrityError):
        episode.create(conn, guid="guid-1", feed="https://feeds.example.com/pl")

    # 同 guid 不同 feed 是不同节目，允许
    other = episode.create(
        conn, guid="guid-1", feed="https://feeds.example.com/other"
    )
    assert episode.get(conn, other)["feed"] == "https://feeds.example.com/other"

    assert episode.get_by_guid_feed(conn, "guid-1", "https://feeds.example.com/pl")["id"] == ep_id


def test_episode_status_transition_and_invalid_status(conn):
    ep_id = episode.create(conn, guid="g", feed="f")
    episode.update_status(conn, ep_id, "downloaded")
    assert episode.get(conn, ep_id)["status"] == "downloaded"

    # 非法状态由 CHECK 约束拒绝
    with pytest.raises(sqlite3.IntegrityError):
        episode.update_status(conn, ep_id, "flying")


def test_episode_set_audio_first_version(conn):
    ep_id = episode.create(conn, guid="g", feed="f")
    episode.set_audio(conn, ep_id, sha256="abc123", duration_secs=2940.0)
    row = episode.get(conn, ep_id)
    assert row["audio_sha256"] == "abc123"
    assert row["duration_secs"] == 2940.0


def test_manifest_lifecycle(conn):
    ep_id = episode.create(conn, guid="g", feed="f")
    art_id = manifest.add(
        conn,
        episode_id=ep_id,
        step="scribe_raw",
        kind="json",
        path="ep1/scribe_raw.json",
    )
    manifest.finish(conn, art_id, sha256="deadbeef")

    arts = manifest.list_for(conn, ep_id)
    assert len(arts) == 1
    assert arts[0]["status"] == "done"
    assert arts[0]["sha256"] == "deadbeef"


def test_manifest_episode_cascade(conn):
    ep_id = episode.create(conn, guid="g", feed="f")
    manifest.add(conn, episode_id=ep_id, step="en_srt", kind="srt", path="ep1/en.srt")
    # 外键开启：删除 Episode 级联删除产物清单
    conn.execute("DELETE FROM episode WHERE id = ?", (ep_id,))
    conn.commit()
    assert manifest.list_for(conn, ep_id) == []


def test_ledger_intent_outcome_flow(conn):
    ep_id = episode.create(conn, guid="g", feed="f")
    call_ref = "scribe:g:abc123"
    ledger.record_intent(
        conn,
        episode_id=ep_id,
        purpose="transcribe",
        provider="elevenlabs",
        model="scribe_v2",
        usage_basis="2940 sec",
        amount_est=0.18,
        call_ref=call_ref,
    )
    assert ledger.get_by_ref(conn, call_ref)["status"] == "intent"

    ledger.record_outcome(conn, call_ref, status="success", amount_est=0.18)
    row = ledger.get_by_ref(conn, call_ref)
    assert row["status"] == "success"
    assert row["amount_est"] == 0.18


@pytest.mark.parametrize("amount", [-0.01, float("nan"), float("inf"), -float("inf"), True, Decimal("1e10000"), Decimal("1e-10000")])
def test_ledger_rejects_invalid_amount_before_write(conn, amount):
    with pytest.raises(ValueError, match="有限的非负金额"):
        ledger.record_intent(
            conn, episode_id=None, purpose="classify", provider="deepseek",
            model="mock", usage_basis="phase=intent", amount_est=amount,
            call_ref=f"invalid:{amount}",
        )
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_v3_migration_marks_existing_ledger_calls_as_started(tmp_path):
    from state.migrations import migrate

    conn = sqlite3.connect(tmp_path / "v3.db")
    conn.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, call_ref TEXT, status TEXT, amount_est REAL)")
    conn.execute("INSERT INTO ledger (call_ref,status,amount_est) VALUES ('old-call','intent',0.2)")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    migrate(conn)
    row = conn.execute("SELECT request_started FROM ledger WHERE call_ref='old-call'").fetchone()
    assert row[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    conn.close()


def test_ledger_cli_authorize_and_deferred_claim(tmp_path, monkeypatch, capsys):
    from state.ledger_cli import main

    path = str(tmp_path / "cli-ledger.db")
    setup = db.connect(path); db.init_schema(setup)
    episode_id = episode.create(setup, guid="cli-episode", feed="test")
    setup.close()
    monkeypatch.setattr("pipeline.cost_guard.load_limits", lambda _episode_id: [])
    common = ["--episode-id", str(episode_id), "--purpose", "translate", "--provider", "deepseek",
              "--model", "model-x", "--usage-basis", "estimate=max", "--amount-est", "0.2",
              "--db", path]
    assert main(["authorize", *common, "--call-ref", "child-call", "--defer-start"]) == 0
    capsys.readouterr()
    conn = db.connect(path); db.init_schema(conn)
    assert conn.execute("SELECT request_started FROM ledger WHERE call_ref='child-call'").fetchone()[0] == 0
    conn.close()
    assert main(["claim", "--call-ref", "child-call", "--db", path]) == 0
    capsys.readouterr()
    conn = db.connect(path)
    assert conn.execute("SELECT request_started FROM ledger WHERE call_ref='child-call'").fetchone()[0] == 1
    conn.close()
    assert main(["authorize", *common, "--call-ref", "root-call"]) == 0
    capsys.readouterr()
    conn = db.connect(path)
    assert conn.execute("SELECT request_started FROM ledger WHERE call_ref='root-call'").fetchone()[0] == 1
    conn.close()


def test_ledger_claim_request_is_single_use_and_requires_estimate(conn):
    ledger.record_intent(conn, episode_id=None, purpose="translate", provider="deepseek",
                         model="model-x", usage_basis="reserved", amount_est=0.25,
                         call_ref="claim:once", request_started=False)
    ledger.claim_request(conn, "claim:once")
    row = ledger.get_by_ref(conn, "claim:once")
    assert row["request_started"] == 1
    with pytest.raises(ValueError, match="claim 不可用"):
        ledger.claim_request(conn, "claim:once")

    ledger.record_intent(conn, episode_id=None, purpose="translate", provider="deepseek",
                         model="model-x", usage_basis="unpriced", amount_est=None,
                         call_ref="claim:unpriced", request_started=False)
    with pytest.raises(ValueError, match="claim 不可用"):
        ledger.claim_request(conn, "claim:unpriced")
    assert ledger.get_by_ref(conn, "claim:unpriced")["request_started"] == 0


def test_ledger_duplicate_call_ref_rejected(conn):
    ledger.record_intent(
        conn,
        episode_id=None,
        purpose="classify",
        provider="google",
        model="gemini-flash",
        usage_basis="300 tokens",
        amount_est=0.001,
        call_ref="classify:title:1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_intent(
            conn,
            episode_id=None,
            purpose="classify",
            provider="google",
            model="gemini-flash",
            usage_basis="300 tokens",
            amount_est=0.001,
            call_ref="classify:title:1",
        )


def test_ledger_unknown_outcome_for_reconcile(conn):
    """结果未知：intent 推进到 unknown，可对账，不自动重跑。"""
    call_ref = "scribe:g:unknown"
    ledger.record_intent(
        conn,
        episode_id=None,
        purpose="transcribe",
        provider="elevenlabs",
        model="scribe_v2",
        usage_basis="unknown",
        amount_est=None,
        call_ref=call_ref,
    )
    ledger.record_outcome(conn, call_ref, status="unknown")
    row = ledger.get_by_ref(conn, call_ref)
    assert row["status"] == "unknown"
    assert row["amount_est"] is None

    # outcome 只能从 intent 推进：终态后重复写不生效
    ledger.record_outcome(conn, call_ref, status="failed")
    assert ledger.get_by_ref(conn, call_ref)["status"] == "unknown"


def test_ledger_invalid_outcome_status(conn):
    call_ref = "scribe:g:bad"
    ledger.record_intent(
        conn,
        episode_id=None,
        purpose="transcribe",
        provider="elevenlabs",
        model="scribe_v2",
        usage_basis="x",
        amount_est=None,
        call_ref=call_ref,
    )
    with pytest.raises(ValueError):
        ledger.record_outcome(conn, call_ref, status="maybe")


def _seed_ledger_with_status(conn, call_ref: str, status: str) -> None:
    ledger.record_intent(
        conn,
        episode_id=None,
        purpose="transcribe",
        provider="elevenlabs",
        model="scribe_v2",
        usage_basis="x",
        amount_est=None,
        call_ref=call_ref,
    )
    if status != "intent":
        ledger.record_outcome(conn, call_ref, status=status)


def test_ledger_reconcile_success_from_unknown(conn):
    _seed_ledger_with_status(conn, "r:unknown", "unknown")
    ledger.reconcile_success(conn, "r:unknown", amount_est=Decimal("0.25"), usage_basis="cost_usd=0.25")
    row = ledger.get_by_ref(conn, "r:unknown")
    assert row["status"] == "success"
    assert row["amount_est"] == pytest.approx(0.25)
    assert row["usage_basis"] == "cost_usd=0.25"


@pytest.mark.parametrize("status", ["intent", "failed", "success"])
def test_ledger_reconcile_success_rejects_other_statuses(conn, status):
    _seed_ledger_with_status(conn, f"r:{status}", status)
    with pytest.raises(ValueError):
        ledger.reconcile_success(conn, f"r:{status}")
    assert ledger.get_by_ref(conn, f"r:{status}")["status"] == status


def test_ledger_reconcile_success_missing_ref(conn):
    with pytest.raises(ValueError):
        ledger.reconcile_success(conn, "r:missing")


def test_pending_includes_resumable_statuses(conn):
    """可恢复状态（downloaded/transcribed/translated）必须重新列出，否则中断的期数永远不再处理。"""
    from state import execution

    ids = {}
    for status in ("discovered", "downloaded", "transcribed", "translated", "failed", "delivered"):
        episode_id = episode.create(conn, guid=f"g-{status}", feed="f")
        episode.update_status(conn, episode_id, status)
        ids[status] = episode_id
    pending = {row["id"] for row in execution.pending_episodes(conn, limit=20)}
    assert ids["discovered"] in pending
    assert ids["downloaded"] in pending
    assert ids["transcribed"] in pending
    assert ids["translated"] in pending
    assert ids["failed"] not in pending
    assert ids["delivered"] not in pending
