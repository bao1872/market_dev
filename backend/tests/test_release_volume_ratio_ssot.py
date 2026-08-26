"""[CHANGE-20260826] release_volume_ratio 双 owner 修复测试（修正版，非 tautology）。

两层测试：
  B1. SSOT 层：直接测试 build_momentum_history 的 SQZ_RELEASE 业务合同
      （独立验证 SSOT 自身正确，不依赖 consumer）。
  B2. Consumer wiring 层：用 monkeypatch 注入 sentinel，
      证明 _build_momentum_dimension 只转发 SSOT 结果、不自己重算；
      并独立验证 vol_divergence 阈值语义（放量释放等价 release/squeeze > 1.5）。

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_release_volume_ratio_ssot.py -v -p no:cacheprovider
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services import first_pyramid_service as fps
from app.strategy_assets.algorithms.features.sqzmom_lb import build_momentum_history


def _sqzmom(sqz_on: list[bool], sqz_off: list[bool], val: list[float]) -> dict:
    no_sqz = [not (so or sff) for so, sff in zip(sqz_on, sqz_off)]
    return {"sqzOn": sqz_on, "sqzOff": sqz_off, "noSqz": no_sqz, "val": val}


# ---------------------------------------------------------------------------
# B1. SSOT 层（直接测试 build_momentum_history，不经由任何 consumer）
# ---------------------------------------------------------------------------


def test_b1_exactly_one_release_on_transition():
    # sqzOn[T-1]=True, sqzOff[T]=True -> 恰好一个 SQZ_RELEASE
    n = 10
    sqz_on = [False] * (n - 2) + [True, False]
    sqz_off = [False] * (n - 1) + [True]
    val = [-1.0] * (n - 1) + [0.5]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), [100.0] * n, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert len(rel) == 1
    assert rel[0]["bar_index"] == n - 1


def test_b1_no_release_after_continued_sqzoff():
    # release 后继续 sqzOff，但 sqzOn=False -> 后续 bar 不再生成新 release
    n = 12
    sqz_on = [False] * (n - 4) + [True, True, False, False]
    sqz_off = [False] * (n - 2) + [True, True]
    val = [-1.0] * (n - 2) + [0.5, 0.6]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), [100.0] * n, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    # 只有 sqzOn[n-3] && sqzOff[n-2] 这一根 transition
    assert len(rel) == 1
    assert rel[0]["bar_index"] == n - 2


def test_b1_no_release_when_still_squeezing():
    # T 仍 sqzOn -> no release
    n = 10
    sqz_on = [False] * (n - 2) + [True, True]
    sqz_off = [False] * n
    val = [-1.0] * n
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), [100.0] * n, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert len(rel) == 0


def test_b1_no_release_when_no_sqz():
    n = 10
    sqz_on = [False] * n
    sqz_off = [False] * n
    val = [0.0] * n
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), [100.0] * n, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert len(rel) == 0


def test_b1_squeeze_length_correct():
    # 连续 3 根 sqzOn 后 release -> squeeze_length == 3
    n = 8
    sqz_on = [False] * 4 + [True, True, True, False]
    sqz_off = [False] * 7 + [True]
    val = [-1.0] * 7 + [0.5]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), [100.0] * n, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert len(rel) == 1
    assert rel[0]["squeeze_length"] == 3
    assert rel[0]["squeeze_start_index"] == 4


def test_b1_squeeze_period_volume_mean_correct():
    # squeeze 区间 [4,6] 量 = 100,100,200 -> mean = 133.333...
    n = 8
    sqz_on = [False] * 4 + [True, True, True, False]
    sqz_off = [False] * 7 + [True]
    val = [-1.0] * 7 + [0.5]
    vol = [50.0, 50, 50, 50, 100.0, 100.0, 200.0, 400.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    expected = (100.0 + 100.0 + 200.0) / 3.0
    assert abs(rel[0]["squeeze_period_volume_mean"] - expected) < 1e-6


def test_b1_ratio_is_squeeze_mean_over_volume():
    # squeeze mean = 100 (idx 0-4), vol[T]=500 -> 0.2
    n = 6
    sqz_on = [False] * 4 + [True, True]
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    vol = [100.0, 100.0, 100.0, 100.0, 100.0, 500.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert abs(rel[0]["release_volume_ratio"] - 0.2) < 1e-6


def test_b1_volume_le_zero_ratio_none():
    # vol[T] <= 0 -> ratio = None（但 squeeze_period_volume_mean 仍应算出）
    n = 6
    sqz_on = [False] * 4 + [True, True]
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    vol = [100.0, 100.0, 100.0, 100.0, 100.0, 0.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert rel[0]["release_volume_ratio"] is None
    assert rel[0]["squeeze_period_volume_mean"] is not None


# ---------------------------------------------------------------------------
# B2. Consumer wiring 层（monkeypatch 注入 sentinel，证明只转发不重算）
# ---------------------------------------------------------------------------


def _build_bbdf(n: int) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
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


def _build_bars(n: int, vol) -> pd.DataFrame:
    return pd.DataFrame({"volume": np.array(vol, dtype=float)}, index=pd.date_range("2026-01-01", periods=n, freq="B"))


def test_b2_forwards_ssot_without_recompute(monkeypatch):
    """_build_momentum_dimension 必须直接转发 SSOT 值，不自行重算。"""
    sentinel_ratio = 0.37
    sentinel_mean = 12345.0
    fake_mh = {
        "sqz_release_events": [
            {
                "type": "SQZ_RELEASE",
                "bar_index": 9,
                "squeeze_start_index": 8,
                "squeeze_length": 1,
                "squeeze_period_volume_mean": sentinel_mean,
                "release_volume_ratio": sentinel_ratio,
            }
        ]
    }

    def fake_build(sqzmom_result, vol_series, times=None):
        return fake_mh

    monkeypatch.setattr(fps, "build_momentum_history", fake_build)

    n = 10
    sqz_on = [False] * 8 + [True, False]  # T-1 sqzOn
    sqz_off = [False] * 9 + [True]       # T sqzOff
    val = [-1.0] * 9 + [0.5]
    dim = fps._build_momentum_dimension(
        _build_bbdf(n), _sqzmom(sqz_on, sqz_off, val), n, n - 1, None, _build_bars(n, [100.0] * n)
    )
    cf = dim.continuousFactors
    # 只转发，不重算
    assert cf["release_vs_squeeze_volume_ratio"] == sentinel_ratio
    assert cf["squeeze_period_volume_mean"] == sentinel_mean


def test_b2_no_release_day_both_none(monkeypatch):
    """T 不是 release 日 -> 两个字段均为 None（不读 T+1）。"""
    def fake_build(sqzmom_result, vol_series, times=None):
        return {"sqz_release_events": []}  # T 非 release

    monkeypatch.setattr(fps, "build_momentum_history", fake_build)

    n = 10
    sqz_on = [False] * 8 + [True, True]  # T 仍 sqzOn
    sqz_off = [False] * n
    val = [-1.0] * n
    dim = fps._build_momentum_dimension(
        _build_bbdf(n), _sqzmom(sqz_on, sqz_off, val), n, n - 1, None, _build_bars(n, [100.0] * n)
    )
    cf = dim.continuousFactors
    assert cf["release_vs_squeeze_volume_ratio"] is None
    assert cf["squeeze_period_volume_mean"] is None


def test_b2_vol_divergence_threshold_semantics(monkeypatch):
    """放量释放等价 release/squeeze > 1.5。

    canonical ratio = squeeze_mean / release_volume。
    原业务「放量释放」= release_volume > 1.5 * squeeze_mean
    ⟺ ratio < 1/1.5 ≈ 0.6667。
    """
    state = {"ratio": 0.50}

    def fake_build(sqzmom_result, vol_series, times=None):
        # 注入不同 ratio 的 SSOT，观察 vol_divergence 判定
        return {
            "sqz_release_events": [
                {
                    "type": "SQZ_RELEASE",
                    "bar_index": 9,
                    "squeeze_start_index": 8,
                    "squeeze_length": 1,
                    "squeeze_period_volume_mean": 100.0,
                    "release_volume_ratio": state["ratio"],
                }
            ]
        }

    monkeypatch.setattr(fps, "build_momentum_history", fake_build)

    n = 10
    sqz_on = [False] * 8 + [True, False]
    sqz_off = [False] * 9 + [True]
    val = [-1.0] * 9 + [0.5]

    # ratio = 0.50 (< 0.6667) -> 放量释放 TRUE
    state["ratio"] = 0.50
    dim = fps._build_momentum_dimension(
        _build_bbdf(n), _sqzmom(sqz_on, sqz_off, val), n, n - 1, None, _build_bars(n, [100.0] * n)
    )
    assert dim.continuousFactors["vol_divergence"] == "放量释放"

    # ratio = 0.80 (> 0.6667) -> 放量释放 FALSE
    state["ratio"] = 0.80
    dim = fps._build_momentum_dimension(
        _build_bbdf(n), _sqzmom(sqz_on, sqz_off, val), n, n - 1, None, _build_bars(n, [100.0] * n)
    )
    assert dim.continuousFactors["vol_divergence"] != "放量释放"
