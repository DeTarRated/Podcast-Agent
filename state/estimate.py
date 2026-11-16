"""跨层数据契约·单期费用估算（预留用，非账单）。

用途：配置了金额限额后，job 领取必须带可审计估算；无估算一律失败关闭。
估算口径写进 usage_basis，实际费用仍以 SQLite 账本 outcome 为准。

配置（缺失或非法即失败关闭）：
- PODCAST_ESTIMATE_VERSION           估算口径版本，写入 payload
- PODCAST_ESTIMATE_MAX_DURATION_SECS 未知时长时的保守上限
- PODCAST_ESTIMATE_SCRIBE_USD_PER_HOUR
- PODCAST_ESTIMATE_TRANSLATE_USD     翻译子代理上界
- PODCAST_ESTIMATE_REVIEW_USD        复核／主 Agent 上界
"""
from __future__ import annotations

import math
import os
from decimal import Decimal, InvalidOperation
from typing import Optional


class EstimateError(RuntimeError):
    """估算配置缺失或非法；调用方必须失败关闭。"""


def _decimal(name: str) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raise EstimateError(f"缺少估算配置: {name}")
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise EstimateError(f"估算配置非法: {name}={raw!r}") from None
    if not value.is_finite() or value < 0:
        raise EstimateError(f"估算配置非法: {name}={raw!r}")
    return value


def _duration_limit() -> int:
    raw = os.environ.get("PODCAST_ESTIMATE_MAX_DURATION_SECS")
    if raw is None or not raw.strip():
        raise EstimateError("缺少估算配置: PODCAST_ESTIMATE_MAX_DURATION_SECS")
    try:
        value = int(raw.strip())
    except ValueError:
        raise EstimateError(f"估算配置非法: PODCAST_ESTIMATE_MAX_DURATION_SECS={raw!r}") from None
    if value <= 0:
        raise EstimateError("估算配置非法: PODCAST_ESTIMATE_MAX_DURATION_SECS 必须为正数")
    return value


def episode_estimate(duration_secs: Optional[float] = None) -> tuple[Decimal, str]:
    """返回 (金额上限估算, 依据)。时长未知时用配置上限，并在依据中标注。"""
    version = (os.environ.get("PODCAST_ESTIMATE_VERSION") or "").strip()
    if not version:
        raise EstimateError("缺少估算配置: PODCAST_ESTIMATE_VERSION")
    limit = _duration_limit()
    scribe_rate = _decimal("PODCAST_ESTIMATE_SCRIBE_USD_PER_HOUR")
    translate = _decimal("PODCAST_ESTIMATE_TRANSLATE_USD")
    review = _decimal("PODCAST_ESTIMATE_REVIEW_USD")

    if translate <= 0 or review <= 0 or scribe_rate <= 0:
        # 分项为零会让总额低估；估算分项必须为正。
        raise EstimateError("估算分项必须为正数: scribe/translate/review")

    known = duration_secs is not None
    if known:
        seconds = float(duration_secs)
        if not math.isfinite(seconds):
            raise EstimateError(f"Episode 时长非法: {duration_secs!r}")
        if seconds <= 0 or seconds > limit:
            # 超过已配置上限的时长不裁剪成上限，避免低估；明确拒绝。
            raise EstimateError(
                f"Episode 时长 {seconds:.0f}s 超出估算上限 {limit}s，拒绝预留"
            )
        basis_duration = f"duration_secs={seconds:.0f}"
    else:
        seconds = float(limit)
        basis_duration = f"max_duration_secs={limit}"

    scribe = (Decimal(str(seconds)) / Decimal(3600)) * scribe_rate
    total = scribe + translate + review
    amount = total.quantize(Decimal("0.000001"))
    if not amount.is_finite():
        raise EstimateError("估算金额非法: 非有限值")
    basis = (
        f"estimate_version={version};{basis_duration};duration_source="
        f"{'episode' if known else 'cap'};scribe_usd={scribe.quantize(Decimal('0.000001'))};"
        f"translate_usd={translate};review_usd={review};total_usd={amount}"
    )
    return amount, basis
