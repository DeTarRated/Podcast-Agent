"""发现层·分类策略：一份可组装的关注名单，按 feed 组合，供分类提示与预过滤使用。

设计目标：运维只维护一个 JSON 文件，就能自由搭配「哪些节目关心哪些球队」，
而不用改代码。策略非法时失败关闭（零付费请求），避免配置写错变成批量下载。

JSON 结构（版本 1）：

{
  "version": 1,
  "scope": "男子一线队",
  "unknown_is_relevant": true,
  "topics": {
    "chelsea": {"label": "Chelsea 男子足球队", "aliases": ["切尔西", "Blues"]},
    "man-utd": {"label": "Manchester United 男子足球队", "aliases": ["曼联", "Man Utd"]}
  },
  "default_topics": ["chelsea", "man-utd"],
  "feeds": {"1": ["chelsea"], "3": ["chelsea", "man-utd"]},
  "prefilter": {"exclude_keywords": ["rugby", "cricket", "Formula 1"]}
}

- default_topics 缺省表示「所有 feed 用全部 topics」
- feeds 里出现的 feed 覆盖 default_topics；数组为空表示该 feed 不做分类（一律跳过）
- prefilter.exclude_keywords 命中标题或简介即跳过，不调用付费分类
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_TOPICS_ENV = "PODCAST_CLASSIFY_TOPICS"
POLICY_ENV = "PODCAST_CLASSIFY_POLICY"
FALLBACK_LABEL = "Chelsea 男子足球队"
FALLBACK_MAX_TOPICS = 8
MAX_TOPICS = 12
MAX_ALIASES = 8
MAX_KEYWORDS = 40


class PolicyError(RuntimeError):
    """策略文件缺失以外的任何问题都必须失败关闭。"""


@dataclass(frozen=True)
class Topic:
    key: str
    label: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    scope: str
    unknown_is_relevant: bool
    topics: dict[str, Topic]
    default_topics: tuple[str, ...]
    feed_topics: dict[int, tuple[str, ...]]
    exclude_keywords: tuple[str, ...]

    def resolve(self, feed_id: int) -> tuple[Topic, ...]:
        """返回该 feed 生效的关注对象；空元组表示不分类。"""
        keys = self.feed_topics.get(feed_id, self.default_topics)
        return tuple(self.topics[key] for key in keys)

    def excluded_by(self, text: str) -> Optional[str]:
        lowered = text.lower()
        for keyword in self.exclude_keywords:
            if keyword and keyword.lower() in lowered:
                return keyword
        return None


def policy_path(path: Optional[str | Path] = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(POLICY_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "var" / "interest-policy.json"


def _string_list(value, label: str, *, limit: int, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PolicyError(f"{label} 必须是数组")
    if not value and not allow_empty:
        raise PolicyError(f"{label} 不能为空")
    if len(value) > limit:
        raise PolicyError(f"{label} 最多 {limit} 项")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError(f"{label} 只接受非空字符串")
        text = item.strip()
        if len(text) > 40 or any(ch in text for ch in "\n\r{}"):
            raise PolicyError(f"{label} 含非法字符: {text!r}")
        result.append(text)
    return tuple(result)


def parse(data) -> Policy:
    if not isinstance(data, dict):
        raise PolicyError("策略必须是 JSON object")
    allowed = {"version", "scope", "unknown_is_relevant", "topics", "default_topics", "feeds", "prefilter"}
    unknown = set(data) - allowed
    if unknown:
        raise PolicyError(f"策略含未知字段: {sorted(unknown)}")
    if data.get("version") != 1:
        raise PolicyError("策略 version 必须为 1")

    scope = data.get("scope", "男子一线队")
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 40:
        raise PolicyError("scope 必须是 1..40 字的字符串")
    unknown_is_relevant = data.get("unknown_is_relevant", True)
    if not isinstance(unknown_is_relevant, bool):
        raise PolicyError("unknown_is_relevant 必须是布尔值")

    raw_topics = data.get("topics")
    if not isinstance(raw_topics, dict) or not raw_topics:
        raise PolicyError("topics 必须是非空 object")
    if len(raw_topics) > MAX_TOPICS:
        raise PolicyError(f"topics 最多 {MAX_TOPICS} 项")
    topics: dict[str, Topic] = {}
    for key, value in raw_topics.items():
        if not isinstance(key, str) or not key.strip():
            raise PolicyError("topics 的键必须是非空字符串")
        if not isinstance(value, dict):
            raise PolicyError(f"topics.{key} 必须是 object")
        extra = set(value) - {"label", "aliases"}
        if extra:
            raise PolicyError(f"topics.{key} 含未知字段: {sorted(extra)}")
        label = value.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 40:
            raise PolicyError(f"topics.{key}.label 必须是 1..40 字字符串")
        aliases = _string_list(value.get("aliases", []), f"topics.{key}.aliases",
                               limit=MAX_ALIASES, allow_empty=True)
        topics[key] = Topic(key=key, label=label.strip(), aliases=aliases)

    default_topics = _string_list(
        data.get("default_topics", list(topics)), "default_topics",
        limit=MAX_TOPICS, allow_empty=True,
    )
    for key in default_topics:
        if key not in topics:
            raise PolicyError(f"default_topics 引用了未定义的 topic: {key}")

    raw_feeds = data.get("feeds", {})
    if not isinstance(raw_feeds, dict):
        raise PolicyError("feeds 必须是 object")
    feed_topics: dict[int, tuple[str, ...]] = {}
    for raw_id, value in raw_feeds.items():
        try:
            feed_id = int(raw_id)
        except (TypeError, ValueError):
            raise PolicyError(f"feeds 的键必须是 feed id: {raw_id!r}") from None
        if feed_id <= 0:
            raise PolicyError(f"feeds 的键必须是正数: {raw_id!r}")
        keys = _string_list(value, f"feeds.{feed_id}", limit=MAX_TOPICS, allow_empty=True)
        for key in keys:
            if key not in topics:
                raise PolicyError(f"feeds.{feed_id} 引用了未定义的 topic: {key}")
        feed_topics[feed_id] = keys

    raw_prefilter = data.get("prefilter", {})
    if not isinstance(raw_prefilter, dict):
        raise PolicyError("prefilter 必须是 object")
    extra = set(raw_prefilter) - {"exclude_keywords"}
    if extra:
        raise PolicyError(f"prefilter 含未知字段: {sorted(extra)}")
    excludes = _string_list(
        raw_prefilter.get("exclude_keywords", []), "prefilter.exclude_keywords",
        limit=MAX_KEYWORDS, allow_empty=True,
    )

    return Policy(
        scope=scope.strip(),
        unknown_is_relevant=unknown_is_relevant,
        topics=topics,
        default_topics=default_topics,
        feed_topics=feed_topics,
        exclude_keywords=excludes,
    )


def _fallback_policy() -> Policy:
    """没有策略文件时保持历史行为：env 名单或内置默认。"""
    raw = os.environ.get(DEFAULT_TOPICS_ENV, FALLBACK_LABEL)
    labels = [item.strip() for item in raw.split(",") if item.strip()]
    if not labels or len(labels) > FALLBACK_MAX_TOPICS:
        raise PolicyError(f"{DEFAULT_TOPICS_ENV} 必须为 1..{FALLBACK_MAX_TOPICS} 个逗号分隔条目")
    for label in labels:
        if len(label) > 40 or any(ch in label for ch in "\n\r{}"):
            raise PolicyError(f"{DEFAULT_TOPICS_ENV} 条目非法: {label!r}")
    topics = {f"t{index}": Topic(key=f"t{index}", label=label, aliases=()) for index, label in enumerate(labels)}
    return Policy(
        scope="男子一线队",
        unknown_is_relevant=True,
        topics=topics,
        default_topics=tuple(topics),
        feed_topics={},
        exclude_keywords=(),
    )


def load(path: Optional[str | Path] = None) -> Policy:
    """读取策略；文件不存在时退回 env 名单。文件存在但非法 → PolicyError。"""
    target = policy_path(path)
    if not target.exists():
        return _fallback_policy()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"策略文件不可读或不是合法 JSON: {target}: {exc}") from None
    return parse(data)


def prompt_for(policy: Policy, topics: tuple[Topic, ...]) -> str:
    """把生效对象渲染成分类型提示词。"""
    if not topics:
        raise PolicyError("该 feed 未配置任何关注对象，不应发起付费分类")
    described = "、".join(
        topic.label + (f"（别名：{'、'.join(topic.aliases)}）" if topic.aliases else "")
        for topic in topics
    )
    unknown = (
        "无法确定但有潜在男足关联时判 true，避免漏掉。"
        if policy.unknown_is_relevant
        else "无法确定时判 false。"
    )
    return (
        f"你是播客元数据分类器。只判断是否可能涉及以下对象：{described}。"
        f"判定范围限于{policy.scope}，包括球员、教练、管理层、比赛、转会、伤病和战术讨论。"
        "与上述对象无关的其他球队、其他运动或女足内容应判 false。"
        f"{unknown}"
        '只输出 JSON object：{"relevant":true或false,"reason":"简短原因"}。'
    )
