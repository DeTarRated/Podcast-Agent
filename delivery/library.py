"""交付层·私人播客库只读查询与动态签名资源。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from delivery.errors import DeliveryError
from delivery.r2 import R2Settings, R2Store
from pipeline import override as srt_override
from state import review_state

REPO_ROOT = Path(__file__).resolve().parents[1]


def _path(row: sqlite3.Row) -> Path:
    path = Path(row["path"])
    return path if path.is_absolute() else REPO_ROOT / path


def list_episodes(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT d.id AS delivery_id, d.highlights_json, d.subtitle_key,
               e.id, e.guid, e.feed, e.title, e.published_at, e.duration_secs,
               e.status AS episode_status, a.review_revision_id, a.sha256
        FROM delivery d
        JOIN episode e ON e.id = d.episode_id
        JOIN approval a ON a.id = d.approval_id
        WHERE d.is_current = 1 AND d.status = 'delivered'
        ORDER BY e.published_at DESC, e.id DESC
        """
    ).fetchall()
    items = []
    for row in rows:
        highlights = json.loads(row["highlights_json"]) if row["highlights_json"] else {"summary": "", "items": []}
        items.append(
            {
                "id": row["id"],
                "guid": row["guid"],
                "feed": row["feed"],
                "title": row["title"],
                "published_at": row["published_at"],
                "duration_secs": row["duration_secs"],
                "episode_status": row["episode_status"],
                "review_revision_id": row["review_revision_id"],
                "sha256": row["sha256"],
                "summary": highlights.get("summary", ""),
                "url": f"#/library/episodes/{row['id']}",
            }
        )
    return {"items": items, "count": len(items)}


def _current(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            d.*, e.guid, e.feed, e.title, e.published_at, e.duration_secs, e.status AS episode_status,
            a.review_revision_id, a.sha256 AS approval_sha256,
            art.path, art.sha256 AS artifact_sha256
        FROM delivery d
        JOIN episode e ON e.id = d.episode_id
        JOIN approval a ON a.id = d.approval_id
        JOIN artifact art ON art.id = d.artifact_id
        WHERE d.episode_id = ? AND d.is_current = 1 AND d.status = 'delivered'
        """,
        (episode_id,),
    ).fetchone()
    if row is None:
        raise DeliveryError("not_found", "当前发布节目不存在")
    return row


def episode_detail(conn: sqlite3.Connection, *, episode_id: int, store=None) -> dict:
    row = _current(conn, episode_id)
    subtitle_path = _path(row)
    if not subtitle_path.is_file():
        raise DeliveryError("artifact_mismatch", "当前发布字幕文件不存在")
    entries = srt_override.parse_srt(subtitle_path.read_text(encoding="utf-8-sig"))
    source_row = conn.execute(
        """
        SELECT path, sha256 FROM artifact
        WHERE episode_id = ? AND step = 'transcript_fix' AND status = 'done'
        ORDER BY version DESC, id DESC LIMIT 1
        """,
        (episode_id,),
    ).fetchone()
    source_entries = []
    if source_row is not None:
        source_entries = srt_override.parse_srt(_path(source_row).read_text(encoding="utf-8-sig"))
    cues = []
    for number, entry in enumerate(entries, 1):
        source_text = "\n".join(
            source.text for source in source_entries if source.start < entry.end and source.end > entry.start
        )
        cues.append(
            {
                "entry_number": number,
                "start_ms": entry.start,
                "end_ms": entry.end,
                "text": entry.text,
                "source_text": source_text,
            }
        )
    if store is None:
        store = R2Store(R2Settings.from_env())
    if not row["audio_key"] or not row["subtitle_key"]:
        raise DeliveryError("artifact_mismatch", "发布记录缺少音频或字幕对象")
    current_revision = review_state.current_for(conn, episode_id)
    return {
        "episode": {
            "id": row["episode_id"],
            "guid": row["guid"],
            "feed": row["feed"],
            "title": row["title"],
            "published_at": row["published_at"],
            "duration_secs": row["duration_secs"],
            "status": row["episode_status"],
        },
        "delivery_id": row["id"],
        "review_revision_id": row["review_revision_id"],
        "current_revision_id": current_revision["id"] if current_revision is not None else None,
        "current_revision_status": current_revision["status"] if current_revision is not None else None,
        "sha256": row["approval_sha256"],
        "cues": cues,
        "highlights": json.loads(row["highlights_json"]),
        "audio_url": store.sign_get(row["audio_key"]),
        "subtitle_url": store.sign_get(row["subtitle_key"], download_name=f"episode-{episode_id}-zh.srt"),
        "highlights_url": store.sign_get(row["highlights_key"]) if row["highlights_key"] else None,
        "expires_seconds": getattr(store.settings, "expires_seconds", 900),
    }
