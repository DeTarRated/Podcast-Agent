"""执行层·发现后 Episode 的下载固化与状态查询。

音频下载最多三次、指数退避；完成后登记 artifact 并推进 downloaded。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Optional

import httpx

from pipeline import download
from state import db, episode, execution, manifest

REPO_ROOT = Path(__file__).resolve().parents[1]


class EpisodeFlowError(RuntimeError):
    """Episode 状态或输入不满足执行条件。"""


def _register_download(conn, episode_id: int, path: Path, sha256: str) -> int:
    existing = conn.execute(
        """
        SELECT id FROM artifact WHERE episode_id = ? AND step = 'download' AND path = ?
        """,
        (episode_id, str(path)),
    ).fetchone()
    if existing is not None:
        manifest.finish(conn, existing["id"], sha256=sha256)
        return int(existing["id"])
    artifact_id = manifest.add(
        conn,
        episode_id=episode_id,
        step="download",
        kind="audio",
        path=str(path),
        sha256=sha256,
    )
    manifest.finish(conn, artifact_id, sha256=sha256)
    return artifact_id


def download_episode(
    conn,
    episode_id: int,
    *,
    workspace: Optional[Path] = None,
    trust_env: bool = True,
    max_attempts: int = 3,
    sleep=time.sleep,
) -> dict:
    ep = episode.get(conn, episode_id)
    if ep is None:
        raise EpisodeFlowError(f"Episode 不存在: {episode_id}")
    if ep["status"] not in ("discovered", "downloaded"):
        raise EpisodeFlowError(f"下载要求 discovered/downloaded，当前 {ep['status']}")
    if not ep["enclosure"]:
        raise EpisodeFlowError("Episode 缺少 enclosure")
    ws = workspace or REPO_ROOT / "var" / "episodes" / str(episode_id)
    ws.mkdir(parents=True, exist_ok=True)
    audio = ws / "audio.mp3"

    if audio.exists() and ep["audio_sha256"]:
        actual = download.sha256_file(audio)
        if actual == ep["audio_sha256"] and ep["status"] == "downloaded":
            artifact_id = _register_download(conn, episode_id, audio, actual)
            return {
                "status": "success",
                "episode_id": episode_id,
                "episode_status": "downloaded",
                "path": str(audio),
                "sha256": actual,
                "artifact_id": artifact_id,
                "reused": True,
                "attempts": 0,
            }

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = download.download(ep["enclosure"], audio, trust_env=trust_env)
            episode.set_audio(
                conn, episode_id, sha256=result["sha256"], duration_secs=result["duration_secs"]
            )
            episode.update_status(conn, episode_id, "downloaded")
            artifact_id = _register_download(conn, episode_id, audio, result["sha256"])
            return {
                "status": "success",
                "episode_id": episode_id,
                "episode_status": "downloaded",
                "path": str(audio),
                "sha256": result["sha256"],
                "duration_secs": result["duration_secs"],
                "artifact_id": artifact_id,
                "reused": False,
                "attempts": attempt,
                "warning": result["warning"],
            }
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            if attempt < max_attempts:
                sleep(0.2 * (2 ** (attempt - 1)))
    episode.update_status(conn, episode_id, "failed")
    raise EpisodeFlowError(f"音频下载失败，已重试 {max_attempts} 次: {last_error}")


def episode_status(conn, episode_id: int) -> dict:
    ep = episode.get(conn, episode_id)
    if ep is None:
        raise EpisodeFlowError(f"Episode 不存在: {episode_id}")
    artifacts = manifest.list_for(conn, episode_id)
    return {
        "status": "success",
        "episode": dict(ep),
        "artifacts": [
            {
                "id": row["id"], "step": row["step"], "path": row["path"],
                "version": row["version"], "sha256": row["sha256"], "status": row["status"],
            }
            for row in artifacts
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Episode 下载、状态与执行租约")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("download", "status", "acquire", "release", "heartbeat"):
        item = sub.add_parser(command)
        item.add_argument("episode_id", type=int)
        item.add_argument("--workspace")
        item.add_argument("--db")
        if command in ("acquire", "release", "heartbeat"):
            item.add_argument("--owner", required=True)
            item.add_argument("--now")
    pending = sub.add_parser("pending")
    pending.add_argument("--limit", type=int, default=10)
    pending.add_argument("--db")
    args = parser.parse_args(argv)
    conn = db.connect(args.db) if args.db else db.open_default()
    if args.db:
        db.init_schema(conn)
    try:
        workspace = Path(args.workspace) if getattr(args, "workspace", None) else None
        if args.command == "download":
            result = download_episode(conn, args.episode_id, workspace=workspace)
        elif args.command == "status":
            result = episode_status(conn, args.episode_id)
        elif args.command == "pending":
            result = {
                "status": "success",
                "items": [dict(row) for row in execution.pending_episodes(conn, limit=args.limit)],
            }
        elif args.command == "acquire":
            result = {"status": "success", **execution.acquire(
                conn, episode_id=args.episode_id, owner=args.owner, now=args.now
            )}
        elif args.command == "heartbeat":
            result = {"status": "success", "updated": execution.heartbeat(
                conn, episode_id=args.episode_id, owner=args.owner, now=args.now
            )}
        else:
            result = {"status": "success", "released": execution.release(
                conn, episode_id=args.episode_id, owner=args.owner
            )}
    except (EpisodeFlowError, OSError, ValueError) as exc:
        result = {"status": "failed", "error": str(exc)}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
