"""审核层·审核、override、审批、驳回与再次编辑领域服务。

所有多步写入由本模块的短事务控制；Node 只经 review.cli 调用。
文件先生成并内容寻址，数据库登记前再次确认当前 revision，防止旧页面覆盖新稿。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pipeline import override as srt_override
from pipeline.download import sha256_file
from state import delivery_state, episode, manifest, review_state

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUTO_APPROVE_HOURS = 12.0
MAX_TEXT_LENGTH = 5000


class ReviewError(RuntimeError):
    """稳定错误码供 CLI／HTTP 映射，details 不含凭据。"""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@contextmanager
def _immediate(conn: sqlite3.Connection):
    if conn.in_transaction:
        raise ReviewError("invalid_state", "领域操作入口存在未提交事务")
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _canonical_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ReviewError("validation_error", "非法时间") from exc
    if parsed.tzinfo is None:
        raise ReviewError("validation_error", "时间必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _auto_hours() -> float:
    raw = os.environ.get("PODCAST_REVIEW_AUTO_APPROVE_HOURS", str(DEFAULT_AUTO_APPROVE_HOURS))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ReviewError("validation_error", "自动审批小时配置不是数字") from exc
    if not math.isfinite(value) or value <= 0:
        raise ReviewError("validation_error", "自动审批小时配置必须为正数")
    return value


def _auto_deadline(now: str, paused: bool) -> Optional[str]:
    if paused:
        return None
    parsed = datetime.fromisoformat(_canonical_time(now))
    return (parsed + timedelta(hours=_auto_hours())).isoformat(timespec="seconds")


def _require_request_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ReviewError("validation_error", "request_id 必填且长度不能超过 200")
    return value.strip()


def _artifact_path(row: sqlite3.Row) -> Path:
    path = Path(row["path"])
    return path if path.is_absolute() else REPO_ROOT / path


def _artifact(
    conn: sqlite3.Connection,
    episode_id: int,
    artifact_id: int,
    *,
    expected_step: Optional[str] = None,
    actual_hash: bool = False,
) -> tuple[sqlite3.Row, Path]:
    row = conn.execute(
        "SELECT * FROM artifact WHERE id = ? AND episode_id = ?",
        (artifact_id, episode_id),
    ).fetchone()
    if row is None:
        raise ReviewError("not_found", "产物不存在或不属于该期", {"artifact_id": artifact_id})
    if row["status"] != "done" or not row["sha256"]:
        raise ReviewError("artifact_mismatch", "产物尚未完成或缺少 SHA256")
    if expected_step is not None and row["step"] != expected_step:
        raise ReviewError("artifact_mismatch", f"产物 step 必须为 {expected_step}")
    path = _artifact_path(row)
    if actual_hash:
        if not path.is_file():
            raise ReviewError("artifact_mismatch", "产物文件不存在", {"path": str(path)})
        actual = sha256_file(path)
        if actual != row["sha256"]:
            raise ReviewError(
                "artifact_mismatch",
                "产物文件 hash 与登记不一致",
                {"artifact_id": artifact_id, "expected": row["sha256"], "actual": actual},
            )
    return row, path


def _episode(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    row = episode.get(conn, episode_id)
    if row is None:
        raise ReviewError("not_found", "Episode 不存在", {"episode_id": episode_id})
    return row


def _current(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    row = review_state.current_for(conn, episode_id)
    if row is None:
        raise ReviewError("not_found", "当前审核修订不存在", {"episode_id": episode_id})
    return row


def _require_current(
    conn: sqlite3.Connection,
    episode_id: int,
    base_revision_id: int,
) -> sqlite3.Row:
    current = _current(conn, episode_id)
    if int(current["id"]) != int(base_revision_id):
        raise ReviewError(
            "stale_revision",
            "页面版本已过期",
            {"expected": base_revision_id, "current": current["id"]},
        )
    return current


def _entries(path: Path) -> list[srt_override.Entry]:
    try:
        return srt_override.parse_srt(path.read_text(encoding="utf-8-sig"))
    except (OSError, srt_override.OverrideError) as exc:
        raise ReviewError("artifact_mismatch", f"SRT 无法解析: {exc}") from exc


def _source_artifact(conn: sqlite3.Connection, episode_id: int) -> tuple[sqlite3.Row, Path]:
    row = conn.execute(
        """
        SELECT id FROM artifact
        WHERE episode_id = ? AND step = 'transcript_fix' AND status = 'done'
        ORDER BY version DESC, id DESC LIMIT 1
        """,
        (episode_id,),
    ).fetchone()
    if row is None:
        raise ReviewError("artifact_mismatch", "缺少 transcript_fix 源稿")
    return _artifact(conn, episode_id, int(row[0]), expected_step="transcript_fix", actual_hash=True)


def _revision_data(conn: sqlite3.Connection, revision_id: int, *, reused: bool = False) -> dict:
    row = conn.execute(
        """
        SELECT r.*, e.status AS episode_status, a.path, a.sha256
        FROM review_revision r
        JOIN episode e ON e.id = r.episode_id
        JOIN artifact a ON a.id = r.rendered_artifact_id
        WHERE r.id = ?
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        raise ReviewError("not_found", "审核修订不存在")
    conflicts = conn.execute(
        """
        SELECT oa.override_id, oa.reason, oa.target_start_ms, oa.target_end_ms
        FROM override_application oa
        WHERE oa.candidate_artifact_id = ? AND oa.status = 'conflict'
        ORDER BY oa.id
        """,
        (row["rendered_artifact_id"],),
    ).fetchall()
    return {
        "episode_id": row["episode_id"],
        "revision_id": row["id"],
        "status": row["status"],
        "episode_status": row["episode_status"],
        "base_artifact_id": row["base_artifact_id"],
        "rendered_artifact_id": row["rendered_artifact_id"],
        "variant": row["variant"],
        "reviewing_since": row["reviewing_since"],
        "auto_approve_at": row["auto_approve_at"],
        "auto_approve_paused": bool(row["auto_approve_paused"]),
        "path": row["path"],
        "sha256": row["sha256"],
        "has_conflicts": bool(conflicts),
        "conflicts": [dict(item) for item in conflicts],
        "reused": reused,
    }


def _approval_data(conn: sqlite3.Connection, approval_id: int, *, reused: bool = False) -> dict:
    row = conn.execute("SELECT * FROM approval WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ReviewError("not_found", "审批记录不存在")
    return {
        "approval_id": row["id"],
        "episode_id": row["episode_id"],
        "review_revision_id": row["review_revision_id"],
        "artifact_id": row["artifact_id"],
        "sha256": row["sha256"],
        "method": row["method"],
        "request_id": row["request_id"],
        "created_at": row["created_at"],
        "reused": reused,
    }


def create_review_revision(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    base_artifact_id: int,
    rendered_artifact_id: Optional[int] = None,
    variant: str,
    now: str,
    paused: bool = False,
) -> dict:
    now = _canonical_time(now)
    _episode(conn, episode_id)
    rendered_id = rendered_artifact_id or base_artifact_id
    _artifact(conn, episode_id, base_artifact_id, actual_hash=True)
    _artifact(conn, episode_id, rendered_id, actual_hash=True)
    if variant not in ("review", "split"):
        raise ReviewError("validation_error", "variant 必须为 review 或 split")
    with _immediate(conn):
        revision_id, reused = review_state.create_current_revision(
            conn,
            episode_id=episode_id,
            base_artifact_id=base_artifact_id,
            rendered_artifact_id=rendered_id,
            variant=variant,
            reviewing_since=now,
            auto_approve_at=_auto_deadline(now, paused),
            auto_approve_paused=paused,
            commit=False,
        )
    return _revision_data(conn, revision_id, reused=reused)


def _override_inputs(conn: sqlite3.Connection, episode_id: int) -> list[srt_override.OverrideInput]:
    rows = conn.execute(
        "SELECT * FROM override WHERE episode_id = ? ORDER BY id", (episode_id,)
    ).fetchall()
    result = []
    for row in rows:
        application = conn.execute(
            """
            SELECT target_start_ms, target_end_ms
            FROM override_application
            WHERE override_id = ? AND target_start_ms IS NOT NULL AND target_end_ms IS NOT NULL
            ORDER BY CASE status WHEN 'applied' THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        result.append(
            srt_override.OverrideInput(
                id=row["id"],
                start_ms=row["start_ms"],
                end_ms=row["end_ms"],
                source_text_sha256=row["source_text_sha256"],
                after_text=row["after_text"],
                target_start_ms=application["target_start_ms"] if application else None,
                target_end_ms=application["target_end_ms"] if application else None,
                supersedes_override_id=row["supersedes_override_id"],
                retracted=bool(row["retracted"]),
            )
        )
    return result


def _render_for_candidate(
    conn: sqlite3.Connection,
    episode_id: int,
    base_artifact_id: int,
    overrides: list[srt_override.OverrideInput],
) -> dict:
    _, source_path = _source_artifact(conn, episode_id)
    _, candidate_path = _artifact(conn, episode_id, base_artifact_id, actual_hash=True)
    directory = candidate_path.parent
    if directory.name == "rendered":
        directory = directory.parent
    directory = directory / "rendered"
    try:
        return srt_override.render_content_addressed(
            source_path=source_path,
            candidate_path=candidate_path,
            directory=directory,
            overrides=overrides,
        )
    except srt_override.OverrideError as exc:
        raise ReviewError("validation_error", f"override 渲染失败: {exc}") from exc


def _register_rendered(
    conn: sqlite3.Connection, episode_id: int, rendered: dict
) -> int:
    path = rendered["output_path"]
    existing = conn.execute(
        "SELECT * FROM artifact WHERE episode_id = ? AND step = 'review_render' AND path = ?",
        (episode_id, path),
    ).fetchone()
    if existing is not None:
        if existing["sha256"] != rendered["sha256"] or existing["status"] != "done":
            raise ReviewError("artifact_mismatch", "内容寻址产物登记冲突")
        return int(existing["id"])
    version = conn.execute(
        """
        SELECT COALESCE(MAX(version), 0) + 1 FROM artifact
        WHERE episode_id = ? AND step = 'review_render'
        """,
        (episode_id,),
    ).fetchone()[0]
    artifact_id = manifest.add(
        conn,
        episode_id=episode_id,
        step="review_render",
        kind="srt",
        path=path,
        version=version,
        sha256=rendered["sha256"],
        commit=False,
    )
    manifest.finish(conn, artifact_id, sha256=rendered["sha256"], commit=False)
    return artifact_id


def _record_applications(
    conn: sqlite3.Connection,
    artifact_id: int,
    applications: list[dict],
    *,
    new_override_id: Optional[int] = None,
) -> None:
    for application in applications:
        raw_id = application["override_id"]
        override_id = new_override_id if raw_id == "__new__" else int(raw_id)
        if override_id is None:
            raise ReviewError("internal_error", "新 override 缺少持久化 id")
        conn.execute(
            """
            INSERT INTO override_application (
                override_id, candidate_artifact_id, status,
                target_start_ms, target_end_ms, reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (override_id, candidate_artifact_id) DO NOTHING
            """,
            (
                override_id,
                artifact_id,
                application["status"],
                application["target_start_ms"],
                application["target_end_ms"],
                application["reason"],
            ),
        )


def _new_revision_from_render(
    conn: sqlite3.Connection,
    current: sqlite3.Row,
    *,
    base_artifact_id: int,
    rendered: dict,
    now: str,
    variant: Optional[str] = None,
    new_override_id: Optional[int] = None,
) -> int:
    artifact_id = _register_rendered(conn, int(current["episode_id"]), rendered)
    _record_applications(
        conn, artifact_id, rendered["applications"], new_override_id=new_override_id
    )
    revision_id, _ = review_state.create_current_revision(
        conn,
        episode_id=current["episode_id"],
        base_artifact_id=base_artifact_id,
        rendered_artifact_id=artifact_id,
        variant=variant or current["variant"],
        reviewing_since=now,
        auto_approve_at=_auto_deadline(now, bool(current["auto_approve_paused"])),
        auto_approve_paused=bool(current["auto_approve_paused"]),
        commit=False,
    )
    return revision_id


def _validate_after_text(after_text: str, *, retracted: bool) -> None:
    if not isinstance(after_text, str) or len(after_text) > MAX_TEXT_LENGTH:
        raise ReviewError("validation_error", f"修改文本为空或超过 {MAX_TEXT_LENGTH} 字符")
    if not retracted and not after_text.strip():
        raise ReviewError("validation_error", "修改后文本不能为空")
    if "\x00" in after_text or "-->" in after_text or re.search(r"\n\s*\n", after_text):
        raise ReviewError("validation_error", "修改文本不能包含 SRT 时间轴、NUL 或空行")


def save_override(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    base_revision_id: int,
    base_entry_number: int,
    source_artifact_id: int,
    start_ms: int,
    end_ms: int,
    source_text: str,
    after_text: str,
    request_id: str,
    supersedes_override_id: Optional[int] = None,
    retracted: bool = False,
    now: str,
) -> dict:
    request_id = _require_request_id(request_id)
    now = _canonical_time(now)
    _episode(conn, episode_id)
    _validate_after_text(after_text, retracted=retracted)
    existing = conn.execute(
        "SELECT * FROM override WHERE request_id = ?", (request_id,)
    ).fetchone()
    if existing is not None:
        expected = (
            episode_id, base_revision_id, source_artifact_id, start_ms, end_ms,
            after_text, supersedes_override_id, int(retracted),
        )
        actual = (
            existing["episode_id"], existing["base_revision_id"], existing["source_artifact_id"],
            existing["start_ms"], existing["end_ms"], existing["after_text"],
            existing["supersedes_override_id"], existing["retracted"],
        )
        if actual != expected:
            raise ReviewError("invalid_state", "同一 request_id 对应了不同修改")
        result_revision_id = existing["result_revision_id"] or _current(conn, episode_id)["id"]
        data = _revision_data(conn, result_revision_id, reused=True)
        data["override_id"] = existing["id"]
        return data

    current = _require_current(conn, episode_id, base_revision_id)
    if current["status"] != "reviewing" or _episode(conn, episode_id)["status"] != "reviewing":
        raise ReviewError("invalid_state", "只有 reviewing 版本可以保存修改")
    _, rendered_path = _artifact(
        conn, episode_id, current["rendered_artifact_id"], actual_hash=True
    )
    rendered_entries = _entries(rendered_path)
    if not isinstance(base_entry_number, int) or not 1 <= base_entry_number <= len(rendered_entries):
        raise ReviewError("validation_error", "base_entry_number 超出当前字幕范围")
    base_entry = rendered_entries[base_entry_number - 1]
    if not retracted and after_text.strip() == base_entry.text.strip():
        data = _revision_data(conn, current["id"], reused=True)
        data["override_id"] = None
        return data

    source_row, source_path = _artifact(
        conn, episode_id, source_artifact_id, expected_step="transcript_fix", actual_hash=True
    )
    source_entries = _entries(source_path)
    actual_source_hash = srt_override.anchor_text_sha256(source_entries, start_ms, end_ms)
    if actual_source_hash is None or actual_source_hash != srt_override.source_text_sha256(source_text):
        raise ReviewError("validation_error", "源时间范围与原文不匹配")
    if supersedes_override_id is not None:
        parent = conn.execute(
            "SELECT * FROM override WHERE id = ? AND episode_id = ?",
            (supersedes_override_id, episode_id),
        ).fetchone()
        if parent is None:
            raise ReviewError("not_found", "被取代的 override 不存在")
        if (
            parent["source_artifact_id"] != source_artifact_id
            or parent["start_ms"] != start_ms
            or parent["end_ms"] != end_ms
        ):
            raise ReviewError("validation_error", "修改链不能改变源稿锚点")
        child = conn.execute(
            "SELECT id FROM override WHERE supersedes_override_id = ?",
            (supersedes_override_id,),
        ).fetchone()
        if child is not None:
            raise ReviewError("invalid_state", "只能修改修改链末端")
    elif retracted:
        raise ReviewError("validation_error", "撤销必须指定 supersedes_override_id")

    inputs = _override_inputs(conn, episode_id)
    new_input = srt_override.OverrideInput(
        id="__new__",
        start_ms=start_ms,
        end_ms=end_ms,
        source_text_sha256=actual_source_hash,
        after_text=after_text,
        target_start_ms=base_entry.start,
        target_end_ms=base_entry.end,
        supersedes_override_id=supersedes_override_id,
        retracted=retracted,
    )
    inputs.append(new_input)
    rendered = _render_for_candidate(conn, episode_id, current["base_artifact_id"], inputs)

    with _immediate(conn):
        # 文件生成后二次确认；并发同键重试直接复用，不登记重复产物。
        duplicate = conn.execute(
            "SELECT * FROM override WHERE request_id = ?", (request_id,)
        ).fetchone()
        if duplicate is not None:
            result_id = duplicate["result_revision_id"] or _current(conn, episode_id)["id"]
            data = _revision_data(conn, result_id, reused=True)
            data["override_id"] = duplicate["id"]
            return data
        current = _require_current(conn, episode_id, base_revision_id)
        if current["status"] != "reviewing" or _episode(conn, episode_id)["status"] != "reviewing":
            raise ReviewError("invalid_state", "保存期间版本状态已变化")
        cur = conn.execute(
            """
            INSERT INTO override (
                episode_id, base_revision_id, base_artifact_id, source_artifact_id,
                start_ms, end_ms, source_text_sha256, before_text, after_text,
                supersedes_override_id, retracted, request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id, base_revision_id, current["rendered_artifact_id"], source_row["id"],
                start_ms, end_ms, actual_source_hash, base_entry.text, after_text,
                supersedes_override_id, int(retracted), request_id,
            ),
        )
        override_id = int(cur.lastrowid)
        revision_id = _new_revision_from_render(
            conn, current, base_artifact_id=current["base_artifact_id"],
            rendered=rendered, now=now, new_override_id=override_id,
        )
        conn.execute(
            "UPDATE override SET result_revision_id = ? WHERE id = ?",
            (revision_id, override_id),
        )
    data = _revision_data(conn, revision_id)
    data["override_id"] = override_id
    return data


def select_variant(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    base_revision_id: int,
    variant: str,
    now: str,
) -> dict:
    now = _canonical_time(now)
    current = _current(conn, episode_id)
    if current["variant"] == variant:
        return _revision_data(conn, current["id"], reused=True)
    current = _require_current(conn, episode_id, base_revision_id)
    if current["status"] != "reviewing":
        raise ReviewError("invalid_state", "只有 reviewing 版本可以切换分条版本")
    if variant not in ("review", "split"):
        raise ReviewError("validation_error", "variant 必须为 review 或 split")
    row = conn.execute(
        """
        SELECT id FROM artifact
        WHERE episode_id = ? AND step = ? AND status = 'done'
        ORDER BY version DESC, id DESC LIMIT 1
        """,
        (episode_id, variant),
    ).fetchone()
    if row is None:
        raise ReviewError("not_found", f"缺少 {variant} 候选产物")
    base_artifact_id = int(row[0])
    rendered = _render_for_candidate(
        conn, episode_id, base_artifact_id, _override_inputs(conn, episode_id)
    )
    with _immediate(conn):
        current = _require_current(conn, episode_id, base_revision_id)
        if current["status"] != "reviewing" or _episode(conn, episode_id)["status"] != "reviewing":
            raise ReviewError("invalid_state", "切换候选期间版本状态已变化")
        revision_id = _new_revision_from_render(
            conn, current, base_artifact_id=base_artifact_id, rendered=rendered, now=now,
            variant=variant,
        )
    return _revision_data(conn, revision_id)


def hold_review(
    conn: sqlite3.Connection, *, episode_id: int, base_revision_id: int
) -> dict:
    with _immediate(conn):
        current = _require_current(conn, episode_id, base_revision_id)
        if current["status"] != "reviewing":
            raise ReviewError("invalid_state", "只有 reviewing 版本可以保留待审")
        if current["auto_approve_paused"]:
            return _revision_data(conn, current["id"], reused=True)
        conn.execute(
            """
            UPDATE review_revision
            SET auto_approve_paused = 1, auto_approve_at = NULL WHERE id = ?
            """,
            (current["id"],),
        )
    return _revision_data(conn, current["id"])


def resume_review(
    conn: sqlite3.Connection, *, episode_id: int, base_revision_id: int, now: str
) -> dict:
    now = _canonical_time(now)
    with _immediate(conn):
        current = _require_current(conn, episode_id, base_revision_id)
        if current["status"] != "reviewing":
            raise ReviewError("invalid_state", "只有 reviewing 版本可以恢复倒计时")
        if not current["auto_approve_paused"] and current["auto_approve_at"] is not None:
            return _revision_data(conn, current["id"], reused=True)
        conn.execute(
            """
            UPDATE review_revision SET auto_approve_paused = 0, auto_approve_at = ?
            WHERE id = ?
            """,
            (_auto_deadline(now, False), current["id"]),
        )
    return _revision_data(conn, current["id"])


def approve(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    base_revision_id: int,
    method: str,
    request_id: str,
    now: str,
) -> dict:
    request_id = _require_request_id(request_id)
    now = _canonical_time(now)
    if method not in ("manual", "auto_timeout"):
        raise ReviewError("validation_error", "未知审批方式")
    existing_request = conn.execute(
        "SELECT * FROM approval WHERE request_id = ?", (request_id,)
    ).fetchone()
    if existing_request is not None:
        if (
            existing_request["episode_id"] != episode_id
            or existing_request["review_revision_id"] != base_revision_id
            or existing_request["method"] != method
        ):
            raise ReviewError("invalid_state", "同一 request_id 对应了不同审批")
        return _approval_data(conn, existing_request["id"], reused=True)
    existing_revision = conn.execute(
        "SELECT * FROM approval WHERE review_revision_id = ?", (base_revision_id,)
    ).fetchone()
    if existing_revision is not None:
        if existing_revision["episode_id"] != episode_id:
            raise ReviewError("invalid_state", "审批与 Episode 不匹配")
        return _approval_data(conn, existing_revision["id"], reused=True)

    current = _require_current(conn, episode_id, base_revision_id)
    if current["status"] != "reviewing" or _episode(conn, episode_id)["status"] != "reviewing":
        existing_now = conn.execute(
            "SELECT id FROM approval WHERE review_revision_id = ?", (base_revision_id,)
        ).fetchone()
        if existing_now is not None:
            return _approval_data(conn, existing_now["id"], reused=True)
        raise ReviewError("invalid_state", "只有 reviewing 版本可以审批")
    if method == "auto_timeout":
        if current["auto_approve_paused"] or current["auto_approve_at"] is None:
            raise ReviewError("invalid_state", "该版本已暂停自动审批")
        if datetime.fromisoformat(current["auto_approve_at"]) > datetime.fromisoformat(now):
            raise ReviewError("invalid_state", "尚未到自动审批时间")
    rendered_artifact, rendered_path = _artifact(
        conn, episode_id, current["rendered_artifact_id"], actual_hash=True
    )
    if not _entries(rendered_path):
        raise ReviewError("artifact_mismatch", "空字幕不能审批")
    conflict = conn.execute(
        """
        SELECT COUNT(*) FROM override_application
        WHERE candidate_artifact_id = ? AND status = 'conflict'
        """,
        (current["rendered_artifact_id"],),
    ).fetchone()[0]
    if conflict:
        raise ReviewError("override_conflict", "存在未解决的 override 冲突")

    with _immediate(conn):
        existing_inside = conn.execute(
            "SELECT id FROM approval WHERE review_revision_id = ?", (base_revision_id,)
        ).fetchone()
        if existing_inside is not None:
            return _approval_data(conn, existing_inside["id"], reused=True)
        current = _require_current(conn, episode_id, base_revision_id)
        if current["status"] != "reviewing" or _episode(conn, episode_id)["status"] != "reviewing":
            raise ReviewError("invalid_state", "审批期间版本状态已变化")
        if method == "auto_timeout":
            if (
                current["auto_approve_paused"]
                or current["auto_approve_at"] is None
                or datetime.fromisoformat(current["auto_approve_at"]) > datetime.fromisoformat(now)
            ):
                raise ReviewError("invalid_state", "自动审批条件已变化")
        conflict = conn.execute(
            """
            SELECT COUNT(*) FROM override_application
            WHERE candidate_artifact_id = ? AND status = 'conflict'
            """,
            (current["rendered_artifact_id"],),
        ).fetchone()[0]
        if conflict:
            raise ReviewError("override_conflict", "存在未解决的 override 冲突")
        cur = conn.execute(
            """
            INSERT INTO approval (
                review_revision_id, episode_id, artifact_id, sha256, method, request_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                current["id"], episode_id, current["rendered_artifact_id"],
                rendered_artifact["sha256"], method, request_id,
            ),
        )
        approval_id = int(cur.lastrowid)
        conn.execute("UPDATE review_revision SET status = 'approved' WHERE id = ?", (current["id"],))
        conn.execute(
            "UPDATE episode SET status = 'approved', updated_at = ? WHERE id = ?",
            (now, episode_id),
        )
    return _approval_data(conn, approval_id)


def _normalise_ranges(conn: sqlite3.Connection, episode_id: int, ranges: list) -> list[list[int]]:
    if not isinstance(ranges, list) or not ranges:
        raise ReviewError("validation_error", "驳回至少需要一个时间范围")
    parsed: list[list[int]] = []
    for value in ranges:
        try:
            if isinstance(value, dict):
                start, end = int(value["start_ms"]), int(value["end_ms"])
            else:
                start, end = int(value[0]), int(value[1])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ReviewError("validation_error", "非法驳回时间范围") from exc
        if start < 0 or end <= start:
            raise ReviewError("validation_error", "非法驳回时间范围")
        parsed.append([start, end])
    parsed.sort()
    merged: list[list[int]] = []
    for start, end in parsed:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    _, source_path = _source_artifact(conn, episode_id)
    source_entries = _entries(source_path)
    for start, end in merged:
        if srt_override.anchor_text_sha256(source_entries, start, end) is None:
            raise ReviewError("validation_error", "驳回范围超出源稿")
    return merged


def _active_override_ids(conn: sqlite3.Connection, episode_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT o.id FROM override o
        WHERE o.episode_id = ? AND o.retracted = 0
          AND NOT EXISTS (
              SELECT 1 FROM override child WHERE child.supersedes_override_id = o.id
          )
        ORDER BY o.id
        """,
        (episode_id,),
    ).fetchall()
    return [int(row[0]) for row in rows]


def reject(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    base_revision_id: int,
    reason: str,
    ranges: list,
    request_id: str,
    now: str,
) -> dict:
    request_id = _require_request_id(request_id)
    now = _canonical_time(now)
    if not isinstance(reason, str) or not reason.strip():
        raise ReviewError("validation_error", "驳回原因不能为空")
    merged = _normalise_ranges(conn, episode_id, ranges)
    existing = conn.execute(
        "SELECT * FROM rerun_request WHERE request_id = ?", (request_id,)
    ).fetchone()
    if existing is not None:
        if (
            existing["episode_id"] != episode_id
            or existing["base_revision_id"] != base_revision_id
            or existing["reason"] != reason.strip()
            or json.loads(existing["ranges_json"]) != merged
        ):
            raise ReviewError("invalid_state", "同一 request_id 对应了不同驳回")
        return {"rerun_request_id": existing["id"], "status": existing["status"], "reused": True}
    current = _require_current(conn, episode_id, base_revision_id)
    if current["status"] == "rejected":
        row = conn.execute(
            "SELECT * FROM rerun_request WHERE base_revision_id = ? ORDER BY id DESC LIMIT 1",
            (current["id"],),
        ).fetchone()
        if row is not None:
            return {"rerun_request_id": row["id"], "status": row["status"], "reused": True}
    if current["status"] != "reviewing" or _episode(conn, episode_id)["status"] != "reviewing":
        raise ReviewError("invalid_state", "只有 reviewing 版本可以驳回")
    override_ids = _active_override_ids(conn, episode_id)
    with _immediate(conn):
        current = _require_current(conn, episode_id, base_revision_id)
        if current["status"] != "reviewing" or _episode(conn, episode_id)["status"] != "reviewing":
            raise ReviewError("invalid_state", "驳回期间版本状态已变化")
        cur = conn.execute(
            """
            INSERT INTO rerun_request (
                episode_id, base_revision_id, reason, ranges_json,
                override_ids_json, status, request_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                episode_id, current["id"], reason.strip(),
                json.dumps(merged), json.dumps(override_ids), request_id, now,
            ),
        )
        request = int(cur.lastrowid)
        conn.execute(
            """
            UPDATE review_revision
            SET status = 'rejected', auto_approve_at = NULL WHERE id = ?
            """,
            (current["id"],),
        )
        conn.execute(
            "UPDATE episode SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now, episode_id),
        )
    return {"rerun_request_id": request, "status": "pending", "reused": False}


def revise_published(
    conn: sqlite3.Connection,
    *,
    episode_id: int,
    base_revision_id: Optional[int] = None,
    now: str,
) -> dict:
    now = _canonical_time(now)
    current = _current(conn, episode_id)
    if base_revision_id is not None and current["id"] != base_revision_id:
        raise ReviewError("stale_revision", "再次编辑的基准修订已变化")
    if current["status"] == "reviewing":
        return _revision_data(conn, current["id"], reused=True)
    if current["status"] == "approved":
        source_revision = current
    elif current["status"] == "delivered":
        published = delivery_state.current_for(conn, episode_id)
        if published is None or published["artifact_id"] != current["rendered_artifact_id"]:
            raise ReviewError("artifact_mismatch", "delivered 修订与当前发布记录不一致")
        source_revision = current
    else:
        raise ReviewError("invalid_state", "只有 approved 或 delivered 节目可以再次编辑")
    with _immediate(conn):
        current = _current(conn, episode_id)
        if current["status"] == "reviewing":
            return _revision_data(conn, current["id"], reused=True)
        if base_revision_id is not None and current["id"] != base_revision_id:
            raise ReviewError("stale_revision", "再次编辑期间基准修订已变化")
        if current["id"] != source_revision["id"]:
            raise ReviewError("stale_revision", "再次编辑期间当前修订已变化")
        revision_id, reused = review_state.create_current_revision(
            conn,
            episode_id=episode_id,
            base_artifact_id=source_revision["base_artifact_id"],
            rendered_artifact_id=source_revision["rendered_artifact_id"],
            variant=source_revision["variant"],
            reviewing_since=now,
            auto_approve_at=_auto_deadline(now, False),
            auto_approve_paused=False,
            commit=False,
        )
    return _revision_data(conn, revision_id, reused=reused)


def auto_approve_due(conn: sqlite3.Connection, *, now: str) -> dict:
    now = _canonical_time(now)
    rows = conn.execute(
        """
        SELECT episode_id, id FROM review_revision
        WHERE is_current = 1 AND status = 'reviewing'
          AND auto_approve_paused = 0 AND auto_approve_at IS NOT NULL
          AND auto_approve_at <= ?
        ORDER BY id
        """,
        (now,),
    ).fetchall()
    items = []
    for row in rows:
        try:
            result = approve(
                conn,
                episode_id=row["episode_id"],
                base_revision_id=row["id"],
                method="auto_timeout",
                request_id=f"auto:{row['id']}",
                now=now,
            )
            items.append({"episode_id": row["episode_id"], "status": "approved", "approval": result})
        except ReviewError as exc:
            items.append(
                {
                    "episode_id": row["episode_id"],
                    "status": "skipped",
                    "error": {"code": exc.code, "message": exc.message},
                }
            )
    return {
        "approved": sum(item["status"] == "approved" for item in items),
        "skipped": sum(item["status"] == "skipped" for item in items),
        "items": items,
    }


def inbox(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT
            e.id, e.guid, e.feed, e.title, e.published_at, e.status,
            r.id AS revision_id, r.status AS revision_status, r.variant,
            r.auto_approve_at, r.auto_approve_paused,
            (
                SELECT COUNT(*) FROM override_application oa
                WHERE oa.candidate_artifact_id = r.rendered_artifact_id
                  AND oa.status = 'conflict'
            ) AS conflict_count,
            (
                SELECT d.status FROM delivery d
                WHERE d.episode_id = e.id ORDER BY d.id DESC LIMIT 1
            ) AS delivery_status
        FROM episode e
        JOIN review_revision r ON r.episode_id = e.id AND r.is_current = 1
        WHERE (
            e.status IN ('reviewing','rejected')
            OR (
                e.status = 'approved'
                AND NOT EXISTS (
                    SELECT 1 FROM delivery d
                    WHERE d.episode_id = e.id
                      AND d.is_current = 1
                      AND d.status = 'delivered'
                )
            )
        )
        ORDER BY e.published_at DESC, e.id DESC
        """
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["auto_approve_paused"] = bool(item["auto_approve_paused"])
        item["has_conflicts"] = bool(item["conflict_count"])
        items.append(item)
    return {"items": items, "count": len(items)}


def detail(conn: sqlite3.Connection, *, episode_id: int) -> dict:
    revision = _current(conn, episode_id)
    _, rendered_path = _artifact(
        conn, episode_id, revision["rendered_artifact_id"], actual_hash=True
    )
    source_row, source_path = _source_artifact(conn, episode_id)
    source_entries = _entries(source_path)
    cues = []
    for number, entry in enumerate(_entries(rendered_path), 1):
        matching = [
            source for source in source_entries
            if source.start < entry.end and source.end > entry.start
        ]
        source_text = "\n".join(source.text for source in matching)
        cues.append(
            {
                "entry_number": number,
                "start_ms": entry.start,
                "end_ms": entry.end,
                "text": entry.text,
                "source_artifact_id": source_row["id"],
                "source_text": source_text,
            }
        )
    candidates = conn.execute(
        """
        SELECT id, step, version, sha256 FROM artifact
        WHERE episode_id = ? AND step IN ('review','split') AND status = 'done'
        ORDER BY step, version DESC, id DESC
        """,
        (episode_id,),
    ).fetchall()
    data = _revision_data(conn, revision["id"])
    data["episode"] = dict(_episode(conn, episode_id))
    data["cues"] = cues
    data["candidates"] = [dict(row) for row in candidates]
    current_delivery = delivery_state.current_for(conn, episode_id)
    data["current_delivery"] = dict(current_delivery) if current_delivery is not None else None
    return data


def media_info(conn: sqlite3.Connection, *, episode_id: int) -> dict:
    ep = _episode(conn, episode_id)
    row = conn.execute(
        """
        SELECT id FROM artifact WHERE episode_id = ? AND step = 'download' AND status = 'done'
        ORDER BY version DESC, id DESC LIMIT 1
        """,
        (episode_id,),
    ).fetchone()
    if row is not None:
        artifact, path = _artifact(conn, episode_id, int(row[0]), expected_step="download", actual_hash=True)
        artifact_hash = artifact["sha256"]
    else:
        # 最小 MVP 早期样本只登记 Episode.audio_sha256，没有 download artifact；
        # 仅允许该期约定路径作为兼容来源，不接受任意路径。
        path = REPO_ROOT / "var" / "episodes" / str(episode_id) / "audio.mp3"
        if not path.is_file():
            raise ReviewError("not_found", "固化音频不存在")
        artifact_hash = sha256_file(path)
    if not ep["audio_sha256"] or artifact_hash != ep["audio_sha256"]:
        raise ReviewError("artifact_mismatch", "音频不是首次固化版本")
    return {
        "episode_id": episode_id,
        "path": str(path.resolve()),
        "workspace": str(path.parent.resolve()),
        "size": path.stat().st_size,
        "sha256": artifact_hash,
        "content_type": "audio/mpeg",
    }
