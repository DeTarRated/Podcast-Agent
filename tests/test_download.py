"""最小下载工具测试：本地 HTTP 源 + 合成静音 mp3。

模拟场景：
- 302 重定向跟随下载，产物描述字段完整（sha256 / size / duration）
- 同内容重复下载：复用现有文件，无告警
- 动态广告插入（hash 漂移）：告警并保留首版，临时文件清理
- HTTP 4xx：明确抛 HTTPStatusError
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from pipeline import download

# 最小合法 MPEG-1 Layer III 静音帧（128kbps / 44.1kHz / stereo，417 字节）
_SILENT_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413


def silent_mp3(frames: int = 20) -> bytes:
    return _SILENT_FRAME * frames


@pytest.fixture()
def http_source():
    """本地 HTTP 源：/feed.mp3 302 → /real/a.mp3，模拟 Captivate 直链跳转。"""
    payload_a = silent_mp3(20)
    payload_b = silent_mp3(40)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/feed.mp3":
                self.send_response(302)
                self.send_header("Location", "/real/a.mp3")
                self.end_headers()
            elif self.path == "/real/a.mp3":
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(payload_a)))
                self.end_headers()
                self.wfile.write(payload_a)
            elif self.path == "/real/b.mp3":
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(payload_b)))
                self.end_headers()
                self.wfile.write(payload_b)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # 安静测试
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_download_follows_redirect_and_describes(http_source, tmp_path):
    dest = tmp_path / "ep1.mp3"
    result = download.download(f"{http_source}/feed.mp3", dest, trust_env=False)

    assert result["path"] == str(dest)
    assert result["sha256"] == download.sha256_file(dest)
    assert result["size"] == len(silent_mp3(20))
    assert result["duration_secs"] and result["duration_secs"] > 0
    assert result["warning"] is None
    assert dest.exists()


def test_same_hash_keeps_existing(http_source, tmp_path):
    dest = tmp_path / "ep1.mp3"
    first = download.download(f"{http_source}/feed.mp3", dest, trust_env=False)
    second = download.download(f"{http_source}/feed.mp3", dest, trust_env=False)

    assert second["sha256"] == first["sha256"]
    assert second["warning"] is None


def test_hash_mismatch_warns_and_keeps_first_version(http_source, tmp_path):
    dest = tmp_path / "ep1.mp3"
    first = download.download(f"{http_source}/feed.mp3", dest, trust_env=False)

    # 同一 guid 再次下载拿到不同内容（动态广告插入场景）
    result = download.download(f"{http_source}/real/b.mp3", dest, trust_env=False)

    assert result["warning"] == "hash_mismatch"
    assert result["sha256"] == first["sha256"]  # 保留首版
    assert download.sha256_file(dest) == first["sha256"]  # 文件未被覆盖
    assert not dest.with_suffix(".mp3.part").exists()  # 临时文件已清理


def test_http_error_raises(http_source, tmp_path):
    with pytest.raises(httpx.HTTPStatusError):
        download.download(f"{http_source}/missing.mp3", tmp_path / "ep2.mp3", trust_env=False)
