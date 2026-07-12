"""测试 data_access.py — 无泄漏、白名单、dtype。"""
from __future__ import annotations

import pytest

from app.research.regime_discovery.data_access import (
    FEATURE_MATRIX_COLUMNS,
    FORBIDDEN_PREFIXES,
    _dtype_map,
)
from app.research.regime_discovery.feature_builder import validate_no_leakage


class TestValidateNoLeakage:
    def test_blocks_hindsight_prefix(self):
        with pytest.raises(ValueError, match="hindsight_"):
            validate_no_leakage(["hindsight_dsa_finalized_segment"])

    def test_blocks_label_prefix(self):
        with pytest.raises(ValueError, match="label_"):
            validate_no_leakage(["label_future_return_5d"])

    def test_blocks_amount_prefix(self):
        with pytest.raises(ValueError, match="amount"):
            validate_no_leakage(["amount_traded"])

    def test_allows_causal(self):
        # 不抛异常即通过
        validate_no_leakage(["causal_atr", "causal_bb_percent_b"])

    def test_allows_confirmed_delay(self):
        validate_no_leakage(["confirmed_delay_confirmed_swing_high"])


class TestFeatureMatrixColumns:
    def test_no_forbidden_prefix(self):
        for col in FEATURE_MATRIX_COLUMNS:
            for prefix in FORBIDDEN_PREFIXES:
                assert not col.startswith(prefix), f"禁止列 {col} 出现在白名单"

    def test_contains_metadata(self):
        assert "instrument_id" in FEATURE_MATRIX_COLUMNS
        assert "symbol" in FEATURE_MATRIX_COLUMNS
        assert "trade_date" in FEATURE_MATRIX_COLUMNS

    def test_contains_causal(self):
        assert "causal_atr" in FEATURE_MATRIX_COLUMNS
        assert "causal_dsa_confirmed_direction" in FEATURE_MATRIX_COLUMNS

    def test_contains_confirmed_delay(self):
        assert "confirmed_delay_confirmed_swing_high" in FEATURE_MATRIX_COLUMNS

    def test_excludes_hindsight(self):
        assert "hindsight_dsa_finalized_segment" not in FEATURE_MATRIX_COLUMNS

    def test_excludes_label(self):
        assert "label_future_return_5d" not in FEATURE_MATRIX_COLUMNS


class TestDtypeMap:
    def test_float_features_use_float32(self):
        dtypes = _dtype_map()
        assert dtypes["causal_atr"] == "float32"
        assert dtypes["causal_bb_percent_b"] == "float32"

    def test_trade_date_is_datetime(self):
        dtypes = _dtype_map()
        assert dtypes["trade_date"] == "datetime64[ns]"

    def test_symbol_is_string(self):
        dtypes = _dtype_map()
        assert dtypes["symbol"] == "string"
