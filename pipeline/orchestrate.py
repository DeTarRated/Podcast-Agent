"""执行层·translate-srt 确定性步骤编排。

职责边界：
- 封装工作区、知识库匹配、确定性替换、译文贴回、重切分与 provenance。
- 只调用本地 skill 脚本，不直接调用模型或付费 API。
- 通读、简报、转录修正、翻译/复核/调研派发及质量判断由主代理完成。
- 仅推进到本地 reviewing；不负责知识库沉淀、人工审核或交付上传。

子命令（主代理驱动，步骤间主代理介入）：
- prepare   输入体检 + 工作区初始化 + 知识库命中 + 初始术语表
- fix       原文整平 + 说话人处理 + 别名替换（到人工修正点返回待办）
- fix --manual-done   主代理确认修正后：split/normalize + remap-log + 最终体检
- apply     校验翻译账本与译文，贴回时间轴，推进 translated
- finalize  校验复核账本与产物，收尾检查 + 重切分 + provenance，推进 reviewing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from pipeline.download import sha256_file
from pipeline.scribe import _load_dotenv
from state import db, episode, ledger, manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRT_TOOLS = REPO_ROOT / "skills/translate-srt/scripts/srt_tools.py"
KB_TOOLS = REPO_ROOT / "skills/translate-srt/scripts/kb_tools.py"

SOURCE_LANG = "en"
TARGET_LANG = "zh"

# 内部 phase（内容命名，非 Episode 状态）
PHASES = [
    "initialized",          # prepare 完成，待主代理补上下文
    "context_ready",        # 主代理已补 brief/gaps/glossary
    "transcript_manual_review",  # fix 前半段完成，主代理正在修正 en_fix.srt
    "ready_for_translate",  # fix --manual-done 完成，可派发翻译
    "translated",           # apply 完成，可派发复核
    "reviewing",            # finalize 完成，本地待审核
]


class OrchestrateError(RuntimeError):
    """可预期错误（前置校验、子进程失败、产物冲突）；信息不含凭据。"""


# ---- 共享设施 ----


def _workspace(episode_id: int, workspace: Optional[str]) -> Path:
    return Path(workspace) if workspace else Path(f"var/episodes/{episode_id}")


def _ctx(ws: Path) -> Path:
    return ws / "_context"


def _state_path(ws: Path) -> Path:
    return _ctx(ws) / "orchestrate_state.json"


def _load_state(ws: Path) -> dict:
    p = _state_path(ws)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_state(ws: Path, state: dict) -> None:
    _ctx(ws).mkdir(parents=True, exist_ok=True)
    tmp = _state_path(ws).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_state_path(ws))


def _run_skill(script: Path, args: list[str], log_path: Path) -> subprocess.CompletedProcess:
    """调用 skill 脚本（子进程，不 import 为库）；完整输出写日志，返回结果。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
    )
    log_path.write_text(
        f"$ {script.name} {' '.join(args)}\n--- exit {proc.returncode} ---\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        encoding="utf-8",
    )
    return proc


def _register(conn, episode_id: int, step: str, kind: str, path: Path, version: int = 1) -> None:
    """登记产物为 done + sha256；同 step+path+version 且 hash 相同则跳过。"""
    sha = sha256_file(path)
    rel = str(path)
    for art in manifest.list_for(conn, episode_id):
        if art["step"] == step and art["path"] == rel and art["version"] == version:
            if art["status"] != "done" or art["sha256"] != sha:
                manifest.finish(conn, art["id"], sha256=sha)
            return
    art_id = manifest.add(conn, episode_id=episode_id, step=step, kind=kind,
                          path=rel, version=version, sha256=sha)
    manifest.finish(conn, art_id, sha256=sha)


def _result(command: str, episode_id: int, ws: Path, *, phase: str,
            next_action: str, instructions: list[str], artifacts: Optional[dict] = None,
            dispatch: Optional[dict] = None, warnings: Optional[list] = None) -> dict:
    return {
        "status": "success",
        "command": command,
        "episode_id": episode_id,
        "workspace": str(ws.resolve()),
        "phase": phase,
        "artifacts": artifacts or {},
        "dispatch": dispatch,
        "warnings": warnings or [],
        "next_action": next_action,
        "instructions": instructions,
        "error": None,
    }


def _get_episode(conn, episode_id: int) -> "object":
    ep = episode.get(conn, episode_id)
    if ep is None:
        raise OrchestrateError(f"Episode 不存在: {episode_id}")
    return ep


def _require_file(path: Path, what: str) -> Path:
    if not path.exists():
        raise OrchestrateError(f"缺少{what}: {path}")
    return path


# ---- prepare：体检 + 工作区初始化 + 知识库命中 + 初始术语表 ----


def cmd_prepare(conn, episode_id: int, ws: Path, *, video_date: Optional[str],
                domains: list[str]) -> dict:
    ep = _get_episode(conn, episode_id)
    if ep["status"] != "transcribed":
        raise OrchestrateError(f"Episode 状态为 {ep['status']}，prepare 要求 transcribed")
    en_srt = _require_file(ws / "en.srt", "英文 SRT（先完成词级重组）")

    state = _load_state(ws)
    if state.get("phase") == "initialized":
        # 幂等：已初始化，不覆盖主代理可能已填的上下文
        return _result("prepare", episode_id, ws, phase="initialized",
                       next_action="main_context_and_research",
                       instructions=_prepare_instructions(),
                       artifacts={"en_srt": str(en_srt.resolve())})

    # 工作区骨架（自建，不用 srt_tools init 避免 en/ 嵌套）
    _ctx(ws).mkdir(parents=True, exist_ok=True)
    (_ctx(ws) / "research").mkdir(exist_ok=True)
    (_ctx(ws) / "logs").mkdir(exist_ok=True)
    for placeholder in ("brief.md", "gaps.md", "glossary.md"):
        p = _ctx(ws) / placeholder
        if not p.exists():
            p.write_text("", encoding="utf-8")

    # 体检
    stats = _run_skill(SRT_TOOLS, ["stats", str(en_srt), "-l", SOURCE_LANG],
                       _ctx(ws) / "logs/stats.initial.txt")
    if stats.returncode != 0:
        raise OrchestrateError(f"stats 体检失败，见 {_ctx(ws) / 'logs/stats.initial.txt'}")
    verdict = stats.stdout
    merge_needed = "merge: NEEDED" in verdict
    split_needed = "split: NEEDED" in verdict

    # 知识库状态（缺库明确报待办，不静默跳过）
    kb_status = _run_skill(KB_TOOLS, ["status"], _ctx(ws) / "logs/kb.status.txt")
    if kb_status.returncode != 0:
        raise OrchestrateError("知识库未初始化或异常；请先 kb_tools init（不自动执行）")

    # 命中匹配
    match = _run_skill(KB_TOOLS, ["match", str(en_srt), "-o", str(_ctx(ws) / "hits.json")],
                       _ctx(ws) / "logs/kb.match.txt")
    if match.returncode != 0:
        raise OrchestrateError("kb match 失败，见 logs/kb.match.txt")

    # 视频日期：显式 > Episode published_at
    if not video_date:
        pub = ep["published_at"]
        video_date = (pub[:10] if pub else None)
    if not video_date:
        raise OrchestrateError("缺视频日期：--video-date 或 Episode.published_at 必填其一")

    # 初始术语表（知识库部分）
    glossary_args = [str(en_srt), "-o", str(_ctx(ws) / "glossary.md"),
                     "--video-date", video_date]
    for d in domains:
        glossary_args += ["-d", d]
    gl = _run_skill(KB_TOOLS, ["glossary", *glossary_args], _ctx(ws) / "logs/kb.glossary.txt")
    if gl.returncode != 0:
        raise OrchestrateError("kb glossary 失败，见 logs/kb.glossary.txt")

    state = {
        "phase": "initialized",
        "episode_id": episode_id,
        "source_lang": SOURCE_LANG, "target_lang": TARGET_LANG,
        "source_sha256": sha256_file(en_srt),
        "video_date": video_date,
        "domains": domains,
        "stats_verdict": {"merge": merge_needed, "split": split_needed},
    }
    _save_state(ws, state)

    _register(conn, episode_id, "en_srt", "srt", en_srt)
    _register(conn, episode_id, "kb_hits", "json", _ctx(ws) / "hits.json")
    _register(conn, episode_id, "glossary_skeleton", "md", _ctx(ws) / "glossary.md")

    return _result("prepare", episode_id, ws, phase="initialized",
                   next_action="main_context_and_research",
                   instructions=_prepare_instructions(),
                   artifacts={"en_srt": str(en_srt.resolve()),
                              "hits": str((_ctx(ws) / "hits.json").resolve()),
                              "glossary": str((_ctx(ws) / "glossary.md").resolve())},
                   warnings=[f"merge={'NEEDED' if merge_needed else 'no'}",
                             f"split={'NEEDED' if split_needed else 'no'}"])


def _prepare_instructions() -> list[str]:
    return [
        "Read hits.json、知识库 index.md 与命中领域的 entities/glossary/style/sources",
        "分段通读 en.srt 全文，填实 brief.md（专名/疑似错听/引述时间段/风格基调/外语插播段）",
        "填 gaps.md（只列知识库缺口）；缺口少可建议跳过调研",
        "确定说话人真名映射或 drop 策略（不臆造真名）",
        "补全 glossary「本次新增」（含说话人真名）",
        "若调研，经 Runner dispatch 派发 research（不自动派发），产物入 _context/research/",
    ]


# ---- fix：整平 + 说话人 + 别名替换（人工修正点保护）----


def cmd_fix(conn, episode_id: int, ws: Path, *, manual_done: bool,
            maps: list[str], drop: bool, allow_unmatched: bool) -> dict:
    ep = _get_episode(conn, episode_id)
    state = _load_state(ws)
    if not state:
        raise OrchestrateError("未初始化，先运行 prepare")

    # 人工修改保护：已进 transcript_manual_review 的普通 fix 重跑只返回待办
    if state.get("phase") == "transcript_manual_review" and not manual_done:
        return _result("fix", episode_id, ws, phase="transcript_manual_review",
                       next_action="main_transcript_fix",
                       instructions=_fix_manual_instructions(),
                       artifacts={"en_fix": str((ws / "en_fix.srt").resolve())})

    if manual_done:
        return _fix_complete(conn, episode_id, ws, state, allow_unmatched=allow_unmatched)

    # 前半段：要求上下文已补全
    _require_file(_ctx(ws) / "brief.md", "brief.md")
    if not (_ctx(ws) / "brief.md").read_text(encoding="utf-8").strip():
        raise OrchestrateError("brief.md 未填实（主代理须先补上下文）")
    if not (_ctx(ws) / "glossary.md").read_text(encoding="utf-8").strip():
        raise OrchestrateError("glossary.md 未填实")

    en_srt = ws / "en.srt"
    verdict = state.get("stats_verdict", {})
    current = en_srt
    if verdict.get("merge"):
        merged = ws / "en_merged.srt"
        m = _run_skill(SRT_TOOLS, ["merge", str(en_srt), "-o", str(merged)],
                       _ctx(ws) / "logs/merge.txt")
        if m.returncode != 0:
            raise OrchestrateError("merge 失败，见 logs/merge.txt")
        _register(conn, episode_id, "en_merged", "srt", merged)
        current = merged

    en_fix = ws / "en_fix.srt"
    shutil.copyfile(current, en_fix)

    # 说话人处理
    has_speakers = bool(re.search(r"^\[S\d+\]", en_fix.read_text(encoding="utf-8"), re.M))
    if has_speakers:
        if maps:
            sp = _run_skill(SRT_TOOLS, ["speakers", str(en_fix), *[a for m in maps for a in ("--map", m)]],
                            _ctx(ws) / "logs/speakers.txt")
        elif drop:
            sp = _run_skill(SRT_TOOLS, ["speakers", str(en_fix), "--drop"],
                            _ctx(ws) / "logs/speakers.txt")
        else:
            raise OrchestrateError("有说话人前缀但未提供 --map 或 --drop 决策")
        if sp.returncode != 0:
            raise OrchestrateError("speakers 处理失败，见 logs/speakers.txt")

    # 别名替换（与 glossary 同领域）
    replace_args = [str(en_fix), "--log", str(_ctx(ws) / "alias_log.tsv")]
    for d in state.get("domains", []):
        replace_args += ["-d", d]
    rp = _run_skill(KB_TOOLS, ["replace", *replace_args], _ctx(ws) / "logs/replace.txt")
    if rp.returncode != 0:
        raise OrchestrateError("kb replace 失败，见 logs/replace.txt")

    state["phase"] = "transcript_manual_review"
    state["speaker_action"] = {"maps": maps, "drop": drop}
    _save_state(ws, state)

    return _result("fix", episode_id, ws, phase="transcript_manual_review",
                   next_action="main_transcript_fix",
                   instructions=_fix_manual_instructions(),
                   artifacts={"en_fix": str(en_fix.resolve()),
                              "alias_log": str((_ctx(ws) / "alias_log.tsv").resolve())})


def _fix_manual_instructions() -> list[str]:
    return [
        "Read en_fix.srt 全文，对照 brief/glossary/replace 日志",
        "抽查 merge 最长条目，修正误合/漏合/归属错位",
        "逐项处理 mode=ask（不当作已自动替换）",
        "用 Edit 定点纠错，不整份重写，不翻译",
        "对确属粘连且无句读的条目补句读；已标点长单句不动",
        "完成后显式运行 fix --manual-done",
    ]


def _fix_complete(conn, episode_id: int, ws: Path, state: dict, *, allow_unmatched: bool) -> dict:
    en_fix = _require_file(ws / "en_fix.srt", "en_fix.srt")

    # 当前稿体检，决定 split / normalize
    stats = _run_skill(SRT_TOOLS, ["stats", str(en_fix), "-l", SOURCE_LANG],
                       _ctx(ws) / "logs/stats.fix.txt")
    if stats.returncode != 0:
        raise OrchestrateError("fix 后体检失败")
    if "split: NEEDED" in stats.stdout:
        sp = _run_skill(SRT_TOOLS, ["split", str(en_fix), "-l", SOURCE_LANG],
                        _ctx(ws) / "logs/split.txt")
        if sp.returncode != 0:
            raise OrchestrateError("split 失败，见 logs/split.txt")
    else:
        nz = _run_skill(SRT_TOOLS, ["normalize", str(en_fix)], _ctx(ws) / "logs/normalize.txt")
        if nz.returncode != 0:
            raise OrchestrateError("normalize 失败")

    # 别名日志重映射
    rm = _run_skill(KB_TOOLS, ["remap-log", str(en_fix), "--log", str(_ctx(ws) / "alias_log.tsv")],
                    _ctx(ws) / "logs/remap-log.txt")
    if rm.returncode != 0 and not allow_unmatched:
        raise OrchestrateError("remap-log 存在无法映射行；主代理确认后传 --allow-unmatched")

    _register(conn, episode_id, "transcript_fix", "srt", en_fix)
    _register(conn, episode_id, "alias_log", "tsv", _ctx(ws) / "alias_log.tsv")

    call_ref = f"translate:{episode_id}:v1"
    state["phase"] = "ready_for_translate"
    state["call_refs"] = {**state.get("call_refs", {}), "translate": call_ref}
    _save_state(ws, state)

    return _result("fix", episode_id, ws, phase="ready_for_translate",
                   next_action="dispatch_translate",
                   instructions=["主代理经 runner dispatch 派发整份翻译（不直接运行模型）"],
                   artifacts={"en_fix": str(en_fix.resolve())},
                   dispatch={"role": "translate",
                             "inputPath": str(en_fix.resolve()),
                             "outputPath": str((ws / "en_zh.txt").resolve()),
                             "callRef": call_ref})


# ---- apply：校验翻译账本与译文，贴回时间轴，推进 translated ----


def _ledger_success_for(conn, call_ref: str, purpose: str, episode_id: int) -> None:
    """硬闸门：当前 call_ref 账本必须 success，且 episode/purpose 对齐。"""
    row = ledger.get_by_ref(conn, call_ref)
    if row is None:
        raise OrchestrateError(f"账本不存在: {call_ref}（须先经 dispatch 完成付费调用）")
    if row["status"] != "success":
        raise OrchestrateError(f"账本状态为 {row['status']}，要求 success: {call_ref}")
    if row["purpose"] != purpose or row["episode_id"] != episode_id:
        raise OrchestrateError(f"账本 episode/purpose 不匹配: {call_ref}")


def cmd_apply(conn, episode_id: int, ws: Path) -> dict:
    ep = _get_episode(conn, episode_id)
    state = _load_state(ws)
    call_ref = state.get("call_refs", {}).get("translate")
    if not call_ref:
        raise OrchestrateError("无翻译 call_ref，先完成 fix --manual-done")

    en_fix = _require_file(ws / "en_fix.srt", "en_fix.srt")
    en_zh_txt = _require_file(ws / "en_zh.txt", "译文 en_zh.txt（翻译子代理落盘）")

    # 账本闸门
    _ledger_success_for(conn, call_ref, "translate", episode_id)

    # 贴回时间轴 + 校验
    ap = _run_skill(SRT_TOOLS, ["apply", str(en_fix), str(en_zh_txt),
                                "-o", str(ws / "en_zh.srt"), "-l", TARGET_LANG],
                    _ctx(ws) / "logs/apply.txt")
    if ap.returncode != 0:
        raise OrchestrateError("apply 失败（缺行/重号/空译文等），见 logs/apply.txt")
    ck = _run_skill(SRT_TOOLS, ["check", str(en_fix), str(ws / "en_zh.srt")],
                    _ctx(ws) / "logs/check.translate.txt")
    warnings = []
    if ck.returncode != 0:
        raise OrchestrateError("check 检出结构性 DIFF，见 logs/check.translate.txt")
    if "NOTE" in ck.stdout:
        warnings.append("check 含 NOTE（可能漏译或专名合法相同），主代理须判断")

    _register(conn, episode_id, "translate_txt", "txt", en_zh_txt)
    _register(conn, episode_id, "translate", "srt", ws / "en_zh.srt")

    episode.update_status(conn, episode_id, "translated")
    review_ref = f"review:{episode_id}:v1"
    state["phase"] = "translated"
    state["call_refs"] = {**state.get("call_refs", {}), "review": review_ref}
    _save_state(ws, state)

    return _result("apply", episode_id, ws, phase="translated",
                   next_action="dispatch_review",
                   instructions=["主代理经 runner dispatch 派发复核（同档强模型）",
                                 "复核清单为空/过短时主代理必须亲自补查"],
                   artifacts={"en_zh": str((ws / "en_zh.srt").resolve())},
                   dispatch={"role": "review",
                             "inputPath": str((ws / "en_zh.srt").resolve()),
                             "outputPath": str((ws / "en_zh_fix.srt").resolve()),
                             "callRef": review_ref},
                   warnings=warnings)


# ---- finalize：校验复核账本与产物，收尾 + 重切分 + provenance，推进 reviewing ----


def cmd_finalize(conn, episode_id: int, ws: Path) -> dict:
    ep = _get_episode(conn, episode_id)
    if ep["status"] not in ("translated", "reviewing"):
        raise OrchestrateError(f"Episode 状态为 {ep['status']}，finalize 要求 translated")
    state = _load_state(ws)
    review_ref = state.get("call_refs", {}).get("review")
    if not review_ref:
        raise OrchestrateError("无复核 call_ref，先完成 apply")

    for f, what in ((ws / "en_zh.srt", "翻译稿"), (ws / "en_zh_fix.srt", "复核稿"),
                    (_ctx(ws) / "review_notes.md", "review_notes.md")):
        _require_file(f, what)

    _ledger_success_for(conn, review_ref, "review", episode_id)

    # 复核稿清理 + 校验
    cl = _run_skill(SRT_TOOLS, ["clean", "-l", TARGET_LANG, str(ws / "en_zh_fix.srt")],
                    _ctx(ws) / "logs/clean.review.txt")
    if cl.returncode != 0:
        raise OrchestrateError("clean 失败")
    ck = _run_skill(SRT_TOOLS, ["check", str(ws / "en_zh.srt"), str(ws / "en_zh_fix.srt")],
                    _ctx(ws) / "logs/check.review.txt")
    if ck.returncode != 0:
        # 条目数减少且为「复核删除合法条目（停顿/口语废句）」时接受，不强制对齐
        if "DIFF entry count" in ck.stdout and "->" in ck.stdout:
            m = re.search(r"entry count: (\d+) -> (\d+)", ck.stdout)
            if m and int(m.group(2)) < int(m.group(1)):
                # 复核删除条目是合法编辑（subtitle-rules：无意义口语/停顿可删），时间轴保留原值
                pass
            else:
                raise OrchestrateError("复核稿 check 仍有未解释 DIFF（条目数异常）")
        else:
            # 尝试时间轴修复（条目数一致时）
            ft = _run_skill(SRT_TOOLS, ["check", str(ws / "en_fix.srt"), str(ws / "en_zh_fix.srt"),
                                        "--fix-timeline"], _ctx(ws) / "logs/fix-timeline.review.txt")
            ck2 = _run_skill(SRT_TOOLS, ["check", str(ws / "en_fix.srt"), str(ws / "en_zh_fix.srt")],
                             _ctx(ws) / "logs/check.review-final.txt")
            if ck2.returncode != 0 and "DIFF entry count" not in ck2.stdout:
                raise OrchestrateError("复核稿 check 仍有未解释 DIFF")

    # 重切分 + provenance（split 稿不做对齐 check）
    rs = _run_skill(SRT_TOOLS, ["resplit", "-l", TARGET_LANG, str(ws / "en_zh_fix.srt"),
                                "-o", str(ws / "en_zh_split.srt")],
                    _ctx(ws) / "logs/resplit.txt")
    if rs.returncode != 0:
        raise OrchestrateError("resplit 失败")
    for derived, name in ((ws / "en_zh_fix.srt", "provenance.fix.txt"),
                          (ws / "en_zh_split.srt", "provenance.split.txt")):
        pv = _run_skill(SRT_TOOLS, ["provenance", str(ws / "en.srt"), str(derived)],
                        _ctx(ws) / f"logs/{name}")
        if pv.returncode != 0:
            raise OrchestrateError(f"provenance 越界: {name}")

    _register(conn, episode_id, "review", "srt", ws / "en_zh_fix.srt")
    _register(conn, episode_id, "review_notes", "md", _ctx(ws) / "review_notes.md")
    _register(conn, episode_id, "split", "srt", ws / "en_zh_split.srt")

    episode.update_status(conn, episode_id, "reviewing")
    state["phase"] = "reviewing"
    _save_state(ws, state)

    return _result("finalize", episode_id, ws, phase="reviewing",
                   next_action="human_review",
                   instructions=["抽查两个候选的开头/中间/结尾",
                                 "如实报告两份插值统计；交付哪一份由用户定，不自动选择",
                                 "不上传、不推进 approved/delivered"],
                   artifacts={"review": str((ws / "en_zh_fix.srt").resolve()),
                              "split": str((ws / "en_zh_split.srt").resolve())})


# ---- CLI 入口 ----


def main(argv=None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="translate-srt 确定性步骤编排")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("episode_id", type=int)
        p.add_argument("--workspace", default=None)
        p.add_argument("--db", default=None)

    p_prepare = sub.add_parser("prepare")
    common(p_prepare)
    p_prepare.add_argument("--video-date", default=None)
    p_prepare.add_argument("-d", "--domain", action="append", default=[])

    p_fix = sub.add_parser("fix")
    common(p_fix)
    p_fix.add_argument("--manual-done", action="store_true")
    p_fix.add_argument("--map", action="append", default=[])
    p_fix.add_argument("--drop", action="store_true")
    p_fix.add_argument("--allow-unmatched", action="store_true")

    p_apply = sub.add_parser("apply")
    common(p_apply)
    p_finalize = sub.add_parser("finalize")
    common(p_finalize)

    args = parser.parse_args(argv)
    ws = _workspace(args.episode_id, args.workspace)
    conn = db.connect(args.db) if args.db else db.open_default()
    if args.db:
        db.init_schema(conn)
    try:
        if args.command == "prepare":
            result = cmd_prepare(conn, args.episode_id, ws,
                                 video_date=args.video_date, domains=args.domain)
        elif args.command == "fix":
            result = cmd_fix(conn, args.episode_id, ws, manual_done=args.manual_done,
                             maps=args.map, drop=args.drop, allow_unmatched=args.allow_unmatched)
        elif args.command == "apply":
            result = cmd_apply(conn, args.episode_id, ws)
        else:
            result = cmd_finalize(conn, args.episode_id, ws)
    except OrchestrateError as exc:
        result = {"status": "failed", "command": args.command,
                  "episode_id": args.episode_id, "error": str(exc)}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
