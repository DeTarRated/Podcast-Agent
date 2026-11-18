"""最小下载工具测试：本地 HTTP 源 + 合成静音 mp3。

模拟场景：
- 302 重定向跟随下载，产物描述字段完整（sha256 / size / duration）
- 同内容重复下载：复用现有文件，无告警
- 动态广告插入（hash 漂移）：告警并保留首版，临时文件清理
- HTTP 4xx：明确抛 HTTPStatusError
- 签名直链过期（403/404/410）：从 feed 刷新 enclosure 后重试成功，新链接落库
- 500 等非过期错误不刷新链接，避免掩盖真实故障
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from pipeline import download
from state import episode

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


def test_cli_end_to_end(http_source, tmp_path):
    """CLI 入口：供执行层工具经 bash 调用，输出一行 JSON。"""
    import json
    import subprocess

    dest = tmp_path / "cli.mp3"
    proc = subprocess.run(
        ["uv", "run", "python", "pipeline/download.py", f"{http_source}/feed.mp3", str(dest)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["sha256"] == download.sha256_file(dest)
    assert info["size"] == len(silent_mp3(20))
    assert info["warning"] is None
    assert dest.exists()


# ---- 签名直链过期：从 feed 刷新 enclosure ----


@pytest.fixture()
def conn(tmp_path):
    from state import db

    connection = db.connect(tmp_path / "episodes.db")
    db.init_schema(connection)
    yield connection
    connection.close()

RSS_ONE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><guid>ep-stale</guid><title>Stale</title>
<enclosure url="https://cdn.example.com/fresh.mp3" type="audio/mpeg"/></item>
</channel></rss>"""


def test_fetch_fresh_enclosure_from_feed():
    from pipeline import download_refresh

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=RSS_ONE.encode()))
    url = download_refresh.fetch_fresh_enclosure(
        "https://feeds.example.com/show.xml", "ep-stale", transport=transport, trust_env=False
    )
    assert url == "https://cdn.example.com/fresh.mp3"


def test_fetch_fresh_enclosure_missing_guid_fails():
    from pipeline import download_refresh

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=RSS_ONE.encode()))
    with pytest.raises(download_refresh.EnclosureError, match="找不到该 guid"):
        download_refresh.fetch_fresh_enclosure(
            "https://feeds.example.com/show.xml", "other", transport=transport, trust_env=False
        )


def test_fetch_fresh_enclosure_http_error_fails():
    from pipeline import download_refresh

    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    with pytest.raises(download_refresh.EnclosureError, match="HTTP 500"):
        download_refresh.fetch_fresh_enclosure(
            "https://feeds.example.com/show.xml", "ep-stale", transport=transport, trust_env=False
        )


def test_is_stale_link_statuses():
    from pipeline import download_refresh

    def error_with(status):
        request = httpx.Request("GET", "https://cdn.example.com/a.mp3")
        return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status))

    assert download_refresh.is_stale_link(error_with(404)) is True
    assert download_refresh.is_stale_link(error_with(403)) is True
    assert download_refresh.is_stale_link(error_with(410)) is True
    assert download_refresh.is_stale_link(error_with(500)) is False
    assert download_refresh.is_stale_link(RuntimeError("x")) is False


def test_download_refreshes_expired_link_and_succeeds(conn, tmp_path, monkeypatch):
    """404 → 刷新 enclosure → 用新链接下载成功，且新链接落库。"""
    from pipeline import download, download_refresh, episode_flow
    from state import episode

    # 真实首下路径：discovered + 入队时带 enclosure。
    episode_id = episode.create(
        conn, guid="ep-stale", feed="https://feeds.example.com/show.xml",
        enclosure="https://cdn.example.com/expired.mp3",
    )

    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        if url.endswith("expired.mp3"):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                "404", request=request, response=httpx.Response(404, request=request)
            )
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"audio-bytes")
        return {"path": str(dest), "sha256": "deadbeef", "size": 11,
                "duration_secs": 12.5, "warning": None}

    monkeypatch.setattr(download, "download", fake_download)
    monkeypatch.setattr(
        download_refresh, "fetch_fresh_enclosure",
        lambda *a, **k: "https://cdn.example.com/fresh.mp3",
    )
    result = episode_flow.download_episode(conn, episode_id, workspace=tmp_path, sleep=lambda _s: None)
    assert result["status"] == "success"
    assert calls == ["https://cdn.example.com/expired.mp3", "https://cdn.example.com/fresh.mp3"]
    row = episode.get(conn, episode_id)
    assert row["enclosure"] == "https://cdn.example.com/fresh.mp3"


def test_download_does_not_refresh_on_server_error(conn, tmp_path, monkeypatch):
    """500 不是链接过期：刷新链接会掩盖真实故障，应保持原样失败。"""
    from pipeline import download, download_refresh, episode_flow
    from state import episode

    episode_id = episode.create(
        conn, guid="ep-500", feed="https://feeds.example.com/show.xml",
        enclosure="https://cdn.example.com/boom.mp3",
    )
    refreshed = []
    monkeypatch.setattr(
        download_refresh, "fetch_fresh_enclosure",
        lambda *a, **k: refreshed.append(1) or "https://cdn.example.com/fresh.mp3",
    )

    def fake_download(url, dest, **kwargs):
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))

    monkeypatch.setattr(download, "download", fake_download)
    with pytest.raises(episode_flow.EpisodeFlowError, match="音频下载失败"):
        episode_flow.download_episode(conn, episode_id, workspace=tmp_path, sleep=lambda _s: None)
    assert refreshed == []


def test_refresh_failure_reason_is_preserved_in_final_error(conn, tmp_path, monkeypatch):
    """刷新链接失败时，最终错误必须保留刷新原因，而不是只剩下载 404。"""
    from pipeline import download, download_refresh, episode_flow
    from state import episode

    episode_id = episode.create(conn, guid="ep-refresh-fail", feed="https://feeds.example.com/show.xml")
    conn.execute("UPDATE episode SET enclosure='https://cdn.example.com/expired.mp3' WHERE id=?", (episode_id,))
    conn.commit()
    monkeypatch.setattr(
        download_refresh, "fetch_fresh_enclosure",
        lambda *a, **k: (_ for _ in ()).throw(download_refresh.EnclosureError("feed 中已找不到该 guid")),
    )

    def fake_download(url, dest, **kwargs):
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr(download, "download", fake_download)
    with pytest.raises(episode_flow.EpisodeFlowError) as error:
        episode_flow.download_episode(conn, episode_id, workspace=tmp_path, sleep=lambda _s: None)
    message = str(error.value)
    assert "刷新链接失败" in message and "找不到该 guid" in message


def test_refresh_attempts_again_after_transient_feed_failure(conn, tmp_path, monkeypatch):
    """feed 抖动一次不应让本轮彻底放弃刷新：后续重试仍会再刷新一次。"""
    from pipeline import download, download_refresh, episode_flow
    from state import episode

    episode_id = episode.create(conn, guid="ep-flaky-feed", feed="https://feeds.example.com/show.xml")
    attempts = {"refresh": 0, "download": 0}

    def flaky_refresh(*args, **kwargs):
        attempts["refresh"] += 1
        if attempts["refresh"] == 1:
            raise download_refresh.EnclosureError("刷新 feed 失败: timeout")
        return "https://cdn.example.com/fresh.mp3"

    monkeypatch.setattr(download_refresh, "fetch_fresh_enclosure", flaky_refresh)

    def fake_download(url, dest, **kwargs):
        attempts["download"] += 1
        if url.endswith("expired.mp3"):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"audio")
        return {"path": str(dest), "sha256": "aa", "size": 5, "duration_secs": 1.0, "warning": None}

    monkeypatch.setattr(download, "download", fake_download)
    conn.execute("UPDATE episode SET enclosure='https://cdn.example.com/expired.mp3' WHERE id=?", (episode_id,))
    conn.commit()
    result = episode_flow.download_episode(conn, episode_id, workspace=tmp_path, sleep=lambda _s: None)
    assert result["status"] == "success"
    assert attempts["refresh"] >= 2  # 第一次失败后仍会在后续重试再刷新


def test_refresh_updates_candidate_enclosure(conn, tmp_path, monkeypatch):
    """刷新成功后候选表也同步新链接，便于人工核对来源。"""
    from pipeline import download, download_refresh, episode_flow
    from state import episode

    from state import feed as feed_state

    feed_id = feed_state.add(
        conn, name="F", feed_url="https://feeds.example.com/show.xml", show_name="S",
        now="2026-01-01T00:00:00+00:00", process_existing=True,
    )
    episode_id = episode.create(conn, guid="ep-cand", feed="https://feeds.example.com/show.xml")
    conn.execute(
        "INSERT INTO discovery_candidate (feed_id, guid, title, enclosure, updated_at, episode_id, status) "
        "VALUES (?, 'ep-cand', 't', 'https://cdn.example.com/old.mp3', '2026-01-01T00:00:00+00:00', ?, 'enqueued')",
        (feed_id, episode_id),
    )
    conn.execute("UPDATE episode SET enclosure='https://cdn.example.com/old.mp3' WHERE id=?", (episode_id,))
    conn.commit()
    monkeypatch.setattr(
        download_refresh, "fetch_fresh_enclosure", lambda *a, **k: "https://cdn.example.com/new.mp3"
    )

    def fake_download(url, dest, **kwargs):
        if url.endswith("old.mp3"):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"audio")
        return {"path": str(dest), "sha256": "bb", "size": 5, "duration_secs": 1.0, "warning": None}

    monkeypatch.setattr(download, "download", fake_download)
    episode_flow.download_episode(conn, episode_id, workspace=tmp_path, sleep=lambda _s: None)
    row = conn.execute("SELECT enclosure FROM discovery_candidate WHERE episode_id = ?", (episode_id,)).fetchone()
    assert row["enclosure"] == "https://cdn.example.com/new.mp3"


def test_is_stale_link_uses_real_raise_for_status():
    """真实 raise_for_status 抛出的异常必须被识别为过期链接。"""
    from pipeline import download_refresh

    request = httpx.Request("GET", "https://cdn.example.com/a.mp3")
    response = httpx.Response(404, request=request)
    with pytest.raises(httpx.HTTPStatusError) as error:
        response.raise_for_status()
    assert download_refresh.is_stale_link(error.value) is True


# ---- 200 + HTML 错误体 / Atom feed ----


def test_html_error_page_is_treated_as_invalid_audio(tmp_path):
    """托管方返回 200 + HTML 错误页时按下载失败处理，不落成品文件。"""
    from pipeline import download

    body = b"<!DOCTYPE html><html><body>Not found</body></html>"

    def handler(request):
        return httpx.Response(
            200, content=body, headers={"content-type": "audio/mpeg"}
        )

    dest = tmp_path / "a.mp3"
    with httpx.Client(transport=httpx.MockTransport(handler)) as _client:
        pass  # 保持与生产一致：内部自建 Client
    import pipeline.download as module
    original = httpx.Client
    try:
        module.httpx.Client = lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
        with pytest.raises(download.InvalidAudioPayload) as error:
            download.download("https://cdn.example.com/a.mp3", dest, trust_env=False)
    finally:
        module.httpx.Client = original
    message = str(error.value)
    assert "不是音频" in message and "content_type=audio/mpeg" in message
    assert not dest.exists()
    assert not dest.with_suffix(".mp3.part").exists()


def test_bom_and_whitespace_before_markup_is_detected():
    from pipeline.download import _looks_like_markup

    assert _looks_like_markup(b"\xef\xbb\xbf  \n<!doctype html>") is True
    assert _looks_like_markup(b"<?xml version='1.0'?><Error/>") is True
    # 真实 mp3 帧头不应被误判
    assert _looks_like_markup(bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 100) is False


def test_atom_feed_enclosure_is_resolved():
    """Atom 方言同样走真实解析：link rel=enclosure。"""
    from pipeline import download_refresh

    atom = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>t</title>
  <entry>
    <id>atom-ep-1</id>
    <title>Atom episode</title>
    <link rel="enclosure" type="audio/mpeg" href="https://cdn.example.com/atom.mp3"/>
  </entry>
</feed>"""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=atom))
    url = download_refresh.fetch_fresh_enclosure(
        "https://feeds.example.com/atom.xml", "atom-ep-1", transport=transport, trust_env=False
    )
    assert url == "https://cdn.example.com/atom.mp3"
