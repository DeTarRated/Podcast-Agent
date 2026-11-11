"""Chelsea 分类任务测试；所有 HTTP 与分类器均为 mock，零真实付费调用。

模拟场景：
- 新候选分类后入队／跳过，重复轮询不重复分类，历史基线不分类
- 单候选分类失败不进入执行层，也不阻塞其他候选
- DeepSeek JSON 输出、token 用量、账本 success 与同 call_ref 防重发
- 网络超时与无效 JSON 记 unknown，HTTP 失败记 failed
"""
import json

import httpx
import pytest

from discover.classify import ClassificationError, DeepSeekClassifier, ClassificationResult
from discover import poller
from state import db, discovery, episode, feed as feed_state, ledger

NOW = "2026-11-09T12:00:00+00:00"
FEED_URL = "https://feeds.example.com/show.xml"
RSS = """<rss version="2.0"><channel><title>Test</title>
<item><guid>episode-1</guid><title>Chelsea</title><description>Blues</description>
<enclosure url="https://cdn.example.com/one.mp3" type="audio/mpeg"/></item>
<item><guid>episode-2</guid><title>Other team</title><description>Nothing</description>
<enclosure url="https://cdn.example.com/two.mp3" type="audio/mpeg"/></item>
</channel></rss>"""


@pytest.fixture(autouse=True)
def _fake_provider_gate(monkeypatch):
    class FakeGate:
        def release(self, provider, owner):
            return True
    monkeypatch.setattr(
        "discover.classify.acquire_or_raise",
        lambda provider: (FakeGate(), type("Decision", (), {"owner": "test-owner"})()),
    )


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "classify.db")
    db.init_schema(connection)
    yield connection
    connection.close()


def _feed(conn, *, process_existing=True):
    return feed_state.add(
        conn, name="PL", feed_url=FEED_URL, process_existing=process_existing, now=NOW
    )


def _candidate(conn, feed_id=1, guid="episode-1"):
    candidate_id = discovery.insert_candidate(
        conn,
        feed_id=feed_id,
        guid=guid,
        title="Chelsea title",
        summary="Chelsea summary",
        enclosure="https://cdn.example.com/audio.mp3",
        now=NOW,
        published_at=None,
    )
    conn.commit()
    return conn.execute("SELECT * FROM discovery_candidate WHERE id=?", (candidate_id,)).fetchone()


def test_only_new_candidates_are_classified(conn):
    feed_id = _feed(conn)
    calls = []

    def classifier(_conn, candidate):
        calls.append(candidate["guid"])
        relevant = candidate["guid"] == "episode-1"
        return ClassificationResult(relevant, "test", f"test:{candidate['guid']}")

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=RSS.encode()))
    first = poller.poll_feed(
        conn, feed_id, now=NOW, transport=transport, trust_env=False, classifier=classifier
    )
    assert calls == ["episode-1", "episode-2"]
    assert first["enqueued"] == 1
    assert first["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0] == 1
    second = poller.poll_feed(
        conn, feed_id, now=NOW, transport=transport, trust_env=False, classifier=classifier
    )
    assert calls == ["episode-1", "episode-2"]
    assert second["duplicates"] == 2


def test_baseline_does_not_call_classifier(conn):
    feed_id = _feed(conn, process_existing=False)
    calls = []

    def classifier(_conn, candidate):
        calls.append(candidate["guid"])
        return ClassificationResult(True, "unused", "unused")

    result = poller.poll_feed(
        conn,
        feed_id,
        now=NOW,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=RSS.encode())),
        trust_env=False,
        classifier=classifier,
    )
    assert result["status"] == "baseline"
    assert calls == []


def test_classification_failure_does_not_enqueue_or_block_other_candidate(conn):
    feed_id = _feed(conn)

    def classifier(_conn, candidate):
        if candidate["guid"] == "episode-1":
            raise ClassificationError("unknown")
        return ClassificationResult(True, "ok", "test:episode-2")

    result = poller.poll_feed(
        conn,
        feed_id,
        now=NOW,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=RSS.encode())),
        trust_env=False,
        classifier=classifier,
    )
    assert result["classification_failed"] == 1
    assert result["enqueued"] == 1
    assert discovery.get_candidate(conn, feed_id, "episode-1")["status"] == "failed"
    assert discovery.get_candidate(conn, feed_id, "episode-2")["status"] == "enqueued"


def test_deepseek_success_usage_and_duplicate_guard(conn):
    _feed(conn)
    candidate = _candidate(conn)
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "Chelsea title" in payload["messages"][1]["content"]
        assert "audio.mp3" not in request.content.decode()
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"relevant":true,"reason":"Chelsea"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })

    classifier = DeepSeekClassifier(
        api_key="test-key", transport=httpx.MockTransport(handler)
    )
    result = classifier(conn, candidate)
    assert result.relevant is True
    row = ledger.get_by_ref(conn, result.call_ref)
    assert row["status"] == "success"
    assert "prompt_tokens=100" in row["usage_basis"]
    with pytest.raises(ClassificationError, match="禁止自动重发"):
        classifier(conn, candidate)
    assert len(requests) == 1


def test_deepseek_timeout_marks_unknown_without_retry(conn):
    _feed(conn)
    candidate = _candidate(conn)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("mock", request=request)

    classifier = DeepSeekClassifier(api_key="test", transport=httpx.MockTransport(handler))
    with pytest.raises(ClassificationError, match="结果未知"):
        classifier(conn, candidate)
    call_ref = f"classify:1:episode-1:" + __import__('hashlib').sha256(b"Chelsea title\nChelsea summary").hexdigest()
    assert ledger.get_by_ref(conn, call_ref)["status"] == "unknown"
    assert len(calls) == 1


def test_deepseek_invalid_json_and_http_failure(conn):
    _feed(conn)
    candidate = _candidate(conn)
    invalid = DeepSeekClassifier(
        api_key="test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices": [{"message": {"content": "not-json"}}], "usage": {},
        })),
    )
    with pytest.raises(ClassificationError, match="无法解析"):
        invalid(conn, candidate)
    call_ref = f"classify:1:episode-1:" + __import__('hashlib').sha256(b"Chelsea title\nChelsea summary").hexdigest()
    assert ledger.get_by_ref(conn, call_ref)["status"] == "unknown"

    other = _candidate(conn, guid="episode-2")
    failed = DeepSeekClassifier(
        api_key="test", transport=httpx.MockTransport(lambda request: httpx.Response(400))
    )
    with pytest.raises(ClassificationError, match="HTTP 400"):
        failed(conn, other)
    other_ref = f"classify:1:episode-2:" + __import__('hashlib').sha256(b"Chelsea title\nChelsea summary").hexdigest()
    assert ledger.get_by_ref(conn, other_ref)["status"] == "failed"
