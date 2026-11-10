"""交付层·approved → R2 → delivered 发布编排。

数据库短事务与外部上传分离；同 approval 幂等重试，旧发布版本在新版本完整前保留。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

from delivery.errors import DeliveryError
from delivery.highlights import HighlightsError, load_validated, render_highlights_html
from delivery.r2 import R2Settings, R2Store
from pipeline import override as srt_override
from state import ledger, manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
HIGHLIGHTS_VERSION = "v1"


def _artifact(conn, episode_id: int, artifact_id: int) -> tuple[sqlite3.Row, Path]:
    row = conn.execute(
        "SELECT * FROM artifact WHERE id = ? AND episode_id = ? AND status = 'done'",
        (artifact_id, episode_id),
    ).fetchone()
    if row is None:
        raise DeliveryError("artifact_mismatch", "交付产物不存在或未完成")
    path = Path(row["path"])
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
        raise DeliveryError("artifact_mismatch", "交付产物缺失或 hash 不一致")
    return row, path


def _workspace(path: Path) -> Path:
    return path.parent.parent if path.parent.name == "rendered" else path.parent


def highlights_paths(artifact: sqlite3.Row) -> tuple[Path, Path]:
    workspace = _workspace(Path(artifact["path"]) if Path(artifact["path"]).is_absolute() else REPO_ROOT / artifact["path"])
    directory = workspace / "highlights"
    stem = f"{HIGHLIGHTS_VERSION}-{artifact['sha256']}"
    return directory / f"{stem}.json", directory / f"{stem}.html"


def _audio(conn, episode: sqlite3.Row) -> tuple[Path, str]:
    episode_id = episode["id"]
    row = conn.execute(
        """
        SELECT id FROM artifact WHERE episode_id = ? AND step = 'download' AND status = 'done'
        ORDER BY version DESC, id DESC LIMIT 1
        """,
        (episode_id,),
    ).fetchone()
    if row is not None:
        artifact, path = _artifact(conn, episode_id, int(row[0]))
        digest = artifact["sha256"]
    else:
        path = REPO_ROOT / "var" / "episodes" / str(episode_id) / "audio.mp3"
        if not path.is_file():
            raise DeliveryError("artifact_mismatch", "首次固化音频不存在")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not episode["audio_sha256"] or digest != episode["audio_sha256"]:
        raise DeliveryError("artifact_mismatch", "音频与首次固化 hash 不一致")
    return path, digest


def _slug(guid: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", guid).strip("-")[:80]
    return value or hashlib.sha256(guid.encode("utf-8")).hexdigest()[:16]


def _register_artifact(conn, episode_id: int, step: str, kind: str, path: Path, payload: bytes) -> int:
    digest = hashlib.sha256(payload).hexdigest()
    existing = conn.execute(
        "SELECT * FROM artifact WHERE episode_id = ? AND step = ? AND path = ?",
        (episode_id, step, str(path)),
    ).fetchone()
    if existing is not None:
        if existing["status"] != "done" or existing["sha256"] != digest:
            raise DeliveryError("artifact_mismatch", f"{step} 登记冲突")
        return int(existing["id"])
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM artifact WHERE episode_id = ? AND step = ?",
        (episode_id, step),
    ).fetchone()[0]
    artifact_id = manifest.add(
        conn, episode_id=episode_id, step=step, kind=kind, path=str(path),
        version=version, sha256=digest, commit=False,
    )
    manifest.finish(conn, artifact_id, sha256=digest, commit=False)
    return artifact_id


def _delivery_for(conn, approval_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM delivery WHERE approval_id = ?", (approval_id,)).fetchone()


def _ensure_delivery(conn, approval: sqlite3.Row) -> sqlite3.Row:
    existing = _delivery_for(conn, approval["id"])
    if existing is not None:
        return existing
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO delivery (approval_id, episode_id, artifact_id, status, sha256, updated_at)
            VALUES (?, ?, ?, 'pending', ?, datetime('now'))
            """,
            (approval["id"], approval["episode_id"], approval["artifact_id"], approval["sha256"]),
        )
    return _delivery_for(conn, approval["id"])


def _supersede_stale(conn, now: str) -> None:
    with conn:
        conn.execute(
            """
            UPDATE delivery SET status = 'superseded', is_current = 0, updated_at = ?
            WHERE status IN ('pending','waiting_highlights','uploading','failed')
              AND approval_id IN (
                  SELECT a.id FROM approval a
                  JOIN review_revision r ON r.id = a.review_revision_id
                  WHERE r.is_current = 0 OR r.status = 'superseded'
              )
            """,
            (now,),
        )


def _approved(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*, e.guid, e.title, e.feed, e.published_at, e.audio_sha256
        FROM approval a
        JOIN review_revision r ON r.id = a.review_revision_id
        JOIN episode e ON e.id = a.episode_id
        WHERE r.is_current = 1 AND r.status = 'approved' AND e.status = 'approved'
        ORDER BY a.id
        """
    ).fetchall()


def _waiting_highlights(conn, delivery: sqlite3.Row, approval: sqlite3.Row, artifact: sqlite3.Row, subtitle_path: Path, json_path: Path, now: str) -> dict:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    call_ref = f"highlights:{approval['episode_id']}:{approval['sha256']}:prompt-{HIGHLIGHTS_VERSION}"
    existing_call = ledger.get_by_ref(conn, call_ref)
    if existing_call is not None and existing_call["status"] in ("failed", "unknown"):
        with conn:
            conn.execute(
                "UPDATE delivery SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (f"highlights 调用 {existing_call['status']}，禁止自动重试", now, delivery["id"]),
            )
        return {"status": "failed", "error": "highlights 结果失败或未知，不自动重试"}
    with conn:
        conn.execute(
            "UPDATE delivery SET status = 'waiting_highlights', updated_at = ? WHERE id = ?",
            (now, delivery["id"]),
        )
    dispatch = {
        "dispatch": {
            "role": "highlights",
            "inputPath": str(subtitle_path),
            "outputPath": str(json_path),
            "callRef": call_ref,
            "ledger": {
                "episodeId": approval["episode_id"],
                "purpose": "highlights",
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
            },
            "referenceFiles": [],
            "targetLang": "zh",
        }
    }
    dispatch_path = _workspace(Path(artifact["path"]) if Path(artifact["path"]).is_absolute() else REPO_ROOT / artifact["path"]) / "delivery" / f"approval-{approval['id']}-highlights.json"
    srt_override.write_create_only(dispatch_path, json.dumps(dispatch, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"status": "waiting_highlights", "dispatchFile": str(dispatch_path)}


def _mark_failed(conn, delivery_id: int, error: str, now: str) -> None:
    with conn:
        conn.execute(
            "UPDATE delivery SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error[:1000], now, delivery_id),
        )


def _publish_one(conn, approval: sqlite3.Row, store, now: str) -> dict:
    delivery = _ensure_delivery(conn, approval)
    if delivery["status"] == "superseded":
        return {"status": "superseded"}
    if delivery["status"] == "failed" and (delivery["error"] or "").startswith("highlights"):
        return {"status": "failed", "error": delivery["error"]}
    episode = conn.execute("SELECT * FROM episode WHERE id = ?", (approval["episode_id"],)).fetchone()
    artifact, subtitle_path = _artifact(conn, approval["episode_id"], approval["artifact_id"])
    json_path, html_path = highlights_paths(artifact)
    if not json_path.exists():
        return _waiting_highlights(conn, delivery, approval, artifact, subtitle_path, json_path, now)
    entries = srt_override.parse_srt(subtitle_path.read_text(encoding="utf-8-sig"))
    highlights = load_validated(json_path, entries)
    html_payload = render_highlights_html(episode, highlights).encode("utf-8")
    srt_override.write_create_only(html_path, html_payload)
    audio_path, audio_sha = _audio(conn, episode)
    if store is None:
        store = R2Store(R2Settings.from_env())

    prefix = f"episodes/{_slug(approval['guid'])}"
    audio_key = f"{prefix}/audio-{audio_sha}.mp3"
    subtitle_key = f"{prefix}/subtitles/zh-{approval['sha256']}.srt"
    highlights_digest = hashlib.sha256(html_payload).hexdigest()
    highlights_key = f"{prefix}/highlights/{HIGHLIGHTS_VERSION}-{approval['sha256']}-{highlights_digest}.html"
    with conn:
        conn.execute(
            "UPDATE delivery SET status = 'uploading', error = NULL, updated_at = ? WHERE id = ?",
            (now, delivery["id"]),
        )
    store.ensure_file(audio_key, audio_path, "audio/mpeg")
    store.ensure_file(subtitle_key, subtitle_path, "application/x-subrip; charset=utf-8")
    store.ensure_bytes(highlights_key, html_payload, "text/html; charset=utf-8")
    canonical_highlights = json.dumps(highlights, ensure_ascii=False, separators=(",", ":"))

    with conn:
        current = conn.execute(
            """
            SELECT a.id FROM approval a
            JOIN review_revision r ON r.id = a.review_revision_id
            JOIN episode e ON e.id = a.episode_id
            WHERE a.id = ? AND r.is_current = 1 AND r.status = 'approved' AND e.status = 'approved'
            """,
            (approval["id"],),
        ).fetchone()
        if current is None:
            conn.execute(
                "UPDATE delivery SET status = 'superseded', is_current = 0, updated_at = ? WHERE id = ?",
                (now, delivery["id"]),
            )
            return {"status": "superseded"}
        _register_artifact(conn, approval["episode_id"], "highlights_json", "json", json_path, json_path.read_bytes())
        _register_artifact(conn, approval["episode_id"], "highlights_html", "html", html_path, html_payload)
        conn.execute(
            "UPDATE delivery SET is_current = 0, updated_at = ? WHERE episode_id = ? AND is_current = 1",
            (now, approval["episode_id"]),
        )
        conn.execute(
            """
            UPDATE delivery SET
                status = 'delivered', is_current = 1,
                audio_key = ?, subtitle_key = ?, highlights_key = ?, highlights_json = ?,
                error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (audio_key, subtitle_key, highlights_key, canonical_highlights, now, delivery["id"]),
        )
        conn.execute(
            "UPDATE review_revision SET status = 'delivered' WHERE id = ?",
            (approval["review_revision_id"],),
        )
        conn.execute(
            "UPDATE episode SET status = 'delivered', updated_at = ? WHERE id = ?",
            (now, approval["episode_id"]),
        )
    return {
        "status": "delivered",
        "delivery_id": delivery["id"],
        "audio_key": audio_key,
        "subtitle_key": subtitle_key,
        "highlights_key": highlights_key,
    }


def mark_highlights_failed(conn: sqlite3.Connection, *, approval_id: int, error: str, now: str) -> dict:
    """交付 worker 明确记录不可自动重试的 highlights 失败。"""
    message = error[:1000]
    if not message.startswith("highlights"):
        message = f"highlights:{message}"
    with conn:
        conn.execute(
            """
            UPDATE delivery SET status = 'failed', error = ?, updated_at = ?
            WHERE approval_id = ? AND status IN ('pending','waiting_highlights','uploading','failed')
            """,
            (message, now, approval_id),
        )
    return {"status": "failed", "approval_id": approval_id, "error": message}


def publish_approved(conn: sqlite3.Connection, *, now: str, store=None) -> dict:
    """逐期发布，单期失败不阻塞其他期；store 为 fake 时零真实 R2。"""
    _supersede_stale(conn, now)
    items = []
    for approval in _approved(conn):
        try:
            result = _publish_one(conn, approval, store, now)
        except (DeliveryError, HighlightsError, OSError, srt_override.OverrideError) as exc:
            delivery = _delivery_for(conn, approval["id"])
            message = str(exc)
            if isinstance(exc, HighlightsError):
                message = f"highlights:{message}"
            if delivery is not None:
                _mark_failed(conn, delivery["id"], message, now)
            result = {"status": "failed", "error": message}
        items.append({"episode_id": approval["episode_id"], "approval_id": approval["id"], **result})
    return {
        "published": sum(item["status"] == "delivered" for item in items),
        "waiting": sum(item["status"] == "waiting_highlights" for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
        "superseded": sum(item["status"] == "superseded" for item in items),
        "items": items,
    }
