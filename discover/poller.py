"""发现层·RSS 条件轮询、guid 去重与原子入队。

只读取 feed 元数据，不下载 enclosure、不调用分类模型或付费 API。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx

from state import discovery, episode, feed as feed_state
from discover.classify import ClassificationError


class DiscoveryError(RuntimeError):
    """feed 请求、解析或候选处理失败。"""


def _published_at(entry) -> Optional[str]:
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if not value:
        return None
    return datetime(*value[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")


def _entry_values(entry) -> dict:
    guid = entry.get("id") or entry.get("guid") or entry.get("link")
    enclosures = entry.get("enclosures") or []
    enclosure = None
    for item in enclosures:
        href = item.get("href") or item.get("url")
        if href and (item.get("type") or "audio/mpeg").startswith("audio/"):
            enclosure = href
            break
    if enclosure is None:
        enclosure = next(
            (item.get("href") or item.get("url") for item in enclosures if item.get("href") or item.get("url")),
            None,
        )
    return {
        "guid": guid.strip() if isinstance(guid, str) and guid.strip() else None,
        "title": entry.get("title") or "未命名节目",
        "published_at": _published_at(entry),
        "enclosure": enclosure,
        "summary": entry.get("summary") or entry.get("description"),
    }


def _process_entry(
    conn,
    feed: sqlite3.Row,
    values: dict,
    now: str,
    result: dict,
    *,
    baseline: bool,
    classifier=None,
) -> None:
    if values["guid"] is None:
        result["invalid"] += 1
        result["warnings"].append("条目缺少 guid/id/link，未创建候选")
        return

    # 先短事务持久化候选，分类模型绝不在该事务内调用。
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = discovery.get_candidate(conn, feed["id"], values["guid"])
        if existing is not None:
            result["duplicates"] += 1
            if existing["status"] != "pending" or not values["enclosure"]:
                conn.commit()
                return
            candidate_id = int(existing["id"])
        else:
            candidate_id = discovery.insert_candidate(
                conn,
                feed_id=feed["id"], guid=values["guid"], title=values["title"],
                published_at=values["published_at"], enclosure=values["enclosure"],
                summary=values["summary"], now=now,
            )
            result["new_candidates"] += 1
        if baseline:
            discovery.mark_candidate(
                conn, candidate_id, status="skipped", reason="initial_feed_baseline", now=now
            )
            result["skipped"] += 1
            conn.commit()
            return
        if not values["enclosure"]:
            discovery.mark_candidate(
                conn, candidate_id, status="failed", reason="缺少音频 enclosure", now=now
            )
            result["invalid"] += 1
            conn.commit()
            return
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    candidate = conn.execute(
        "SELECT * FROM discovery_candidate WHERE id = ?", (candidate_id,)
    ).fetchone()
    if classifier is not None and candidate["classification_relevant"] is None:
        try:
            decision = classifier(conn, candidate)
        except ClassificationError as exc:
            with conn:
                discovery.mark_candidate(
                    conn, candidate_id, status="failed", reason=str(exc)[:500], now=now
                )
            result["classification_failed"] += 1
            return
        relevant = decision.relevant
        reason = decision.reason
        call_ref = decision.call_ref
        result["classified"] += 1
    else:
        relevant = True if classifier is None else bool(candidate["classification_relevant"])
        reason = candidate["reason"] if classifier is not None else None
        call_ref = candidate["classification_call_ref"]

    conn.execute("BEGIN IMMEDIATE")
    try:
        if classifier is not None:
            conn.execute(
                """
                UPDATE discovery_candidate
                SET classification_relevant = ?, classification_call_ref = ?, reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(relevant), call_ref, reason, now, candidate_id),
            )
        if relevant:
            episode_id, reused = discovery.enqueue_candidate(
                conn, candidate_id, feed=feed, now=now, commit=False
            )
            result["enqueued"] += 0 if reused else 1
            result["episode_ids"].append(episode_id)
        else:
            discovery.mark_candidate(conn, candidate_id, status="skipped", reason=reason, now=now)
            result["skipped"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def poll_feed(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    now: str,
    transport: Optional[httpx.BaseTransport] = None,
    trust_env: bool = True,
    classifier=None,
) -> dict:
    feed = feed_state.get(conn, feed_id)
    if feed is None:
        raise DiscoveryError(f"feed 不存在: {feed_id}")
    if not feed["enabled"]:
        return {"feed_id": feed_id, "status": "disabled", "new_candidates": 0, "enqueued": 0}
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        "User-Agent": "podcast-agent/discovery",
    }
    if feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]
    with httpx.Client(
        timeout=30.0, transport=transport, trust_env=trust_env, follow_redirects=True
    ) as client:
        response = client.get(feed["feed_url"], headers=headers)
    if response.status_code == 304:
        return {
            "feed_id": feed_id,
            "status": "not_modified",
            "new_candidates": 0,
            "enqueued": 0,
            "duplicates": 0,
            "episode_ids": [],
        }
    if response.status_code != 200:
        raise DiscoveryError(f"feed HTTP {response.status_code}")

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries and not parsed.feed:
        raise DiscoveryError(f"feed 解析失败: {parsed.bozo_exception}")
    baseline = bool(feed["bootstrap_pending"])
    result = {
        "feed_id": feed_id,
        "status": "baseline" if baseline else "ok",
        "new_candidates": 0,
        "enqueued": 0,
        "duplicates": 0,
        "invalid": 0,
        "skipped": 0,
        "classified": 0,
        "classification_failed": 0,
        "episode_ids": [],
        "warnings": [],
    }
    for entry in parsed.entries:
        _process_entry(
            conn, feed, _entry_values(entry), now, result,
            baseline=baseline, classifier=classifier,
        )
    feed_state.record_success(
        conn,
        feed_id,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
        now=now,
    )
    return result


def poll_all(
    conn: sqlite3.Connection,
    *,
    now: Optional[str] = None,
    feed_id: Optional[int] = None,
    transport: Optional[httpx.BaseTransport] = None,
    trust_env: bool = True,
    classifier=None,
) -> dict:
    timestamp = now or episode.utcnow_iso()
    if feed_id is None:
        feeds = feed_state.list_feeds(conn, include_disabled=False)
    else:
        selected = feed_state.get(conn, feed_id)
        feeds = [selected] if selected is not None and selected["enabled"] else []
    items = []
    for feed in feeds:
        try:
            result = poll_feed(
                conn,
                feed["id"],
                now=timestamp,
                transport=transport,
                trust_env=trust_env,
                classifier=classifier,
            )
        except (DiscoveryError, httpx.HTTPError, ValueError, sqlite3.Error) as exc:
            error = str(exc)[:1000]
            feed_state.record_error(conn, feed["id"], error, timestamp)
            result = {
                "feed_id": feed["id"],
                "status": "failed",
                "error": error,
                "new_candidates": 0,
                "enqueued": 0,
            }
        items.append(result)
    return {
        "status": "success",
        "feeds": len(items),
        "new_candidates": sum(item.get("new_candidates", 0) for item in items),
        "enqueued": sum(item.get("enqueued", 0) for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
        "items": items,
    }
