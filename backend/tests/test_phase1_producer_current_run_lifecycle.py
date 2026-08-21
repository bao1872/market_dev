"""[CHANGE-20260821-001 Phase 1] producer current-run lifecycle 纯单元测试。

验证 ``ensure_current_first_pyramid_history_run`` 作为 PRODUCER resolver 的硬边界：

1. 相同 canonical config → resolve 同一个 run（委托既有 create_history_run 幂等契约）
2. 无兼容 run → 通过既有创建契约建新 run（不硬编码 UUID）
3. algorithm_version / contract / output_bars rollover → resolve key 改变，不复用旧 run
4. 与 Review consumer resolver / readiness 完全独立（绝不调用）
5. 本阶段不计算 / 不修改 membership（不调用 create_history_run_items）
6. 默认 canonical config = 核心算法版本 + all_a_share + 250 bars + include_chip=False

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_phase1_producer_current_run_lifecycle.py -v -p no:cacheprovider
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.first_pyramid_history_run import SCOPE_ALL_A_SHARE
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION


def _make_run(
    run_id: uuid.UUID | None = None,
    algorithm_version: str = "1.0.0-core-split",
    scope: str = "all_a_share",
    parameter_hash: str = "ph",
) -> object:
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun

    return FirstPyramidHistoryRun(
        id=run_id or uuid.uuid4(),
        algorithm_version=algorithm_version,
        parameter_hash=parameter_hash,
        scope=scope,
        output_bars=250,
    )


class TestProducerCurrentRunLifecycle:
    """PRODUCER current-run resolver 边界（纯单元，不连库）。"""

    @pytest.mark.asyncio
    async def test_resolves_same_run_on_same_config(self):
        """相同 config 两次调用 → 返回同一 run（is_new=False），委托 create_history_run 幂等。"""
        from app.services.first_pyramid_history_service import (
            ensure_current_first_pyramid_history_run,
        )

        existing = _make_run()
        with patch(
            "app.services.first_pyramid_history_service.create_history_run",
            new=AsyncMock(return_value=(existing, False)),
        ) as mock_create:
            run1, is_new1 = await ensure_current_first_pyramid_history_run(MagicMock())
            run2, is_new2 = await ensure_current_first_pyramid_history_run(MagicMock())

        assert run1 is existing and run2 is existing
        assert is_new1 is False and is_new2 is False
        assert mock_create.await_count == 2

    @pytest.mark.asyncio
    async def test_creates_new_when_no_compatible_run(self):
        """无兼容 run → 通过既有创建契约建新 run（is_new=True），不硬编码生产 run id。"""
        from app.services.first_pyramid_history_service import (
            ensure_current_first_pyramid_history_run,
        )

        new_run = _make_run()
        with patch(
            "app.services.first_pyramid_history_service.create_history_run",
            new=AsyncMock(return_value=(new_run, True)),
        ):
            run, is_new = await ensure_current_first_pyramid_history_run(MagicMock())

        assert is_new is True
        assert run is new_run
        # 基于契约断言（而非硬编码历史 UUID）：返回对象必须反映请求的 canonical config，
        # 真正“不硬编码生产 run id”的证据来自 production source grep（PHASE 1 验证 = 0 次）。
        assert run.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION
        assert run.scope == SCOPE_ALL_A_SHARE
        assert run.parameter_hash == "ph"

    @pytest.mark.asyncio
    async def test_rollover_changes_resolve_key(self):
        """algorithm_version / contract(include_chip) rollover → resolve key 改变，旧 run 不复用。"""
        from app.services.first_pyramid_history_service import (
            FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            ensure_current_first_pyramid_history_run,
        )

        captured: dict = {}

        async def _fake_create(session, **kwargs):  # noqa: ANN001
            captured.clear()
            captured.update(kwargs)
            return _make_run(), True

        with patch(
            "app.services.first_pyramid_history_service.create_history_run",
            new=_fake_create,
        ):
            # 1) 默认 canonical config
            await ensure_current_first_pyramid_history_run(MagicMock())
            assert captured["algorithm_version"] == FIRST_PYRAMID_CORE_ALGORITHM_VERSION
            assert captured["scope"] == "all_a_share"
            assert captured["output_bars"] == 250
            assert captured["include_chip"] is False
            assert captured["instrument_ids"] == ()  # membership 不在此解决

            # 2) algorithm_version rollover → key 改变
            await ensure_current_first_pyramid_history_run(
                MagicMock(), algorithm_version="2.0.0-core-split"
            )
            assert captured["algorithm_version"] == "2.0.0-core-split"

            # 3) contract 经 parameter_hash 进入（include_chip 改变 → 不同 hash → 旧 run 不复用）
            await ensure_current_first_pyramid_history_run(MagicMock(), include_chip=True)
            assert captured["include_chip"] is True

    @pytest.mark.asyncio
    async def test_independent_of_review_resolver(self):
        """PRODUCER resolver 绝不调用 Review 的 resolver / readiness（FIX_DIRECTION=UPSTREAM_ONLY）。"""
        from app.services.first_pyramid_history_service import (
            ensure_current_first_pyramid_history_run,
        )

        new_run = _make_run()
        with patch(
            "app.services.first_pyramid_history_service.create_history_run",
            new=AsyncMock(return_value=(new_run, True)),
        ), patch(
            "app.services.review_orchestrator_service._resolve_canonical_history_source",
            new=AsyncMock(
                side_effect=AssertionError("Review resolver must not be called by producer")
            ),
        ) as mock_review_resolve, patch(
            "app.services.review_history_readiness_service.validate_canonical_history_run_readiness",
            new=AsyncMock(
                side_effect=AssertionError("Review readiness must not be called by producer")
            ),
        ) as mock_review_ready:
            await ensure_current_first_pyramid_history_run(MagicMock())

        mock_review_resolve.assert_not_called()
        mock_review_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_compute_membership(self):
        """Phase 1 不计算 / 不修改 participating set（不调用 create_history_run_items）。"""
        from app.services.first_pyramid_history_service import (
            ensure_current_first_pyramid_history_run,
        )

        new_run = _make_run()
        with patch(
            "app.services.first_pyramid_history_service.create_history_run",
            new=AsyncMock(return_value=(new_run, True)),
        ), patch(
            "app.services.first_pyramid_history_service.create_history_run_items",
            new=AsyncMock(
                side_effect=AssertionError("membership must not be solved in Phase 1")
            ),
        ) as mock_items:
            await ensure_current_first_pyramid_history_run(MagicMock())

        mock_items.assert_not_called()
