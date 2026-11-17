"""分类策略测试：一份 JSON 自由组合关注对象、按 feed 覆盖、排除词预过滤与失败关闭。

模拟场景：
- 未提供策略文件时退回 env 名单（历史行为）
- 策略文件定义 topics/default_topics/feeds 覆盖，resolve 返回正确组合
- 空数组表示该 feed 不分类
- 排除词命中直接跳过且不产生付费调用
- 未知字段、未知 topic 引用、非法类型一律 PolicyError
"""
import json

import pytest

from discover import policy


BASE = {
    "version": 1,
    "scope": "男子一线队",
    "unknown_is_relevant": True,
    "topics": {
        "chelsea": {"label": "Chelsea 男子足球队", "aliases": ["切尔西", "Blues"]},
        "man-utd": {"label": "Manchester United 男子足球队", "aliases": ["曼联", "Man Utd"]},
    },
    "default_topics": ["chelsea"],
    "feeds": {"3": ["chelsea", "man-utd"], "9": []},
    "prefilter": {"exclude_keywords": ["rugby", "Formula 1"]},
}


@pytest.fixture()
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "interest-policy.json"
    path.write_text(json.dumps(BASE), encoding="utf-8")
    monkeypatch.setenv(policy.POLICY_ENV, str(path))
    return path


def test_fallback_without_file(monkeypatch, tmp_path):
    monkeypatch.setenv(policy.POLICY_ENV, str(tmp_path / "absent.json"))
    monkeypatch.delenv(policy.DEFAULT_TOPICS_ENV, raising=False)
    current = policy.load()
    assert [topic.label for topic in current.resolve(1)] == [policy.FALLBACK_LABEL]
    monkeypatch.setenv(policy.DEFAULT_TOPICS_ENV, "Chelsea 男子足球队,Manchester United 男子足球队")
    assert [t.label for t in policy.load().resolve(1)][1] == "Manchester United 男子足球队"


def test_feed_composition(policy_file):
    current = policy.load()
    # feed 1 走 default_topics
    assert [t.key for t in current.resolve(1)] == ["chelsea"]
    # feed 3 覆盖为两个对象
    assert [t.key for t in current.resolve(3)] == ["chelsea", "man-utd"]
    # feed 9 显式空数组 → 不分类
    assert current.resolve(9) == ()
    # 未列出的 feed 仍按默认
    assert [t.key for t in current.resolve(42)] == ["chelsea"]


def test_prompt_includes_aliases_and_scope(policy_file):
    current = policy.load()
    prompt = policy.prompt_for(current, current.resolve(3))
    assert "Chelsea 男子足球队" in prompt and "曼联" in prompt
    assert "男子一线队" in prompt
    assert "无法确定但有潜在男足关联时判 true" in prompt


def test_unknown_is_relevant_false_changes_prompt(policy_file):
    data = dict(BASE, unknown_is_relevant=False)
    policy_file.write_text(json.dumps(data), encoding="utf-8")
    current = policy.load()
    prompt = policy.prompt_for(current, current.resolve(1))
    assert "无法确定时判 false" in prompt


def test_prefilter_excludes_by_keyword(policy_file):
    current = policy.load()
    assert current.excluded_by("Dragons Slay the All Blacks | Rugby round-up") == "rugby"
    assert current.excluded_by("Formula 1: Verstappen wins") == "Formula 1"
    assert current.excluded_by("Chelsea beat Spurs") is None


@pytest.mark.parametrize("mutate,message", [
    (lambda d: d.update(unknown_field=1), "未知字段"),
    (lambda d: d.update(version=2), "version"),
    (lambda d: d.update(topics={}), "topics"),
    (lambda d: d.update(default_topics=["nope"]), "未定义的 topic"),
    (lambda d: d.update(feeds={"x": ["chelsea"]}), "feed id"),
    (lambda d: d.update(feeds={"1": ["nope"]}), "未定义的 topic"),
    (lambda d: d["topics"]["chelsea"].update(extra=1), "未知字段"),
    (lambda d: d["topics"]["chelsea"].update(label=""), "label"),
    (lambda d: d.update(prefilter={"exclude_keywords": "rugby"}), "数组"),
    (lambda d: d.update(prefilter={"other": []}), "未知字段"),
    (lambda d: d.update(unknown_is_relevant="yes"), "布尔"),
])
def test_invalid_policy_fails_closed(tmp_path, monkeypatch, mutate, message):
    data = json.loads(json.dumps(BASE))
    mutate(data)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(policy.POLICY_ENV, str(path))
    with pytest.raises(policy.PolicyError, match=message):
        policy.load()


def test_invalid_json_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(policy.POLICY_ENV, str(path))
    with pytest.raises(policy.PolicyError, match="合法 JSON"):
        policy.load()
