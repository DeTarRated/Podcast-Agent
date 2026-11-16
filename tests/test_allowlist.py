"""业务白名单契约测试：缺省放行、维度过滤、job 维度与非法配置拒绝。
模拟场景：文件缺失放行、各维度允许/拒绝、未知键与非列表值失败关闭、布尔/字符串混排拒绝。
"""
import json

import pytest

from state import allowlist


def test_missing_file_allows_everything(tmp_path):
    assert allowlist.load(tmp_path / "absent.json") is None
    assert allowlist.episode_allowed(None, 1) is True
    assert allowlist.feed_allowed(None, 1) is True
    assert allowlist.guid_allowed(None, "f", "g") is True
    assert allowlist.job_allowed(None, 1) is True


def test_dimension_scoping(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({"feeds": [1], "episodes": [7], "jobs": [3],
                                "guids": ["feed-a::guid-1"]}))
    data = allowlist.load(path)
    assert allowlist.feed_allowed(data, 1) is True
    assert allowlist.feed_allowed(data, 2) is False
    assert allowlist.episode_allowed(data, 7) is True
    assert allowlist.episode_allowed(data, 8) is False
    assert allowlist.job_allowed(data, 3) is True
    assert allowlist.job_allowed(data, 4) is False
    assert allowlist.guid_allowed(data, "feed-a", "guid-1") is True
    assert allowlist.guid_allowed(data, "feed-b", "guid-1") is False
    # 键缺省表示该维度不限制。
    path.write_text(json.dumps({"episodes": [7]}))
    data = allowlist.load(path)
    assert allowlist.feed_allowed(data, 99) is True
    assert allowlist.job_allowed(data, 99) is True
    assert allowlist.guid_allowed(data, "any", "guid") is True
    assert allowlist.episode_allowed(data, 99) is False


@pytest.mark.parametrize("payload", [
    {"unknown_key": [1]},
    {"episodes": "12"},
    {"episodes": ["1", "2"]},
    {"feeds": [True]},
    {"jobs": ["3"]},
    {"guids": [1, 2]},
    {"feeds": None},
    {"episodes": []},
    ["not", "an", "object"],
])
def test_invalid_config_fails_closed(tmp_path, payload):
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        allowlist.load(path)


def test_runtime_guard_pause(tmp_path, monkeypatch):
    from state import runtime_guard

    monkeypatch.setenv("PODCAST_BUSINESS_PAUSE_FILE", str(tmp_path / "pause"))
    assert runtime_guard.is_business_paused() is False
    runtime_guard.assert_business_running()
    (tmp_path / "pause").write_text("paused")
    assert runtime_guard.is_business_paused() is True
    with pytest.raises(RuntimeError, match="业务暂停标记存在"):
        runtime_guard.assert_business_running()
