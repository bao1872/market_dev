"""[BOARD-LOCAL-OWNERSHIP-01] 盘后 DAG 不再包含板块同步 + legacy 读取兼容。

验证项（对应任务 §18 A/B）：
1. AfterCloseRunStatus.SYNCING_BOARDS 仅作为 **legacy 读取 token** 保留（新 run 不写）。
2. 盘后编排源码中**没有**任何板块同步执行路径
   （无 _execute_syncing_boards / fetch_board_snapshot / sync_boards 调用）。
3. `_COMPLETED_STEPS` 当前集合不再依赖 syncing_boards 作为真实可执行阶段；
   历史 last_completed_step="syncing_boards" 仅映射为 refreshing_daily 已完成。
4. `_CHECKPOINT_ORDER` 不含 syncing_boards（不是合法 NEW restart 边界）；
   旧 persisted mainchain_stage="syncing_boards" 按 legacy 兼容读取，不 fail closed。
5. board-sync 领域的 `resolve_board_instruments(db, symbols)` 批量解析（无 N+1/无 commit）。
6. BOARD_SYNC_ENABLED 已从盘后编排源码中彻底移除。
"""

from __future__ import annotations

import inspect
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.services import after_close_orchestrator as orch
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    _resolve_execution_completed_steps,
)
from app.services.board_sync_service import resolve_board_instruments

# =============================================================================
# 1. 状态枚举：SYNCING_BOARDS 仅 legacy
# =============================================================================


class TestAfterCloseRunStatus:
    """AfterCloseRunStatus 枚举：SYNCING_BOARDS 保留为 legacy 读取 token。"""

    def test_syncing_boards_kept_as_legacy_token(self) -> None:
        """SYNCING_BOARDS 仍存在（历史 run 必须可读），但语义为 legacy。"""
        assert hasattr(AfterCloseRunStatus, "SYNCING_BOARDS")
        assert AfterCloseRunStatus.SYNCING_BOARDS.value == "syncing_boards"

    def test_enum_order_unchanged(self) -> None:
        """枚举成员顺序不变（legacy token 位置保留，避免历史序列化漂移）。"""
        statuses = list(AfterCloseRunStatus)
        refreshing_idx = statuses.index(AfterCloseRunStatus.REFRESHING_DAILY)
        syncing_idx = statuses.index(AfterCloseRunStatus.SYNCING_BOARDS)
        waiting_idx = statuses.index(AfterCloseRunStatus.WAITING_DSA_WORKER)
        assert refreshing_idx < syncing_idx < waiting_idx


# =============================================================================
# 2. 盘后编排源码中不存在板块同步执行路径
# =============================================================================


class TestNoCurrentBoardSyncPath:
    """当前盘后 DAG 绝不执行/跳过/汇总板块同步。"""

    def test_orchestrator_has_no_board_sync_execution(self) -> None:
        """编排函数源码不包含板块同步步骤/调用。"""
        src = inspect.getsource(orch.execute_after_close_run)
        assert "_execute_syncing_boards" not in src
        assert "fetch_board_snapshot" not in src, "盘后不得再抓取问财板块"
        assert "AfterCloseRunStatus.SYNCING_BOARDS" not in src, (
            "编排函数不得再切换 SYNCING_BOARDS 状态"
        )
        assert "skip_board_sync" not in src

    def test_retired_helpers_removed(self) -> None:
        """已退役 helper 不得再存在于 orchestrator 模块。"""
        assert not hasattr(orch, "_execute_syncing_boards")
        assert not hasattr(orch, "_record_board_sync_outcome")
        assert not hasattr(orch, "_resolve_instruments_for_board_sync")

    def test_board_sync_helpers_not_referenced_module_wide(self) -> None:
        """整个 orchestrator 模块均不含板块同步**执行路径**。

        允许：文档/legacy 兼容字符串（如 _COMPLETED_STEPS["syncing_boards"]）。
        禁止：任何导入或调用板块同步业务。
        """
        src = inspect.getsource(orch)
        assert "_execute_syncing_boards" not in src
        assert "fetch_board_snapshot" not in src
        assert "from app.services.board_sync_service import" not in src, (
            "盘后编排不得导入 board_sync_service"
        )
        assert "AfterCloseRunStatus.SYNCING_BOARDS" not in src
        assert "skip_board_sync" not in src

    def test_no_board_sync_enabled_switch(self) -> None:
        """BOARD_SYNC_ENABLED 已从盘后编排源码中移除。"""
        src = inspect.getsource(orch)
        assert "board_sync_enabled" not in src
        assert "BOARD_SYNC_ENABLED" not in src


# =============================================================================
# 3. checkpoint：current 集合不含 syncing_boards；legacy 只映射到 refreshing_daily
# =============================================================================


class TestCheckpointVocabulary:
    """_COMPLETED_STEPS / _CHECKPOINT_ORDER 的 current vs legacy 语义。"""

    def test_current_completed_sets_do_not_depend_on_syncing_boards(self) -> None:
        """current completed 集合不再包含 syncing_boards 作为真实阶段。"""
        assert orch._COMPLETED_STEPS["refreshing_daily"] == {"refreshing_daily"}
        assert "syncing_boards" not in orch._COMPLETED_STEPS["computing_features"]
        assert "syncing_boards" not in orch._COMPLETED_STEPS["computing_history"]
        assert "syncing_boards" not in orch._COMPLETED_STEPS["succeeded"]

    def test_legacy_syncing_boards_maps_to_refreshing_daily_only(self) -> None:
        """历史 last_completed_step="syncing_boards" 仅表示 refreshing_daily 已完成。"""
        completed = _resolve_execution_completed_steps("syncing_boards", None)
        assert completed == {"refreshing_daily"}
        assert "computing_features" not in completed
        assert "syncing_boards" not in completed

    def test_resume_after_refreshing_daily_does_not_need_syncing_boards(self) -> None:
        """日线成功后即可作为断点，不需要伪造 syncing_boards 检查点。"""
        assert _resolve_execution_completed_steps("refreshing_daily", None) == {
            "refreshing_daily"
        }

    def test_syncing_boards_not_a_current_checkpoint(self) -> None:
        """syncing_boards 已从 current _CHECKPOINT_ORDER 移除。"""
        assert "syncing_boards" not in orch._CHECKPOINT_ORDER

    def test_legacy_mainchain_stage_does_not_fail_closed(self) -> None:
        """旧 persisted mainchain_stage="syncing_boards" 按 legacy 读取，不抛错。"""
        completed = _resolve_execution_completed_steps(None, "syncing_boards")
        assert completed == {"refreshing_daily"}

    def test_daily_ready_start_stage_equivalent(self) -> None:
        """新 daily_ready 起点（computing_features）与旧 syncing_boards 起点语义等价。"""
        legacy = _resolve_execution_completed_steps(None, "syncing_boards")
        new = _resolve_execution_completed_steps(None, "computing_features")
        assert legacy == new == {"refreshing_daily"}

    def test_invalid_mainchain_stage_still_fail_closed(self) -> None:
        """corrupt/typo mainchain_stage 仍 fail closed。"""
        with pytest.raises(ValueError):
            _resolve_execution_completed_steps(None, "checking_coverage")
        with pytest.raises(ValueError):
            _resolve_execution_completed_steps(None, "not_a_stage")


# =============================================================================
# 4. board-sync 领域 resolver（原 orchestrator helper 的迁移）
# =============================================================================


class TestResolveBoardInstruments:
    """backend/app/services/board_sync_service.resolve_board_instruments。"""

    @pytest.mark.asyncio
    async def test_resolve_existing_symbols(self, db_session: AsyncSession) -> None:
        """已存在的 symbol 正确解析为 instrument_id（单次批量查询）。"""
        db_session.add(Instrument(symbol="600000", name="测试1", market="SH", status="active"))
        db_session.add(Instrument(symbol="000001", name="测试2", market="SZ", status="active"))
        await db_session.flush()

        result = await resolve_board_instruments(
            db_session, ["600000", "000001", "999999"]
        )

        assert len(result) == 2
        assert "600000" in result
        assert "000001" in result
        assert "999999" not in result
        assert isinstance(result["600000"], UUID)

    @pytest.mark.asyncio
    async def test_resolve_empty_list(self, db_session: AsyncSession) -> None:
        """空列表返回空 dict（不查询）。"""
        assert await resolve_board_instruments(db_session, []) == {}

    @pytest.mark.asyncio
    async def test_resolve_no_matches(self, db_session: AsyncSession) -> None:
        """无匹配 symbol 返回空 dict。"""
        assert await resolve_board_instruments(db_session, ["999999", "888888"]) == {}


# =============================================================================
# 5. 触发时间 / 非交易日 / 幂等（不受本次迁移影响，保留回归）
# =============================================================================


class TestSchedulerUnchanged:
    """板块同步迁出不改变盘后触发时间/非交易日/幂等合同。"""

    def test_trigger_time_is_15_05(self) -> None:
        """盘后日线刷新触发时间仍为 15:05 Asia/Shanghai。"""
        from app.services.bars_scheduler_worker_runtime import (
            run_bars_scheduler_worker_runtime,
        )

        source = inspect.getsource(run_bars_scheduler_worker_runtime)
        assert "hour=15" in source and "minute=5" in source
        assert "Asia/Shanghai" in source

    def test_non_trading_day_skip(self) -> None:
        """非交易日不运行盘后编排。"""
        from app.services.bars_scheduler_worker_runtime import (
            run_bars_scheduler_worker_runtime,
        )

        source = inspect.getsource(run_bars_scheduler_worker_runtime)
        assert "is_trading_day_async" in source
        assert "非交易日" in source

    def test_same_business_date_idempotent(self) -> None:
        """同一 business_date 幂等（重复创建返回已有 run）。"""
        from app.services.after_close_orchestrator import create_after_close_run

        source = inspect.getsource(create_after_close_run)
        assert "acquire_job_run_lock" in source or "run_key" in source
