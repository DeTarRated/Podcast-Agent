"""Chelsea 分类任务测试；所有 HTTP 与分类器均为 mock，零真实付费调用。

模拟场景：
- 新候选分类后入队／跳过，重复轮询不重复分类，历史基线不分类
- 单候选分类失败不进入执行层，也不阻塞其他候选
- DeepSeek JSON 输出、token 用量、预算拒绝零HTTP、账本 success 与同 call_ref 防重发
- 网络超时与无效 JSON 记 unknown，HTTP 失败记 failed
"""
import json
from datetime import date

import httpx
import pytest

from discover import classify
from discover.classify import ClassificationError, DeepSeekClassifier, ClassificationResult
from discover import poller
from pipeline.cost_guard import authorize_and_record_intent as REAL_COST_GUARD_AUTHORIZE
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
    monkeypatch.setenv("PODCAST_CLASSIFY_PRICE_MODEL", "deepseek-flash")
    monkeypatch.setenv("PODCAST_CLASSIFY_PRICING_SOURCE", "https://api-docs.deepseek.com/quick_start/pricing")
    monkeypatch.setenv("PODCAST_CLASSIFY_PRICING_DATE", date.today().isoformat())
    monkeypatch.setenv("PODCAST_CLASSIFY_MAX_INPUT_TOKENS", "20000")
    monkeypatch.setenv("PODCAST_CLASSIFY_MAX_OUTPUT_TOKENS", "256")
    monkeypatch.setenv("PODCAST_CLASSIFY_MAX_INPUT_USD_PER_MILLION", "0.3")
    monkeypatch.setenv("PODCAST_CLASSIFY_MAX_OUTPUT_USD_PER_MILLION", "1.2")

    def authorize(conn, **kwargs):
        return ledger.record_intent(conn, **kwargs)
    monkeypatch.setattr("discover.classify.cost_guard.authorize_and_record_intent", authorize)

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


def test_budget_denial_prevents_classifier_http_and_intent(conn, monkeypatch):
    from decimal import Decimal
    from state.postgres.budget import BudgetDataError
    real_authorize = REAL_COST_GUARD_AUTHORIZE

    feed_id = _feed(conn)
    candidate = _candidate(conn, feed_id)
    calls = []
    classifier = DeepSeekClassifier(api_key="test-key", transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={})
    ))
    monkeypatch.setattr("discover.classify.cost_guard.authorize_and_record_intent", real_authorize)
    monkeypatch.setattr("discover.classify.cost_guard.load_limits", lambda _episode_id: [{
        "scope": "global", "period": "day", "max_amount": Decimal("0.000001"),
    }])
    with pytest.raises(BudgetDataError, match="预算超限"):
        classifier(conn, candidate)
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


@pytest.mark.parametrize("key,value", [
    ("PODCAST_CLASSIFY_PRICE_MODEL", "wrong-model"),
    ("PODCAST_CLASSIFY_PRICING_SOURCE", ""),
    ("PODCAST_CLASSIFY_PRICING_DATE", "2000-01-01"),
    ("PODCAST_CLASSIFY_MAX_INPUT_TOKENS", "1"),
])
def test_unreliable_estimate_config_blocks_before_gate_or_http(conn, monkeypatch, key, value):
    _feed(conn)
    candidate = _candidate(conn)
    monkeypatch.setenv(key, value)
    calls = []
    classifier = DeepSeekClassifier(api_key="test-key", transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={})
    ))
    with pytest.raises(ClassificationError):
        classifier(conn, candidate)
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_business_pause_blocks_classifier_before_gate_intent_and_http(conn, monkeypatch, tmp_path):
    _feed(conn)
    candidate = _candidate(conn)
    pause = tmp_path / "business.pause"
    pause.write_text("paused")
    monkeypatch.setenv("PODCAST_BUSINESS_PAUSE_FILE", str(pause))
    calls = []
    classifier = DeepSeekClassifier(api_key="test-key", transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={})
    ))
    with pytest.raises(RuntimeError, match="业务暂停标记存在"):
        classifier(conn, candidate)
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_business_pause_during_gate_window_prevents_intent_and_http(conn, monkeypatch, tmp_path):
    """gate 返回后、intent 写入前出现暂停：许可释放、无 intent、无 HTTP。"""
    _feed(conn)
    candidate = _candidate(conn)
    pause = tmp_path / "business.pause"
    monkeypatch.setenv("PODCAST_BUSINESS_PAUSE_FILE", str(pause))
    real_acquire = classify.acquire_or_raise

    def pausing_acquire(provider):
        gate, decision = real_acquire(provider)
        pause.write_text("paused in gate window\n")
        return gate, decision

    monkeypatch.setattr(classify, "acquire_or_raise", pausing_acquire)
    calls = []
    classifier = DeepSeekClassifier(api_key="test-key", transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={})
    ))
    with pytest.raises(RuntimeError, match="业务暂停标记存在"):
        classifier(conn, candidate)
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


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


def test_classify_budget_limits_calls_and_defers_rest(conn):
    """轮内分类上限：达到上限后剩余候选保持 pending 不入队不分类。"""
    feed_id = _feed(conn)
    calls = []

    def classifier(_conn, candidate):
        calls.append(candidate["guid"])
        return ClassificationResult(True, "test", f"test:{candidate['guid']}")

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=RSS.encode()))
    result = poller.poll_feed(
        conn, feed_id, now=NOW, transport=transport, trust_env=False,
        classifier=classifier, classify_budget={"remaining": 1},
    )
    assert calls == ["episode-1"]
    assert result["classified"] == 1
    assert result["classify_deferred"] == 1
    assert result["enqueued"] == 1
    # 未分类候选保留 pending，待下一轮处理，不标失败。
    pending = discovery.get_candidate(conn, feed_id, "episode-2")
    assert pending["status"] == "pending"
    assert pending["classification_relevant"] is None


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


def test_deepseek_success_usage_and_duplicate_guard(conn, monkeypatch):
    _feed(conn)
    candidate = _candidate(conn)
    requests = []
    real_authorize = REAL_COST_GUARD_AUTHORIZE
    monkeypatch.setattr("discover.classify.cost_guard.authorize_and_record_intent", real_authorize)
    monkeypatch.setattr("discover.classify.cost_guard.load_limits", lambda _episode_id: [])
    sqlite_path = conn.execute("PRAGMA database_list").fetchone()[2]

    def handler(request):
        requests.append(request)
        independent = __import__("sqlite3").connect(sqlite_path)
        try:
            row = independent.execute("SELECT status,amount_est FROM ledger").fetchone()
            assert row[0] == "intent" and row[1] > 0
        finally:
            independent.close()
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 256
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
    assert row["amount_est"] > 0
    assert "estimate=versioned_max_tokens" in row["usage_basis"]
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


def test_interests_default_and_custom_topics(monkeypatch):
    """关注对象可配置：默认 Chelsea；可追加曼联等；非法配置失败关闭。"""
    monkeypatch.delenv("PODCAST_CLASSIFY_TOPICS", raising=False)
    assert classify.interests() == [classify.DEFAULT_INTERESTS]
    assert "Chelsea" in classify.system_prompt_for(classify.interests())

    monkeypatch.setenv("PODCAST_CLASSIFY_TOPICS", "Chelsea 男子足球队, Manchester United 男子足球队")
    items = classify.interests()
    assert items == ["Chelsea 男子足球队", "Manchester United 男子足球队"]
    prompt = classify.system_prompt_for(items)
    assert "Manchester United" in prompt and "Chelsea" in prompt
    # 判定范围明确限定男足一线队，避免把女足或其他运动算进来。
    assert "男子一线队" in prompt


@pytest.mark.parametrize("value", ["", "   ", "a,b,c,d,e,f,g,h,i", "bad\nvalue", "x" * 41])
def test_invalid_topics_fail_closed(monkeypatch, value):
    monkeypatch.setenv("PODCAST_CLASSIFY_TOPICS", value)
    with pytest.raises(classify.ClassificationError):
        classify.interests()


def test_custom_topics_reach_classifier_request(conn, monkeypatch):
    """配置的关注对象必须真正出现在发给供应商的提示里。"""
    _feed(conn)
    candidate = _candidate(conn)
    monkeypatch.setenv("PODCAST_CLASSIFY_TOPICS", "Chelsea 男子足球队,Manchester United 男子足球队")
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"relevant": True, "reason": "MU"})}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        })

    classifier = DeepSeekClassifier(api_key="test-key", transport=httpx.MockTransport(handler))
    classifier(conn, candidate)
    system = captured[0]["messages"][0]["content"]
    assert "Manchester United" in system


def test_prefilter_keyword_skips_without_paid_call(conn, monkeypatch, tmp_path):
    """排除词命中：零付费调用、零账本行，直接判定不相关。"""
    feed_id = _feed(conn)
    policy_file = tmp_path / "p.json"
    policy_file.write_text(json.dumps({
        "version": 1,
        "topics": {"chelsea": {"label": "Chelsea 男子足球队"}},
        "default_topics": ["chelsea"],
        "prefilter": {"exclude_keywords": ["rugby"]},
    }), encoding="utf-8")
    monkeypatch.setenv("PODCAST_CLASSIFY_POLICY", str(policy_file))
    row = discovery.insert_candidate(
        conn, feed_id=feed_id, guid="rugby-ep", title="Rugby round-up",
        published_at=None, enclosure="https://cdn.example.com/a.mp3",
        summary="Dragons Slay the All Blacks", now=NOW,
    )
    conn.commit()
    candidate = discovery.get_candidate(conn, feed_id, "rugby-ep")
    calls = []
    classifier = DeepSeekClassifier(api_key="test-key", transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={})
    ))
    decision = classifier(conn, candidate)
    assert decision.relevant is False
    assert decision.reason.startswith("prefilter_excluded:")
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_feed_scoped_topics_change_prompt(conn, monkeypatch, tmp_path):
    """按 feed 组合：不同 feed 拿到不同关注对象。"""
    feed_id = _feed(conn)
    policy_file = tmp_path / "p2.json"
    policy_file.write_text(json.dumps({
        "version": 1,
        "topics": {
            "chelsea": {"label": "Chelsea 男子足球队"},
            "man-utd": {"label": "Manchester United 男子足球队"},
        },
        "default_topics": ["chelsea"],
        "feeds": {str(feed_id): ["man-utd"]},
    }), encoding="utf-8")
    monkeypatch.setenv("PODCAST_CLASSIFY_POLICY", str(policy_file))
    candidate = _candidate(conn)
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"relevant": True, "reason": "MU"})}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5},
        })

    classifier = DeepSeekClassifier(api_key="test-key", transport=httpx.MockTransport(handler))
    classifier(conn, candidate)
    system = captured[0]["messages"][0]["content"]
    assert "Manchester United" in system and "Chelsea" not in system
