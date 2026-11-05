"""最小下载工具：跟随重定向下载 mp3，sha256 固化，读取时长。

固化语义：dest 已存在时重新下载到临时文件对比 hash——
一致则丢弃临时返回现有；不一致则告警并保留首版（动态广告插入场景）。
后续步骤只读首次固化版本。
"""
import hashlib
import logging
from pathlib import Path
from typing import Optional

import httpx
from mutagen.mp3 import MP3

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _describe(path: Path, sha256: str, warning: Optional[str]) -> dict:
    size = path.stat().st_size
    duration: Optional[float] = None
    try:
        duration = float(MP3(path).info.length)
    except Exception as exc:  # 无法解析时长时不阻塞固化，交由调用方处理
        log.warning("读取时长失败: %s (%s)", path, exc)
    return {
        "path": str(path),
        "sha256": sha256,
        "size": size,
        "duration_secs": duration,
        "warning": warning,
    }


def download(url: str, dest: Path, *, timeout: float = 120.0, trust_env: bool = True) -> dict:
    """下载并固化到 dest；返回描述 dict（path/sha256/size/duration_secs/warning）。

    warning 可能为 "hash_mismatch"（同路径已有不同内容，保留首版）。
    trust_env=False 用于测试，不读取环境代理。
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.Client(follow_redirects=True, timeout=timeout, trust_env=trust_env) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)

    new_hash = sha256_file(tmp)
    if dest.exists():
        old_hash = sha256_file(dest)
        tmp.unlink()
        if old_hash == new_hash:
            return _describe(dest, old_hash, warning=None)
        log.warning("重复下载 hash 不一致（动态广告插入？），保留首版: %s", dest)
        return _describe(dest, old_hash, warning="hash_mismatch")

    tmp.rename(dest)
    return _describe(dest, new_hash, warning=None)
