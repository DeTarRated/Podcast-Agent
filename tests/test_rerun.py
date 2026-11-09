"""局部驳回重跑编排测试；翻译／复核均为 faux 产物与模拟账本，零真实付费调用。

模拟场景：
- prepare 按时间范围生成局部源稿与 call_ref，空范围不派发
- apply-translate 对缺失／intent／failed／unknown 账本拒绝，成功后只合并范围内模型产物
- finalize 校验复核账本、复核不得越界、override 跨重跑保留并重新送审
- split 变体执行 resplit 与 provenance，completed 请求幂等返回
"""
import json
from pathlib import Path

import pytest

from pipeline import rerun
from pipeline.download import sha256_file
from review import service
from state import db, episode, ledger, manifest, review_state

START = "2026-11-01T12:00:00+00:00"
LATER = "2026-11-01T13:00:00+00:00"
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
    connection = db.connect(tmp_path / "rerun.db")
    db.init_schema(connection)
    yield connection
    connection.close()


def _artifact(conn, episode_id, step, path):
    artifact_id = manifest.add(
        conn, episode_id=episode_id, step=step, kind="srt", path=str(path), sha256=sha256_file(path)
    )
    manifest.finish(conn, artifact_id, sha256=sha256_file(path))
    return artifact_id


def _seed(conn, tmp_path, *, variant="review"):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = workspace / "en_fix.srt"
    original = workspace / "en.srt"
    review = workspace / "en_zh_fix.srt"
    split = workspace / "en_zh_split.srt"
    source.write_text(SOURCE, encoding="utf-8")
    original.write_text(SOURCE, encoding="utf-8")
    review.write_text(REVIEW, encoding="utf-8")
    split.write_text(SPLIT, encoding="utf-8")
    episode_id = episode.create(conn, guid="guid", feed="feed")
    source_id = _artifact(conn, episode_id, "transcript_fix", source)
    review_id = _artifact(conn, episode_id, "review", review)
    split_id = _artifact(conn, episode_id, "split", split)
    current = service.create_review_revision(
        conn, episode_id=episode_id, base_artifact_id=review_id, variant="review", now=START
    )
    saved = service.save_override(
        conn,
        episode_id=episode_id,
        base_revision_id=current["revision_id"],
        base_entry_number=2,
        source_artifact_id=source_id,
        start_ms=4000,
        end_ms=6000,
        source_text="Next sentence",
        after_text="人工下一句",
        request_id="outside-override",
        now=LATER,
    )
    if variant == "split":
        saved = service.select_variant(
            conn,
            episode_id=episode_id,
            base_revision_id=saved["revision_id"],
            variant="split",
            now=LATER,
        )
    rejected = service.reject(
        conn,
        episode_id=episode_id,
        base_revision_id=saved["revision_id"],
        reason="首句重跑",
        ranges=[[1000, 3000]],
        request_id="reject-rerun",
        now=LATER,
    )
    return {
        "episode_id": episode_id,
        "workspace": workspace,
        "source_id": source_id,
        "review_id": review_id,
        "split_id": split_id,
        "request_id": rejected["rerun_request_id"],
    }


def _seed_outcome(conn, seeded, call_ref, purpose, status="success"):
    ledger.record_intent(
        conn,
        episode_id=seeded["episode_id"],
        purpose=purpose,
        provider="deepseek",
        model="mock",
        usage_basis="phase=intent",
        amount_est=None,
        call_ref=call_ref,
    )
    if status != "intent":
        ledger.record_outcome(conn, call_ref, status=status)


def _prepare(conn, seeded):
    result = rerun.cmd_prepare(conn, seeded["request_id"], now=LATER)
    directory = seeded["workspace"] / "reruns" / str(seeded["request_id"])
    return result, directory


def _apply(conn, seeded, prepared=None):
    prepared = prepared or rerun.cmd_prepare(conn, seeded["request_id"], now=LATER)
    dispatch = prepared["dispatch"]
    Path(dispatch["outputPath"]).write_text("1\t新模型译文\n", encoding="utf-8")
    _seed_outcome(conn, seeded, dispatch["callRef"], "translate", "success")
    return rerun.cmd_apply_translate(conn, seeded["request_id"], now=LATER)


def test_prepare_builds_scoped_input_and_stable_call_ref(conn, tmp_path):
    seeded = _seed(conn, tmp_path)
    prepared, directory = _prepare(conn, seeded)
    dispatch = prepared["dispatch"]
    assert dispatch["role"] == "translate"
    assert "旧" not in Path(dispatch["inputPath"]).read_text(encoding="utf-8")
    assert "Hello world" in Path(dispatch["inputPath"]).read_text(encoding="utf-8")
    assert "Next sentence" not in Path(dispatch["inputPath"]).read_text(encoding="utf-8")
    assert dispatch["callRef"].startswith(f"rerun-translate:{seeded['request_id']}:")
    assert conn.execute("SELECT status FROM rerun_request WHERE id=?", (seeded["request_id"],)).fetchone()[0] == "running"
    repeated = rerun.cmd_prepare(conn, seeded["request_id"], now=LATER)
    assert repeated["dispatch"]["callRef"] == dispatch["callRef"]


def test_prepare_empty_scope_fails_before_dispatch(conn, tmp_path):
    seeded = _seed(conn, tmp_path)
    conn.execute("UPDATE rerun_request SET ranges_json='[]' WHERE id=?", (seeded["request_id"],))
    conn.commit()
    with pytest.raises(rerun.RerunError, match="没有可重跑源条目"):
        rerun.cmd_prepare(conn, seeded["request_id"], now=LATER)


@pytest.mark.parametrize("status", ["missing", "intent", "failed", "unknown"])
def test_apply_translate_requires_current_success(conn, tmp_path, status):
    seeded = _seed(conn, tmp_path)
    prepared, _ = _prepare(conn, seeded)
    if status != "missing":
        _seed_outcome(conn, seeded, prepared["dispatch"]["callRef"], "translate", status)
    with pytest.raises(rerun.RerunError, match="账本"):
        rerun.cmd_apply_translate(conn, seeded["request_id"], now=LATER)


def test_full_rerun_preserves_outside_override_and_completes_once(conn, tmp_path):
    seeded = _seed(conn, tmp_path)
    prepared, directory = _prepare(conn, seeded)
    review_dispatch = _apply(conn, seeded, prepared)
    assert review_dispatch["dispatch"]["role"] == "review"
    merged = (directory / "merged_translate.srt").read_text(encoding="utf-8")
    assert "新模型译文" in merged
    assert "人工下一句" not in merged, "override 应在 finalize 统一重放，而非翻译阶段混入"
    assert "下一句" in merged

    Path(review_dispatch["dispatch"]["outputPath"]).write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n复核新译文\n", encoding="utf-8"
    )
    _seed_outcome(conn, seeded, review_dispatch["dispatch"]["callRef"], "review", "success")
    finalized = rerun.cmd_finalize(conn, seeded["request_id"], now="2026-11-01T15:00:00+00:00")
    assert finalized["has_conflicts"] is False
    assert finalized["revision_id"] is not None
    revision = review_state.get_revision(conn, finalized["revision_id"])
    assert revision["status"] == "reviewing"
    assert revision["variant"] == "review"
    detail = service.detail(conn, episode_id=seeded["episode_id"])
    assert [cue["text"] for cue in detail["cues"]] == ["复核新译文", "人工下一句"]
    request = conn.execute("SELECT * FROM rerun_request WHERE id=?", (seeded["request_id"],)).fetchone()
    assert request["status"] == "completed"
    assert request["result_revision_id"] == finalized["revision_id"]
    repeated = rerun.cmd_finalize(conn, seeded["request_id"], now=LATER)
    assert repeated["revision_id"] == finalized["revision_id"]


def test_review_output_cannot_escape_rejected_scope(conn, tmp_path):
    seeded = _seed(conn, tmp_path)
    prepared, _ = _prepare(conn, seeded)
    review_dispatch = _apply(conn, seeded, prepared)
    Path(review_dispatch["dispatch"]["outputPath"]).write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n范围内\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\n越界修改\n",
        encoding="utf-8",
    )
    _seed_outcome(conn, seeded, review_dispatch["dispatch"]["callRef"], "review", "success")
    with pytest.raises(rerun.RerunError, match="超出驳回范围"):
        rerun.cmd_finalize(conn, seeded["request_id"], now=LATER)


def test_finalize_requires_review_success(conn, tmp_path):
    seeded = _seed(conn, tmp_path)
    prepared, _ = _prepare(conn, seeded)
    review_dispatch = _apply(conn, seeded, prepared)
    Path(review_dispatch["dispatch"]["outputPath"]).write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n范围内\n", encoding="utf-8"
    )
    with pytest.raises(rerun.RerunError, match="账本"):
        rerun.cmd_finalize(conn, seeded["request_id"], now=LATER)


def test_split_variant_reruns_resplit_and_provenance(conn, tmp_path):
    seeded = _seed(conn, tmp_path, variant="split")
    prepared, _ = _prepare(conn, seeded)
    review_dispatch = _apply(conn, seeded, prepared)
    Path(review_dispatch["dispatch"]["outputPath"]).write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n复核新译文\n", encoding="utf-8"
    )
    _seed_outcome(conn, seeded, review_dispatch["dispatch"]["callRef"], "review", "success")
    finalized = rerun.cmd_finalize(conn, seeded["request_id"], now=LATER)
    assert finalized["has_conflicts"] is False
    revision = review_state.get_revision(conn, finalized["revision_id"])
    assert revision["variant"] == "split"
    directory = seeded["workspace"] / "reruns" / str(seeded["request_id"])
    assert (directory / "candidate_split.srt").exists()
    assert (directory / "provenance.candidate_split.log").exists()
