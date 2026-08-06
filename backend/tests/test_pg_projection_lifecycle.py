"""PG projection 生命周期测试（PANJI_REMOTE_VERIFY_DB_TEST=1）。

[CHANGE-20260806-CP4A-Amendment] 正式化 CP4A 诊断阶段 /tmp 临时脚本的 projection 验证为
**受版本控制的测试文件**。本文件只在远程验证库（bz_stock_verify_<sha>）运行：

    CoreArtifactRepository.project_dsa_batch + persist_precomputed_dsa_results：
    - per-batch commit（N 只 / batch_size → ceil 批）；
    - heartbeat/progress 逐批递增；
    - StrategyResult 每股一条（真实持久化）；
    - 幂等：run 全部处理终态后再次投影不重复写（终态守卫拒绝）。

**注意**：依赖 `db_session` savepoint fixture；PURE_UNIT_TEST=1 时 skip。
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

_PURE_UNIT_TEST = os.environ.get("PURE_UNIT_TEST", "0") == "1"

# [CHANGE-20260806-005 / Phase 5] 显式声明 postgres marker（不得只靠 conftest 扫描推断）。
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        _PURE_UNIT_TEST,
        reason="PG projection 生命周期测试需远程验证库（PANJI_REMOTE_VERIFY_DB_TEST=1）",
    ),
]


async def _create_strategy_version(db, strategy_version_id: uuid.UUID) -> None:
    """为已存在的 dsa_selector 建一个唯一 strategy_versions 行。"""
    ver = f"verify-{uuid.uuid4().hex[:8]}"
    await db.execute(
        text(
            "INSERT INTO strategy_versions "
            "(id, strategy_definition_id, version, status, manifest, build_hash, released_at) "
            "SELECT :id, id, :ver, 'released', '{}', 'build-1', now() "
            "FROM strategy_definitions WHERE strategy_key='dsa_selector' "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(strategy_version_id), "ver": ver},
    )


async def _create_strategy_run(db, dsa_run_id: uuid.UUID, version_id: uuid.UUID,
                               instrument_ids: list[uuid.UUID]) -> None:
    await db.execute(
        text(
            "INSERT INTO strategy_runs "
            "(id, strategy_version_id, run_type, trade_date, status, total_instruments, "
            "succeeded_count, failed_count, started_at, worker_id, idempotency_key) "
            "VALUES (:id, :vid, 'after_close', '2026-08-06', 'running', :n, 0, 0, now(), "
            "'w1', :ik) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(dsa_run_id), "vid": str(version_id), "n": len(instrument_ids),
         "ik": str(dsa_run_id)},
    )
    for iid in instrument_ids:
        await db.execute(
            text(
                "INSERT INTO strategy_run_items "
                "(id, run_id, instrument_id, status, attempt_count, started_at) "
                "VALUES (:id, :rid, :iid, 'pending', 0, now()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(uuid.uuid4()), "rid": str(dsa_run_id), "iid": str(iid)},
        )


async def _create_snapshots(db, snapshot_run_id: uuid.UUID,
                            instrument_ids: list[uuid.UUID]) -> None:
    """为每股创建带 coreArtifact summary 的真实 snapshot（供 decode DSA projection）。"""
    for iid in instrument_ids:
        summary = {
            "coreArtifact": {
                "schemaVersion": 1,
                "firstPyramidCore": {"nBars": 250},
                "structuralPayload": {},
                "dsaProjectionPayload": {
                    "dsa_vwap": 10.5, "regime_value": 1, "dsa_dir_bars": 5,
                },
                "dsaVisualContract": {"dsa_vwap": 10.5},
                "stateEventCandidates": [],
                "availability": {"structure": "ready"},
                "parameterHash": "ph-1",
                "sourceCoreRunId": str(snapshot_run_id),
                "algorithmVersions": {"dsa": "dsa-v1"},
                "inputHash": "in-1",
                "barsHash": "bh-1",
                "adjFactorHash": "ah-1",
                "diagnostics": {},
            }
        }
        await db.execute(
            text(
                "INSERT INTO stock_feature_snapshots "
                "(id, instrument_id, trade_date, source_run_id, primary_timeframe, "
                "schema_version, structural_payload, temporal_payload, summary_payload) "
                "VALUES (:id, :iid, '2026-08-06', :run, '1d', 1, '{}', '{}', :sum)"
            ),
            {
                "id": str(uuid.uuid4()), "iid": str(iid), "run": str(snapshot_run_id),
                "sum": __import__("json").dumps(summary),
            },
        )


@pytest.mark.asyncio
async def test_pg_projection_lifecycle(db_session) -> None:
    """per-batch commit + heartbeat/progress + 幂等。"""
    from app.services.core_artifact_repository import CoreArtifactRepository
    from app.services.strategy_batch_service import persist_precomputed_dsa_results

    snapshot_run_id = uuid.uuid4()
    dsa_run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    instrument_ids = [uuid.uuid4() for _ in range(4)]
    batch_size = 2

    await _create_strategy_version(db_session, version_id)
    await _create_strategy_run(db_session, dsa_run_id, version_id, instrument_ids)
    await _create_snapshots(db_session, snapshot_run_id, instrument_ids)
    await db_session.commit()

    repo = CoreArtifactRepository(session=db_session)
    progress_seen: list[int] = []

    async def heartbeat(processed: int, *_a, **_k) -> None:
        progress_seen.append(int(processed))

    await repo.project_dsa_batch(
        source_core_run_id=snapshot_run_id,
        persist_fn=persist_precomputed_dsa_results,
        batch_size=batch_size,
        dsa_run_id=dsa_run_id,
        heartbeat=heartbeat,
    )
    await db_session.commit()

    # per-batch commit：4 只 / 2 = 2 批；progress 逐批递增
    assert progress_seen, "heartbeat 应被调用"
    assert progress_seen == sorted(progress_seen), f"progress 应递增: {progress_seen}"
    assert progress_seen[-1] == len(instrument_ids), f"末批进度应=总只数: {progress_seen}"

    # StrategyResult 每股一条
    res_count = (
        await db_session.execute(
            text("SELECT count(*) FROM strategy_results WHERE run_id=:rid"),
            {"rid": str(dsa_run_id)},
        )
    ).scalar_one()
    assert res_count == len(instrument_ids), (
        f"StrategyResult 应为每股一条（{len(instrument_ids)}），实际={res_count}"
    )

    # 幂等：run 全处理终态后再次投影不重复写（终态守卫拒绝 completed run）
    res_count_before = res_count
    try:
        await repo.project_dsa_batch(
            source_core_run_id=snapshot_run_id,
            persist_fn=persist_precomputed_dsa_results,
            batch_size=batch_size,
            dsa_run_id=dsa_run_id,
            heartbeat=heartbeat,
        )
        await db_session.commit()
    except Exception:  # noqa: BLE001 — 终态守卫拒绝（completed run 不再写入）
        await db_session.rollback()
    res_count_after = (
        await db_session.execute(
            text("SELECT count(*) FROM strategy_results WHERE run_id=:rid"),
            {"rid": str(dsa_run_id)},
        )
    ).scalar_one()
    assert res_count_after == res_count_before, (
        f"幂等重跑不应增长 StrategyResult（应仍={res_count_before}，实际={res_count_after}）"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
