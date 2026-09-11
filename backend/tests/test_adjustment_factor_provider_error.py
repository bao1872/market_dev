"""[FACTOR-PROVIDER] 公司行为检测的 provider 故障语义（纯单元）。

为什么需要这组测试：``detect_company_action_change`` 的 ``None`` 语义是
「确实没有公司行为变化」。若把 provider 异常也吞成 ``None``，
「数据源挂了」与「没有除权」在返回值上无法区分，调用方会继续在**无法证明
freshness** 的 qfq 序列上算指标。

因此 provider 故障必须抛 :class:`CorporateActionProviderError`，由调用方自行决定
degraded / retry / fail-closed。
"""

from __future__ import annotations

import uuid
from typing import Any

import pandas as pd
import pytest

from app.services.adjustment_factor_service import (
    AdjustmentFactorService,
    CorporateActionProviderError,
)


class _FlakyAdapter:
    """get_xdxr_info 抛任意异常（模拟 TDX 故障）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_xdxr_info(self, symbol: str) -> Any:
        raise self._exc


class _EmptyAdapter:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def get_xdxr_info(self, symbol: str) -> Any:
        return self._df


@pytest.mark.asyncio
async def test_provider_error_raises_instead_of_returning_none() -> None:
    """provider 抛错必须抛出，绝不能返回 None（不得冒充「无公司行为」）。"""
    service = AdjustmentFactorService()
    adapter = _FlakyAdapter(RuntimeError("calling function error"))

    with pytest.raises(CorporateActionProviderError, match="xdxr provider unavailable"):
        await service.detect_company_action_change(
            None,  # type: ignore[arg-type]
            uuid.uuid4(),
            "600519",
            adapter,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_provider_error_is_chained_for_diagnosis() -> None:
    """必须保留原始异常（``raise ... from exc``），否则定位不到上游错误。"""
    service = AdjustmentFactorService()
    root = TimeoutError("tcp timeout")
    adapter = _FlakyAdapter(root)

    with pytest.raises(CorporateActionProviderError) as excinfo:
        await service.detect_company_action_change(
            None,  # type: ignore[arg-type]
            uuid.uuid4(),
            "600519",
            adapter,  # type: ignore[arg-type]
        )

    assert excinfo.value.__cause__ is root


@pytest.mark.asyncio
async def test_no_company_action_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真·无公司行为（xdxr 为空且 fingerprint 未变）→ 返回 None。"""
    service = AdjustmentFactorService()
    adapter = _EmptyAdapter(pd.DataFrame())
    # 让「已存 fingerprint」与空事件集合的 fingerprint（""）一致 → 无变化
    monkeypatch.setattr(service, "_get_stored_fingerprint", lambda iid: "")
    monkeypatch.setattr(service, "_store_fingerprint", lambda iid, fp: None)

    result = await service.detect_company_action_change(
        None,  # type: ignore[arg-type]
        uuid.uuid4(),
        "600519",
        adapter,  # type: ignore[arg-type]
    )

    assert result is None
