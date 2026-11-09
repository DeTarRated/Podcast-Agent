"""审核领域服务与 CLI 测试；全部使用临时数据库和 SRT，零外部调用。

模拟场景：
- 新审核修订默认 12 小时，详情双语锚点、审核箱与本地媒体信息
- 保存修改与 request_id 幂等、重试返回首次修订、同键不同内容拒绝、旧 revision 拒绝
- 保留待审与恢复完整窗口、人工提前通过、到期自动通过与人工／自动并发唯一审批
- override 冲突和产物 hash 变化阻断审批
- 候选切换重新应用 override 并保存正确 variant
- 驳回范围合并、快照与幂等；approved／delivered 后再次编辑
- 多期自动审批单期失败隔离、CLI 单行 JSON 与领域错误
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.download import sha256_file
from review import service
from state import db, episode, manifest, review_state

START = "2026-11-01T12:00:00+00:00"
LATER = "2026-11-01T13:00:00+00:00"
DUE = "2026-11-02T01:00:00+00:00"
NOT_DUE = "2026-11-01T20:00:00+00:00"

SOURCE = """1
00:00:01,000 --> 00:00:03,000
Hello world

2
00:00:04,000 --> 00:00:06,000
Next sentence
"""
REVIEW = """1
00:00:01,000 --> 00:00:03,000
旧译文

2
00:00:04,000 --> 00:00:06,000
下一句
"""
SPLIT = """1
00:00:01,000 --> 00:00:01,500
旧

2
00:00:01,500 --> 00:00:03,000
译文

3
00:00:04,000 --> 00:00:06,000
下一句
"""


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "review.db")
    db.init_schema(connection)
    yield connection
    connection.close()


def _artifact(conn, episode_id, step, path):
    artifact_id = manifest.add(
        conn,
        episode_id=episode_id,
        step=step,
        kind="audio" if step == "download" else "srt",
        path=str(path),
        sha256=sha256_file(path),
    )
    manifest.finish(conn, artifact_id, sha256=sha256_file(path))
    return artifact_id


def _seed_episode(conn, root: Path, name="episode"):
    workspace = root / name
    workspace.mkdir()
    source = workspace / "en_fix.srt"
    review = workspace / "en_zh_fix.srt"
    split = workspace / "en_zh_split.srt"
    audio = workspace / "audio.mp3"
    source.write_text(SOURCE, encoding="utf-8")
    review.write_text(REVIEW, encoding="utf-8")
    split.write_text(SPLIT, encoding="utf-8")
    audio.write_bytes(b"fake-audio")
    episode_id = episode.create(
        conn, guid=f"guid-{name}", feed="feed", title=f"Title {name}", published_at=START
    )
    episode.set_audio(conn, episode_id, sha256=sha256_file(audio), duration_secs=6.0)
    source_id = _artifact(conn, episode_id, "transcript_fix", source)
    review_id = _artifact(conn, episode_id, "review", review)
    split_id = _artifact(conn, episode_id, "split", split)
    _artifact(conn, episode_id, "download", audio)
    revision = service.create_review_revision(
        conn,
        episode_id=episode_id,
        base_artifact_id=review_id,
        variant="review",
        now=START,
    )
    return {
        "episode_id": episode_id,
        "workspace": workspace,
        "source_id": source_id,
        "review_id": review_id,
        "split_id": split_id,
        "revision_id": revision["revision_id"],
    }


def _save(conn, seeded, *, request="save-1", after="人工译文", now=LATER):
    return service.save_override(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=seeded["revision_id"],
        base_entry_number=1,
        source_artifact_id=seeded["source_id"],
        start_ms=1000,
        end_ms=3000,
        source_text="Hello world",
        after_text=after,
        request_id=request,
        now=now,
    )


def _mark_delivered(conn, seeded):
    approval = service.approve(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=seeded["revision_id"],
        method="manual",
        request_id="publish-approval",
        now=LATER,
    )
    conn.execute(
        "UPDATE review_revision SET status='delivered' WHERE id=?",
        (seeded["revision_id"],),
    )
    conn.execute(
        "UPDATE episode SET status='delivered' WHERE id=?", (seeded["episode_id"],)
    )
    cur = conn.execute(
        """
        INSERT INTO delivery (
            approval_id, episode_id, artifact_id, status, is_current,
            highlights_json, sha256
        ) VALUES (?, ?, ?, 'delivered', 1, '{}', ?)
        """,
        (
            approval["approval_id"], seeded["episode_id"], approval["artifact_id"],
            approval["sha256"],
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_new_revision_detail_inbox_and_media(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    current = review_state.current_for(conn, seeded["episode_id"])
    assert current["auto_approve_at"] == "2026-11-02T00:00:00+00:00"
    assert current["auto_approve_paused"] == 0

    detail = service.detail(conn, episode_id=seeded["episode_id"])
    assert detail["cues"][0]["source_text"] == "Hello world"
    assert detail["cues"][0]["source_artifact_id"] == seeded["source_id"]
    assert len(detail["candidates"]) == 2
    inbox = service.inbox(conn)
    assert inbox["count"] == 1
    assert inbox["items"][0]["has_conflicts"] is False
    media = service.media_info(conn, episode_id=seeded["episode_id"])
    assert media["size"] == len(b"fake-audio")
    assert media["sha256"] == sha256_file(seeded["workspace"] / "audio.mp3")


def test_media_info_supports_legacy_conventional_audio_path(conn, tmp_path, monkeypatch):
    seeded = _seed_episode(conn, tmp_path)
    conn.execute("DELETE FROM artifact WHERE episode_id=? AND step='download'", (seeded["episode_id"],))
    conn.commit()
    legacy = tmp_path / "var" / "episodes" / str(seeded["episode_id"])
    legacy.mkdir(parents=True)
    (legacy / "audio.mp3").write_bytes((seeded["workspace"] / "audio.mp3").read_bytes())
    monkeypatch.setattr(service, "REPO_ROOT", tmp_path)
    media = service.media_info(conn, episode_id=seeded["episode_id"])
    assert Path(media["path"]) == legacy / "audio.mp3"
    assert media["sha256"] == sha256_file(legacy / "audio.mp3")


def test_save_override_is_persistent_and_idempotent(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    saved = _save(conn, seeded)
    assert saved["revision_id"] != seeded["revision_id"]
    assert saved["has_conflicts"] is False
    assert saved["auto_approve_at"] == "2026-11-02T01:00:00+00:00"
    detail = service.detail(conn, episode_id=seeded["episode_id"])
    assert detail["cues"][0]["text"] == "人工译文"
    old = review_state.get_revision(conn, seeded["revision_id"])
    assert old["status"] == "superseded"
    assert old["is_current"] == 0

    reused = _save(conn, seeded, request="save-1")
    assert reused["reused"] is True
    assert conn.execute("SELECT COUNT(*) FROM override").fetchone()[0] == 1
    with pytest.raises(service.ReviewError) as error:
        _save(conn, seeded, request="save-1", after="不同内容")
    assert error.value.code == "invalid_state"


def test_override_retry_returns_original_result_after_later_edit(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    first = _save(conn, seeded, request="first-save", after="第一次修改")
    second = service.save_override(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=first["revision_id"],
        base_entry_number=1,
        source_artifact_id=seeded["source_id"],
        start_ms=1000,
        end_ms=3000,
        source_text="Hello world",
        after_text="第二次修改",
        supersedes_override_id=first["override_id"],
        request_id="second-save",
        now="2026-11-01T14:00:00+00:00",
    )
    reused = _save(conn, seeded, request="first-save", after="第一次修改")
    assert reused["reused"] is True
    assert reused["revision_id"] == first["revision_id"]
    assert reused["revision_id"] != second["revision_id"]
    assert conn.execute("SELECT COUNT(*) FROM override").fetchone()[0] == 2


def test_stale_revision_cannot_overwrite_newer_edit(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    _save(conn, seeded)
    with pytest.raises(service.ReviewError) as error:
        _save(conn, seeded, request="stale-save", after="旧页面覆盖")
    assert error.value.code == "stale_revision"
    assert conn.execute("SELECT COUNT(*) FROM override").fetchone()[0] == 1
    assert service.detail(conn, episode_id=seeded["episode_id"])["cues"][0]["text"] == "人工译文"


def test_hold_resume_and_auto_window(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    held = service.hold_review(
        conn, episode_id=seeded["episode_id"], base_revision_id=seeded["revision_id"]
    )
    assert held["auto_approve_paused"] is True
    assert held["auto_approve_at"] is None
    skipped = service.auto_approve_due(conn, now=DUE)
    assert skipped["approved"] == 0

    resumed = service.resume_review(
        conn, episode_id=seeded["episode_id"], base_revision_id=seeded["revision_id"], now=DUE
    )
    assert resumed["auto_approve_at"] == "2026-11-02T13:00:00+00:00"
    early = service.auto_approve_due(conn, now="2026-11-02T12:00:00+00:00")
    assert early["approved"] == 0


def test_manual_approve_is_early_and_idempotent(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    approved = service.approve(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=seeded["revision_id"],
        method="manual",
        request_id="approve-1",
        now=LATER,
    )
    assert approved["method"] == "manual"
    assert episode.get(conn, seeded["episode_id"])["status"] == "approved"
    reused = service.approve(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=seeded["revision_id"],
        method="manual",
        request_id="approve-1",
        now=DUE,
    )
    assert reused["reused"] is True
    assert conn.execute("SELECT COUNT(*) FROM approval").fetchone()[0] == 1


def test_auto_approve_requires_due_time(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    with pytest.raises(service.ReviewError) as error:
        service.approve(
            conn,
            episode_id=seeded["episode_id"],
            base_revision_id=seeded["revision_id"],
            method="auto_timeout",
            request_id="auto-too-early",
            now=NOT_DUE,
        )
    assert error.value.code == "invalid_state"
    result = service.auto_approve_due(conn, now=DUE)
    assert result["approved"] == 1
    assert result["items"][0]["approval"]["method"] == "auto_timeout"


def test_manual_and_auto_approval_race_has_one_winner(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    db_path = str(tmp_path / "review.db")
    conn.close()

    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def run(method):
        connection = db.connect(db_path)
        try:
            barrier.wait()
            return service.approve(
                connection,
                episode_id=seeded["episode_id"],
                base_revision_id=seeded["revision_id"],
                method=method,
                request_id=f"race-{method}",
                now=DUE,
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["manual", "auto_timeout"]))
    assert all(result["approval_id"] == results[0]["approval_id"] for result in results)
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM approval").fetchone()[0] == 1
        assert conn.execute("SELECT method FROM approval").fetchone()[0] in ("manual", "auto_timeout")
    finally:
        conn.close()


def test_override_conflict_and_hash_change_block_approval(conn, tmp_path):
    conflict_seed = _seed_episode(conn, tmp_path, "conflict")
    saved = _save(conn, conflict_seed)
    conn.execute(
        """
        UPDATE override_application SET status='conflict', reason='partial_overlap'
        WHERE candidate_artifact_id=? AND override_id=?
        """,
        (saved["rendered_artifact_id"], saved["override_id"]),
    )
    conn.commit()
    with pytest.raises(service.ReviewError) as error:
        service.approve(
            conn,
            episode_id=conflict_seed["episode_id"],
            base_revision_id=saved["revision_id"],
            method="manual",
            request_id="conflict-approve",
            now=LATER,
        )
    assert error.value.code == "override_conflict"

    hash_seed = _seed_episode(conn, tmp_path, "hash")
    path = Path(hash_seed["workspace"]) / "en_zh_fix.srt"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(service.ReviewError) as error:
        service.approve(
            conn,
            episode_id=hash_seed["episode_id"],
            base_revision_id=hash_seed["revision_id"],
            method="manual",
            request_id="hash-approve",
            now=LATER,
        )
    assert error.value.code == "artifact_mismatch"


def test_select_variant_reapplies_override_and_records_split(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    saved = _save(conn, seeded, after="完整人工译文")
    selected = service.select_variant(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=saved["revision_id"],
        variant="split",
        now="2026-11-01T14:00:00+00:00",
    )
    assert selected["variant"] == "split"
    assert selected["base_artifact_id"] == seeded["split_id"]
    detail = service.detail(conn, episode_id=seeded["episode_id"])
    assert len(detail["cues"]) == 2
    assert detail["cues"][0]["text"] == "完整人工译文"


def test_reject_merges_ranges_snapshots_overrides_and_is_idempotent(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    saved = _save(conn, seeded)
    rejected = service.reject(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=saved["revision_id"],
        reason="本段重新翻译",
        ranges=[[1000, 2000], [2000, 3000]],
        request_id="reject-1",
        now=DUE,
    )
    assert rejected["status"] == "pending"
    row = conn.execute("SELECT * FROM rerun_request WHERE id=?", (rejected["rerun_request_id"],)).fetchone()
    assert json.loads(row["ranges_json"]) == [[1000, 3000]]
    assert json.loads(row["override_ids_json"]) == [saved["override_id"]]
    current = review_state.current_for(conn, seeded["episode_id"])
    assert current["status"] == "rejected"
    assert current["auto_approve_at"] is None

    reused = service.reject(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=saved["revision_id"],
        reason="本段重新翻译",
        ranges=[[1000, 3000]],
        request_id="reject-1",
        now=DUE,
    )
    assert reused["reused"] is True
    assert conn.execute("SELECT COUNT(*) FROM rerun_request").fetchone()[0] == 1
    with pytest.raises(service.ReviewError):
        service.reject(
            conn,
            episode_id=seeded["episode_id"],
            base_revision_id=saved["revision_id"],
            reason="不同原因",
            ranges=[[1000, 3000]],
            request_id="reject-1",
            now=DUE,
        )


def test_revise_approved_supersedes_old_delivery_eligibility(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    with pytest.raises(service.ReviewError) as stale:
        service.revise_published(
            conn,
            episode_id=seeded["episode_id"],
            base_revision_id=seeded["revision_id"] + 999,
            now=DUE,
        )
    assert stale.value.code == "stale_revision"
    service.approve(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=seeded["revision_id"],
        method="manual",
        request_id="approve-before-revise",
        now=LATER,
    )
    revised = service.revise_published(conn, episode_id=seeded["episode_id"], now=DUE)
    assert revised["status"] == "reviewing"
    assert revised["revision_id"] != seeded["revision_id"]
    assert review_state.get_revision(conn, seeded["revision_id"])["status"] == "superseded"
    assert episode.get(conn, seeded["episode_id"])["status"] == "reviewing"


def test_revise_delivered_keeps_old_current_delivery(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    delivery_id = _mark_delivered(conn, seeded)
    revised = service.revise_published(conn, episode_id=seeded["episode_id"], now=DUE)
    assert revised["status"] == "reviewing"
    detail = service.detail(conn, episode_id=seeded["episode_id"])
    assert detail["current_delivery"]["id"] == delivery_id
    assert detail["current_delivery"]["is_current"] == 1


def test_rejected_revision_cannot_revise_around_existing_delivery(conn, tmp_path):
    seeded = _seed_episode(conn, tmp_path)
    delivery_id = _mark_delivered(conn, seeded)
    revised = service.revise_published(conn, episode_id=seeded["episode_id"], now=DUE)
    service.reject(
        conn,
        episode_id=seeded["episode_id"],
        base_revision_id=revised["revision_id"],
        reason="重新翻译",
        ranges=[[1000, 3000]],
        request_id="reject-revision",
        now=DUE,
    )
    with pytest.raises(service.ReviewError) as error:
        service.revise_published(conn, episode_id=seeded["episode_id"], now=DUE)
    assert error.value.code == "invalid_state"
    assert service.detail(conn, episode_id=seeded["episode_id"])["current_delivery"]["id"] == delivery_id


def test_auto_batch_one_conflict_does_not_block_other_episode(conn, tmp_path):
    bad = _seed_episode(conn, tmp_path, "bad")
    good = _seed_episode(conn, tmp_path, "good")
    saved = _save(conn, bad)
    # 把 bad 的当前 revision 到期，并制造 conflict；good 保持初始到期版本。
    conn.execute(
        "UPDATE review_revision SET auto_approve_at=? WHERE id=?",
        (START, saved["revision_id"]),
    )
    conn.execute(
        """
        UPDATE override_application SET status='conflict', reason='partial_overlap'
        WHERE candidate_artifact_id=? AND override_id=?
        """,
        (saved["rendered_artifact_id"], saved["override_id"]),
    )
    conn.execute(
        "UPDATE review_revision SET auto_approve_at=? WHERE id=?",
        (START, good["revision_id"]),
    )
    conn.commit()
    result = service.auto_approve_due(conn, now=DUE)
    assert result["approved"] == 1
    assert result["skipped"] == 1
    assert episode.get(conn, good["episode_id"])["status"] == "approved"
    assert episode.get(conn, bad["episode_id"])["status"] == "reviewing"


def test_cli_success_and_domain_error(tmp_path):
    conn = db.connect(tmp_path / "cli.db")
    db.init_schema(conn)
    seeded = _seed_episode(conn, tmp_path)
    conn.close()
    db_path = str(tmp_path / "cli.db")
    command = [sys.executable, "-m", "review.cli", "detail", "--db", db_path]
    result = subprocess.run(
        command,
        input=json.dumps({"episode_id": seeded["episode_id"]}),
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["data"]["revision_id"] == seeded["revision_id"]

    conn = db.connect(db_path)
    current = review_state.current_for(conn, seeded["episode_id"])
    override_id = conn.execute(
        "INSERT INTO override (episode_id, base_revision_id, base_artifact_id, source_artifact_id, start_ms, end_ms, source_text_sha256, before_text, after_text, request_id) VALUES (?, ?, ?, ?, 1000, 3000, 'x', 'a', 'b', 'conflict-request') RETURNING id",
        (seeded["episode_id"], current["id"], current["base_artifact_id"], seeded["source_id"]),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO override_application (override_id, candidate_artifact_id, status, reason) VALUES (?, ?, 'conflict', 'partial_overlap')",
        (override_id, current["rendered_artifact_id"]),
    )
    conn.commit()
    conn.close()
    detail_result = subprocess.run(
        command,
        input=json.dumps({"episode_id": seeded["episode_id"]}),
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert detail_result.returncode == 0
    assert json.loads(detail_result.stdout)["data"]["has_conflicts"] is True

    result = subprocess.run(
        [sys.executable, "-m", "review.cli", "approve", "--db", db_path, "--now", LATER],
        input=json.dumps({
            "episode_id": seeded["episode_id"],
            "base_revision_id": seeded["revision_id"],
            "method": "manual",
            "request_id": "cli-conflict",
        }),
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "override_conflict"
