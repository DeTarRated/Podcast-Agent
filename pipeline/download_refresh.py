"""执行层·音频链接刷新：签名／临时直链失效时，从 feed 重新取同一 guid 的 enclosure。

Acast、Simplecast 等托管方的 enclosure 是短时签名链接，长期保存会过期。
下载遇到 403/404/410 时应当重新拉取 feed 换取新链接，而不是拿旧链接反复重试。

本模块只读 feed、只更新本地 enclosure 字段，不产生任何付费调用。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import feedparser
import httpx

# 只有这些状态码视为「链接过期」，其余错误按原样失败，避免掩盖真实故障。
STALE_LINK_STATUSES = frozenset({403, 404, 410})


class EnclosureError(RuntimeError):
    """无法从 feed 取回新的音频链接。"""


def is_stale_link(error: BaseException) -> bool:
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status in STALE_LINK_STATUSES


def _entry_enclosure(entry) -> Optional[str]:
    for item in entry.get("enclosures") or []:
        href = item.get("href") or item.get("url")
        if not href:
            continue
        if (item.get("type") or "audio/mpeg").startswith("audio/"):
            return href
    return None


def fetch_fresh_enclosure(
    feed_url: str,
    guid: str,
    *,
    transport: Optional[httpx.BaseTransport] = None,
    trust_env: bool = True,
    timeout: float = 30.0,
) -> str:
    """从 feed 重新解析该 guid 的 enclosure；取不到则抛 EnclosureError。"""
    if not feed_url:
        raise EnclosureError("缺少 feed URL，无法刷新音频链接")
    try:
        with httpx.Client(
            timeout=timeout, transport=transport, trust_env=trust_env, follow_redirects=True
        ) as client:
            # 必须取完整响应：条件请求可能返回 304，拿不到新链接。
            response = client.get(feed_url, headers={"User-Agent": "podcast-agent/download-refresh"})
    except httpx.HTTPError as exc:
        raise EnclosureError(f"刷新 feed 失败: {exc}") from exc
    if response.status_code != 200:
        raise EnclosureError(f"刷新 feed 返回 HTTP {response.status_code}")
    parsed = feedparser.parse(response.content)
    for entry in parsed.entries:
        entry_guid = entry.get("id") or entry.get("guid") or entry.get("link")
        if isinstance(entry_guid, str) and entry_guid.strip() == guid:
            fresh = _entry_enclosure(entry)
            if fresh:
                return fresh
            raise EnclosureError("feed 中该条目缺少音频 enclosure")
    raise EnclosureError("feed 中已找不到该 guid（可能已下架）")


def refresh_episode_enclosure(
    conn: sqlite3.Connection,
    episode_id: int,
    *,
    guid: str,
    feed_url: str,
    current: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
    trust_env: bool = True,
) -> str:
    """刷新并落库；返回新链接。链接未变化时也返回（调用方据此决定是否重试）。"""
    fresh = fetch_fresh_enclosure(
        feed_url, guid, transport=transport, trust_env=trust_env
    )
    if fresh != current:
        conn.execute(
            "UPDATE episode SET enclosure = ?, updated_at = datetime('now') WHERE id = ?",
            (fresh, episode_id),
        )
        # 候选表也同步，便于人工核对来源链接。
        conn.execute(
            "UPDATE discovery_candidate SET enclosure = ?, updated_at = datetime('now') "
            "WHERE episode_id = ?",
            (fresh, episode_id),
        )
        conn.commit()
    return fresh
