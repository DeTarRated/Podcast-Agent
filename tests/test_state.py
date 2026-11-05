"""state 层最小读写封装测试。"""
import sqlite3

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
