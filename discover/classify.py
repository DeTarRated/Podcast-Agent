"""发现层·可选 Chelsea 标题／简介分类任务。

候选先落库，分类是独立付费调用；同 call_ref 已有账本时绝不自动重发。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

import httpx

from state import ledger


class ClassificationError(RuntimeError):
    """分类失败或结果未知；调用方记录候选失败，不自动重试。"""


@dataclass(frozen=True)
class ClassificationResult:
    relevant: bool
    reason: str
    call_ref: str


def call_ref_for(feed_id: int, guid: str, title: str, summary: str) -> str:
    digest = hashlib.sha256(f"{title}\n{summary}".encode("utf-8")).hexdigest()
    return f"classify:{feed_id}:{guid}:{digest}"


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
        ledger.record_intent(
            conn,
            episode_id=None,
            purpose="classify",
            provider="deepseek",
            model=self.model,
            usage_basis="phase=intent",
            amount_est=None,
            call_ref=call_ref,
        )
        request_payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是播客元数据分类器。只判断是否可能涉及 Chelsea 男子足球队，包括球员、教练、管理层、比赛、转会、伤病和战术讨论。"
                        "纯切尔西女足、与切尔西无关的其他球队或体育内容应判 false。无法确定但有潜在男足关联时判 true，避免漏掉。"
                        "只输出 JSON object：{\"relevant\":true或false,\"reason\":\"简短原因\"}。"
                    ),
                },
                {"role": "user", "content": f"标题：{title}\n简介：{summary}"},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        try:
            with httpx.Client(timeout=30.0, transport=self.transport, trust_env=False) as client:
                response = client.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=request_payload,
                )
        except httpx.HTTPError as exc:
            ledger.record_outcome(conn, call_ref, status="unknown")
            raise ClassificationError("分类网络结果未知，禁止自动重试") from exc
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
            f"prompt_tokens={usage.get('prompt_tokens')};"
            f"completion_tokens={usage.get('completion_tokens')};"
            f"total_tokens={usage.get('total_tokens')}"
        )
        ledger.record_outcome(conn, call_ref, status="success", usage_basis=usage_basis)
        return ClassificationResult(relevant=decision["relevant"], reason=reason, call_ref=call_ref)
