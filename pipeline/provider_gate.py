"""执行层·Redis 分布式限流与并发租约。

Lua 原子令牌桶控制速率；Hash + owner + TTL 控制长任务并发。
Redis 故障时 acquire 失败关闭，不让付费调用绕过；release 失败等待 TTL，不改写业务结果。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import uuid
from dataclasses import asdict, dataclass
from typing import Optional

import redis

LEASE_LUA = r"""
local values = redis.call('HGETALL', KEYS[1])
local active = {}
local count = 0
for i = 1, #values, 2 do
    local owner = values[i]
    local expiry = tonumber(values[i + 1])
    if expiry <= tonumber(ARGV[1]) then
        redis.call('HDEL', KEYS[1], owner)
    else
        count = count + 1
    end
end
if redis.call('HEXISTS', KEYS[1], ARGV[2]) == 1 then
    redis.call('HSET', KEYS[1], ARGV[2], ARGV[3])
    redis.call('PEXPIRE', KEYS[1], ARGV[4])
    return {1, count}
end
if count >= tonumber(ARGV[5]) then
    return {0, count}
end
redis.call('HSET', KEYS[1], ARGV[2], ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return {1, count + 1}
"""

TOKEN_LUA = r"""
local values = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
local tokens = tonumber(ARGV[2])
local updated = tonumber(ARGV[1])
if values[1] then
    tokens = tonumber(values[1])
    updated = tonumber(values[2])
end
local elapsed = math.max(0, tonumber(ARGV[1]) - updated) / 1000
tokens = math.min(tonumber(ARGV[2]), tokens + elapsed * tonumber(ARGV[3]))
local retry_after = 0
local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    retry_after = math.ceil((1 - tokens) / tonumber(ARGV[3]) * 1000)
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated', ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return {allowed, tokens, retry_after}
"""


class ProviderGateError(RuntimeError):
    """Redis 不可用或配置非法；付费调用应停止，不能绕过。"""


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    provider: str
    owner: str
    retry_after_ms: int
    remaining_tokens: float
    active: int


class ProviderGate:
    def __init__(self, client, *, prefix: str = "podcast-agent"):
        self.client = client
        self.prefix = prefix

    @classmethod
    def from_env(cls) -> "ProviderGate":
        return cls(
            redis.Redis.from_url(
                os.environ.get("PODCAST_REDIS_URL", "redis://127.0.0.1:6379/0"),
                socket_timeout=5.0,
                socket_connect_timeout=5.0,
                decode_responses=False,
            ),
            prefix=os.environ.get("PODCAST_REDIS_PREFIX", "podcast-agent"),
        )

    def _now_ms(self, override: Optional[int]) -> int:
        if override is not None:
            return override
        seconds, micros = self.client.time()
        return seconds * 1000 + micros // 1000

    def _rate(self, provider: str, rate: Optional[float], burst: Optional[int]) -> tuple[float, int]:
        key = provider.upper().replace("-", "_")
        default_rate = 0.2 if provider == "scribe" else 5.0
        actual_rate = rate if rate is not None else float(os.environ.get(f"PODCAST_RATE_{key}", default_rate))
        actual_burst = burst if burst is not None else int(os.environ.get(f"PODCAST_BURST_{key}", max(1, math.ceil(actual_rate))))
        if actual_rate <= 0 or actual_burst <= 0:
            raise ProviderGateError("限流速率与 burst 必须为正数")
        return actual_rate, actual_burst

    def acquire(
        self,
        provider: str,
        *,
        rate_per_second: Optional[float] = None,
        burst: Optional[int] = None,
        max_concurrent: Optional[int] = None,
        lease_seconds: int = 6 * 3600,
        now_ms: Optional[int] = None,
    ) -> GateDecision:
        if not provider.strip() or lease_seconds <= 0:
            raise ProviderGateError("provider 与租约必须有效")
        owner = uuid.uuid4().hex
        rate, burst_value = self._rate(provider, rate_per_second, burst)
        concurrency = max_concurrent if max_concurrent is not None else int(
            os.environ.get(f"PODCAST_CONCURRENT_{provider.upper().replace('-', '_')}", "2")
        )
        if concurrency <= 0:
            raise ProviderGateError("max_concurrent 必须为正数")
        lease_key = f"{self.prefix}:{provider}:leases"
        token_key = f"{self.prefix}:{provider}:tokens"
        try:
            now = self._now_ms(now_ms)
            lease = self.client.eval(
                LEASE_LUA, 1, lease_key, now, owner, now + lease_seconds * 1000,
                lease_seconds * 2000, concurrency,
            )
            if int(lease[0]) != 1:
                return GateDecision(False, provider, owner, 1000, 0.0, int(lease[1]))
            token = self.client.eval(
                TOKEN_LUA, 1, token_key, now, burst_value, rate,
                max(60_000, int(burst_value / rate * 2000)),
            )
            allowed = int(token[0]) == 1
            if not allowed:
                self.release(provider, owner)
            return GateDecision(
                allowed=allowed,
                provider=provider,
                owner=owner,
                retry_after_ms=int(token[2]),
                remaining_tokens=float(token[1]),
                active=int(lease[1]),
            )
        except redis.RedisError as exc:
            raise ProviderGateError("Redis 分布式闸门不可用，付费调用已停止") from exc

    def release(self, provider: str, owner: str) -> bool:
        """只释放自己的租约；失败时依赖 TTL，不将已知业务结果改成 unknown。"""
        key = f"{self.prefix}:{provider}:leases"
        try:
            removed = self.client.hdel(key, owner)
            if self.client.exists(key) == 0:
                self.client.delete(key)
            return bool(removed)
        except redis.RedisError:
            return False


def acquire_or_raise(provider: str) -> tuple[ProviderGate, GateDecision]:
    """供真实付费调用使用；限流时不写 intent，直接返回可恢复错误。"""
    gate = ProviderGate.from_env()
    decision = gate.acquire(provider)
    if not decision.allowed:
        raise ProviderGateError(
            f"provider_rate_limited:{provider};retry_after_ms={decision.retry_after_ms}"
        )
    return gate, decision


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Redis 供应商限流与并发闸门")
    sub = parser.add_subparsers(dest="command", required=True)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("--provider", required=True)
    acquire.add_argument("--rate", type=float)
    acquire.add_argument("--burst", type=int)
    acquire.add_argument("--max-concurrent", type=int)
    acquire.add_argument("--lease-seconds", type=int, default=6 * 3600)
    release = sub.add_parser("release")
    release.add_argument("--provider", required=True)
    release.add_argument("--owner", required=True)
    args = parser.parse_args(argv)
    gate = ProviderGate.from_env()
    try:
        if args.command == "acquire":
            decision = gate.acquire(
                args.provider,
                rate_per_second=args.rate,
                burst=args.burst,
                max_concurrent=args.max_concurrent,
                lease_seconds=args.lease_seconds,
            )
            result = {"status": "success", "data": asdict(decision)}
            code = 0 if decision.allowed else 2
        else:
            released = gate.release(args.provider, args.owner)
            result = {"status": "success", "data": {"released": released}}
            code = 0
    except (ProviderGateError, redis.RedisError) as exc:
        result = {"status": "failed", "error": str(exc)}
        code = 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
