"""交付层·结构化看点校验与固定 HTML 模板。

模型只提供 summary/items；标题、节目源和时间链接由模板填充。
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Mapping

from pipeline.override import Entry


class HighlightsError(ValueError):
    """看点结构或时间点不合法。"""


def validate_highlights(value: str | Mapping, entries: list[Entry]) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise HighlightsError("看点不是有效 JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {"summary", "items"}:
        raise HighlightsError("看点必须且只能包含 summary/items")
    summary = value["summary"]
    items = value["items"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise HighlightsError("summary 必须为非空且不超过 2000 字符的字符串")
    if not isinstance(items, list) or len(items) > 20:
        raise HighlightsError("items 必须为最多 20 项的数组")
    normalized = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"text", "timestampMs"}:
            raise HighlightsError("每个看点必须且只能包含 text/timestampMs")
        text = item["text"]
        timestamp = item["timestampMs"]
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise HighlightsError("看点文本无效")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise HighlightsError("看点 timestampMs 必须为非负整数")
        if not any(entry.start <= timestamp <= entry.end for entry in entries):
            raise HighlightsError(f"看点时间点不在已批准字幕内: {timestamp}")
        normalized.append({"text": text.strip(), "timestampMs": timestamp})
    return {"summary": summary.strip(), "items": normalized}


def align_timestamps(value: str | Mapping, entries: list[Entry], *, max_delta_ms: int = 3000) -> dict:
    """将字幕间隙中的模型时间点吸附到最近字幕起点，超限拒绝，不重新调用模型。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise HighlightsError("看点不是有效 JSON") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("items"), list):
        raise HighlightsError("看点必须包含 items 数组")
    aligned_items = []
    for item in value["items"]:
        if not isinstance(item, Mapping):
            raise HighlightsError("看点条目必须是 object")
        copied = dict(item)
        timestamp = copied.get("timestampMs")
        if isinstance(timestamp, int) and not isinstance(timestamp, bool) and not any(
            entry.start <= timestamp <= entry.end for entry in entries
        ):
            if not entries:
                raise HighlightsError("空字幕无法对齐看点时间")
            nearest = min(entries, key=lambda entry: abs(entry.start - timestamp))
            if abs(nearest.start - timestamp) > max_delta_ms:
                raise HighlightsError(f"看点时间点距最近字幕超过 {max_delta_ms}ms: {timestamp}")
            copied["timestampMs"] = nearest.start
        aligned_items.append(copied)
    return {"summary": value.get("summary"), "items": aligned_items}


def load_validated(path: Path, entries: list[Entry]) -> dict:
    aligned = align_timestamps(path.read_text(encoding="utf-8"), entries)
    return validate_highlights(aligned, entries)


def _time_link(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_highlights_html(episode: Mapping, highlights: dict) -> str:
    """固定模板；所有来自模型、RSS 与人工的文本均 HTML 转义。"""
    if not isinstance(episode, Mapping):
        episode = dict(episode)
    title = html.escape(str(episode.get("title") or episode.get("guid") or "播客节目"))
    feed = html.escape(str(episode.get("feed") or ""))
    summary = html.escape(highlights["summary"])
    items = []
    for item in highlights["items"]:
        timestamp = item["timestampMs"]
        seconds = timestamp // 1000
        items.append(
            f'<li><a href="?t={seconds}">{_time_link(timestamp)}</a>'
            f'<span>{html.escape(item["text"])}</span></li>'
        )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{title}</title>"
        "<style>body{max-width:760px;margin:40px auto;padding:0 20px;font-family:sans-serif;line-height:1.7}"
        "li{margin:16px 0}a{display:inline-block;min-width:88px;color:#216348}</style></head><body>"
        f"<h1>{title}</h1><p>{feed}</p><h2>本期摘要</h2><p>{summary}</p>"
        f"<h2>看点</h2><ul>{''.join(items)}</ul></body></html>"
    )
