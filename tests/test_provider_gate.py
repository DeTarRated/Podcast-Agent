"""真实 Redis 分布式闸门测试；仅使用带随机前缀的测试 key，零外部付费调用。

模拟场景：
- 令牌桶 burst 与速率、并发上限、owner 释放
- 租约 TTL 清理与过期接管
- 多线程竞争下并发数不超限
- Redis 故障时 acquire 失败关闭，release 不篡改业务结果

默认跳过；设置 PODCAST_TEST_REDIS=1 且本地 Redis 可用时运行。
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from pipeline.provider_gate import ProviderGate, ProviderGateError

pytestmark = pytest.mark.skipif(
    os.environ.get("PODCAST_TEST_REDIS") != "1",
    reason="需要显式启用本地 Redis 测试",
)


@pytest.fixture()
def gate():
    client = redis.Redis.from_url(
        os.environ.get("PODCAST_REDIS_URL", "redis://127.0.0.1:6379/0"),
        socket_timeout=2,
        socket_connect_timeout=2,
    )
    prefix = f"podcast-agent-test-{uuid.uuid4().hex}"
    instance = ProviderGate(client, prefix=prefix)
    yield instance
    for key in (f"{prefix}:scribe:tokens", f"{prefix}:scribe:leases"):
        client.delete(key)
    client.close()


def test_token_bucket_concurrency_and_owner_release(gate):
    first = gate.acquire("scribe", rate_per_second=0.001, burst=3, max_concurrent=2)
    second = gate.acquire("scribe", rate_per_second=0.001, burst=3, max_concurrent=2)
    assert first.allowed and second.allowed
    blocked = gate.acquire("scribe", rate_per_second=0.001, burst=3, max_concurrent=2)
    assert not blocked.allowed
    assert blocked.active == 2
    assert gate.release("scribe", first.owner) is True
    assert gate.release("scribe", "not-owner") is False
    third = gate.acquire("scribe", rate_per_second=0.001, burst=3, max_concurrent=2)
    assert third.allowed
    exhausted = gate.acquire("scribe", rate_per_second=0.001, burst=3, max_concurrent=2)
    assert not exhausted.allowed
    assert exhausted.retry_after_ms > 0


def test_lease_expiry_allows_takeover(gate):
    first = gate.acquire("scribe", rate_per_second=100, burst=10, max_concurrent=1, lease_seconds=1, now_ms=1000)
    blocked = gate.acquire("scribe", rate_per_second=100, burst=10, max_concurrent=1, lease_seconds=1, now_ms=1500)
    assert first.allowed and not blocked.allowed
    takeover = gate.acquire("scribe", rate_per_second=100, burst=10, max_concurrent=1, lease_seconds=1, now_ms=2001)
    assert takeover.allowed
    assert takeover.active == 1


def test_concurrent_acquire_never_exceeds_limit(gate):
    def acquire(_index):
        return gate.acquire(
            "scribe", rate_per_second=100, burst=100, max_concurrent=5,
            lease_seconds=60, now_ms=1000,
        )
    with ThreadPoolExecutor(max_workers=10) as pool:
        decisions = list(pool.map(acquire, range(20)))
    allowed = [item for item in decisions if item.allowed]
    assert len(allowed) == 5
    for decision in allowed:
        gate.release("scribe", decision.owner)


def test_redis_failure_fails_closed(gate):
    class BrokenClient:
        def time(self):
            raise redis.ConnectionError("mock offline")

        def eval(self, *args):
            raise redis.ConnectionError("mock offline")

    broken = ProviderGate(BrokenClient(), prefix="broken")
    with pytest.raises(ProviderGateError, match="付费调用已停止"):
        broken.acquire("scribe")
    # release 失败只等待 TTL，不把业务结果改成 unknown
    class BrokenRelease(BrokenClient):
        def hdel(self, *args):
            raise redis.ConnectionError("mock offline")
    broken_release = ProviderGate(BrokenRelease(), prefix="broken")
    assert broken_release.release("scribe", "owner") is False
