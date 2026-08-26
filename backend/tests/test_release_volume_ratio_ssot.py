"""[CHANGE-20260826] SqueezeVolumeFacts 单 owner 修复测试（非 tautology）。

两层测试：
  B1. SSOT 层：直接测试 build_momentum_history 的 SqueezeVolumeFacts 业务合同
      （独立验证 SSOT 自身正确，不依赖 consumer）。
      覆盖两种时态：当前仍 sqzOn（mean 存在, ratio=None）与刚 release（mean+ratio）。
  B2. Consumer wiring 层：用 monkeypatch 注入 daily_state[T] sentinel，
      证明 _build_momentum_dimension 只转发 SSOT 结果、不自己重算；
      并独立验证 vol_divergence 阈值语义（缩量挤压可达；放量释放等价 release/squeeze>1.5）。

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
    # squeeze 区间 = idx 4 单根 sqzOn, mean=100, vol[T=5]=500 -> 0.2
    n = 6
    sqz_on = [False] * 4 + [True, False]  # T=5 为正式 release(sqzOff), sqzOn[5]=False
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    vol = [100.0, 100.0, 100.0, 100.0, 100.0, 500.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert abs(rel[0]["release_volume_ratio"] - 0.2) < 1e-6


def test_b1_volume_le_zero_ratio_none():
    # vol[T] <= 0 -> ratio = None（但 squeeze_period_volume_mean 仍应算出）
    n = 6
    sqz_on = [False] * 4 + [True, False]  # T=5 为正式 release, sqzOn[5]=False
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    vol = [100.0, 100.0, 100.0, 100.0, 100.0, 0.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert rel[0]["release_volume_ratio"] is None
    assert rel[0]["squeeze_period_volume_mean"] is not None


def test_b1_active_squeeze_mean_present_ratio_none():
    # CASE 1：T 仍 sqzOn（连续 3 日 squeeze，idx 0-2 量 100/100/200）
    # daily_state[T] 应有 squeeze_period_volume_mean=mean, release_volume_ratio=None
    n = 4
    sqz_on = [True, True, True, True]   # T 仍 sqzOn
    sqz_off = [False] * n
    val = [-1.0] * n
    vol = [100.0, 100.0, 200.0, 150.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    # 无 release 事件
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert len(rel) == 0
    # daily_state[T=3]（仍在 squeeze）
    ds = mh["daily_state"][3]
    expected = (100.0 + 100.0 + 200.0 + 150.0) / 4.0
    assert ds["squeeze_period_volume_mean"] is not None
    assert abs(ds["squeeze_period_volume_mean"] - expected) < 1e-6
    assert ds["release_volume_ratio"] is None


def test_b1_release_day_mean_from_prior_squeeze():
    # CASE 2：T 刚 release，mean 来自 T-1 前的连续 squeeze 区间（不含 T 当日量）
    n = 6
    sqz_on = [False, True, True, True, False, False]  # sqzOn idx 1-3, release T=4
    sqz_off = [False, False, False, False, True, False]
    val = [-1.0, -1.0, -1.0, -1.0, 0.5, 0.6]
    vol = [999.0, 100.0, 100.0, 200.0, 500.0, 999.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    ds = mh["daily_state"][4]
    expected = (100.0 + 100.0 + 200.0) / 3.0
    assert ds["squeeze_period_volume_mean"] is not None
    assert abs(ds["squeeze_period_volume_mean"] - expected) < 1e-6
    assert ds["release_volume_ratio"] is not None
    assert abs(ds["release_volume_ratio"] - (expected / 500.0)) < 1e-6


def test_b1_release_second_day_both_none():
    # release 后第二日仍 sqzOff（无 sqzOn 前置 transition）-> both None
    n = 6
    sqz_on = [False, True, True, True, False, False]  # release T=4, T=5 仍 sqzOff
    sqz_off = [False, False, False, False, True, True]
    val = [-1.0, -1.0, -1.0, -1.0, 0.5, 0.6]
    vol = [999.0, 100.0, 100.0, 200.0, 500.0, 999.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    ds = mh["daily_state"][5]
    assert ds["squeeze_period_volume_mean"] is None
    assert ds["release_volume_ratio"] is None


def test_b1_event_projection_matches_daily_state():
    # SQZ_RELEASE event 必须投影同一 daily_state[T] 事实（禁止重算）
    n = 8
    sqz_on = [False] * 4 + [True, True, True, False]
    sqz_off = [False] * 7 + [True]
    val = [-1.0] * 7 + [0.5]
    vol = [50.0, 50, 50, 50, 100.0, 100.0, 200.0, 400.0]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"][0]
    ds = mh["daily_state"][7]
    assert rel["squeeze_period_volume_mean"] == ds["squeeze_period_volume_mean"]
    assert rel["release_volume_ratio"] == ds["release_volume_ratio"]


def test_b1_event_independent_of_volume_none():
    # PHASE B(1): vol_arr=None 时 SQZ_RELEASE event 仍生成，身份正确，量能事实=None，无异常
    n = 6
    sqz_on = [False] * 4 + [True, False]  # T=5 为正式 release
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), None, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"]
    assert len(rel) == 1
    assert rel[0]["squeeze_start_index"] == 4
    assert rel[0]["squeeze_length"] == 1
    assert rel[0]["squeeze_period_volume_mean"] is None
    assert rel[0]["release_volume_ratio"] is None
    # daily_state 也不应包含量能事实（无异常）
    assert mh["daily_state"][5]["squeeze_period_volume_mean"] is None


def test_b1_event_identity_unaffected_by_nan_volume():
    # PHASE B(2): squeeze history 含 NaN 不影响 window identity
    n = 6
    sqz_on = [False] * 4 + [True, False]
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    vol = [np.nan, 100.0, 100.0, 100.0, 100.0, 500.0]  # idx0 NaN（在 squeeze 区间外）
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"][0]
    assert rel["squeeze_start_index"] == 4
    assert rel["squeeze_length"] == 1
    assert rel["release_volume_ratio"] is not None  # 区间 [4:5]=[100] → mean/500


def test_b1_event_release_volume_nan_ratio_none():
    # PHASE B(3): release volume=NaN → event 存在, mean 可存在, ratio=None
    n = 6
    sqz_on = [False] * 4 + [True, False]
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    vol = [100.0, 100.0, 100.0, 100.0, 100.0, np.nan]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    rel = [e for e in mh["sqz_release_events"] if e["type"] == "SQZ_RELEASE"][0]
    assert rel["squeeze_start_index"] == 4
    assert rel["squeeze_period_volume_mean"] is not None
    assert rel["release_volume_ratio"] is None


def test_b1_active_squeeze_volume_none_mean_none():
    # PHASE B(4): active squeeze + volume unavailable → state 仍合法, mean=None
    n = 4
    sqz_on = [True, True, True, True]
    sqz_off = [False] * n
    val = [-1.0] * n
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), None, times=[str(i) for i in range(n)])
    ds = mh["daily_state"][3]
    assert ds["squeeze_period_volume_mean"] is None
    assert ds["release_volume_ratio"] is None
    assert ds["volatility_phase"] == "squeeze_on"


def test_b1_valid_volume_active_and_release_correct():
    # PHASE B(5): 有效 volume → active squeeze mean 正确, release mean/ratio 正确
    n = 6
    sqz_on = [False] * 4 + [True, False]
    sqz_off = [False] * 5 + [True]
    val = [-1.0] * 5 + [0.5]
    vol = [100.0, 100.0, 100.0, 100.0, 100.0, 500.0]  # squeeze 区间 [4:5]=[100]
    mh = build_momentum_history(_sqzmom(sqz_on, sqz_off, val), vol, times=[str(i) for i in range(n)])
    # active squeeze at T=4 (sqzOn)
    ds4 = mh["daily_state"][4]
    assert abs(ds4["squeeze_period_volume_mean"] - 100.0) < 1e-6
    assert ds4["release_volume_ratio"] is None
    # release at T=5
    ds5 = mh["daily_state"][5]
    assert abs(ds5["squeeze_period_volume_mean"] - 100.0) < 1e-6
    assert abs(ds5["release_volume_ratio"] - (100.0 / 500.0)) < 1e-6


# ---------------------------------------------------------------------------
# B2. Consumer wiring 层（monkeypatch 注入 daily_state sentinel，证明只转发不重算）
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
    """_build_momentum_dimension 必须直接转发 SSOT daily_state[T] 值，不自行重算。"""
    sentinel_ratio = 0.37
    sentinel_mean = 12345.0

    def fake_build(sqzmom_result, vol_series, times=None):
        # 注入 daily_state[9]（last_bar_index=9）的 sentinel 事实
        daily_state = [{} for _ in range(9)] + [{
            "squeeze_period_volume_mean": sentinel_mean,
            "release_volume_ratio": sentinel_ratio,
        }]
        return {"daily_state": daily_state, "sqz_release_events": []}

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


def test_b2_active_squeeze_shrink_branch_reachable(monkeypatch):
    """缩量挤压分支必须可达：last_sqz_on=True 且 squeeze_period_volume_mean 存在，
    且 volume_percentile_20 < 20。"""
    def fake_build(sqzmom_result, vol_series, times=None):
        # T 仍 sqzOn -> daily_state[T] 有 mean, ratio=None
        daily_state = [{} for _ in range(9)] + [{
            "squeeze_period_volume_mean": 100.0,
            "release_volume_ratio": None,
        }]
        return {"daily_state": daily_state, "sqz_release_events": []}

    monkeypatch.setattr(fps, "build_momentum_history", fake_build)

    n = 10
    sqz_on = [False] * 8 + [True, True]  # T 仍 sqzOn
    sqz_off = [False] * n
    val = [-1.0] * n
    # vc_series：volume_percentile_20 < 20 触发缩量挤压
    vc_df = pd.DataFrame({"volume_percentile_20": [10.0], "readiness": [True]})
    dim = fps._build_momentum_dimension(
        _build_bbdf(n), _sqzmom(sqz_on, sqz_off, val), n, n - 1, vc_df, _build_bars(n, [100.0] * n)
    )
    cf = dim.continuousFactors
    assert cf["squeeze_period_volume_mean"] == 100.0
    assert cf["release_vs_squeeze_volume_ratio"] is None
    assert cf["vol_divergence"] == "缩量挤压"


def test_b2_no_release_day_both_none(monkeypatch):
    """T 不是 release 日且无 squeeze -> 两个字段均为 None。"""
    def fake_build(sqzmom_result, vol_series, times=None):
        return {"daily_state": [{} for _ in range(10)], "sqz_release_events": []}

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
        daily_state = [{} for _ in range(9)] + [{
            "squeeze_period_volume_mean": 100.0,
            "release_volume_ratio": state["ratio"],
        }]
        return {"daily_state": daily_state, "sqz_release_events": []}

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
