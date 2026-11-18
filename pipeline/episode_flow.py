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

from pipeline import download, download_refresh
from pipeline import cost_guard
from state import db, episode, estimate, execution, manifest

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


def _error_summary(exc) -> str:
    """错误摘要：保留状态码，截掉可能带签名的完整 URL。"""
    text = str(exc)
    cut = text.find(" for url ")
    if cut >= 0:
        text = text[:cut]
    return text[:300]


def _redact_url(url: str | None) -> str:
    if not url:
        return "<none>"
    base = url.split("?", 1)[0]
    return base[:120]


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
    refreshed = False
    refreshed_url = None
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
            # InvalidAudioPayload 继承 OSError：200+HTML 错误页走同一重试与失败路径。
            last_error = exc
            # 签名直链过期（403/404/410）：从 feed 重新取链接后立即重试一次，
            # 而不是拿旧链接把 3 次重试耗光。
            if not refreshed and download_refresh.is_stale_link(exc):
                try:
                    fresh = download_refresh.refresh_episode_enclosure(
                        conn, episode_id,
                        guid=ep["guid"], feed_url=ep["feed"],
                        current=ep["enclosure"], trust_env=trust_env,
                    )
                except (download_refresh.EnclosureError, OSError) as refresh_exc:
                    # 刷新失败原因必须保留：否则最终报错只剩下载 404，无法判断是
                    # 链接过期还是 feed 侧下架/网络故障。
                    refresh_failure = _error_summary(refresh_exc)
                    last_error = f"{_error_summary(exc)}; 刷新链接失败: {refresh_failure}"
                else:
                    # 只有刷新真正成功才置位：否则一次 feed 抖动会让本轮彻底放弃刷新。
                    refreshed = True
                    refreshed_url = fresh
                    ep = dict(ep)
                    ep["enclosure"] = fresh
                    continue
            if attempt < max_attempts:
                sleep(0.2 * (2 ** (attempt - 1)))
    episode.update_status(conn, episode_id, "failed")
    detail = _error_summary(last_error)
    if refreshed:
        detail = f"{detail}; 已刷新链接={_redact_url(refreshed_url)}"
    raise EpisodeFlowError(f"音频下载失败，已重试 {max_attempts} 次: {detail}")


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
            items = []
            for row in execution.pending_episodes(conn, limit=args.limit):
                item = dict(row)
                # 预留用估算随候选一起给出，入队方不得自行编造金额。
                try:
                    amount, basis = estimate.episode_estimate(item.get("duration_secs"))
                except estimate.EstimateError as exc:
                    item["estimate_error"] = str(exc)
                else:
                    # 恢复执行时按剩余额度预留：整期上界与已结算费用不重复计入。
                    remaining = cost_guard.remaining_allowance(item["id"])
                    if remaining is not None and remaining <= 0:
                        item["estimate_error"] = "本期额度已用尽，停止入队"
                    else:
                        if remaining is not None and amount > remaining:
                            amount = remaining
                            basis = f"{basis};clamped_to_remaining_usd={amount}"
                        item["estimated_cost"] = str(amount)
                        item["estimate_basis"] = basis
                items.append(item)
            result = {
                "status": "success",
                "items": items,
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
