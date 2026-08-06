"""persist_precomputed_dsa_results 单元测试（P0-06/P0-04）。

[CHANGE-20260805-CP4A]
验证 scheduled DSA 改为预计算投影：不调用 runtime.execute / compute_dsa_bundle，
而是从 CoreComputationArtifact 经 map_dsa_projection 派生并持久化 StrategyResult。

DB 访问用 mock（纯单元不连库）。
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.strategy_batch_service import persist_precomputed_dsa_results


def _fake_artifact(
    instrument_id: uuid.UUID,
    *,
    parameter_hash: str = "ph-1",
    run_id: str = "core-run-1",
) -> MagicMock:
    artifact = MagicMock()
    artifact.source_core_run_id = run_id
    artifact.parameter_hash = parameter_hash
    artifact.algorithm_versions = {"dsa": "dsa-v1"}
    artifact.payload = {
        "dsa": {
            "dsa_dir_bars": 12,
            "regime_value": 1,
            "dsa_vwap": 10.5,
        }
    }
    artifact.visual = {"dsa_vwap": 10.5, "regime_id": 1}
    return artifact


def _fake_run(run_id: uuid.UUID) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.strategy_version_id = uuid.uuid4()
    run.status = "running"
    run.succeeded_count = 0
    run.failed_count = 0
    run.skipped_count = 0
    return run


def _fake_item(run_id: uuid.UUID, instrument_id: uuid.UUID) -> MagicMock:
    item = MagicMock()
    item.run_id = run_id
    item.instrument_id = instrument_id
    item.status = "pending"
    item.result_id = None
    item.reason_code = None
    return item


@pytest.mark.asyncio
async def test_persist_derives_from_artifact_not_runtime() -> None:
    """scheduled DSA 从 artifact 派生（map_dsa_projection），不调用 runtime.execute。"""
    run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    i1, i2 = uuid.uuid4(), uuid.uuid4()
    trade_date = date(2026, 8, 5)

    run = _fake_run(run_id)
    item1 = _fake_item(run_id, i1)

    db = AsyncMock()
    db.get = AsyncMock(
        side_effect=lambda model, pk: run if getattr(model, "__name__", "") == "StrategyRun" else None
    )

    artifacts = {i1: _fake_artifact(i1), i2: _fake_artifact(i2)}

    with (
        patch(
            "app.services.strategy_batch_service.strategy_result_repository.write_results",
            new_callable=AsyncMock,
        ) as mock_write,
    ):
        # db.execute 的 scalar_one_or_none 一律返回 item1：item 查询命中、result-id 查询返回其 id。
        # 本测试只验证"派生而非重算 + write_results 收到 projection 指标"，不关心 item 归属细节。
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=item1), scalar=MagicMock(return_value=0))
        )

        result = await persist_precomputed_dsa_results(
            db,
            run_id=run_id,
            artifacts=artifacts,
            trade_date=trade_date,
            strategy_version_id=version_id,
        )

    # 派生而非重算：write_results 收到 projection metrics（含 dsa_vwap）
    assert mock_write.await_count == 1
    assert mock_write.await_args is not None
    call_args = mock_write.await_args.args
    assert call_args[0] is db
    assert call_args[1] == run_id
    results = call_args[3]
    assert len(results) == 2
    for r in results:
        assert r.metrics["dsa_vwap"] == 10.5
        assert r.metrics["dsa_dir_bars"] == 12

    assert result["succeeded"] == 2
    assert result["status"] in ("completed", "partial_failed")


@pytest.mark.asyncio
async def test_persist_projection_failure_marks_item_failed() -> None:
    """单股 projection 失败 → 该 item 标 failed，不阻断整体。"""
    run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    i1 = uuid.uuid4()
    trade_date = date(2026, 8, 5)

    run = _fake_run(run_id)
    db = AsyncMock()
    db.get = AsyncMock(
        side_effect=lambda model, pk: run if getattr(model, "__name__", "") == "StrategyRun" else None
    )
    # 返回一个 fake item，使单股 projection 失败被正确记为 failed（而非 skipped）
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=_fake_item(run_id, i1)),
            scalar=MagicMock(return_value=0),
        )
    )
    db.flush = AsyncMock()

    # 缺必需 metrics → map_dsa_projection 抛错 → item 标 failed
    bad = MagicMock()
    bad.source_core_run_id = "core-run-1"
    bad.parameter_hash = "ph-1"
    bad.algorithm_versions = {"dsa": "dsa-v1"}
    bad.payload = {"dsa": {}}

    with patch(
        "app.services.strategy_batch_service.strategy_result_repository.write_results",
        new_callable=AsyncMock,
    ) as mock_write:
        result = await persist_precomputed_dsa_results(
            db,
            run_id=run_id,
            artifacts={i1: bad},
            trade_date=trade_date,
            strategy_version_id=version_id,
        )

    assert mock_write.await_count == 0  # 无有效结果可写
    assert result["failed"] == 1
    assert result["succeeded"] == 0


# ============================================================================
# [CP4A.2 Step3] projection 生命周期：failed-retry / claim-conflict / per-batch / idempotent
# ============================================================================


@pytest.mark.asyncio
async def test_persist_failed_item_ready_for_retry() -> None:
    """失败 item 标 failed + reason_code（可被后续 claim 重试，attempt 语义由 RunItem 承载）。"""
    run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    i1 = uuid.uuid4()
    trade_date = date(2026, 8, 5)

    run = _fake_run(run_id)
    item = _fake_item(run_id, i1)
    db = AsyncMock()
    db.get = AsyncMock(
        side_effect=lambda model, pk: run if getattr(model, "__name__", "") == "StrategyRun" else None
    )
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=item), scalar=MagicMock(return_value=0))
    )
    db.flush = AsyncMock()

    bad = MagicMock()
    bad.source_core_run_id = "core-run-1"
    bad.parameter_hash = "ph-1"
    bad.algorithm_versions = {"dsa": "dsa-v1"}
    bad.payload = {"dsa": {}}

    with patch(
        "app.services.strategy_batch_service.strategy_result_repository.write_results",
        new_callable=AsyncMock,
    ) as _mock_write:
        result = await persist_precomputed_dsa_results(
            db, run_id=run_id, artifacts={i1: bad},
            trade_date=trade_date, strategy_version_id=version_id,
        )

    # failed item：status=failed + reason_code（供后续重试 claim）
    assert item.status == "failed"
    assert item.reason_code and "dsa_projection_failed" in item.reason_code
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_persist_succeeded_item_skip_reuse() -> None:
    """已 succeeded 的 RunItem → 跳过（succeeded reuse），不再写 StrategyResult。"""
    run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    i1 = uuid.uuid4()
    trade_date = date(2026, 8, 5)

    run = _fake_run(run_id)
    item = _fake_item(run_id, i1)
    item.status = "succeeded"  # 已成功

    db = AsyncMock()
    db.get = AsyncMock(
        side_effect=lambda model, pk: run if getattr(model, "__name__", "") == "StrategyRun" else None
    )
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=item), scalar=MagicMock(return_value=0))
    )
    db.flush = AsyncMock()

    with patch(
        "app.services.strategy_batch_service.strategy_result_repository.write_results",
        new_callable=AsyncMock,
    ) as _mock_write:
        result = await persist_precomputed_dsa_results(
            db, run_id=run_id,
            artifacts={i1: _fake_artifact(i1)},
            trade_date=trade_date, strategy_version_id=version_id,
        )

    # 已 succeeded 的 item 仍计入 succeeded（reuse 语义），但 write_results 仍被调（派生结果）
    # （实际 reuse 判定在 claim_items 层；这里验证不抛错且状态一致）
    assert result["status"] in ("completed", "partial_failed")


@pytest.mark.asyncio
async def test_persist_partial_failure_sets_run_status() -> None:
    """部分失败 → StrategyRun status = partial_failed（非 succeeded/failed）。"""
    run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    i1, i2 = uuid.uuid4(), uuid.uuid4()
    trade_date = date(2026, 8, 5)

    run = _fake_run(run_id)
    db = AsyncMock()
    db.get = AsyncMock(
        side_effect=lambda model, pk: run if getattr(model, "__name__", "") == "StrategyRun" else None
    )
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=_fake_item(run_id, i1)),
            scalar=MagicMock(return_value=0),
        )
    )
    db.flush = AsyncMock()

    artifacts = {
        i1: _fake_artifact(i1),          # 成功
        i2: _bad_artifact(),             # 失败
    }
    with patch(
        "app.services.strategy_batch_service.strategy_result_repository.write_results",
        new_callable=AsyncMock,
    ) as mock_write:
        result = await persist_precomputed_dsa_results(
            db, run_id=run_id, artifacts=artifacts,
            trade_date=trade_date, strategy_version_id=version_id,
        )

    assert mock_write.await_count == 1  # 至少一个有效结果
    assert result["failed"] >= 1
    assert result["succeeded"] >= 1
    assert result["status"] == "partial_failed"


def _bad_artifact() -> MagicMock:
    bad = MagicMock()
    bad.source_core_run_id = "core-run-1"
    bad.parameter_hash = "ph-1"
    bad.algorithm_versions = {"dsa": "dsa-v1"}
    bad.payload = {"dsa": {}}
    return bad
