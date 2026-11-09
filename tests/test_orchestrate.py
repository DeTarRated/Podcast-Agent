"""orchestrate 编排器测试；零真实付费调用、零真实知识库读取。

模拟场景：
- prepare：工作区骨架 + stats 体检 + 知识库命中 + 术语表骨架；幂等重跑不覆盖主代理上下文
- fix：说话人映射 + 别名替换 → 人工修正点保护（phase=transcript_manual_review 重跑只返回待办、不动 en_fix）
- fix --manual-done：split/normalize + remap-log，输出翻译 dispatch 参数
- apply：账本 success 闸门（缺失/intent/failed/unknown 拒绝），译文贴回时间轴，推进 translated
- finalize：复核账本闸门 + clean/check/resplit/provenance，推进 reviewing 并创建当前审核修订
- 端到端：prepare → 主代理上下文 → fix → 人工修正 → fix --manual-done → seed 翻译账本 → apply → seed 复核账本 → finalize → reviewing
"""
import json
import sqlite3
from pathlib import Path

import pytest

from state import db, episode, ledger, manifest, review_state
from pipeline import orchestrate
from pipeline.download import sha256_file

# 最小合法英文 SRT（两句，含说话人前缀）
SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,000
[S01] Hello, this is Carragher at Chelsea.

2
00:00:03,500 --> 00:00:05,500
[S02] Yes, Alonso here. Good to see you.
"""


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture()
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "en.srt").write_text(SAMPLE_SRT, encoding="utf-8")
    return w


def make_episode(conn, ws, *, status="transcribed"):
    ep_id = episode.create(conn, guid="g1", feed="f1", published_at="2026-10-26T15:00:00+00:00")
    episode.set_audio(conn, ep_id, sha256=sha256_file(ws / "en.srt"), duration_secs=10.0)
    episode.update_status(conn, ep_id, status)
    return ep_id


def seed_ledger_success(conn, ep_id, role, purpose):
    ref = f"{role}:{ep_id}:v1"
    ledger.record_intent(conn, episode_id=ep_id, purpose=purpose, provider="deepseek",
                         model="deepseek-flash", usage_basis="phase=intent",
                         amount_est=None, call_ref=ref)
    ledger.record_outcome(conn, ref, status="success",
                          usage_basis="prompt_tokens=100;completion_tokens=50")
    return ref


# ---- prepare ----


def test_prepare_builds_workspace_and_glossary(conn, ws, monkeypatch):
    ep_id = make_episode(conn, ws)
    # 隔离知识库到临时目录并 init 最小库
    kb_home = ws / "kb"
    monkeypatch.setenv("TRANSLATE_SRT_HOME", str(kb_home))
    import subprocess, sys
    subprocess.run([sys.executable, str(orchestrate.KB_TOOLS), "init"],
                   capture_output=True, cwd=str(orchestrate.REPO_ROOT))

    result = orchestrate.cmd_prepare(conn, ep_id, ws, video_date="2026-10-26", domains=[])
    assert result["status"] == "success"
    assert result["phase"] == "initialized"
    assert (ws / "_context" / "hits.json").exists()
    assert (ws / "_context" / "glossary.md").exists()
    assert (ws / "_context" / "logs" / "stats.initial.txt").exists()
    state = orchestrate._load_state(ws)
    assert state["video_date"] == "2026-10-26"
    # 幂等重跑：不改状态
    again = orchestrate.cmd_prepare(conn, ep_id, ws, video_date="2026-10-26", domains=[])
    assert again["phase"] == "initialized"


def test_prepare_requires_transcribed(conn, ws):
    ep_id = make_episode(conn, ws, status="downloaded")
    with pytest.raises(orchestrate.OrchestrateError, match="要求 transcribed"):
        orchestrate.cmd_prepare(conn, ep_id, ws, video_date="2026-10-26", domains=[])


def test_prepare_requires_en_srt(conn, tmp_path):
    empty_ws = tmp_path / "empty"
    empty_ws.mkdir()
    ep_id = episode.create(conn, guid="g2", feed="f1")
    episode.update_status(conn, ep_id, "transcribed")
    with pytest.raises(orchestrate.OrchestrateError, match="英文 SRT"):
        orchestrate.cmd_prepare(conn, ep_id, empty_ws, video_date="2026-10-26", domains=[])


# ---- fix ----


def _prepared(conn, ws, monkeypatch):
    ep_id = make_episode(conn, ws)
    kb_home = ws / "kb"
    monkeypatch.setenv("TRANSLATE_SRT_HOME", str(kb_home))
    import subprocess, sys
    subprocess.run([sys.executable, str(orchestrate.KB_TOOLS), "init"],
                   capture_output=True, cwd=str(orchestrate.REPO_ROOT))
    orchestrate.cmd_prepare(conn, ep_id, ws, video_date="2026-10-26", domains=[])
    # 主代理补上下文
    (ws / "_context" / "brief.md").write_text("访谈节目。风格：口语。", encoding="utf-8")
    (ws / "_context" / "glossary.md").write_text("Chelsea=切尔西", encoding="utf-8")
    return ep_id


def test_fix_reaches_manual_review_and_protects_edits(conn, ws, monkeypatch):
    ep_id = _prepared(conn, ws, monkeypatch)
    result = orchestrate.cmd_fix(conn, ep_id, ws, manual_done=False,
                                 maps=["S01=Carragher", "S02=Alonso"], drop=False,
                                 allow_unmatched=False)
    assert result["phase"] == "transcript_manual_review"
    en_fix = ws / "en_fix.srt"
    assert "Carragher" in en_fix.read_text(encoding="utf-8")

    # 主代理手动修改
    en_fix.write_text(en_fix.read_text(encoding="utf-8") + "\n# manual edit marker\n", encoding="utf-8")
    before = en_fix.read_text(encoding="utf-8")

    # 普通 fix 重跑：只返回待办，不覆盖人工修改
    again = orchestrate.cmd_fix(conn, ep_id, ws, manual_done=False, maps=[], drop=False,
                                allow_unmatched=False)
    assert again["phase"] == "transcript_manual_review"
    assert again["next_action"] == "main_transcript_fix"
    assert en_fix.read_text(encoding="utf-8") == before, "人工修改不得被覆盖"


def test_fix_requires_filled_context(conn, ws, monkeypatch):
    ep_id = make_episode(conn, ws)
    kb_home = ws / "kb"
    monkeypatch.setenv("TRANSLATE_SRT_HOME", str(kb_home))
    import subprocess, sys
    subprocess.run([sys.executable, str(orchestrate.KB_TOOLS), "init"],
                   capture_output=True, cwd=str(orchestrate.REPO_ROOT))
    orchestrate.cmd_prepare(conn, ep_id, ws, video_date="2026-10-26", domains=[])
    # 不填 brief/glossary，直接 fix 应失败
    with pytest.raises(orchestrate.OrchestrateError, match="brief.md 未填实"):
        orchestrate.cmd_fix(conn, ep_id, ws, manual_done=False, maps=["S01=Carragher"],
                            drop=False, allow_unmatched=False)


def test_fix_manual_done_emits_translate_dispatch(conn, ws, monkeypatch):
    ep_id = _prepared(conn, ws, monkeypatch)
    orchestrate.cmd_fix(conn, ep_id, ws, manual_done=False,
                        maps=["S01=Carragher", "S02=Alonso"], drop=False, allow_unmatched=False)
    result = orchestrate.cmd_fix(conn, ep_id, ws, manual_done=True, maps=[], drop=False,
                                 allow_unmatched=True)
    assert result["phase"] == "ready_for_translate"
    assert result["dispatch"]["role"] == "translate"
    assert result["dispatch"]["callRef"] == f"translate:{ep_id}:v1"
    assert result["dispatch"]["inputPath"].endswith("en_fix.srt")
    assert result["dispatch"]["outputPath"].endswith("en_zh.txt")


# ---- apply ----


def _ready_for_translate(conn, ws, monkeypatch):
    ep_id = _prepared(conn, ws, monkeypatch)
    orchestrate.cmd_fix(conn, ep_id, ws, manual_done=False,
                        maps=["S01=Carragher", "S02=Alonso"], drop=False, allow_unmatched=False)
    orchestrate.cmd_fix(conn, ep_id, ws, manual_done=True, maps=[], drop=False, allow_unmatched=True)
    return ep_id


def test_apply_rejects_without_ledger(conn, ws, monkeypatch):
    ep_id = _ready_for_translate(conn, ws, monkeypatch)
    (ws / "en_zh.txt").write_text("1\t你好\n2\t是的\n", encoding="utf-8")
    with pytest.raises(orchestrate.OrchestrateError, match="账本不存在"):
        orchestrate.cmd_apply(conn, ep_id, ws)


def test_apply_rejects_non_success_ledger(conn, ws, monkeypatch):
    ep_id = _ready_for_translate(conn, ws, monkeypatch)
    (ws / "en_zh.txt").write_text("1\t你好\n2\t是的\n", encoding="utf-8")
    ref = f"translate:{ep_id}:v1"
    ledger.record_intent(conn, episode_id=ep_id, purpose="translate", provider="deepseek",
                         model="deepseek-flash", usage_basis="x", amount_est=None, call_ref=ref)
    with pytest.raises(orchestrate.OrchestrateError, match="要求 success"):
        orchestrate.cmd_apply(conn, ep_id, ws)


def test_apply_success_advances_to_translated(conn, ws, monkeypatch):
    ep_id = _ready_for_translate(conn, ws, monkeypatch)
    (ws / "en_zh.txt").write_text("1\t[卡拉格] 你好，这里是切尔西。\n2\t[阿隆索] 是的，很高兴见到你。\n",
                                  encoding="utf-8")
    seed_ledger_success(conn, ep_id, "translate", "translate")
    result = orchestrate.cmd_apply(conn, ep_id, ws)
    assert result["status"] == "success"
    assert result["phase"] == "translated"
    assert episode.get(conn, ep_id)["status"] == "translated"
    assert (ws / "en_zh.srt").exists()
    assert result["dispatch"]["role"] == "review"


# ---- finalize ----


def _translated(conn, ws, monkeypatch):
    ep_id = _ready_for_translate(conn, ws, monkeypatch)
    (ws / "en_zh.txt").write_text("1\t[卡拉格] 你好。\n2\t[阿隆索] 是的。\n", encoding="utf-8")
    seed_ledger_success(conn, ep_id, "translate", "translate")
    orchestrate.cmd_apply(conn, ep_id, ws)
    return ep_id


def test_finalize_requires_review_ledger(conn, ws, monkeypatch):
    ep_id = _translated(conn, ws, monkeypatch)
    (ws / "en_zh_fix.srt").write_text((ws / "en_zh.srt").read_text(encoding="utf-8"), encoding="utf-8")
    (ws / "_context" / "review_notes.md").write_text("| 条目 | 类型 | 说明 |\n", encoding="utf-8")
    with pytest.raises(orchestrate.OrchestrateError, match="账本不存在"):
        orchestrate.cmd_finalize(conn, ep_id, ws)


def test_finalize_success_advances_to_reviewing(conn, ws, monkeypatch):
    ep_id = _translated(conn, ws, monkeypatch)
    (ws / "en_zh_fix.srt").write_text((ws / "en_zh.srt").read_text(encoding="utf-8"), encoding="utf-8")
    (ws / "_context" / "review_notes.md").write_text("| 条目 | 类型 | 说明 |\n", encoding="utf-8")
    seed_ledger_success(conn, ep_id, "review", "review")
    result = orchestrate.cmd_finalize(conn, ep_id, ws)
    assert result["status"] == "success"
    assert result["phase"] == "reviewing"
    assert episode.get(conn, ep_id)["status"] == "reviewing"
    current = review_state.current_for(conn, ep_id)
    assert current["status"] == "reviewing"
    assert current["variant"] == "split"
    assert current["auto_approve_paused"] == 0
    assert current["auto_approve_at"] is not None
    assert (ws / "en_zh_split.srt").exists()
    assert (ws / "_context" / "logs" / "provenance.fix.txt").exists()


def test_finalize_requires_review_notes(conn, ws, monkeypatch):
    ep_id = _translated(conn, ws, monkeypatch)
    (ws / "en_zh_fix.srt").write_text((ws / "en_zh.srt").read_text(encoding="utf-8"), encoding="utf-8")
    seed_ledger_success(conn, ep_id, "review", "review")
    with pytest.raises(orchestrate.OrchestrateError, match="review_notes"):
        orchestrate.cmd_finalize(conn, ep_id, ws)
