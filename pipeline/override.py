"""执行层·人工 override 的确定性 SRT 定位与合并。

只处理本地文本与文件，不访问数据库、网络或模型。
SRT 条目号仅用于输入解析与输出重排，权威定位使用源时间范围和原文 hash。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

_TIMESTAMP = re.compile(
    r"^(\d+):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
    r"(\d+):(\d{2}):(\d{2}),(\d{3})$"
)


class OverrideError(RuntimeError):
    """输入、时间轴或文件冲突；不静默覆盖或猜测迁移。"""


@dataclass(frozen=True)
class Entry:
    start: int
    end: int
    text: str

    def __post_init__(self) -> None:
        # 既有 resplit 的插值点可能 start == end；仅拒绝负值与倒序区间。
        if self.start < 0 or self.end < self.start:
            raise OverrideError(f"invalid_entry_range:{self.start}:{self.end}")
        if not self.text.strip():
            raise OverrideError("empty_entry_text")


@dataclass(frozen=True)
class OverrideInput:
    id: int | str
    start_ms: int
    end_ms: int
    source_text_sha256: str
    after_text: str
    target_start_ms: Optional[int] = None
    target_end_ms: Optional[int] = None
    supersedes_override_id: Optional[int | str] = None
    retracted: bool = False


@dataclass(frozen=True)
class Application:
    override_id: int | str
    status: str
    target_start_ms: Optional[int]
    target_end_ms: Optional[int]
    reason: Optional[str] = None


@dataclass(frozen=True)
class RenderResult:
    entries: list[Entry]
    applications: list[Application]

    @property
    def has_conflicts(self) -> bool:
        return any(item.status == "conflict" for item in self.applications)


def normalize_source_text(text: str) -> str:
    """用于源句 hash 的稳定规范化，不修改实际字幕文本。"""
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return " ".join(normalized.split())


def source_text_sha256(text: str) -> str:
    return hashlib.sha256(normalize_source_text(text).encode("utf-8")).hexdigest()


def _timestamp_ms(parts: tuple[str, ...]) -> int:
    hours, minutes, seconds, millis = (int(value) for value in parts)
    if minutes >= 60 or seconds >= 60:
        raise OverrideError("invalid_timestamp")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _format_timestamp(value: int) -> str:
    if value < 0:
        raise OverrideError("negative_timestamp")
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def parse_srt(text: str) -> list[Entry]:
    """解析普通 SRT；忽略原编号，保留条目文本与时间顺序。"""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    entries: list[Entry] = []
    for block in re.split(r"\n[ \t]*\n", normalized):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise OverrideError("invalid_srt_block")
        match = _TIMESTAMP.match(lines[1].strip())
        if match is None:
            raise OverrideError("invalid_srt_timeline")
        start = _timestamp_ms(match.group(1, 2, 3, 4))
        end = _timestamp_ms(match.group(5, 6, 7, 8))
        entries.append(Entry(start=start, end=end, text="\n".join(lines[2:]).strip()))
    return entries


def _validate_entries(entries: list[Entry], *, allow_empty: bool = True) -> None:
    if not entries and not allow_empty:
        raise OverrideError("empty_subtitle")
    previous_end: Optional[int] = None
    for entry in entries:
        if previous_end is not None and entry.start < previous_end:
            raise OverrideError("invalid_timeline_order")
        previous_end = entry.end


def serialize_srt(entries: list[Entry]) -> str:
    _validate_entries(entries)
    blocks = []
    for number, entry in enumerate(entries, 1):
        blocks.append(
            f"{number}\n{_format_timestamp(entry.start)} --> "
            f"{_format_timestamp(entry.end)}\n{entry.text.strip()}"
        )
    return "\n\n".join(blocks) + ("\n\n" if blocks else "")


def _active_overrides(overrides: list[OverrideInput]) -> list[OverrideInput]:
    """每条修改链只取末端；末端为撤销时整链不再应用。"""
    by_id = {item.id: item for item in overrides}
    if len(by_id) != len(overrides):
        raise OverrideError("duplicate_override_id")
    children: dict[int | str, int | str] = {}
    for item in overrides:
        parent = item.supersedes_override_id
        if parent is not None:
            if parent in children:
                raise OverrideError("override_branch")
            children[parent] = item.id
    terminals = [item for item in overrides if item.id not in children]
    if overrides and not terminals:
        raise OverrideError("override_cycle")

    active: list[OverrideInput] = []
    for terminal in terminals:
        seen: set[int | str] = set()
        current: Optional[OverrideInput] = terminal
        while current is not None:
            if current.id in seen:
                raise OverrideError("override_cycle")
            seen.add(current.id)
            parent = current.supersedes_override_id
            current = by_id.get(parent) if parent is not None else None
        if not terminal.retracted:
            active.append(terminal)
    return active


def _anchor_hash(source_entries: list[Entry], start: int, end: int) -> Optional[str]:
    if start < 0 or end <= start:
        return None
    selected = [entry for entry in source_entries if entry.start < end and entry.end > start]
    if not selected or selected[0].start > start or selected[-1].end < end:
        return None
    return source_text_sha256("\n".join(entry.text for entry in selected))


def anchor_text_sha256(source_entries: list[Entry], start: int, end: int) -> Optional[str]:
    """返回时间锚点覆盖原文的规范化 hash；无法完整覆盖时为 None。"""
    return _anchor_hash(source_entries, start, end)


def _overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and end_a > start_b


def render_overrides(
    *,
    source_entries: list[Entry],
    candidate_entries: list[Entry],
    overrides: list[OverrideInput],
) -> RenderResult:
    """应用有效人工修改；冲突保留诊断结果，由上层阻止审批。"""
    _validate_entries(source_entries)
    _validate_entries(candidate_entries)
    active = _active_overrides(overrides)
    prepared: list[dict] = []

    for item in active:
        target_start = item.target_start_ms
        target_end = item.target_end_ms
        reason: Optional[str] = None
        actual_hash = _anchor_hash(source_entries, item.start_ms, item.end_ms)
        if actual_hash is None or actual_hash != item.source_text_sha256.lower():
            reason = "source_changed"
        elif not item.retracted and not item.after_text.strip():
            raise OverrideError("empty_after_text")

        if reason is None and (target_start is None) != (target_end is None):
            reason = "invalid_target_range"
        if reason is None and target_start is None:
            matching = [
                entry
                for entry in candidate_entries
                if _overlaps(item.start_ms, item.end_ms, entry.start, entry.end)
            ]
            if not matching:
                reason = "missing_target"
            else:
                target_start = matching[0].start
                target_end = matching[-1].end
        if reason is None and (
            target_start is None or target_end is None or target_start < 0 or target_end <= target_start
        ):
            reason = "invalid_target_range"
        prepared.append(
            {
                "override": item,
                "target_start": target_start,
                "target_end": target_end,
                "reason": reason,
            }
        )

    # 独立的有效修改不得竞争同一目标区间；显式修改链已在前面折叠。
    for index, left in enumerate(prepared):
        if left["reason"] is not None:
            continue
        for right in prepared[index + 1 :]:
            if right["reason"] is not None:
                continue
            if _overlaps(
                left["target_start"], left["target_end"],
                right["target_start"], right["target_end"],
            ):
                left["reason"] = "competing_overrides"
                right["reason"] = "competing_overrides"

    rendered = list(candidate_entries)
    applications: list[Application] = []
    for item in sorted(
        prepared,
        key=lambda value: (
            value["target_start"] if value["target_start"] is not None else -1,
            value["target_end"] if value["target_end"] is not None else -1,
            str(value["override"].id),
        ),
    ):
        override = item["override"]
        start = item["target_start"]
        end = item["target_end"]
        reason = item["reason"]
        if reason is None:
            partial = [
                entry
                for entry in candidate_entries
                if _overlaps(start, end, entry.start, entry.end)
                and not (entry.start >= start and entry.end <= end)
            ]
            if partial:
                reason = "partial_overlap"
        if reason is not None:
            applications.append(
                Application(
                    override_id=override.id,
                    status="conflict",
                    target_start_ms=start,
                    target_end_ms=end,
                    reason=reason,
                )
            )
            continue

        contained = [
            entry
            for entry in rendered
            if entry.start >= start and entry.end <= end
        ]
        if contained:
            replacement = Entry(contained[0].start, contained[-1].end, override.after_text.strip())
            rendered = [entry for entry in rendered if entry not in contained]
        else:
            # 模型漏删时仍在原人工目标区间保留内容，不允许模型删除人工修改。
            replacement = Entry(start, end, override.after_text.strip())
        rendered.append(replacement)
        rendered.sort(key=lambda entry: (entry.start, entry.end))
        applications.append(
            Application(
                override_id=override.id,
                status="applied",
                target_start_ms=replacement.start,
                target_end_ms=replacement.end,
            )
        )

    _validate_entries(rendered)
    return RenderResult(entries=rendered, applications=applications)


def _write_create_only(path: Path, payload: bytes) -> bool:
    """原子创建；已存在同内容复用，不同内容拒绝。返回是否新创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == payload:
            return False
        raise OverrideError(f"output_exists:{path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise OverrideError(f"output_exists:{path}")
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def write_create_only(path: Path, payload: bytes) -> bool:
    """供本地流水线复用的原子 create-only 写入。"""
    return _write_create_only(path, payload)


def render_content_addressed(
    *,
    source_path: Path,
    candidate_path: Path,
    directory: Path,
    overrides: list[OverrideInput],
) -> dict:
    """渲染到 directory/zh-{sha256}.srt，供审核领域事务登记。"""
    source_entries = parse_srt(source_path.read_text(encoding="utf-8-sig"))
    candidate_entries = parse_srt(candidate_path.read_text(encoding="utf-8-sig"))
    result = render_overrides(
        source_entries=source_entries,
        candidate_entries=candidate_entries,
        overrides=overrides,
    )
    payload = serialize_srt(result.entries).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    output_path = directory / f"zh-{digest}.srt"
    created = _write_create_only(output_path, payload)
    return {
        "output_path": str(output_path),
        "sha256": digest,
        "created": created,
        "entry_count": len(result.entries),
        "has_conflicts": result.has_conflicts,
        "applications": [asdict(item) for item in result.applications],
    }


def render_file(
    *,
    source_path: Path,
    candidate_path: Path,
    output_path: Path,
    overrides: list[OverrideInput],
) -> dict:
    source_entries = parse_srt(source_path.read_text(encoding="utf-8-sig"))
    candidate_entries = parse_srt(candidate_path.read_text(encoding="utf-8-sig"))
    result = render_overrides(
        source_entries=source_entries,
        candidate_entries=candidate_entries,
        overrides=overrides,
    )
    payload = serialize_srt(result.entries).encode("utf-8")
    created = _write_create_only(output_path, payload)
    return {
        "output_path": str(output_path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "created": created,
        "entry_count": len(result.entries),
        "has_conflicts": result.has_conflicts,
        "applications": [asdict(item) for item in result.applications],
    }


def _override_from_json(value: dict) -> OverrideInput:
    if not isinstance(value, dict):
        raise OverrideError("invalid_override_json")
    try:
        return OverrideInput(
            id=value["id"],
            start_ms=int(value["start_ms"]),
            end_ms=int(value["end_ms"]),
            source_text_sha256=str(value["source_text_sha256"]),
            after_text=str(value.get("after_text", "")),
            target_start_ms=(
                int(value["target_start_ms"]) if value.get("target_start_ms") is not None else None
            ),
            target_end_ms=(
                int(value["target_end_ms"]) if value.get("target_end_ms") is not None else None
            ),
            supersedes_override_id=value.get("supersedes_override_id"),
            retracted=bool(value.get("retracted", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OverrideError("invalid_override_json") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="确定性应用人工 override")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--source", required=True)
    render.add_argument("--candidate", required=True)
    render.add_argument("--request", required=True, help="含 overrides 列表的派生 JSON")
    render.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        values = request.get("overrides") if isinstance(request, dict) else None
        if not isinstance(values, list):
            raise OverrideError("invalid_override_request")
        result = render_file(
            source_path=Path(args.source),
            candidate_path=Path(args.candidate),
            output_path=Path(args.output),
            overrides=[_override_from_json(value) for value in values],
        )
        print(json.dumps({"status": "success", "data": result}, ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, OverrideError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
