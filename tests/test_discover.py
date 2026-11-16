"""RSS 发现与入队测试；所有 HTTP 为 MockTransport，零音频下载和付费调用。

模拟场景：
- 新 feed 首轮只建立历史基线，显式 process-existing 后 200 才入队，重复 200 去重
- 后续 304 携带 ETag/Last-Modified 条件头且零后续处理；基线轮询不携带条件头，基线期 304 不算成功
- 中断重做保留既有 pending 候选，不无声覆盖；仅新候选标记基线跳过
- 白名单启用时仅轮询允许 feed，未列入 guid 的候选不入队（not_in_allowlist）
- 缺 enclosure 候选失败、缺 guid 使用 link、音频 URL 不被请求
- 单 feed HTTP 失败不阻塞其他 feed
- 禁用 feed 后不再轮询，在途 Episode 保留 feed 快照
- v1 → v2 增量迁移保留 Episode 并创建发现表
- feed CLI 添加、列表与禁用
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from discover import poller
from state import db, discovery, episode, feed as feed_state
from state.migrations import SCHEMA_VERSION

NOW = "2026-11-09T12:00:00+00:00"
FEED_URL = "https://feeds.example.com/show.xml"
RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test Show</title>
<item><guid>episode-1</guid><title>First</title>
<pubDate>Mon, 09 Nov 2026 10:00:00 GMT</pubDate>
<enclosure url="https://cdn.example.com/one.mp3" type="audio/mpeg" length="100"/></item>
<item><guid>episode-2</guid><title>Second</title><description>Summary</description>
<enclosure url="https://cdn.example.com/two.mp3" type="audio/mpeg" length="100"/></item>
</channel></rss>
"""


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "discover.db")
    db.init_schema(connection)
    yield connection
    connection.close()


def _feed(conn, url=FEED_URL, name="PL Podcast", process_existing=True):
    return feed_state.add(
        conn, name=name, feed_url=url, show_name="Test Show", now=NOW,
        process_existing=process_existing,
    )


def _poll(conn, feed_id, xml=RSS, status=200, headers=None):
    requests = []

    def handler(request):
        requests.append(request)
        if str(request.url) != FEED_URL:
            raise AssertionError("不得请求 enclosure 或其他 URL")
        return httpx.Response(status, content=xml.encode(), headers=headers or {})

    transport = httpx.MockTransport(handler)
    result = poller.poll_feed(
        conn, feed_id, now=NOW, transport=transport, trust_env=False
    )
    return result, requests


def test_baseline_poll_never_sends_conditional_headers_and_304_is_not_success(conn):
    feed_id = _feed(conn, process_existing=False)
    # 模拟中断重做：候选与条件头已落库但 bootstrap_pending 未清。
    conn.execute(
        "UPDATE feed SET etag='W-partial', last_modified='Mon, 09 Nov 2026 09:00:00 GMT' WHERE id=?",
        (feed_id,),
    )
    conn.commit()

    seen = []

    def handler(request):
        seen.append(request)
        assert "if-none-match" not in request.headers
        assert "if-modified-since" not in request.headers
        return httpx.Response(200, content=RSS.encode())

    first = poller.poll_feed(
        conn, feed_id, now=NOW, transport=httpx.MockTransport(handler), trust_env=False
    )
    assert first["status"] == "baseline" and first["skipped"] == 2
    assert feed_state.get(conn, feed_id)["bootstrap_pending"] == 0

    def not_modified(request):
        seen.append(request)
        return httpx.Response(304)

    result = poller.poll_feed(
        conn, feed_id, now=NOW, transport=httpx.MockTransport(not_modified), trust_env=False
    )
    assert result["status"] == "not_modified"

    # 若基线未完成时服务器对完整请求回 304，不得把不完整响应当作基线成功。
    conn.execute("UPDATE feed SET bootstrap_pending=1 WHERE id=?", (feed_id,))
    conn.commit()
    blocked = poller.poll_feed(
        conn, feed_id, now=NOW, transport=httpx.MockTransport(not_modified), trust_env=False
    )
    assert blocked["status"] == "not_modified"
    assert feed_state.get(conn, feed_id)["bootstrap_pending"] == 1
    assert len(seen) == 3


def test_baseline_redo_preserves_existing_candidates_and_skips_only_new(conn):
    feed_id = _feed(conn, process_existing=False)
    # 预置中断现场：一个上轮已标记 skipped、一个 pending 未处理。
    stale = discovery.insert_candidate(
        conn, feed_id=feed_id, guid="episode-1", title="First",
        published_at=None, enclosure="https://cdn.example.com/one.mp3",
        summary=None, now=NOW,
    )
    discovery.mark_candidate(conn, stale, status="skipped", reason="initial_feed_baseline", now=NOW)
    interrupted = discovery.insert_candidate(
        conn, feed_id=feed_id, guid="episode-2", title="Second",
        published_at=None, enclosure="https://cdn.example.com/two.mp3",
        summary=None, now=NOW,
    )
    conn.commit()
    before = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM discovery_candidate").fetchall()}

    result, _ = _poll(conn, feed_id)
    assert result["status"] == "baseline"
    # 既有候选保持原记录：skipped 仍 skipped，pending 不被覆盖、不入队。
    assert result["duplicates"] == 2
    assert result["skipped"] == 0
    assert result["enqueued"] == 0
    after = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM discovery_candidate").fetchall()}
    assert after[stale]["status"] == "skipped"
    assert after[interrupted]["status"] == "pending"
    assert after[stale]["updated_at"] == before[stale]["updated_at"]
    assert after[interrupted]["updated_at"] == before[interrupted]["updated_at"]
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 0
    assert feed_state.get(conn, feed_id)["bootstrap_pending"] == 0

    # 再做一次完整轮询：幂等，无新候选、无 Episode、无分类副作用。
    again, _ = _poll(conn, feed_id)
    assert again["new_candidates"] == 0 and again["duplicates"] == 2
    assert conn.execute("SELECT COUNT(*) FROM discovery_candidate").fetchone()[0] == 2


def test_first_poll_baseline_does_not_enqueue_history(conn):
    feed_id = _feed(conn, process_existing=False)
    first, _ = _poll(conn, feed_id)
    assert first["status"] == "baseline"
    assert first["skipped"] == 2
    assert first["enqueued"] == 0
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM discovery_candidate WHERE status='skipped'").fetchone()[0] == 2

    later_rss = RSS.replace("</channel>", """
    <item><guid>episode-3</guid><title>New after baseline</title>
    <enclosure url="https://cdn.example.com/three.mp3" type="audio/mpeg"/></item>
    </channel>""")
    second, _ = _poll(conn, feed_id, xml=later_rss)
    assert second["status"] == "ok"
    assert second["enqueued"] == 1
    assert second["duplicates"] == 2
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 1
    assert episode.get_by_guid_feed(conn, "episode-3", FEED_URL) is not None


def test_first_and_repeated_200_are_deduplicated(conn):
    feed_id = _feed(conn)
    first, requests = _poll(conn, feed_id)
    assert first["new_candidates"] == 2
    assert first["enqueued"] == 2
    assert len(requests) == 1
    rows = conn.execute("SELECT * FROM episode ORDER BY guid").fetchall()
    assert [row["guid"] for row in rows] == ["episode-1", "episode-2"]
    assert all(row["status"] == "discovered" for row in rows)
    assert rows[0]["enclosure"].endswith("one.mp3")
    snapshot = json.loads(rows[0]["feed_snapshot"])
    assert snapshot["name"] == "PL Podcast"
    assert conn.execute("SELECT COUNT(*) FROM discovery_candidate").fetchone()[0] == 2

    second, _ = _poll(conn, feed_id)
    assert second["new_candidates"] == 0
    assert second["duplicates"] == 2
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 2


def test_conditional_304_uses_saved_headers_without_processing(conn):
    feed_id = _feed(conn)
    _poll(conn, feed_id, headers={"ETag": 'W/"v1"', "Last-Modified": "Mon, 09 Nov 2026 10:00:00 GMT"})

    def handler(request):
        assert request.headers["If-None-Match"] == 'W/"v1"'
        assert request.headers["If-Modified-Since"] == "Mon, 09 Nov 2026 10:00:00 GMT"
        return httpx.Response(304)

    result = poller.poll_feed(
        conn, feed_id, now=NOW, transport=httpx.MockTransport(handler), trust_env=False
    )
    assert result["status"] == "not_modified"
    assert result["new_candidates"] == 0
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 2


def test_missing_enclosure_and_guid_link_fallback(conn):
    xml = """<rss version="2.0"><channel><title>x</title>
    <item><guid>missing-audio</guid><title>Bad</title></item>
    <item><link>https://feeds.example.com/episode-link</link><title>Fallback</title>
    <enclosure url="https://cdn.example.com/fallback.mp3" type="audio/mpeg"/></item>
    </channel></rss>"""
    feed_id = _feed(conn)
    result, _ = _poll(conn, feed_id, xml=xml)
    assert result["invalid"] == 1
    assert result["enqueued"] == 1
    failed = discovery.get_candidate(conn, feed_id, "missing-audio")
    assert failed["status"] == "failed"
    linked = discovery.get_candidate(conn, feed_id, "https://feeds.example.com/episode-link")
    assert linked["status"] == "enqueued"
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 1


def test_allowlist_skips_guids_not_listed(conn, tmp_path, monkeypatch):
    allow_file = tmp_path / "allowlist.json"
    allow_file.write_text(json.dumps({"guids": [f"{FEED_URL}::episode-1"]}))
    monkeypatch.setenv("PODCAST_ALLOWLIST_FILE", str(allow_file))
    feed_id = _feed(conn)

    result, _ = _poll(conn, feed_id)
    # episode-1 列入白名单入队；episode-2 未列入被跳过，保留 skipped 记录。
    assert result["enqueued"] == 1
    assert result.get("allowlist_skipped") == 1
    assert episode.get_by_guid_feed(conn, "episode-1", FEED_URL) is not None
    assert episode.get_by_guid_feed(conn, "episode-2", FEED_URL) is None
    skipped = discovery.get_candidate(conn, feed_id, "episode-2")
    assert skipped["status"] == "skipped" and skipped["reason"] == "not_in_allowlist"


def test_allowlist_feed_filter_skips_unlisted_feed(conn, tmp_path, monkeypatch):
    allow_file = tmp_path / "allowlist.json"
    allow_file.write_text(json.dumps({"feeds": [999]}))
    monkeypatch.setenv("PODCAST_ALLOWLIST_FILE", str(allow_file))
    feed_id = _feed(conn)

    def unexpected(request):
        raise AssertionError("未列入白名单的 feed 不应被轮询")

    result = poller.poll_all(
        conn, now=NOW, transport=httpx.MockTransport(unexpected), trust_env=False
    )
    assert result["feeds"] == 0
    assert feed_state.get(conn, feed_id)["last_checked_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM discovery_candidate").fetchone()[0] == 0


def test_feed_failure_does_not_block_other_feed(conn):
    failing = _feed(conn)
    working_url = "https://feeds.example.com/working.xml"
    working = _feed(conn, url=working_url)

    def handler(request):
        if str(request.url) == FEED_URL:
            return httpx.Response(500)
        return httpx.Response(200, content=RSS.encode())

    result = poller.poll_all(
        conn, now=NOW, transport=httpx.MockTransport(handler), trust_env=False
    )
    assert result["failed"] == 1
    assert result["enqueued"] == 2
    assert "HTTP 500" in feed_state.get(conn, failing)["last_error"]
    assert feed_state.get(conn, working)["last_error"] is None


def test_disabled_feed_preserves_inflight_episode_snapshot(conn):
    feed_id = _feed(conn)
    _poll(conn, feed_id)
    before = episode.get_by_guid_feed(conn, "episode-1", FEED_URL)["feed_snapshot"]
    feed_state.disable(conn, feed_id, now=NOW)

    def unexpected(request):
        raise AssertionError("禁用 feed 不应请求")

    result = poller.poll_all(
        conn, now=NOW, transport=httpx.MockTransport(unexpected), trust_env=False
    )
    assert result["feeds"] == 0
    after = episode.get_by_guid_feed(conn, "episode-1", FEED_URL)["feed_snapshot"]
    assert after == before


def test_v1_to_v2_migration_preserves_episode(tmp_path):
    connection = db.connect(tmp_path / "v1.db")
    db.init_schema(connection)
    episode_id = episode.create(connection, guid="existing", feed="feed")
    connection.execute("DROP TABLE discovery_candidate")
    connection.execute("DROP TABLE feed")
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    db.init_schema(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert episode.get(connection, episode_id)["guid"] == "existing"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('feed','discovery_candidate')"
    ).fetchone()[0] == 2
    connection.close()


def test_feed_cli_add_list_and_disable(tmp_path):
    path = str(tmp_path / "cli.db")
    root = Path(__file__).resolve().parents[1]
    added = subprocess.run(
        [sys.executable, "-m", "discover.cli", "feed-add", "--name", "PL", "--url", FEED_URL,
         "--show", "Show", "--db", path],
        text=True, capture_output=True, cwd=root,
    )
    assert added.returncode == 0, added.stdout + added.stderr
    feed_id = json.loads(added.stdout)["data"]["feed_id"]
    listed = subprocess.run(
        [sys.executable, "-m", "discover.cli", "feed-list", "--db", path],
        text=True, capture_output=True, cwd=root,
    )
    assert json.loads(listed.stdout)["data"]["items"][0]["feed_url"] == FEED_URL
    disabled = subprocess.run(
        [sys.executable, "-m", "discover.cli", "feed-disable", str(feed_id), "--db", path],
        text=True, capture_output=True, cwd=root,
    )
    assert disabled.returncode == 0
    assert json.loads(disabled.stdout)["data"]["enabled"] is False
