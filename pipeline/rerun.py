"""执行层·驳回后的局部翻译与复核编排。

只生成局部输入、校验账本、合并确定性产物；模型调用由 Runner dispatch 执行。
阶段：prepare → apply-translate → finalize；付费结果未知时不自动重派。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pipeline import override as srt_override
from state import db, episode, ledger, manifest, review_state

REPO_ROOT = Path(__file__).resolve().parents[1]
SRT_TOOLS = REPO_ROOT / "skills/translate-srt/scripts/srt_tools.py"


class RerunError(RuntimeError):
    """可预期的请求、产物或账本校验失败。"""


def _now(value: Optional[str]) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RerunError("now 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _deadline(now: str) -> str:
    try:
        hours = float(os.environ.get("PODCAST_REVIEW_AUTO_APPROVE_HOURS", "12"))
    except ValueError as exc:
        raise RerunError("自动审批小时配置无效") from exc
    if hours <= 0:
        raise RerunError("自动审批小时配置必须为正数")
    return (datetime.fromisoformat(now) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _write(path: Path, payload: bytes) -> None:
    srt_override.write_create_only(path, payload)


def _state_path(directory: Path) -> Path:
    return directory / "state.json"


def _load_state(directory: Path) -> dict:
    path = _state_path(directory)
    if not path.exists():
        raise RerunError("局部重跑尚未 prepare")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(directory: Path, state: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = _state_path(directory).with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(_state_path(directory))


def _request(conn, request_id: int):
    row = conn.execute("SELECT * FROM rerun_request WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise RerunError(f"rerun_request 不存在: {request_id}")
    return row


def _revision(conn, revision_id: int):
    row = review_state.get_revision(conn, revision_id)
    if row is None:
        raise RerunError(f"review_revision 不存在: {revision_id}")
    return row


def _artifact(conn, episode_id: int, artifact_id: int) -> tuple[sqlite3.Row, Path]:
    row = conn.execute(
        "SELECT * FROM artifact WHERE id = ? AND episode_id = ? AND status = 'done'",
        (artifact_id, episode_id),
    ).fetchone()
    if row is None:
        raise RerunError("artifact 不存在、跨期或未完成")
    path = Path(row["path"])
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
        raise RerunError("artifact 文件缺失或 hash 不匹配")
    return row, path


def _source_artifact(conn, episode_id: int) -> tuple[sqlite3.Row, Path]:
    row = conn.execute(
        """
        SELECT id FROM artifact WHERE episode_id = ? AND step = 'transcript_fix' AND status = 'done'
        ORDER BY version DESC, id DESC LIMIT 1
        """,
        (episode_id,),
    ).fetchone()
    if row is None:
        raise RerunError("缺少 transcript_fix 源稿")
    return _artifact(conn, episode_id, int(row[0]))


def _parse(path: Path) -> list[srt_override.Entry]:
    try:
        return srt_override.parse_srt(path.read_text(encoding="utf-8-sig"))
    except (OSError, srt_override.OverrideError) as exc:
        raise RerunError(f"SRT 解析失败: {exc}") from exc


def _serialized(entries: list[srt_override.Entry]) -> bytes:
    try:
        return srt_override.serialize_srt(entries).encode("utf-8")
    except srt_override.OverrideError as exc:
        raise RerunError(f"SRT 序列化失败: {exc}") from exc


def _replace_selected(
    base_entries: list[srt_override.Entry],
    selected: list[srt_override.Entry],
    replacements: list[srt_override.Entry],
) -> list[srt_override.Entry]:
    """仅替换选中源区间；未选中的模型内容原样保留。"""
    remaining = [
        entry
        for entry in base_entries
        if not any(
            entry.start < selected_entry.end and entry.end > selected_entry.start
            for selected_entry in selected
        )
    ]
    merged = [*remaining, *replacements]
    merged.sort(key=lambda entry: (entry.start, entry.end))
    return merged


def _dispatch(state: dict, role: str) -> dict:
    key = "translate" if role == "translate" else "review"
    return {
        "role": role,
        "inputPath": state[f"{key}_input"],
        "outputPath": state[f"{key}_output"],
        "callRef": state["call_refs"][key],
        "ledger": {
            "episodeId": state["episode_id"],
            "purpose": role,
            "provider": "deepseek",
            "model": "deepseek-flash" if role == "translate" else "deepseek-v4-pro",
        },
        "contextFiles": state.get("context_files", []),
        "referenceFiles": [state["source_path"]],
        "targetLang": "zh",
    }


def _result(command: str, request_id: int, phase: str, **extra) -> dict:
    return {"status": "success", "command": command, "request_id": request_id, "phase": phase, **extra}


def cmd_prepare(conn, request_id: int, *, now: str) -> dict:
    request = _request(conn, request_id)
    base_revision = _revision(conn, request["base_revision_id"])
    directory = _source_artifact(conn, request["episode_id"])[1].parent / "reruns" / str(request_id)
    if _state_path(directory).exists():
        state = _load_state(directory)
        if state["phase"] == "completed":
            return _result("prepare", request_id, "completed", revision_id=request["result_revision_id"])
        role = "review" if state["phase"] == "review_pending" else "translate"
        return _result("prepare", request_id, state["phase"], dispatch=_dispatch(state, role))
    if request["status"] != "pending" or base_revision["status"] != "rejected":
        raise RerunError("只有 pending 且基准为 rejected 的请求可以 prepare")
    ranges = json.loads(request["ranges_json"])
    _, source_path = _source_artifact(conn, request["episode_id"])
    entries = _parse(source_path)
    selected = [
        entry
        for entry in entries
        if any(entry.start < end and entry.end > start for start, end in ranges)
    ]
    if not selected:
        raise RerunError("驳回范围没有可重跑源条目，不派发模型")
    payload = _serialized(selected)
    directory.mkdir(parents=True, exist_ok=True)
    source_input = directory / "source.srt"
    _write(source_input, payload)
    source_sha = hashlib.sha256(payload).hexdigest()
    state = {
        "phase": "translate_pending",
        "request_id": request_id,
        "episode_id": request["episode_id"],
        "base_revision_id": base_revision["id"],
        "base_artifact_id": base_revision["base_artifact_id"],
        "variant": base_revision["variant"],
        "source_path": str(source_path),
        "ranges": ranges,
        "directory": str(directory),
        "translate_input": str(source_input),
        "translate_output": str(directory / "translated.txt"),
        "review_input": str(directory / "review_input.srt"),
        "review_output": str(directory / "reviewed.srt"),
        "context_files": [
            str(source_path.parent / "_context" / "brief.md"),
            str(source_path.parent / "_context" / "glossary.md"),
        ],
        "call_refs": {
            "translate": f"rerun-translate:{request_id}:{source_sha}",
        },
    }
    _save_state(directory, state)
    conn.execute(
        "UPDATE rerun_request SET status = 'running', updated_at = ? WHERE id = ?",
        (now, request_id),
    )
    conn.commit()
    return _result("prepare", request_id, "translate_pending", dispatch=_dispatch(state, "translate"))


def _ledger_success(conn, request, call_ref: str, purpose: str) -> None:
    row = ledger.get_by_ref(conn, call_ref)
    if row is None or row["status"] != "success" or row["purpose"] != purpose or row["episode_id"] != request["episode_id"]:
        raise RerunError(f"{purpose} 账本缺失、非 success 或不匹配: {call_ref}")


def _translated_entries(source_entries: list[srt_override.Entry], path: Path) -> list[srt_override.Entry]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != len(source_entries):
        raise RerunError("局部翻译行数与源条目不一致")
    result = []
    seen = set()
    for expected, (entry, line) in enumerate(zip(source_entries, lines), 1):
        number, separator, text = line.partition("\t")
        if not separator or not text.strip() or not number.isdigit() or int(number) != expected or expected in seen:
            raise RerunError("局部翻译必须为连续编号<TAB>非空译文")
        seen.add(expected)
        result.append(srt_override.Entry(entry.start, entry.end, text.strip()))
    return result


def cmd_apply_translate(conn, request_id: int, *, now: str) -> dict:
    request = _request(conn, request_id)
    directory = _source_artifact(conn, request["episode_id"])[1].parent / "reruns" / str(request_id)
    state = _load_state(directory)
    if state["phase"] == "review_pending":
        return _result("apply-translate", request_id, "review_pending", dispatch=_dispatch(state, "review"))
    if state["phase"] != "translate_pending" or request["status"] != "running":
        raise RerunError("apply-translate 要求 translate_pending / running")
    _ledger_success(conn, request, state["call_refs"]["translate"], "translate")
    source_entries = _parse(Path(state["translate_input"]))
    translated = _translated_entries(source_entries, Path(state["translate_output"]))
    translated_payload = _serialized(translated)
    _write(directory / "translated.srt", translated_payload)
    _write(Path(state["review_input"]), translated_payload)

    _, base_path = _artifact(conn, request["episode_id"], state["base_artifact_id"])
    base_entries = _parse(base_path)
    merged = _replace_selected(base_entries, source_entries, translated)
    merged_path = directory / "merged_translate.srt"
    _write(merged_path, _serialized(merged))
    state["merged_translate"] = str(merged_path)
    review_sha = hashlib.sha256(translated_payload).hexdigest()
    state["call_refs"]["review"] = f"rerun-review:{request_id}:{review_sha}"
    state["phase"] = "review_pending"
    _save_state(directory, state)
    return _result("apply-translate", request_id, "review_pending", dispatch=_dispatch(state, "review"))


def _run_skill(args: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(SRT_TOOLS), *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    log_path.write_text(
        f"$ srt_tools.py {' '.join(args)}\n--- exit {proc.returncode} ---\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RerunError(f"srt_tools {args[0]} 失败，见 {log_path}")


def _override_inputs(conn, episode_id: int) -> list[srt_override.OverrideInput]:
    rows = conn.execute("SELECT * FROM override WHERE episode_id = ? ORDER BY id", (episode_id,)).fetchall()
    result = []
    for row in rows:
        application = conn.execute(
            """
            SELECT target_start_ms, target_end_ms FROM override_application
            WHERE override_id = ? AND target_start_ms IS NOT NULL AND target_end_ms IS NOT NULL
            ORDER BY CASE status WHEN 'applied' THEN 0 ELSE 1 END, id LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        result.append(
            srt_override.OverrideInput(
                id=row["id"], start_ms=row["start_ms"], end_ms=row["end_ms"],
                source_text_sha256=row["source_text_sha256"], after_text=row["after_text"],
                target_start_ms=application["target_start_ms"] if application else None,
                target_end_ms=application["target_end_ms"] if application else None,
                supersedes_override_id=row["supersedes_override_id"], retracted=bool(row["retracted"]),
            )
        )
    return result


def _register_artifact(conn, episode_id: int, step: str, path: Path, sha256: str) -> int:
    existing = conn.execute(
        "SELECT id, sha256, status FROM artifact WHERE episode_id = ? AND step = ? AND path = ?",
        (episode_id, step, str(path)),
    ).fetchone()
    if existing is not None:
        if existing["sha256"] != sha256 or existing["status"] != "done":
            raise RerunError("artifact 内容寻址登记冲突")
        return int(existing["id"])
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM artifact WHERE episode_id = ? AND step = ?",
        (episode_id, step),
    ).fetchone()[0]
    artifact_id = manifest.add(
        conn, episode_id=episode_id, step=step, kind="srt", path=str(path),
        version=version, sha256=sha256, commit=False,
    )
    manifest.finish(conn, artifact_id, sha256=sha256, commit=False)
    return artifact_id


def cmd_finalize(conn, request_id: int, *, now: str) -> dict:
    request = _request(conn, request_id)
    directory = _source_artifact(conn, request["episode_id"])[1].parent / "reruns" / str(request_id)
    state = _load_state(directory)
    if request["status"] == "completed":
        return _result("finalize", request_id, "completed", revision_id=request["result_revision_id"])
    if state["phase"] != "review_pending" or request["status"] != "running":
        raise RerunError("finalize 要求 review_pending / running")
    _ledger_success(conn, request, state["call_refs"]["review"], "review")
    reviewed = _parse(Path(state["review_output"]))
    source_entries = _parse(Path(state["translate_input"]))
    for entry in reviewed:
        if not any(
            entry.start >= source.start and entry.end <= source.end for source in source_entries
        ):
            raise RerunError("复核产物超出驳回范围或改写源时间轴")
    merged_translate = _parse(Path(state["merged_translate"]))
    candidate = _replace_selected(merged_translate, source_entries, reviewed)
    candidate_path = directory / "candidate.srt"
    candidate_payload = _serialized(candidate)
    _write(candidate_path, candidate_payload)

    final_base_path = candidate_path
    original_en = Path(state["source_path"]).parent / "en.srt"
    if not original_en.is_file():
        raise RerunError("缺少原始 en.srt，无法校验 provenance")
    if state["variant"] == "split":
        split_path = directory / "candidate_split.srt"
        _run_skill(["resplit", "-l", "zh", str(candidate_path), "-o", str(split_path)], directory / "resplit.log")
        final_base_path = split_path
    for derived in {candidate_path, final_base_path}:
        _run_skill(["provenance", str(original_en), str(derived)], directory / f"provenance.{derived.stem}.log")

    final_payload = final_base_path.read_bytes()
    final_sha = hashlib.sha256(final_payload).hexdigest()
    overrides = _override_inputs(conn, request["episode_id"])
    rendered = None
    if overrides:
        rendered = srt_override.render_content_addressed(
            source_path=Path(state["source_path"]),
            candidate_path=final_base_path,
            directory=Path(state["source_path"]).parent / "rendered",
            overrides=overrides,
        )

    with conn:
        current = _revision(conn, request["base_revision_id"])
        if current["status"] != "rejected" or not current["is_current"]:
            raise RerunError("finalize 时基准修订已被其他操作取代")
        base_artifact_id = _register_artifact(
            conn, request["episode_id"], state["variant"], final_base_path, final_sha
        )
        rendered_artifact_id = base_artifact_id
        if rendered is not None:
            rendered_artifact_id = _register_artifact(
                conn, request["episode_id"], "review_render", Path(rendered["output_path"]), rendered["sha256"]
            )
            for application in rendered["applications"]:
                conn.execute(
                    """
                    INSERT INTO override_application (
                        override_id, candidate_artifact_id, status,
                        target_start_ms, target_end_ms, reason
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (override_id, candidate_artifact_id) DO NOTHING
                    """,
                    (
                        application["override_id"], rendered_artifact_id, application["status"],
                        application["target_start_ms"], application["target_end_ms"], application["reason"],
                    ),
                )
        revision_id, _ = review_state.create_current_revision(
            conn,
            episode_id=request["episode_id"],
            base_artifact_id=base_artifact_id,
            rendered_artifact_id=rendered_artifact_id,
            variant=state["variant"],
            reviewing_since=now,
            auto_approve_at=_deadline(now),
            auto_approve_paused=False,
            commit=False,
        )
        conn.execute(
            """
            UPDATE rerun_request SET status = 'completed', result_revision_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (revision_id, now, request_id),
        )
    return _result(
        "finalize", request_id, "completed", revision_id=revision_id,
        has_conflicts=bool(rendered and rendered["has_conflicts"]),
        base_artifact_id=base_artifact_id, rendered_artifact_id=rendered_artifact_id,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="驳回后的局部重跑编排")
    parser.add_argument("command", choices=["prepare", "apply-translate", "finalize"])
    parser.add_argument("request_id", type=int)
    parser.add_argument("--db")
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    conn = db.connect(args.db) if args.db else db.open_default()
    if args.db:
        db.init_schema(conn)
    try:
        now = _now(args.now)
        if args.command == "prepare":
            result = cmd_prepare(conn, args.request_id, now=now)
        elif args.command == "apply-translate":
            result = cmd_apply_translate(conn, args.request_id, now=now)
        else:
            result = cmd_finalize(conn, args.request_id, now=now)
    except (RerunError, OSError, ValueError, srt_override.OverrideError) as exc:
        result = {"status": "failed", "error": str(exc)}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
