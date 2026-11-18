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
SNIFF_BYTES = 1024
# 错误页常被声明成 audio/*，所以按字节嗅探而不是信 Content-Type；
# 真实 mp3 不会以这些开头，误杀风险低。
_HTML_MARKERS = (b"<!doctype", b"<html", b"<?xml")


class InvalidAudioPayload(OSError):
    """下载返回 200 但内容明显不是音频（多为托管方错误页）。继承 OSError 以落入既有重试捕获集合。"""


def _looks_like_markup(head: bytes) -> bool:
    probe = head.lstrip(b"\xef\xbb\xbf").lstrip()
    lowered = probe[:16].lower()
    return any(lowered.startswith(marker) for marker in _HTML_MARKERS)


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
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, trust_env=trust_env) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                head = b""
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes():
                        if len(head) < SNIFF_BYTES:
                            head += chunk[: SNIFF_BYTES - len(head)]
                        f.write(chunk)
                if _looks_like_markup(head):
                    # 200 + HTML/XML 错误体：当作下载失败，交给既有重试与清理逻辑。
                    raise InvalidAudioPayload(
                        "响应内容不是音频: content_type="
                        f"{resp.headers.get('content-type', '<none>')} head={head[:80]!r}"
                    )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

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


def main(argv=None) -> None:
    """CLI 入口：供执行层工具经 bash 调用。输出一行 JSON 结果。"""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="下载并固化音频")
    parser.add_argument("url")
    parser.add_argument("dest")
    args = parser.parse_args(argv)
    result = download(args.url, Path(args.dest))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
