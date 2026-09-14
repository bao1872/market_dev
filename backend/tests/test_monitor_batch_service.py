"""F2 PART 3 (runtime target cache) + PART 12 (cooldown contract) 单元测试。

PART 3：runtime target bundle 缓存键绑定 (instrument_id, source_hash, adj_factor_hash, daily_last)；
同日期 XDXR/qfq rebuild（source/factor identity 改变）→ 必须 cache miss。
PART 12：draft.apply_cooldown 决定是否需要 _check_event_cooldown；默认 True（旧行为不变）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pandas as pd

from app.services.monitor_batch_service import MonitorBatchService
from app.strategy.runtime import StrategyEventDraft


async def test_runtime_target_cache_hit_and_miss():
    svc = MonitorBatchService()
    sentinel = SimpleNamespace(target_set=object())
    inst = uuid4()

    # 真实 _build_smc_runtime_target_bundle 运行（含缓存逻辑）；只 mock 内部重计算。
    daily_bars = pd.DataFrame(
        {"open": [1.0] * 30, "high": [1.0] * 30, "low": [1.0] * 30, "close": [1.0] * 30},
        index=pd.date_range("2025-12-01", periods=30, freq="D"),
    )

    def _node(src, adj):
        return SimpleNamespace(
            daily_bars=daily_bars, daily_source_hash=src, daily_adj_factor_hash=adj
        )

    with patch(
        "app.services.monitor_batch_service.compute_smc_pine",
        return_value={"params": {"internal_filter_confluence": True}},
    ), patch(
        "app.services.monitor_batch_service.build_smc_runtime_target_bundle",
        Mock(return_value=sentinel),
    ) as build:
        # 首次 → build
        r1 = await svc._resolve_smc_runtime_target_bundle(inst, "X", _node("src1", "adj1"))
        assert r1.bundle is sentinel
        assert build.call_count == 1

        # 同键 → cache hit（不再 build）
        r2 = await svc._resolve_smc_runtime_target_bundle(inst, "X", _node("src1", "adj1"))
        assert r2.bundle is sentinel
        assert build.call_count == 1

        # source hash 改变 → cache miss / rebuild
        r3 = await svc._resolve_smc_runtime_target_bundle(inst, "X", _node("src2", "adj1"))
        assert r3.bundle is sentinel
        assert build.call_count == 2

        # factor hash 改变 → cache miss / rebuild
        await svc._resolve_smc_runtime_target_bundle(inst, "X", _node("src2", "adj2"))
        assert build.call_count == 3


async def test_cooldown_contract_respects_apply_cooldown():
    svc = MonitorBatchService()
    svc._check_event_cooldown = AsyncMock(return_value=False)
    fake_db = AsyncMock()
    now = datetime.now(UTC)

    draft_default = StrategyEventDraft(
        event_type="node_cluster_touch", event_time=now,
        dedupe_key="k1", logical_entity="e1",
    )
    draft_no_cool = StrategyEventDraft(
        event_type="smc_bos_cross", event_time=now,
        dedupe_key="k2", logical_entity="e2", apply_cooldown=False,
    )

    with patch("app.services.monitor_batch_service.strategy_event_repository") as repo:
        repo.write_event = AsyncMock(return_value=object())
        written = await svc._write_event_drafts(
            fake_db, instrument_id=uuid4(), strategy_version_id="v1",
            drafts=[draft_default, draft_no_cool],
        )

    # 仅 apply_cooldown=True 的 draft 触发冷却检查
    assert svc._check_event_cooldown.await_count == 1
    # 两个 draft 都应被写入（canonical SMC 跳过冷却但照常幂等写入）
    assert repo.write_event.await_count == 2
    assert len(written) == 2


async def test_cooldown_blocks_when_in_cooldown():
    svc = MonitorBatchService()
    svc._check_event_cooldown = AsyncMock(return_value=True)  # 在冷却中
    fake_db = AsyncMock()
    now = datetime.now(UTC)
    draft = StrategyEventDraft(
        event_type="node_cluster_touch", event_time=now,
        dedupe_key="k1", logical_entity="e1",
    )
    with patch("app.services.monitor_batch_service.strategy_event_repository") as repo:
        repo.write_event = AsyncMock(return_value=object())
        written = await svc._write_event_drafts(
            fake_db, instrument_id=uuid4(), strategy_version_id="v1", drafts=[draft],
        )
    assert svc._check_event_cooldown.await_count == 1
    assert repo.write_event.await_count == 0  # 冷却中跳过写入
    assert written == []
