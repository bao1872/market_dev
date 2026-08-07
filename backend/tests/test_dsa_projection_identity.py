"""dsa_projection identity / eligibility 单元测试（required compatibility projection SSOT）。

覆盖（PURE_UNIT，mock DB，不连库）：
1. full：正常链投影全覆盖 → _count_dsa_projections 返回 eligible==matched。
2. partial：部分投影 → matched < eligible（readiness 判 PARTIAL_COVERAGE）。
3. 同日 Core A→Core B：source_core_run_id 隔离 → _count_dsa_projections 只解析当前 core。
4. granular restart：_handle_dsa_projection 复用正式 lifecycle，不写 summary_payload["dsa_projection"]。
5. run/item count inconsistency：eligible 与 StrategyRun.total_instruments 不一致时，
   matched 仍按 strategy_run_items 计算（total_instruments 仅作一致性校验）。

运行：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_dsa_projection_identity.py -q -p no:cacheprovider
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.services.product_readiness_service import ProductReadinessService
from tests.test_product_readiness_service_layer import _FakeDB

_CORE_A = "11111111-1111-1111-1111-111111111111"
_CORE_B = "22222222-2222-2222-2222-222222222222"


def _count(db, core_run_id=_CORE_A, trade_date=date(2026, 8, 4)):
    return ProductReadinessService._count_dsa_projections(
        db, trade_date, core_run_id,
    )


async def test_granular_restart_reuses_projection_lifecycle_no_snake_case():
    """[granular restart] _handle_dsa_projection 复用正式 lifecycle（create_batch_run +
    project_dsa_batch + persist_precomputed_dsa_results），不写 summary_payload["dsa_projection"]。

    通过 patch 断言：create_batch_run 收到 source_core_run_id + requirement，project_dsa_batch
    被调用，且没有任何 snapshot 的 summary_payload 被写入 snake_case 键。
    """
    from unittest.mock import AsyncMock, patch

    from app.services.granular_restart_service import _handle_dsa_projection

    source_core = uuid.uuid4()

    class _Snap:
        def __init__(self, iid):
            self.instrument_id = iid
            self.trade_date = date(2026, 8, 4)
            self.summary_payload = {}  # 原实现会写 summary["dsa_projection"]；新实现不写

    snaps = [_Snap(uuid.uuid4()) for _ in range(3)]

    class _FakeRestartDB:
        def __init__(self):
            self.captured = {}

        async def get(self, model, rid):
            # StockFeatureSnapshotRun.get
            core_run = type("CoreRun", (), {})()
            core_run.id = rid
            core_run.metadata_ = {"algorithm_versions": {"dsa": "dsa-v1"},
                                  "parameter_hash": "ph1"}
            core_run.started_at = None
            return core_run

        async def execute(self, stmt):
            class _ScalarsResult:
                def __init__(self, rows):
                    self._rows = rows
                def all(self):
                    return list(self._rows)
            class _ExecResult:
                def __init__(self, rows):
                    self._rows = rows
                def scalars(self, *a, **k):
                    return _ScalarsResult(self._rows)
                def all(self):
                    return list(self._rows)
            return _ExecResult(snaps)

        def add(self, obj):
            pass

        async def flush(self):
            pass

        async def scalar(self, stmt):
            return None

    db = _FakeRestartDB()

    async def _fake_create_batch_run(self, db, strategy_key, trade_date, run_type="scheduled",
                                     instrument_ids=None, *, claim_for_worker=None,
                                     source_core_run_id=None, requirement="required_compatibility",
                                     force_restart=False):
        assert strategy_key == "dsa_selector"
        assert source_core_run_id == source_core
        assert requirement == "required_compatibility"
        assert force_restart is True, "granular restart 应创建新 attempt（force_restart=True）"
        db.captured["source_core_run_id"] = str(source_core_run_id)
        db.captured["force_restart"] = True
        dsa_run = type("DsaRun", (), {})()
        dsa_run.id = uuid.uuid4()
        dsa_run.strategy_version_id = uuid.uuid4()
        return dsa_run

    async def _fake_project_dsa_batch(self, *, source_core_run_id, dsa_run_id, trade_date,
                                      strategy_version_id, persist_fn, heartbeat=None, job_run_id=None):
        assert source_core_run_id == source_core
        return {"projected": 3}

    async def _fake_publish_run(self, db, run_id):
        db.captured["published_run_id"] = str(run_id)
        published = type("PublishedRun", (), {})()
        published.id = run_id
        published.status = "published"
        return published

    with patch("app.services.strategy_batch_service.StrategyBatchService.create_batch_run",
               _fake_create_batch_run), \
         patch("app.services.core_artifact_repository.CoreArtifactRepository.project_dsa_batch",
               _fake_project_dsa_batch), \
         patch("app.services.strategy_batch_service.StrategyBatchService.publish_run",
               _fake_publish_run):
        await _handle_dsa_projection(
            db, trade_date="2026-08-04", parent_job_run_id=uuid.uuid4(),
            source_core_run_id=source_core, input_hash="h", actor="test", attempt=1,
        )

    # 新实现不写任何 snapshot 的 summary_payload["dsa_projection"]（snake_case 键）
    assert db.captured.get("source_core_run_id") == str(source_core)
    assert db.captured.get("force_restart") is True, "granular restart 应显式 force_restart"
    assert db.captured.get("published_run_id") is not None, \
        "granular restart 应复用正式 lifecycle 最终 publish_run"
    assert all("dsa_projection" not in s.summary_payload for s in snaps)


async def test_full_projection_all_matched():
    """full：eligible==matched（dsa_counts=(10,10)）。"""
    db = _FakeDB({"dsa_counts": (10, 10)})
    r = await _count(db)
    assert r["eligible"] == 10
    assert r["matched"] == 10
    assert r["stale"] == 0


async def test_partial_projection_matched_less_than_eligible():
    """partial：matched < eligible（dsa_counts=(10,5)）→ readiness 判 PARTIAL_COVERAGE。"""
    db = _FakeDB({"dsa_counts": (10, 5)})
    r = await _count(db)
    assert r["eligible"] == 10
    assert r["matched"] == 5
    assert r["stale"] == 0
    assert r["matched"] < r["eligible"]


async def test_no_projection_for_unknown_core():
    """当前 core 无投影 run → eligible=0, matched=0（不得误判 full）。"""
    db = _FakeDB({"dsa_counts": (0, 0)})
    r = await _count(db, core_run_id=_CORE_B)
    assert r["eligible"] == 0
    assert r["matched"] == 0


async def test_run_item_count_inconsistency_keeps_matched_from_items():
    """run/item count inconsistency：eligible 与 total_instruments 不一致 → count_mismatch=True。

    _count_dsa_projections 把 total_instruments 仅作一致性校验（count_mismatch），
    不改变 matched（matched 一律来自 strategy_run_items succeeded + result_id 非空）。
    """
    # eligible=10（items 口径）, total_instruments=12（不一致）→ count_mismatch=True
    db = _FakeDB({"dsa_counts": (10, 7), "dsa_total_instruments": 12})
    r = await _count(db)
    assert r["eligible"] == 10
    assert r["matched"] == 7
    assert r["count_mismatch"] is True, "eligible != total_instruments 应置 count_mismatch"

    # 一致时 count_mismatch=False
    db_ok = _FakeDB({"dsa_counts": (10, 10), "dsa_total_instruments": 10})
    r_ok = await _count(db_ok)
    assert r_ok["count_mismatch"] is False
    assert r_ok["run_status"] == "published"


async def test_force_restart_requires_compatibility_identity():
    """[required compatibility projection identity] force_restart 仅允许
    required_compatibility + source_core_run_id 场景。

    对普通/manual/replay run 强制重启（force_restart=True 但无 source_core 或非 required
    requirement）必须在触碰 DB 前抛 ValueError（fail-closed）。
    """
    from app.services.strategy_batch_service import StrategyBatchService

    svc = StrategyBatchService()
    # 无 source_core_run_id → 拒绝
    with pytest.raises(ValueError, match="force_restart 仅允许"):
        await svc.create_batch_run(
            object(), "dsa_selector", date(2026, 8, 4), "scheduled",
            force_restart=True,
        )
    # 非 required requirement → 拒绝
    with pytest.raises(ValueError, match="force_restart 仅允许"):
        await svc.create_batch_run(
            object(), "dsa_selector", date(2026, 8, 4), "scheduled",
            force_restart=True,
            source_core_run_id=uuid.uuid4(),
            requirement="optional",
        )
