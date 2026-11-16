"""单期费用估算测试：预留口径可审计、配置缺失或非法失败关闭。
模拟场景：配置完整时按时长估算并写入依据；时长未知用配置上限并标注来源；
时长超上限拒绝；缺任一配置失败关闭；非法数值失败关闭。
"""
from decimal import Decimal

import pytest

from state import estimate

ENV = {
    "PODCAST_ESTIMATE_VERSION": "v1",
    "PODCAST_ESTIMATE_MAX_DURATION_SECS": "5400",
    "PODCAST_ESTIMATE_SCRIBE_USD_PER_HOUR": "0.116",
    "PODCAST_ESTIMATE_TRANSLATE_USD": "0.40",
    "PODCAST_ESTIMATE_REVIEW_USD": "0.40",
}


@pytest.fixture()
def configured(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


def test_estimate_uses_known_duration(configured):
    amount, basis = estimate.episode_estimate(3600)
    # 1 小时 Scribe 0.116 + 0.40 + 0.40
    assert amount == pytest.approx(Decimal("0.916000"))
    assert "duration_secs=3600" in basis
    assert "duration_source=episode" in basis
    assert "estimate_version=v1" in basis


def test_estimate_uses_cap_when_duration_unknown(configured):
    amount, basis = estimate.episode_estimate(None)
    # 5400s = 1.5h → 0.174 + 0.40 + 0.40
    assert amount == pytest.approx(Decimal("0.974000"))
    assert "max_duration_secs=5400" in basis
    assert "duration_source=cap" in basis


def test_duration_beyond_cap_is_rejected(configured):
    with pytest.raises(estimate.EstimateError, match="超出估算上限"):
        estimate.episode_estimate(7200)


@pytest.mark.parametrize("missing", list(ENV))
def test_missing_config_fails_closed(configured, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(estimate.EstimateError, match="缺少估算配置"):
        estimate.episode_estimate(1800)


@pytest.mark.parametrize("key,value", [
    ("PODCAST_ESTIMATE_SCRIBE_USD_PER_HOUR", "abc"),
    ("PODCAST_ESTIMATE_TRANSLATE_USD", "-1"),
    ("PODCAST_ESTIMATE_MAX_DURATION_SECS", "0"),
    ("PODCAST_ESTIMATE_MAX_DURATION_SECS", "x"),
])
def test_invalid_config_fails_closed(configured, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(estimate.EstimateError, match="非法|必须为正数"):
        estimate.episode_estimate(1800)


def test_non_finite_duration_fails_closed(configured):
    import math

    for bad in (float("nan"), float("inf")):
        with pytest.raises(estimate.EstimateError, match="时长非法"):
            estimate.episode_estimate(bad)


def test_zero_component_fails_closed(configured, monkeypatch):
    monkeypatch.setenv("PODCAST_ESTIMATE_TRANSLATE_USD", "0")
    with pytest.raises(estimate.EstimateError, match="必须为正数"):
        estimate.episode_estimate(1800)
