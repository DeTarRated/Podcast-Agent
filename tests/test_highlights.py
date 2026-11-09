"""结构化看点校验与固定模板测试；无模型或网络调用。

模拟场景：
- 合法 summary/items 与字幕内时间点通过
- 非 JSON、额外字段、空文本、非法时间类型与越界时间点拒绝
- 固定 HTML 模板转义模型／RSS 文本并生成时间链接
"""
import pytest

from delivery.highlights import HighlightsError, load_validated, render_highlights_html, validate_highlights
from pipeline.override import Entry


def _entries():
    return [Entry(1000, 3000, "Hello"), Entry(4000, 6000, "Next")]


def test_valid_highlights_are_normalized():
    result = validate_highlights(
        '{"summary":"  摘要  ","items":[{"text":"  看点  ","timestampMs":1000}]}', _entries()
    )
    assert result == {"summary": "摘要", "items": [{"text": "看点", "timestampMs": 1000}]}


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        '{"summary":"x","items":[],"extra":true}',
        '{"summary":"","items":[]}',
        '{"summary":"x","items":[{"text":"","timestampMs":1000}]}',
        '{"summary":"x","items":[{"text":"x","timestampMs":true}]}',
        '{"summary":"x","items":[{"text":"x","timestampMs":3500}]}',
    ],
)
def test_invalid_highlights_are_rejected(value):
    with pytest.raises(HighlightsError):
        validate_highlights(value, _entries())


def test_timestamp_in_gap_is_aligned_but_distant_timestamp_is_rejected(tmp_path):
    path = tmp_path / "highlights.json"
    path.write_text('{"summary":"x","items":[{"text":"x","timestampMs":3500}]}', encoding="utf-8")
    assert load_validated(path, _entries())["items"][0]["timestampMs"] == 4000
    path.write_text('{"summary":"x","items":[{"text":"x","timestampMs":20000}]}', encoding="utf-8")
    with pytest.raises(HighlightsError, match="超过"):
        load_validated(path, _entries())


def test_fixed_template_escapes_content_and_links_time():
    rendered = render_highlights_html(
        {"title": "<script>alert(1)</script>", "feed": "feed&name"},
        {"summary": "<b>摘要</b>", "items": [{"text": "<img src=x>", "timestampMs": 4000}]},
    )
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "&lt;b&gt;摘要&lt;/b&gt;" in rendered
    assert "&lt;img src=x&gt;" in rendered
    assert "feed&amp;name" in rendered
    assert 'href="?t=4"' in rendered
