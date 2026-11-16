"""跨层数据契约·业务暂停与运行环境保护。

暂停标记存在时，所有新增付费调用与阶段推进必须失败关闭；
已发出的请求自然结束，不做强杀。路径经 PODCAST_BUSINESS_PAUSE_FILE 覆盖。

队列启动的进程带有 PODCAST_JOB_ID/PODCAST_JOB_WORKER_ID 身份时，
每个付费边界还须复查 job 租约：失租或被接管即写暂停标记并抛错，
保证暂停文件写盘失败时失租依然能阻止新增付费调用。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def business_pause_path() -> Path:
    env = os.environ.get("PODCAST_BUSINESS_PAUSE_FILE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "var" / "business.pause"


def is_business_paused() -> bool:
    return business_pause_path().exists()


def assert_business_running() -> None:
    path = business_pause_path()
    if path.exists():
        raise RuntimeError(f"业务暂停标记存在，停止新增付费调用与派发: {path}")


def assert_job_lease_alive() -> None:
    """队列子进程的跨进程失租感知；无 job 身份时跳过（人工入口由锁与暂停文件约束）。"""
    job_id = os.environ.get("PODCAST_JOB_ID")
    worker_id = os.environ.get("PODCAST_JOB_WORKER_ID")
    if not job_id or not worker_id:
        return
    try:
        result = subprocess.run(
            ["uv", "run", "python", "-m", "state.postgres.job_cli",
             "heartbeat", job_id, "--worker-id", worker_id],
            capture_output=True, text=True, timeout=60, cwd=_REPO_ROOT,
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1] or "{}")
        if not isinstance(payload, dict):
            raise TypeError(f"heartbeat 返回非法 JSON 顶层类型: {type(payload).__name__}")
        data = payload.get("data")
        if data is not None and not isinstance(data, dict):
            raise TypeError(f"heartbeat 返回非法 data 类型: {type(data).__name__}")
        updated = (
            result.returncode == 0
            and payload.get("status") == "success"
            and isinstance(data, dict)
            and data.get("updated") is True
        )
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, TypeError):
        updated = False
    if not updated:
        try:
            business_pause_path().write_text(
                f"job_lease_lost job={job_id} worker={worker_id}\n", encoding="utf-8"
            )
        except OSError:
            pass
        raise RuntimeError(
            f"job 租约失效（失租或被接管），停止新增付费调用: job={job_id}"
        )
