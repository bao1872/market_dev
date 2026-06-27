"""DSA 发布前硬校验 + metric_filters 数值转换测试。

测试覆盖（TDD - 先写失败测试，再实施）：
1. validate_dsa_metrics: 缺字段 / NaN / Inf / 合法
2. dict_filters_to_metric_filters: 非数值字符串 / NaN 字符串

对应 advice.md 第二节"应增加发布前硬校验"与第三节"强制转换数值"。
"""
import math

import pytest
from fastapi import HTTPException

from app.repositories.strategy_result_repository import dict_filters_to_metric_filters
from app.services.strategy_batch_service import (
    InvalidStrategyResult,
    REQUIRED_DSA_METRICS,
    validate_dsa_metrics,
)


def _valid_dsa_metrics() -> dict:
    """构造完整合法的 DSA metrics（7 个必填字段全部存在且有限）。"""
    return {
        "dsa_dir_bars": 60,
        "vwap_ret_avg": 0.05,
        "vwap_ret_total": 1.2,
        "offset_mean": 0.03,
        "offset_std": 0.02,
        "offset_variance_rate": 0.5,
        "offset_percentile": 75.0,
    }


class TestValidateDsaMetrics:
    """validate_dsa_metrics 单元测试。"""

    def test_validate_dsa_metrics_missing_field(self):
        # 缺 offset_std，应抛 InvalidStrategyResult 且消息含 "offset_std"
        metrics = _valid_dsa_metrics()
        del metrics["offset_std"]
        with pytest.raises(InvalidStrategyResult) as exc_info:
            validate_dsa_metrics(metrics)
        assert "offset_std" in str(exc_info.value)

    def test_validate_dsa_metrics_nan(self):
        # offset_mean 为 NaN，应抛 InvalidStrategyResult
        metrics = _valid_dsa_metrics()
        metrics["offset_mean"] = float("nan")
        with pytest.raises(InvalidStrategyResult) as exc_info:
            validate_dsa_metrics(metrics)
        assert "offset_mean" in str(exc_info.value)

    def test_validate_dsa_metrics_inf(self):
        # vwap_ret_avg 为 Inf，应抛 InvalidStrategyResult
        metrics = _valid_dsa_metrics()
        metrics["vwap_ret_avg"] = float("inf")
        with pytest.raises(InvalidStrategyResult) as exc_info:
            validate_dsa_metrics(metrics)
        assert "vwap_ret_avg" in str(exc_info.value)

    def test_validate_dsa_metrics_valid(self):
        # 完整合法 metrics，不应抛异常
        metrics = _valid_dsa_metrics()
        validate_dsa_metrics(metrics)  # 不抛即通过


class TestDictFiltersToMetricFilters:
    """dict_filters_to_metric_filters 数值转换单元测试。"""

    def test_dict_filters_to_metric_filters_non_numeric(self):
        # 传字符串 "abc"，应抛 HTTPException 422
        filters = [{"metric_key": "dsa_dir_bars", "operator": "gte", "value": "abc"}]
        with pytest.raises(HTTPException) as exc_info:
            dict_filters_to_metric_filters(filters)
        assert exc_info.value.status_code == 422

    def test_dict_filters_to_metric_filters_nan(self):
        # 传字符串 "nan"，float("nan") 非有限数值，应抛 HTTPException 422
        filters = [{"metric_key": "dsa_dir_bars", "operator": "gte", "value": "nan"}]
        with pytest.raises(HTTPException) as exc_info:
            dict_filters_to_metric_filters(filters)
        assert exc_info.value.status_code == 422
