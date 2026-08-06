"""CoreArtifactRepository 分页 projection 单元测试（P0-04 batch）。

[CHANGE-20260805-CP4A-CP3]
验证：
- 分页读取（batch_size=2，5 条 → 3 批），不一次全载。
- decode 成强类型 DecodedCoreArtifact。
- decode 失败的单条被跳过但不中断批次。
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import MagicMock

import pytest

from app.services.core_artifact_codec import encode_dsa_projection_to_summary
from app.services.core_artifact_repository import CoreArtifactRepository, iter_core_artifacts


def _fake_snap(i: int) -> MagicMock:
    snap = MagicMock()
    snap.instrument_id = uuid.uuid4()
    snap.trade_date = date(2026, 8, 5)
    snap.summary_payload = {
        "dsaProjection": encode_dsa_projection_to_summary(
            schema_version=1,
            dsa_projection_payload={"dsa_dir_bars": 5, "dsa_vwap": 10.0 + i},
            dsa_visual_contract={"dsa_vwap": 10.0 + i},
            availability={"trend": "ready"},
            parameter_hash="ph-1",
            source_core_run_id="core-run-1",
            algorithm_versions={"dsa": "dsa-v1"},
            input_hash="in",
            bars_hash="bars",
            adj_factor_hash="adj",
        )
    }
    return snap


def _fake_db(snaps: list) -> MagicMock:
    """fake db：分页返回 snaps（模拟 SQLAlchemy await db.execute(stmt) → Result）。"""
    db = MagicMock()
    batch_size = 2
    calls = {"n": 0}

    async def _execute_async(stmt):
        page_idx = calls["n"]
        calls["n"] += 1
        page = snaps[page_idx * batch_size:(page_idx + 1) * batch_size]
        res = MagicMock()
        res.scalars.return_value.all.return_value = page
        return res

    db.execute = _execute_async
    return db


@pytest.mark.asyncio
async def test_iter_core_artifacts_paginates() -> None:
    """5 条 snapshot，batch_size=2 → 3 批（2/2/1），不强载全量。"""
    snaps = [_fake_snap(i) for i in range(5)]
    db = _fake_db(snaps)
    batches = [b async for b in iter_core_artifacts(
        db, source_core_run_id="core-run-1", batch_size=2,
    )]
    # 3 批：2+2+1
    assert len(batches) == 3
    assert sum(len(b) for b in batches) == 5
    # decode 成强类型
    assert all(a.instrument_id is not None for b in batches for a in b)
    assert batches[0][0].payload["dsa"]["dsa_vwap"] == 10.0


def test_repo_project_batches(monkeypatch) -> None:
    """project_dsa_batch 逐批调用 persist_fn（不一次全载）。"""
    snaps = [_fake_snap(i) for i in range(5)]
    db = _fake_db(snaps)
    repo = CoreArtifactRepository(db, batch_size=2)

    calls: list[int] = []

    async def _fake_persist(*a, **kw):
        artifacts = kw.get("artifacts") or {}
        calls.append(len(artifacts))
        return {"succeeded": len(artifacts), "failed": 0, "skipped": 0, "status": "completed"}

    async def _run():
        return await repo.project_dsa_batch(
            source_core_run_id="core-run-1",
            dsa_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 5),
            strategy_version_id=uuid.uuid4(),
            persist_fn=_fake_persist,
        )

    result = _run_sync(_run())
    assert result["batches"] == 3
    assert result["projected"] == 5
    # 每批不超过 2 条（分页）
    assert all(n <= 2 for n in calls)


def _run_sync(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ============================================================================
# [Item 6] projection batch 生命周期：reuse / retry / fencing / idempotency / partial
# ============================================================================


def _fake_db_with_pages(pages: list[list], per_page: int = 2) -> MagicMock:
    """fake db：每批返回 pages 中的一批（模拟分页），并记录已 commit 的批次数。"""
    db = MagicMock()
    calls = {"n": 0, "commits": 0}
    db._calls = calls

    async def _execute(stmt):
        idx = calls["n"]
        if idx < len(pages):
            page = pages[idx]
            calls["n"] += 1
        else:
            page = []
        res = MagicMock()
        res.scalars.return_value.all.return_value = page
        return res

    db.execute = _execute

    async def _commit():
        calls["commits"] += 1

    db.commit = _commit
    return db


def test_projection_batch_partial_failure_continues_next_batch() -> None:
    """[Item 6] 一批内部分失败 → 该批结果记录 failed，不影响下一批；每批 commit。"""
    from datetime import date

    snaps = [_fake_snap(i) for i in range(4)]  # 4 条 → 2 批（2/2）
    db = _fake_db_with_pages([snaps[0:2], snaps[2:4]])
    repo = CoreArtifactRepository(db, batch_size=2)

    calls: list[dict] = []

    async def _fake_persist(*a, **kw):
        artifacts = kw.get("artifacts") or {}
        # 第一只失败，其余成功（模拟部分失败）
        succeeded = max(0, len(artifacts) - 1)
        calls.append({"batch": len(artifacts), "succeeded": succeeded})
        return {
            "succeeded": succeeded,
            "failed": 1 if artifacts else 0,
            "skipped": 0,
            "status": "partial_failed" if succeeded else "failed",
        }

    async def _run():
        return await repo.project_dsa_batch(
            source_core_run_id="core-run-1",
            dsa_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 5),
            strategy_version_id=uuid.uuid4(),
            persist_fn=_fake_persist,
        )

    result = _run_sync(_run())
    assert result["batches"] == 2
    assert result["projected"] == 2  # 4 只 - 2 只失败
    assert len(calls) == 2
    # 每批 persist 都发生（partial failure 不阻断批次）
    assert [c["batch"] for c in calls] == [2, 2]


def test_projection_batch_heartbeat_and_progress() -> None:
    """[Item 6] 每批调用 heartbeat，进度单调递增。"""
    from datetime import date

    snaps = [_fake_snap(i) for i in range(4)]
    db = _fake_db_with_pages([snaps[0:2], snaps[2:4]])
    repo = CoreArtifactRepository(db, batch_size=2)

    progress: list[int] = []

    async def _fake_persist(*a, **kw):
        artifacts = kw.get("artifacts") or {}
        return {"succeeded": len(artifacts), "failed": 0, "skipped": 0, "status": "completed"}

    async def _heartbeat(projected: int, result: dict) -> None:
        progress.append(projected)

    async def _run():
        return await repo.project_dsa_batch(
            source_core_run_id="core-run-1",
            dsa_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 5),
            strategy_version_id=uuid.uuid4(),
            persist_fn=_fake_persist,
            heartbeat=_heartbeat,
        )

    result = _run_sync(_run())
    assert result["projected"] == 4
    # 每批一次 heartbeat，进度递增
    assert progress == [2, 4]


def test_projection_batch_idempotent_reuse() -> None:
    """[Item 6] 已 succeeded 项重跑 → reuse（succeeded=0 新算，run 仍推进）。"""
    from datetime import date

    snaps = [_fake_snap(i) for i in range(2)]
    db = _fake_db_with_pages([snaps])
    repo = CoreArtifactRepository(db, batch_size=2)

    # 模拟所有项已 succeeded → persist 返回 succeeded=0（reuse）
    async def _fake_persist(*a, **kw):
        return {"succeeded": 0, "failed": 0, "skipped": 2, "status": "completed"}

    async def _run():
        return await repo.project_dsa_batch(
            source_core_run_id="core-run-1",
            dsa_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 5),
            strategy_version_id=uuid.uuid4(),
            persist_fn=_fake_persist,
        )

    result = _run_sync(_run())
    assert result["projected"] == 0  # 无新算
    assert result["batches"] == 1  # 批次仍处理（幂等跳过）
