"""跨层数据契约·业务白名单。

文件缺失或未配置 = 全局放行（生产默认）；文件存在时，列出的维度才允许通行。
用于 poll、enqueue、claim、auto-approve、publish 的统一限定，验收时强制启用。

JSON 形态：{"feeds": [1], "episodes": [7], "guids": ["feed_url::guid"], "jobs": [3]}
某个键缺省表示该维度不限制；键存在时必须为非空列表，仅允许列表内条目。
配置非法（未知键、null、非列表、元素类型错误）一律抛错，不得退化放行。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_ALLOWED_KEYS = {"feeds", "episodes", "guids", "jobs"}
_ID_KEYS = ("feeds", "episodes", "jobs")


def default_path() -> Path:
    env = os.environ.get("PODCAST_ALLOWLIST_FILE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "var" / "allowlist.json"


def load(path: Optional[str | Path] = None) -> Optional[dict]:
    """读取白名单；文件不存在返回 None（放行）。格式非法抛 ValueError。"""
    target = Path(path) if path is not None else default_path()
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("白名单必须是 JSON object")
    for key, value in data.items():
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"白名单含未知键: {key}")
        if not isinstance(value, list):
            raise ValueError(f"白名单键 {key} 必须是列表")
        if not value:
            raise ValueError(f"白名单键 {key} 不能为空列表；缺省该键表示不限制")
    for key in _ID_KEYS:
        for item in data.get(key) or []:
            if not isinstance(item, int) or isinstance(item, bool):
                raise ValueError(f"白名单键 {key} 只接受整数列表")
    for item in data.get("guids") or []:
        if not isinstance(item, str):
            raise ValueError("白名单键 guids 只接受字符串列表")
    return data


def _ids(allowlist: Optional[dict], key: str) -> Optional[list[int]]:
    if not allowlist or key not in allowlist or allowlist[key] is None:
        return None
    return [int(item) for item in allowlist[key]]


def feed_ids(allowlist: Optional[dict]) -> Optional[list[int]]:
    return _ids(allowlist, "feeds")


def episode_ids(allowlist: Optional[dict]) -> Optional[list[int]]:
    return _ids(allowlist, "episodes")


def job_ids(allowlist: Optional[dict]) -> Optional[list[int]]:
    return _ids(allowlist, "jobs")


def _guids(allowlist: Optional[dict]) -> Optional[set[str]]:
    if not allowlist or "guids" not in allowlist or allowlist["guids"] is None:
        return None
    return {str(item) for item in allowlist["guids"]}


def feed_allowed(allowlist: Optional[dict], feed_id: int) -> bool:
    ids = feed_ids(allowlist)
    return True if ids is None else feed_id in ids


def episode_allowed(allowlist: Optional[dict], episode_id: int) -> bool:
    ids = episode_ids(allowlist)
    return True if ids is None else episode_id in ids


def guid_allowed(allowlist: Optional[dict], feed_url: str, guid: str) -> bool:
    allowed = _guids(allowlist)
    if allowed is None:
        return True
    return guid in allowed or f"{feed_url}::{guid}" in allowed


def job_allowed(allowlist: Optional[dict], job_id: int) -> bool:
    ids = job_ids(allowlist)
    return True if ids is None else job_id in ids
