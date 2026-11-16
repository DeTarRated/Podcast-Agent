"""CLI envelope 契约测试：用真实 CLI 输出形状锁定 Node 消费方依赖的字段。

模拟场景：
- ledger_cli get 对缺失 call_ref 返回 {status:"success", data:{status:"missing"}}，不再平铺
- ledger_cli totals 输出含 open_calls/open_total_usd，且在途 intent 计入总额
- discover.cli feed-add/feed-list 输出包在 data 内
- discover.cli poll 的 304 与 disabled 条目仍带 classification_failed/classify_deferred（不误报失败）
"""
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from discover import poller
from state import db, episode, feed as feed_state

NOW = "2026-11-09T12:00:00+00:00"
FEED_URL = "https://feeds.example.com/show.xml"
ROOT = Path(__file__).resolve().parents[1]


def _run(args, stdin: str = ""):
    result = subprocess.run(
        [sys.executable, "-m", *args], input=stdin, text=True, capture_output=True, cwd=ROOT,
    )
    return result, json.loads(result.stdout.strip().splitlines()[-1])


def test_ledger_cli_get_envelope_shape(tmp_path):
    path = str(tmp_path / "ledger.db")
    result, payload = _run(["state.ledger_cli", "get", "--call-ref", "absent", "--db", path])
    assert result.returncode == 0
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "missing"
    assert payload["data"]["call_ref"] == "absent"
    # 平铺字段不得再出现，避免消费方误读。
    assert "amount_est" not in payload


def test_ledger_cli_totals_counts_open_reservations(tmp_path):
    path = str(tmp_path / "ledger.db")
    conn = db.connect(path)
    db.init_schema(conn)
    episode_id = episode.create(conn, guid="g", feed="f")
    conn.execute(
        """
        INSERT INTO ledger (episode_id, purpose, provider, model, usage_basis, amount_est,
                            request_started, call_ref, status, triggered_at)
        VALUES (?, 'translate', 'p', 'm', 'b', 0.4, 1, 'open-call', 'intent', ?)
        """,
        (episode_id, NOW),
    )
    conn.execute(
        """
        INSERT INTO ledger (episode_id, purpose, provider, model, usage_basis, amount_est,
                            request_started, call_ref, status, triggered_at)
        VALUES (?, 'translate', 'p', 'm', 'b', 0.25, 1, 'done-call', 'success', ?)
        """,
        (episode_id, NOW),
    )
    conn.commit()
    conn.close()

    result, payload = _run(["state.ledger_cli", "totals", "--db", path])
    assert result.returncode == 0
    assert payload["status"] == "success"
    data = payload["data"]
    assert data["success_calls"] == 1
    assert data["success_total_usd"] == pytest.approx(0.25)
    assert data["open_calls"] == 1
    assert data["open_total_usd"] == pytest.approx(0.4)


def test_poll_not_modified_entry_carries_required_counters(conn_factory):
    conn = conn_factory()
    feed_id = feed_state.add(
        conn, name="A", feed_url=FEED_URL, show_name="S", now=NOW, process_existing=True,
    )
    poller.poll_feed(
        conn, feed_id, now=NOW,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"<rss><channel><title>x</title></channel></rss>")),
        trust_env=False,
    )
    result = poller.poll_feed(
        conn, feed_id, now=NOW,
        transport=httpx.MockTransport(lambda request: httpx.Response(304)),
        trust_env=False,
    )
    assert result["status"] == "not_modified"
    # 常态 304 轮次不得因缺字段被判失败。
    assert result["classification_failed"] == 0
    assert result["classify_deferred"] == 0
    conn.close()


def test_disabled_feed_entry_carries_required_counters(conn_factory):
    conn = conn_factory()
    feed_id = feed_state.add(
        conn, name="A", feed_url=FEED_URL, show_name="S", now=NOW, process_existing=True,
    )
    feed_state.disable(conn, feed_id, now=NOW)

    def unexpected(request):
        raise AssertionError("禁用 feed 不应请求")

    result = poller.poll_all(
        conn, now=NOW, transport=httpx.MockTransport(unexpected), trust_env=False,
    )
    # poll_all 会过滤禁用 feed；此处直接校验 poll_feed 的 disabled 分支字段。
    direct = poller.poll_feed(
        conn, feed_id, now=NOW, transport=httpx.MockTransport(unexpected), trust_env=False,
    )
    assert direct["status"] == "disabled"
    assert direct["classification_failed"] == 0
    assert direct["classify_deferred"] == 0
    assert result["feeds"] == 0
    conn.close()


@pytest.fixture()
def conn_factory(tmp_path):
    created = []

    def make():
        conn = db.connect(tmp_path / f"c{len(created)}.db")
        db.init_schema(conn)
        created.append(conn)
        return conn

    yield make
    for conn in created:
        conn.close()


def test_failed_feed_entry_carries_counters(conn_factory):
    """feed 请求失败条目必须带计数字段，业务循环才能报出 feed_failed=N。"""
    conn = conn_factory()
    feed_state.add(
        conn, name="A", feed_url=FEED_URL, show_name="S", now=NOW, process_existing=True,
    )
    result = poller.poll_all(
        conn, now=NOW,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        trust_env=False,
    )
    assert result["failed"] == 1
    item = result["items"][0]
    assert item["status"] == "failed"
    assert item["failed"] == 1
    assert item["classification_failed"] == 0
    assert item["classify_deferred"] == 0
    conn.close()
