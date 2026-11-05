"""词级重组：Scribe v2 词级时间戳 JSON → 一句一条、带说话人前缀的英文 SRT。

所属分层：执行层·流水线工具。
职责边界：只做「词级证据 → 字幕条目」的确定性重组，不调用外部服务；
句读补全、merge / split 整平、翻译等由 translate-srt skill 后续步骤负责。

重组规则（每条边界都有词级证据，不伪造时间点）：
- 句末标点（. ! ? … 及中文句号）结束一条
- 说话人变化结束一条
- 词间停顿超过 pause_threshold 结束一条（无标点 ASR 的兜底）
- 词时间戳缺失 → 明确失败（ValueError），不猜测、不插值
- speaker_id 为 None 的词沿用前一说话人；全程无说话人 → 不加前缀
- audio_event（笑声等）不进字幕文本，时间与事件写入 provenance
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

SENTENCE_TERMINATORS = {".", "!", "?", "…", "。", "！", "？"}
_TRAILING_QUOTES = "\"'”’"


@dataclass
class Cue:
    index: int
    start: float
    end: float
    speaker: Optional[str]  # "S01" 等占位；单说话人无前缀时为 None
    text: str
    word_indices: list[int]
    boundary_reason: str  # punct / speaker_change / pause / end


def _is_terminal(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    last = stripped[-1]
    if last in _TRAILING_QUOTES and len(stripped) > 1:
        last = stripped[-2]
    return last in SENTENCE_TERMINATORS


def _srt_time(seconds: float) -> str:
    ms = int(seconds * 1000 + 0.5)  # round-half-up，避免银行家舍入
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _normalize(text: str) -> str:
    """词与 spacing 拼接后归一化空白，保留标点。"""
    return " ".join(text.split())


def load_words(source: Union[str, Path, dict]) -> dict:
    """读入 Scribe convert 响应（文件或 dict），校验最小结构。"""
    if isinstance(source, dict):
        data = source
    else:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    if "words" not in data:
        raise ValueError("输入 JSON 缺少 words 字段（不是 Scribe 词级响应？）")
    return data


def build_cues(
    data: dict, *, pause_threshold: float = 1.5
) -> tuple[list[Cue], dict, list[dict]]:
    """词列表 → (cues, speaker_map, audio_events)。"""
    words = data["words"]
    speaker_map: dict[str, str] = {}
    cues: list[Cue] = []
    events: list[dict] = []

    buffer: list[str] = []
    cue_word_indices: list[int] = []
    cue_start: Optional[float] = None
    cue_end: Optional[float] = None
    cue_speaker: Optional[str] = None
    prev_word_end: Optional[float] = None
    prev_speaker: Optional[str] = None
    any_speaker = any(
        t.get("type", "word") == "word" and t.get("speaker_id") is not None
        for t in words
    )

    def close(reason: str) -> None:
        nonlocal buffer, cue_word_indices, cue_start, cue_end
        text = _normalize("".join(buffer))
        if text and cue_word_indices and cue_start is not None and cue_end is not None:
            cues.append(
                Cue(
                    index=len(cues) + 1,
                    start=cue_start,
                    end=cue_end,
                    speaker=cue_speaker,
                    text=text,
                    word_indices=list(cue_word_indices),
                    boundary_reason=reason,
                )
            )
        buffer, cue_word_indices, cue_start, cue_end = [], [], None, None

    for idx, token in enumerate(words):
        ttype = token.get("type", "word")
        ttext = token.get("text", "")

        if ttype == "audio_event":
            events.append(
                {
                    "text": ttext,
                    "start": token.get("start"),
                    "end": token.get("end"),
                }
            )
            continue

        if ttype == "word":
            start, end = token.get("start"), token.get("end")
            if start is None or end is None:
                raise ValueError(
                    f"第 {idx} 个词缺少时间戳，无法生成可信时间轴（不伪造时间点）: {ttext!r}"
                )

            # 先算有效说话人（None 沿用前一说话人），再判边界
            sid = token.get("speaker_id")
            if any_speaker and sid is None:
                sid = prev_speaker

            # 边界判定（在追加新词之前）
            if cue_word_indices:
                if _is_terminal("".join(buffer)):
                    close("punct")
                elif sid is not None and sid != prev_speaker:
                    close("speaker_change")
                elif prev_word_end is not None and start - prev_word_end > pause_threshold:
                    close("pause")

            if not cue_word_indices:
                cue_start = start
            cue_word_indices.append(idx)
            cue_end = end
            prev_word_end = end

            if any_speaker and sid is not None:
                if sid not in speaker_map:
                    speaker_map[sid] = f"S{len(speaker_map) + 1:02d}"
                cue_speaker = speaker_map[sid]
            prev_speaker = sid

        buffer.append(ttext)

    close("end")
    return cues, speaker_map, events


def render(
    source: Union[str, Path, dict],
    out_srt: Path,
    out_prov: Optional[Path] = None,
    *,
    pause_threshold: float = 1.5,
) -> None:
    """读 Scribe 响应，写 SRT 与 provenance sidecar。"""
    data = load_words(source)
    cues, speaker_map, events = build_cues(data, pause_threshold=pause_threshold)

    lines: list[str] = []
    for cue in cues:
        prefix = f"[{cue.speaker}] " if cue.speaker else ""
        lines.extend(
            [
                str(cue.index),
                f"{_srt_time(cue.start)} --> {_srt_time(cue.end)}",
                f"{prefix}{cue.text}",
                "",
            ]
        )
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    out_srt.write_text("\n".join(lines), encoding="utf-8")

    if out_prov is not None:
        prov = {
            "source": "scribe_v2_words",
            "language_code": data.get("language_code"),
            "pause_threshold": pause_threshold,
            "speaker_map": speaker_map,
            "cues": [
                {
                    "index": c.index,
                    "start": c.start,
                    "end": c.end,
                    "speaker": c.speaker,
                    "boundary_reason": c.boundary_reason,
                    "word_indices": c.word_indices,
                }
                for c in cues
            ],
            "audio_events": events,
        }
        out_prov.parent.mkdir(parents=True, exist_ok=True)
        out_prov.write_text(
            json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8"
        )
