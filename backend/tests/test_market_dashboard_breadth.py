"""Market Dashboard breadth 核心的纯数学单元测试（无 DB、无 fixture）。"""
from __future__ import annotations

import math

import pytest

from app.domain.market_dashboard.breadth import (
    WINDOWS,
    BreadthResult,
    MemberCloses,
    WindowBreadth,
    compute_breadth,
)


def _rising(n: int) -> list[float]:
    return [float(i + 1) for i in range(n)]  # 1,2,...,n（严格递增）


def _member(closes: list[float | None], member_id: str = "m") -> MemberCloses:
    return MemberCloses(member_id=member_id, closes=closes)


# ---------------------------------------------------------------
# 正常计算
# ---------------------------------------------------------------

def test_ma5_normal_computation() -> None:
    """closes 1..5 → MA5=3，close_T=5>3 → above；其余窗口长度不足。"""
    result = compute_breadth([_member(_rising(5))])
    assert result.member_count == 1
    w5 = result.windows[5]
    assert (w5.valid_count, w5.above_count, w5.ratio) == (1, 1, 1.0)
    for k in (10, 20, 50, 120):
        wk = result.windows[k]
        assert (wk.valid_count, wk.above_count) == (0, 0)
        assert wk.ratio is None


def test_ma120_exact_computation() -> None:
    """120 根严格递增序列 → MA120 = (1+120)/2 = 60.5，close_T=120 在线上。"""
    result = compute_breadth([_member(_rising(120))])
    w120 = result.windows[120]
    assert (w120.valid_count, w120.above_count) == (1, 1)
    assert w120.ratio == 1.0
    assert result.windows[20].valid_count == 1


# ---------------------------------------------------------------
# 分母独立性
# ---------------------------------------------------------------

def test_window_denominators_are_independent() -> None:
    """m1 有 120 根（全窗口可用），m2 只有 10 根（仅 MA5/MA10 可用）。"""
    long_member = _member(_rising(120), member_id="long")
    short_member = _member(_rising(10), member_id="short")
    result = compute_breadth([long_member, short_member])

    assert result.windows[5].valid_count == 2
    assert result.windows[10].valid_count == 2
    assert result.windows[20].valid_count == 1, "m2 不得进入 MA20 分母"
    assert result.windows[50].valid_count == 1
    assert result.windows[120].valid_count == 1, "m2 不得进入 MA120 分母"
    # m1 全程递增、m2 递增 → 可用者全部在线上
    for k in WINDOWS:
        assert result.windows[k].above_count == result.windows[k].valid_count


# ---------------------------------------------------------------
# 历史不足 / 无效输入
# ---------------------------------------------------------------

def test_insufficient_history_window_unavailable() -> None:
    """只有 4 根 → 连 MA5 都不可用。"""
    result = compute_breadth([_member(_rising(4))])
    for k in WINDOWS:
        wk = result.windows[k]
        assert wk.valid_count == 0
        assert wk.ratio is None, "valid_count==0 时 ratio 必须为 None 而非 0"


def test_none_nan_inf_do_not_enter_denominator() -> None:
    """最近 5 根含 None / NaN / inf 的成员不可用；同 scope 好成员仍计入。"""
    bad = [
        _member([1.0, 2.0, 3.0, 4.0, None], "bad-none"),
        _member([1.0, 2.0, 3.0, float("nan"), 5.0], "bad-nan"),
        _member([1.0, 2.0, 3.0, float("inf"), 5.0], "bad-inf"),
    ]
    good = _member(_rising(5), "good")
    result = compute_breadth([*bad, good])
    w5 = result.windows[5]
    assert w5.valid_count == 1, "无效成员不得进入分母"
    assert w5.above_count == 1
    assert w5.ratio == 1.0


def test_non_positive_prices_unusable() -> None:
    """0 或负价不可用，不得进入分母。"""
    result = compute_breadth(
        [
            _member([1.0, 2.0, 3.0, 0.0, 4.0], "zero"),
            _member([1.0, 2.0, 3.0, -4.0, 5.0], "negative"),
            _member(_rising(5), "good"),
        ]
    )
    assert result.windows[5].valid_count == 1
    # 但 return 只依赖最后两个 close：negative/zero 成员 T 日 return 部分仍可能有效
    # zero: C_T-1=0 不可用；negative: C_T-1=-4 不可用；good: r=5/4-1
    assert result.valid_return_count == 1
    assert result.equal_weight_return == 5.0 / 4.0 - 1.0


# ---------------------------------------------------------------
# close == MA 不算 above
# ---------------------------------------------------------------

def test_close_equal_to_ma_is_not_above() -> None:
    """常数序列 5×5 → MA5=5，close_T==MA → 不算 above，ratio=0.0（非 None）。"""
    result = compute_breadth([_member([5.0] * 5)])
    w5 = result.windows[5]
    assert w5.valid_count == 1
    assert w5.above_count == 0
    assert w5.ratio == 0.0


def test_partial_above_ratio() -> None:
    """两个可用成员，一个在线上一个在线下 → ratio=0.5。"""
    above = _member(_rising(5))            # 5 > 3 → above
    below = _member([5.0, 4.0, 3.0, 2.0, 1.0])  # 1 < 3 → below
    result = compute_breadth([above, below])
    w5 = result.windows[5]
    assert (w5.valid_count, w5.above_count, w5.ratio) == (2, 1, 0.5)


# ---------------------------------------------------------------
# 等权日收益
# ---------------------------------------------------------------

def test_equal_weight_return_multiple_members() -> None:
    """r1=0.1, r2=-0.1 → 等权 = 0.0。"""
    result = compute_breadth(
        [
            _member([100.0, 110.0], "a"),
            _member([200.0, 180.0], "b"),
        ]
    )
    assert result.member_count == 2
    assert result.valid_return_count == 2
    assert result.equal_weight_return == pytest.approx(0.0)


def test_equal_weight_return_none_when_all_invalid() -> None:
    """单 close 成员（无法算收益）→ equal_weight_return=None，valid_return_count=0。"""
    result = compute_breadth([_member([10.0], "a"), _member([20.0], "b")])
    assert result.valid_return_count == 0
    assert result.equal_weight_return is None


def test_return_excludes_none_nan_inf_tail() -> None:
    """最后两个 close 含 None/NaN/inf 的成员不计入 return。"""
    result = compute_breadth(
        [
            _member([10.0, None], "none"),
            _member([10.0, float("nan")], "nan"),
            _member([10.0, float("inf")], "inf"),
            _member([100.0, 150.0], "good"),
        ]
    )
    assert result.valid_return_count == 1
    assert result.equal_weight_return == 0.5


# ---------------------------------------------------------------
# 空 scope / 冻结字段
# ---------------------------------------------------------------

def test_empty_scope_ratio_none() -> None:
    result = compute_breadth([])
    assert result.member_count == 0
    assert result.valid_return_count == 0
    assert result.equal_weight_return is None
    for k in WINDOWS:
        wk = result.windows[k]
        assert (wk.valid_count, wk.above_count) == (0, 0)
        assert wk.ratio is None


def test_output_shape_frozen_contract() -> None:
    """输出只含冻结合同字段，窗口键完整。"""
    result = compute_breadth([_member(_rising(10))])
    assert isinstance(result, BreadthResult)
    assert set(result.windows.keys()) == set(WINDOWS)
    sample: WindowBreadth = result.windows[5]
    assert set(sample.__dataclass_fields__) == {
        "window", "valid_count", "above_count", "ratio",
    }
    assert set(result.__dataclass_fields__) == {
        "member_count", "valid_return_count", "equal_weight_return", "windows",
    }


def test_ratio_is_real_number_or_none() -> None:
    """ratio 只能是有限 float 或 None，不允许 NaN。"""
    result = compute_breadth([_member(_rising(5)), _member([5.0, 4.0, 3.0, 2.0, 1.0])])
    ratio = result.windows[5].ratio
    assert ratio is None or (isinstance(ratio, float) and math.isfinite(ratio))
