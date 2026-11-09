"""人工 override 纯 SRT 合并测试；不访问数据库、网络或真实样本。

模拟场景：
- 单条精确替换与 SRT 重编号
- split 多条安全替换为一个人工条目，模型漏删时按原时间范围保留
- 源文变化、部分重叠、竞争修改与缺少目标产生冲突
- supersedes 只应用末端修改，retracted 撤销整条修改链
- 原文空白规范化 hash、非法文本与非法时间轴
- 内容寻址文件 create-only 复用与不同内容拒绝、render CLI 输出
"""
import hashlib
import json

import pytest

from pipeline import override


def _source():
    return [
        override.Entry(1000, 3000, "Hello world"),
        override.Entry(4000, 6000, "Next sentence"),
    ]


def _candidate():
    return [
        override.Entry(1000, 3000, "旧译文"),
        override.Entry(4000, 6000, "下一句"),
    ]


def _override(
    identifier=1,
    *,
    after="人工修正",
    target=(1000, 3000),
    source_hash=None,
    supersedes=None,
    retracted=False,
):
    return override.OverrideInput(
        id=identifier,
        start_ms=1000,
        end_ms=3000,
        source_text_sha256=source_hash or override.source_text_sha256("Hello world"),
        after_text=after,
        target_start_ms=target[0] if target is not None else None,
        target_end_ms=target[1] if target is not None else None,
        supersedes_override_id=supersedes,
        retracted=retracted,
    )


def test_parse_and_serialize_renumbers_entries():
    entries = override.parse_srt(
        "7\r\n00:00:01,000 --> 00:00:03,000\r\n第一行\r\n第二行\r\n\r\n"
        "9\r\n00:00:04,000 --> 00:00:06,000\r\n下一句\r\n"
    )
    assert entries[0].text == "第一行\n第二行"
    rendered = override.serialize_srt(entries)
    assert rendered.startswith("1\n00:00:01,000 --> 00:00:03,000\n")
    assert "\n\n2\n00:00:04,000 --> 00:00:06,000\n" in rendered
    assert override.parse_srt(rendered) == entries


def test_exact_override_replaces_model_text():
    result = override.render_overrides(
        source_entries=_source(), candidate_entries=_candidate(), overrides=[_override()]
    )
    assert [entry.text for entry in result.entries] == ["人工修正", "下一句"]
    assert not result.has_conflicts
    assert result.applications[0].status == "applied"


def test_split_entries_become_one_human_entry_without_duplicating_text():
    split = [
        override.Entry(1000, 1500, "你"),
        override.Entry(1500, 3000, "好"),
        override.Entry(4000, 6000, "下一句"),
    ]
    result = override.render_overrides(
        source_entries=_source(), candidate_entries=split, overrides=[_override(after="完整人工译文")]
    )
    assert len(result.entries) == 2
    assert result.entries[0] == override.Entry(1000, 3000, "完整人工译文")
    assert result.entries[0].text.count("完整人工译文") == 1


def test_model_deletion_does_not_delete_human_override():
    result = override.render_overrides(
        source_entries=_source(),
        candidate_entries=[override.Entry(4000, 6000, "下一句")],
        overrides=[_override(after="模型漏译也必须保留")],
    )
    assert result.entries[0] == override.Entry(1000, 3000, "模型漏译也必须保留")
    assert result.entries[1].text == "下一句"


def test_missing_target_without_original_range_is_conflict():
    result = override.render_overrides(
        source_entries=_source(),
        candidate_entries=[override.Entry(4000, 6000, "下一句")],
        overrides=[_override(target=None)],
    )
    assert result.applications[0].reason == "missing_target"
    assert result.has_conflicts


def test_partial_overlap_is_conflict_and_keeps_candidate():
    candidate = [override.Entry(500, 2500, "跨界模型条目")]
    result = override.render_overrides(
        source_entries=_source(), candidate_entries=candidate, overrides=[_override()]
    )
    assert result.entries == candidate
    assert result.applications[0].reason == "partial_overlap"


def test_source_hash_mismatch_is_source_changed_conflict():
    result = override.render_overrides(
        source_entries=_source(),
        candidate_entries=_candidate(),
        overrides=[_override(source_hash=override.source_text_sha256("different source"))],
    )
    assert result.applications[0].reason == "source_changed"
    assert result.entries == _candidate()


def test_competing_independent_overrides_are_conflicts():
    first = _override(1)
    second = _override(2, target=(2000, 4000))
    result = override.render_overrides(
        source_entries=_source(), candidate_entries=_candidate(), overrides=[first, second]
    )
    assert {item.reason for item in result.applications} == {"competing_overrides"}
    assert result.entries == _candidate()


def test_supersedes_applies_only_terminal_edit():
    first = _override(1, after="第一版")
    second = _override(2, after="最终人工版本", supersedes=1)
    result = override.render_overrides(
        source_entries=_source(), candidate_entries=_candidate(), overrides=[first, second]
    )
    assert result.entries[0].text == "最终人工版本"
    assert [item.override_id for item in result.applications] == [2]


def test_retracted_terminal_removes_modification_chain():
    first = _override(1, after="第一版")
    retracted = _override(2, after="", supersedes=1, retracted=True)
    result = override.render_overrides(
        source_entries=_source(), candidate_entries=_candidate(), overrides=[first, retracted]
    )
    assert result.entries == _candidate()
    assert result.applications == []


def test_source_hash_normalizes_unicode_whitespace():
    assert override.source_text_sha256("  Hello\nworld  ") == override.source_text_sha256("Hello world")


def test_empty_human_text_is_rejected():
    with pytest.raises(override.OverrideError, match="empty_after_text"):
        override.render_overrides(
            source_entries=_source(), candidate_entries=_candidate(), overrides=[_override(after="  ")]
        )


def test_zero_duration_interpolation_point_is_preserved():
    candidate = [
        override.Entry(1000, 3000, "旧译文"),
        override.Entry(3000, 3000, "零时长插值点"),
        override.Entry(4000, 6000, "下一句"),
    ]
    result = override.render_overrides(source_entries=_source(), candidate_entries=candidate, overrides=[])
    assert result.entries == candidate
    assert override.parse_srt(override.serialize_srt(candidate)) == candidate


def test_invalid_candidate_timeline_is_rejected():
    candidate = [override.Entry(1000, 5000, "重叠"), override.Entry(4000, 6000, "下一句")]
    with pytest.raises(override.OverrideError, match="invalid_timeline_order"):
        override.render_overrides(source_entries=_source(), candidate_entries=candidate, overrides=[])


def test_render_file_create_only_reuse_and_conflict(tmp_path):
    source_path = tmp_path / "source.srt"
    candidate_path = tmp_path / "candidate.srt"
    output_path = tmp_path / "rendered" / "zh.srt"
    source_path.write_text(override.serialize_srt(_source()), encoding="utf-8")
    candidate_path.write_text(override.serialize_srt(_candidate()), encoding="utf-8")

    first = override.render_file(
        source_path=source_path,
        candidate_path=candidate_path,
        output_path=output_path,
        overrides=[_override()],
    )
    second = override.render_file(
        source_path=source_path,
        candidate_path=candidate_path,
        output_path=output_path,
        overrides=[_override()],
    )
    assert first["created"] is True
    assert second["created"] is False
    assert first["sha256"] == second["sha256"] == hashlib.sha256(output_path.read_bytes()).hexdigest()

    output_path.write_text("different", encoding="utf-8")
    with pytest.raises(override.OverrideError, match="output_exists"):
        override.render_file(
            source_path=source_path,
            candidate_path=candidate_path,
            output_path=output_path,
            overrides=[_override()],
        )


def test_render_cli_outputs_structured_result(tmp_path, capsys):
    source_path = tmp_path / "source.srt"
    candidate_path = tmp_path / "candidate.srt"
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "output.srt"
    source_path.write_text(override.serialize_srt(_source()), encoding="utf-8")
    candidate_path.write_text(override.serialize_srt(_candidate()), encoding="utf-8")
    request_path.write_text(
        json.dumps({"overrides": [{"id": 1, "start_ms": 1000, "end_ms": 3000,
                                  "source_text_sha256": override.source_text_sha256("Hello world"),
                                  "after_text": "CLI 人工稿", "target_start_ms": 1000,
                                  "target_end_ms": 3000}]}),
        encoding="utf-8",
    )
    exit_code = override.main([
        "render", "--source", str(source_path), "--candidate", str(candidate_path),
        "--request", str(request_path), "--output", str(output_path),
    ])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert payload["data"]["has_conflicts"] is False
    assert "CLI 人工稿" in output_path.read_text(encoding="utf-8")
