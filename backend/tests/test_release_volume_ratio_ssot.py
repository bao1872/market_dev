"""[CHANGE-20260826] release_volume_ratio 双 owner 修复测试。

验证 _build_momentum_dimension 不再自行实现 squeeze 区间寻找 + 反方向 ratio，
而是消费 build_momentum_history 唯一 SSOT 的 SQZ_RELEASE.release_volume_ratio。

正式 contract（build_momentum_history）：
    SQZ_RELEASE 触发: sqzOn[t-1]==True && sqzOff[t]==True
    release_volume_ratio = squeeze_period_mean_volume / vol[t]   (squeeze 均量在分子)

旧实现错误：ratio = vol[t] / squeeze_mean  (分子分母相反)，且自行寻找 squeeze 区间。

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_release_volume_ratio_ssot.py -v -p no:cacheprovider
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.first_pyramid_service import _build_momentum_dimension
from app.strategy_assets.algorithms.features.sqzmom_lb import build_momentum_history


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="B")


def _bars(n: int, vol) -> pd.DataFrame:
    return pd.DataFrame({"volume": np.array(vol, dtype=float)}, index=_dates(n))


def _bbdf(n: int) -> pd.DataFrame:
    idx = _dates(n)
    return pd.DataFrame(
        {
            "bb_width": [0.1] * n,
            "bb_pos": [0.5] * n,
            "bb_position": [0.5] * n,
            "bb_upper": [1.0] * n,
            "bb_lower": [0.0] * n,
            "bb_mid": [0.5] * n,
        },
        index=idx,
    )


def _sqzmom(sqz_on: list[bool], sqz_off: list[bool], val: list[float]) -> dict:
    no_sqz = [not (so or sff) for so, sff in zip(sqz_on, sqz_off)]
    return {"sqzOn": sqz_on, "sqzOff": sqz_off, "noSqz": no_sqz, "val": val}


def _dim_release(sqz_on, sqz_off, val, vol):
    n = len(vol)
    res = _sqzmom(sqz_on, sqz_off, val)
    dim = _build_momentum_dimension(_bbdf(n), res, n, n - 1, None, _bars(n, vol))
    return dim.continuousFactors["release_vs_squeeze_volume_ratio"]


def _ssot_release(sqz_on, sqz_off, val, vol):
    n = len(vol)
    res = _sqzmom(sqz_on, sqz_off, val)
    mh = build_momentum_history(res, list(np.array(vol, dtype=float)), times=[str(d) for d in _dates(n)])
    for ev in mh["sqz_release_events"]:
        if ev["bar_index"] == n - 1:  # 只认当前 T 日
            return ev["release_volume_ratio"]
    return None


@pytest.mark.parametrize(
    "sqz_on,sqz_off,val,vol",
    [
        # T-1 sqzOn, T sqzOff -> 恰好一个 release
        ([False] * 8 + [True, True], [False] * 9 + [True], [-1.0] * 9 + [0.5], [100.0] * 10),
        # T 仍 sqzOn
        ([False] * 8 + [True, True], [False] * 10, [-1.0] * 10, [100.0] * 10),
        # 无 squeeze
        ([False] * 10, [False] * 10, [0.0] * 10, [100.0] * 10),
        # 连续 sqzOff: 仅第一次 sqzOn->sqzOff transition 是 release, T 非 release -> None
        ([False] * 5 + [True, True, True], [False] * 5 + [True, True, True], [-1.0] * 5 + [0.5, 0.6, 0.7], [100.0] * 8),
    ],
)
def test_dimension_consumes_ssot(sqz_on, sqz_off, val, vol):
    dim = _dim_release(sqz_on, sqz_off, val, vol)
    ssot = _ssot_release(sqz_on, sqz_off, val, vol)
    if ssot is None:
        assert dim is None, f"expected None, got {dim}"
    else:
        assert dim is not None, "dimension must consume SSOT release ratio"
        assert abs(dim - ssot) < 1e-6, f"dim={dim} != ssot={ssot}"


def test_ratio_direction_is_squeeze_mean_over_vol():
    # squeeze mean = 100 (indices 0-4), vol[T]=500 -> 0.2
    n = 6
    sqz_on = [False] * 4 + [True, True]
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    vol = [100.0, 100.0, 100.0, 100.0, 100.0, 500.0]
    ssot = _ssot_release(sqz_on, sqz_off, val, vol)
    dim = _dim_release(sqz_on, sqz_off, val, vol)
    assert abs(ssot - 0.2) < 1e-6, f"SSOT ratio should be 0.2, got {ssot}"
    # 旧实现方向为 vol[T]/squeeze_mean = 5.0; 修复后必须为 0.2
    assert abs(dim - 0.2) < 1e-6, f"dimension ratio must be 0.2 (squeeze_mean/vol), got {dim}"


def test_no_t_plus_one_read():
    # T 不是 release, 但 T+? 也无意义; 确认只认 T 日, 不向前借更早 release
    n = 7
    sqz_on = [False] * 4 + [True, True, False]  # idx4,5 sqzOn, idx6 noSqz
    sqz_off = [False] * 6 + [True]  # idx6 sqzOff but idx5 sqzOn -> idx6 release
    val = [-1.0] * 6 + [0.5]
    vol = [100.0] * 7
    # sqzOn[5]=True, sqzOff[6]=True -> idx6 IS release (T=last bar)
    ssot = _ssot_release(sqz_on, sqz_off, val, vol)
    dim = _dim_release(sqz_on, sqz_off, val, vol)
    assert ssot is not None and abs(dim - ssot) < 1e-6
