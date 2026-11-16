"""发现层·可选 Chelsea 标题／简介分类任务。

候选先落库，分类是独立付费调用；同 call_ref 已有账本时绝不自动重发。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from typing import Optional

import httpx

from state import ledger, runtime_guard
from pipeline.provider_gate import acquire_or_raise
from pipeline import cost_guard


class ClassificationError(RuntimeError):
    """分类失败或结果未知；调用方记录候选失败，不自动重试。"""


@dataclass(frozen=True)
class ClassificationResult:
    relevant: bool
    reason: str
    call_ref: str


DEFAULT_INTERESTS = "Chelsea 男子足球队"


def interests() -> list[str]:
    """关注的俱乐部／主题，来自 PODCAST_CLASSIFY_TOPICS（逗号分隔）。

    缺失时沿用历史默认（只关心 Chelsea 男足）；配置非法一律失败关闭，
    避免“配置写错就变成全量下载”。
    """
    raw = os.environ.get("PODCAST_CLASSIFY_TOPICS", DEFAULT_INTERESTS)
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items or len(items) > 8:
        raise ClassificationError("PODCAST_CLASSIFY_TOPICS 必须为 1..8 个逗号分隔条目")
    for item in items:
        if len(item) > 40 or any(ch in item for ch in "\n\r{}"):
            raise ClassificationError(f"PODCAST_CLASSIFY_TOPICS 条目非法: {item!r}")
    return items


def system_prompt_for(items: list[str]) -> str:
    topics = "、".join(items)
    return (
        f"你是播客元数据分类器。只判断是否可能涉及以下对象：{topics}。"
        "判定范围限于男子一线队，包括球员、教练、管理层、比赛、转会、伤病和战术讨论。"
        "与上述对象无关的其他球队、其他运动或女足内容应判 false。"
        "无法确定但有潜在男足关联时判 true，避免漏掉。"
        '只输出 JSON object：{"relevant":true或false,"reason":"简短原因"}。'
    )


def call_ref_for(feed_id: int, guid: str, title: str, summary: str) -> str:
    digest = hashlib.sha256(f"{title}\n{summary}".encode("utf-8")).hexdigest()
    return f"classify:{feed_id}:{guid}:{digest}"


def _estimate_request(model: str, system_prompt: str, title: str, summary: str) -> tuple[Decimal, int, str]:
    """使用显式版本化价格及token上限计算保守单次费用，不接受裸金额估算。"""
    if os.environ.get("PODCAST_CLASSIFY_PRICE_MODEL") != model:
        raise ClassificationError("分类价格配置模型缺失或与当前模型不匹配")
    source = os.environ.get("PODCAST_CLASSIFY_PRICING_SOURCE", "")
    pricing_date = os.environ.get("PODCAST_CLASSIFY_PRICING_DATE", "")
    if not source.startswith("https://"):
        raise ClassificationError("分类价格配置缺少 HTTPS 来源")
    try:
        priced_on = date.fromisoformat(pricing_date)
        age_days = (date.today() - priced_on).days
        max_input_tokens = int(os.environ["PODCAST_CLASSIFY_MAX_INPUT_TOKENS"])
        max_output_tokens = int(os.environ["PODCAST_CLASSIFY_MAX_OUTPUT_TOKENS"])
        input_rate = Decimal(os.environ["PODCAST_CLASSIFY_MAX_INPUT_USD_PER_MILLION"])
        output_rate = Decimal(os.environ["PODCAST_CLASSIFY_MAX_OUTPUT_USD_PER_MILLION"])
    except (KeyError, ValueError, InvalidOperation):
        raise ClassificationError("分类价格／token上限配置缺失或非法") from None
    if age_days < 0 or age_days > 30:
        raise ClassificationError("分类价格配置已过期或日期在未来")
    if max_input_tokens <= 0 or max_output_tokens <= 0 or max_output_tokens > 393216:
        raise ClassificationError("分类输入/输出token上限非法")
    if not input_rate.is_finite() or not output_rate.is_finite() or input_rate < 0 or output_rate < 0:
        raise ClassificationError("分类token价格必须是有限非负值")
    input_bound = len((system_prompt + title + summary).encode("utf-8")) + 512
    if input_bound > max_input_tokens:
        raise ClassificationError("分类输入超过已配置的保守token上限")
    estimate = (Decimal(max_input_tokens) * input_rate + Decimal(max_output_tokens) * output_rate) / Decimal(1_000_000)
    if not estimate.is_finite() or estimate <= 0:
        raise ClassificationError("分类单次最高成本估算为零或非法")
    basis = (
        f"estimate=versioned_max_tokens;model={model};pricing_date={pricing_date};"
        f"max_input_tokens={max_input_tokens};max_output_tokens={max_output_tokens};"
        f"input_usd_per_million={input_rate};output_usd_per_million={output_rate};source={source}"
    )
    return estimate, max_output_tokens, basis


class DeepSeekClassifier:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-flash",
        url: str = "https://api.deepseek.com/chat/completions",
        transport: Optional[httpx.BaseTransport] = None,
    ):
        if not api_key.strip():
            raise ClassificationError("缺少 DEEPSEEK_API_KEY")
        self.api_key = api_key
        self.model = model
        self.url = url
        self.transport = transport

    @classmethod
    def from_env(cls, *, transport: Optional[httpx.BaseTransport] = None) -> "DeepSeekClassifier":
        route = os.environ.get("PODCAST_MODEL_CLASSIFY", "deepseek/deepseek-flash")
        provider, separator, model = route.partition("/")
        if provider != "deepseek" or not separator or not model:
            raise ClassificationError("PODCAST_MODEL_CLASSIFY 当前仅支持 deepseek/<model>")
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            model=model,
            url=os.environ.get("DEEPSEEK_CLASSIFY_URL", "https://api.deepseek.com/chat/completions"),
            transport=transport,
        )

    def __call__(self, conn: sqlite3.Connection, candidate: sqlite3.Row) -> ClassificationResult:
        title = (candidate["title"] or "")[:1000]
        summary = (candidate["summary"] or "")[:6000]
        call_ref = call_ref_for(candidate["feed_id"], candidate["guid"], title, summary)
        existing = ledger.get_by_ref(conn, call_ref)
        if existing is not None:
            raise ClassificationError(
                f"分类调用已存在但未形成可用候选结果，status={existing['status']}；禁止自动重发"
            )
        # 付费边界失败关闭：暂停标记存在时不取限流许可、不落 intent、不发请求。
        runtime_guard.assert_business_running()
        system_prompt = system_prompt_for(interests())
        user_content = f"标题：{title}\n简介：{summary}"
        estimate, max_output_tokens, estimate_basis = _estimate_request(
            self.model, system_prompt, title, summary,
        )
        # 先取得分布式限流许可，再在SQLite预算锁内原子落 estimated intent。
        provider_gate, gate_decision = acquire_or_raise("deepseek")
        # gate→intent 窗口复查：限流许可返回后、账本写入前暂停标记可能出现；
        # 暂停时释放许可且不写 intent。
        try:
            runtime_guard.assert_business_running()
        except RuntimeError:
            provider_gate.release("deepseek", gate_decision.owner)
            raise
        try:
            cost_guard.authorize_and_record_intent(
                conn,
                episode_id=None,
                purpose="classify",
                provider="deepseek",
                model=self.model,
                usage_basis=estimate_basis,
                amount_est=estimate,
                call_ref=call_ref,
            )
        except Exception:
            provider_gate.release("deepseek", gate_decision.owner)
            raise
        request_payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_output_tokens,
            "stream": False,
        }
        try:
            # intent 落库与发起供应商请求之间的窗口内可能出现暂停标记，HTTP 前复查；
            # 暂停时先释放本次预留再抛错，避免候选被残留 intent 永久卡住。
            try:
                runtime_guard.assert_business_running()
            except RuntimeError:
                ledger.record_outcome(conn, call_ref, status="failed")
                raise
            with httpx.Client(timeout=30.0, transport=self.transport, trust_env=False) as client:
                response = client.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=request_payload,
                )
        except httpx.HTTPError as exc:
            ledger.record_outcome(conn, call_ref, status="unknown")
            raise ClassificationError("分类网络结果未知，禁止自动重试") from exc
        finally:
            provider_gate.release("deepseek", gate_decision.owner)
        if response.status_code != 200:
            ledger.record_outcome(conn, call_ref, status="failed")
            raise ClassificationError(f"分类 HTTP {response.status_code}")

        usage = None
        try:
            payload = response.json()
            usage = payload.get("usage") or {}
            content = payload["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            decision = json.loads(content)
            if not isinstance(decision, dict) or type(decision.get("relevant")) is not bool:
                raise ValueError("relevant 必须为 boolean")
            reason = str(decision.get("reason") or "")[:500]
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            ledger.record_outcome(conn, call_ref, status="unknown")
            raise ClassificationError("分类响应无法解析，结果未知，禁止自动重试") from exc
        usage_basis = (
            f"{estimate_basis};prompt_tokens={usage.get('prompt_tokens')};"
            f"completion_tokens={usage.get('completion_tokens')};"
            f"total_tokens={usage.get('total_tokens')}"
        )
        ledger.record_outcome(conn, call_ref, status="success", usage_basis=usage_basis)
        return ClassificationResult(relevant=decision["relevant"], reason=reason, call_ref=call_ref)
