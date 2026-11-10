"""Episode 下载状态推进测试；仅使用本地 HTTP 与临时目录。

模拟场景：
- discovered → downloaded：音频 hash、时长、artifact 和状态完整
- 已完成下载幂等复用，不重复请求音频
- 临时 HTTP 故障指数退避后恢复，持续失败三次转 failed
- 缺 enclosure／非法状态拒绝，状态查询返回产物
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline import episode_flow
from state import db, episode, manifest

SILENT_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413
AUDIO = SILENT_FRAME * 20


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "flow.db")
    db.init_schema(connection)
    yield connection
    connection.close()


class _Handler(BaseHTTPRequestHandler):
    attempts = 0
    fail_first = 0
    fail_forever = False

    def do_GET(self):
        type(self).attempts += 1
        if self.fail_forever or type(self).attempts <= self.fail_first:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(AUDIO)))
        self.end_headers()
        self.wfile.write(AUDIO)

    def log_message(self, *args):
        pass


@pytest.fixture()
def http_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.attempts = 0
    _Handler.fail_first = 0
    _Handler.fail_forever = False
    yield f"http://127.0.0.1:{server.server_address[1]}/audio.mp3"
    server.shutdown()
    server.server_close()
    thread.join()


def _episode(conn, url):
    return episode.create(conn, guid="g", feed="f", enclosure=url)


def test_download_advances_episode_and_repeat_reuses(conn, tmp_path, http_url):
    episode_id = _episode(conn, http_url)
    result = episode_flow.download_episode(
        conn, episode_id, workspace=tmp_path / "ws", trust_env=False, sleep=lambda _: None
    )
    assert result["episode_status"] == "downloaded"
    assert result["attempts"] == 1
    assert result["duration_secs"] > 0
    assert episode.get(conn, episode_id)["audio_sha256"] == result["sha256"]
    assert manifest.list_for(conn, episode_id)[0]["step"] == "download"
    assert _Handler.attempts == 1

    repeated = episode_flow.download_episode(
        conn, episode_id, workspace=tmp_path / "ws", trust_env=False, sleep=lambda _: None
    )
    assert repeated["reused"] is True
    assert _Handler.attempts == 1
    status = episode_flow.episode_status(conn, episode_id)
    assert status["episode"]["status"] == "downloaded"
    assert len(status["artifacts"]) == 1


def test_transient_failures_retry_then_recover(conn, tmp_path, http_url):
    _Handler.fail_first = 2
    episode_id = _episode(conn, http_url)
    result = episode_flow.download_episode(
        conn, episode_id, workspace=tmp_path / "ws", trust_env=False, sleep=lambda _: None
    )
    assert result["attempts"] == 3
    assert result["episode_status"] == "downloaded"


def test_persistent_failure_stops_after_three_and_marks_failed(conn, tmp_path, http_url):
    _Handler.fail_forever = True
    episode_id = _episode(conn, http_url)
    with pytest.raises(episode_flow.EpisodeFlowError, match="重试 3 次"):
        episode_flow.download_episode(
            conn, episode_id, workspace=tmp_path / "ws", trust_env=False, sleep=lambda _: None
        )
    assert _Handler.attempts == 3
    assert episode.get(conn, episode_id)["status"] == "failed"
    assert not (tmp_path / "ws" / "audio.mp3.part").exists()


def test_download_requires_discovered_and_enclosure(conn, tmp_path):
    episode_id = episode.create(conn, guid="g", feed="f")
    with pytest.raises(episode_flow.EpisodeFlowError, match="enclosure"):
        episode_flow.download_episode(conn, episode_id, workspace=tmp_path)
    other = episode.create(conn, guid="other", feed="f", enclosure="https://example.com/a.mp3")
    episode.update_status(conn, other, "transcribed")
    with pytest.raises(episode_flow.EpisodeFlowError, match="下载要求"):
        episode_flow.download_episode(conn, other, workspace=tmp_path)
