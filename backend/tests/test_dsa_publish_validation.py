"""DSA 发布校验测试。

覆盖：
- metric_filters 数值强制转换
- publish_run 发布规则（状态 + 成功结果数）
- _check_quality_gates 质量门禁规则
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException

from app.models.strategy_run import StrategyRun
from app.repositories.strategy_result_repository import dict_filters_to_metric_filters
from app.services.strategy_batch_service import StrategyBatchService


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


class TestPublishRunValidation:
    """publish_run 发布规则测试。"""

    @pytest.mark.asyncio
    async def test_publish_run_rejects_completed_with_zero_succeeded(
        self, db_session, test_selector_strategy
    ):
        """completed 运行若没有成功结果，publish_run 应拒绝。"""
        version = test_selector_strategy["version"]
        run = StrategyRun(
            strategy_version_id=version.id,
            run_type="scheduled",
            trade_date=date(2026, 6, 24),
            status="completed",
            input_overrides={},
            idempotency_key=f"test:{uuid.uuid4().hex}",
            attempt_no=1,
            succeeded_count=0,
            failed_count=0,
            total_instruments=100,
        )
        db_session.add(run)
        await db_session.flush()

        service = StrategyBatchService()
        with pytest.raises(ValueError, match="没有成功结果，禁止发布"):
            await service.publish_run(db_session, run.id)

    @pytest.mark.asyncio
    async def test_publish_run_accepts_partial_failed_with_succeeded(
        self, db_session, test_selector_strategy
    ):
        """partial_failed 运行只要有成功结果，publish_run 应允许发布。"""
        version = test_selector_strategy["version"]
        run = StrategyRun(
            strategy_version_id=version.id,
            run_type="scheduled",
            trade_date=date(2026, 6, 24),
            status="partial_failed",
            input_overrides={},
            idempotency_key=f"test:{uuid.uuid4().hex}",
            attempt_no=1,
            succeeded_count=80,
            failed_count=20,
            total_instruments=100,
        )
        db_session.add(run)
        await db_session.flush()

        service = StrategyBatchService()
        published = await service.publish_run(db_session, run.id)
        assert published.status == "published"
        assert published.succeeded_count == 80


class TestQualityGates:
    """_check_quality_gates 质量门禁测试。"""

    @pytest.mark.asyncio
    async def test_quality_gate_passes_partial_failed_with_succeeded(self):
        """partial_failed + succeeded_count > 0 应通过门禁。"""
        run = StrategyRun(
            status="partial_failed",
            succeeded_count=1,
            failed_count=99,
            total_instruments=100,
        )
        service = StrategyBatchService()
        assert await service._check_quality_gates(run) is True

    @pytest.mark.asyncio
    async def test_quality_gate_passes_completed_with_failed_but_succeeded(self):
        """completed + failed_count > 0 但 succeeded_count > 0 仍应通过门禁。"""
        run = StrategyRun(
            status="completed",
            succeeded_count=95,
            failed_count=5,
            total_instruments=100,
        )
        service = StrategyBatchService()
        assert await service._check_quality_gates(run) is True

    @pytest.mark.asyncio
    async def test_quality_gate_rejects_completed_with_zero_succeeded(self):
        """completed 但 succeeded_count == 0 不应通过门禁。"""
        run = StrategyRun(
            status="completed",
            succeeded_count=0,
            failed_count=0,
            total_instruments=100,
        )
        service = StrategyBatchService()
        assert await service._check_quality_gates(run) is False

    @pytest.mark.asyncio
    async def test_quality_gate_rejects_failed_status(self):
        """failed 状态不应通过门禁。"""
        run = StrategyRun(
            status="failed",
            succeeded_count=0,
            failed_count=100,
            total_instruments=100,
        )
        service = StrategyBatchService()
        assert await service._check_quality_gates(run) is False
