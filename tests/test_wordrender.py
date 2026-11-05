"""词级重组组件测试：合成 Scribe 词级 JSON，覆盖正常与异常路径。

模拟场景：
- 句末标点切条、SRT 格式与毫秒取整
- 标点位于 spacing token 的场景
- 说话人变化切条与 S01/S02 编号
- 停顿超阈值切条（无标点 ASR 兜底）
- 时间戳缺失 → 明确失败不伪造
- audio_event 不进字幕文本、保留进 provenance
- speaker_id 为 None 沿用前一说话人；全程无说话人则无前缀
- 输入缺 words 字段 → 拒绝
"""
import json

import pytest

from pipeline import wordrender


def word(text, start, end, speaker_id="speaker_0", type_="word"):
    return {
        "text": text,
        "type": type_,
        "start": start,
        "end": end,
        "speaker_id": speaker_id,
    }


def spacing(text=" "):
    return {"text": text, "type": "spacing", "start": None, "end": None, "speaker_id": None}


def doc(*tokens, language_code="eng"):
    return {"language_code": language_code, "words": list(tokens)}


def test_basic_sentence_split_and_srt_format(tmp_path):
    data = doc(
        word("Hello", 0.0, 0.4),
        spacing(),
        word("world.", 0.4, 0.9),
        spacing(),
        word("This", 1.0, 1.2),
        spacing(),
        word("is", 1.2, 1.4),
        spacing(),
        word("next.", 1.4, 1.9),
    )
    out = tmp_path / "en.srt"
    prov = tmp_path / "en.prov.json"
    wordrender.render(data, out, prov)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:00,900"
    assert lines[2] == "[S01] Hello world."
    assert lines[4] == "2"
    assert lines[5] == "00:00:01,000 --> 00:00:01,900"
    assert lines[6] == "[S01] This is next."

    p = json.loads(prov.read_text(encoding="utf-8"))
    assert len(p["cues"]) == 2
    assert p["cues"][0]["boundary_reason"] == "punct"
    assert p["cues"][0]["word_indices"] == [0, 2]  # 原始 token 索引，含 spacing 占位
    assert p["cues"][1]["boundary_reason"] == "end"
    assert p["speaker_map"] == {"speaker_0": "S01"}


def test_punctuation_in_spacing_token(tmp_path):
    # Scribe 可能把标点放在 spacing 里：word "world" + spacing "! "
    data = doc(
        word("Wow", 0.0, 0.3),
        spacing("! "),
        word("Next", 0.8, 1.1),
        spacing(),
        word("one", 1.1, 1.4),
        spacing("."),
    )
    out = tmp_path / "en.srt"
    wordrender.render(data, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[2] == "[S01] Wow!"
    assert lines[6] == "[S01] Next one."


def test_speaker_change_splits_and_numbers(tmp_path):
    data = doc(
        word("Hi", 0.0, 0.2, speaker_id="speaker_0"),
        spacing(),
        word("there.", 0.2, 0.5, speaker_id="speaker_0"),
        spacing(),
        word("Hello", 0.6, 0.9, speaker_id="speaker_1"),
        spacing(),
        word("back.", 0.9, 1.2, speaker_id="speaker_1"),
    )
    out = tmp_path / "en.srt"
    prov = tmp_path / "en.prov.json"
    wordrender.render(data, out, prov)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[2].startswith("[S01] ")
    assert lines[6].startswith("[S02] ")

    p = json.loads(prov.read_text(encoding="utf-8"))
    assert p["speaker_map"] == {"speaker_0": "S01", "speaker_1": "S02"}
    # 「标点+换人」同时发生时按检查顺序记 punct
    assert p["cues"][0]["boundary_reason"] == "punct"


def test_pause_splits_above_threshold(tmp_path):
    data = doc(
        word("First", 0.0, 0.3),
        spacing(),
        word("part", 0.3, 0.6),
        spacing(),
        word("After", 3.0, 3.3),  # 停顿 2.4s > 阈值 1.5s
        spacing(),
        word("gap", 3.3, 3.6),
        spacing("."),
    )
    prov = tmp_path / "en.prov.json"
    wordrender.render(data, tmp_path / "en.srt", prov)

    p = json.loads(prov.read_text(encoding="utf-8"))
    assert p["cues"][0]["boundary_reason"] == "pause"
    assert p["cues"][1]["start"] == 3.0


def test_missing_timestamps_fail_loudly(tmp_path):
    data = doc(word("ok", 0.0, 0.3), spacing(), word("broken", None, None))
    with pytest.raises(ValueError, match="缺少时间戳"):
        wordrender.render(data, tmp_path / "en.srt")


def test_audio_event_excluded_from_text_kept_in_provenance(tmp_path):
    data = doc(
        word("They", 0.0, 0.3),
        spacing(),
        word("laughed", 0.3, 0.7),
        word("(laughter)", 0.7, 1.2, speaker_id=None, type_="audio_event"),
        spacing(),
        word("loudly.", 1.3, 1.8),
    )
    out = tmp_path / "en.srt"
    prov = tmp_path / "en.prov.json"
    wordrender.render(data, out, prov)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert "(laughter)" not in lines[2]

    p = json.loads(prov.read_text(encoding="utf-8"))
    assert len(p["audio_events"]) == 1
    assert p["audio_events"][0]["start"] == 0.7


def test_none_speaker_inherits_previous(tmp_path):
    data = doc(
        word("A", 0.0, 0.2, speaker_id="speaker_0"),
        spacing(),
        word("B", 0.2, 0.4, speaker_id=None),  # 沿用 speaker_0
        spacing(),
        word("C.", 0.4, 0.6, speaker_id="speaker_0"),
    )
    out = tmp_path / "en.srt"
    wordrender.render(data, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[2] == "[S01] A B C."


def test_no_speakers_anywhere_no_prefix(tmp_path):
    data = doc(word("No", 0.0, 0.2, speaker_id=None), spacing(), word("speaker.", 0.2, 0.5, speaker_id=None))
    out = tmp_path / "en.srt"
    wordrender.render(data, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[2] == "No speaker."


def test_invalid_input_missing_words(tmp_path):
    with pytest.raises(ValueError, match="缺少 words"):
        wordrender.render({"language_code": "eng"}, tmp_path / "en.srt")


def test_srt_time_rounding():
    assert wordrender._srt_time(1.2345) == "00:00:01,235"
    assert wordrender._srt_time(3661.999) == "01:01:01,999"
