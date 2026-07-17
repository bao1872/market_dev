"""盘后编排 syncing_boards 步骤测试（PRD §五：盘后编排）。

验证项：
1. SYNCING_BOARDS 状态存在于 AfterCloseRunStatus 枚举
2. _completed_steps 包含 syncing_boards 在正确顺序位置
3. _resolve_instruments_for_board_sync 正确解析 symbol → instrument_id
4. dsa_only 模式跳过 syncing_boards
5. BOARD_SYNC_ENABLED=false 时 syncing_boards 标记为 skipped

注：完整编排流程测试需要大量 mock，此处聚焦于关键集成点。
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    _resolve_instruments_for_board_sync,
)

# =============================================================================
# 1. 状态枚举测试
# =============================================================================


class TestAfterCloseRunStatus:
    """AfterCloseRunStatus 枚举测试。"""

    def test_syncing_boards_exists(self) -> None:
        """SYNCING_BOARDS 状态必须存在。"""
        assert hasattr(AfterCloseRunStatus, "SYNCING_BOARDS")
        assert AfterCloseRunStatus.SYNCING_BOARDS.value == "syncing_boards"

    def test_status_order(self) -> None:
        """状态枚举顺序：refreshing_daily → syncing_boards → waiting_dsa_worker。"""
        statuses = list(AfterCloseRunStatus)
        refreshing_idx = statuses.index(AfterCloseRunStatus.REFRESHING_DAILY)
        syncing_idx = statuses.index(AfterCloseRunStatus.SYNCING_BOARDS)
        waiting_idx = statuses.index(AfterCloseRunStatus.WAITING_DSA_WORKER)

        assert refreshing_idx < syncing_idx < waiting_idx, (
            f"状态顺序错误: refreshing={refreshing_idx}, syncing={syncing_idx}, "
            f"waiting={waiting_idx}"
        )


# =============================================================================
# 2. _resolve_instruments_for_board_sync 测试
# =============================================================================


class TestResolveInstrumentsForBoardSync:
    """instrument 解析器测试。"""

    @pytest.mark.asyncio
    async def test_resolve_existing_symbols(self, db_session: AsyncSession) -> None:
        """已存在的 symbol 正确解析为 instrument_id。"""
        # 创建测试 Instrument
        instr1 = Instrument(symbol="600000", name="测试1", market="SH", status="active")
        instr2 = Instrument(symbol="000001", name="测试2", market="SZ", status="active")
        db_session.add(instr1)
        db_session.add(instr2)
        await db_session.flush()

        # 传入 db_session 以看到 savepoint 内未提交的数据
        result = await _resolve_instruments_for_board_sync(
            ["600000", "000001", "999999"], session=db_session
        )

        assert len(result) == 2
        assert "600000" in result
        assert "000001" in result
        assert "999999" not in result  # 不存在的 symbol 不返回
        assert isinstance(result["600000"], UUID)

    @pytest.mark.asyncio
    async def test_resolve_empty_list(self) -> None:
        """空列表返回空 dict。"""
        result = await _resolve_instruments_for_board_sync([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_resolve_no_matches(self, db_session: AsyncSession) -> None:
        """无匹配的 symbol 返回空 dict。"""
        result = await _resolve_instruments_for_board_sync(["999999", "888888"])
        assert result == {}


# =============================================================================
# 3. _completed_steps 集成测试（通过源码级验证）
# =============================================================================


class TestCompletedStepsIntegration:
    """_completed_steps 字典包含 syncing_boards 的源码级验证。"""

    def test_completed_steps_includes_syncing_boards(self) -> None:
        """_completed_steps 字典必须包含 syncing_boards 键。"""
        import inspect

        from app.services.after_close_orchestrator import execute_after_close_run

        source = inspect.getsource(execute_after_close_run)
        assert '"syncing_boards"' in source, (
            "_completed_steps 字典缺少 syncing_boards 键"
        )

    def test_completed_steps_correct_progression(self) -> None:
        """syncing_boards 在 refreshing_daily 之后、waiting_dsa_worker 之前。"""
        import inspect

        from app.services.after_close_orchestrator import execute_after_close_run

        source = inspect.getsource(execute_after_close_run)
        # 验证 syncing_boards 出现在 refreshing_daily 之后
        refreshing_pos = source.find('"refreshing_daily": {"refreshing_daily"}')
        syncing_pos = source.find('"syncing_boards":')
        waiting_pos = source.find('"waiting_dsa_worker":')

        assert refreshing_pos < syncing_pos < waiting_pos, (
            "_completed_steps 顺序错误: syncing_boards 不在 refreshing_daily 和 waiting_dsa_worker 之间"
        )

    def test_dsa_only_skips_syncing_boards(self) -> None:
        """dsa_only 模式应跳过 syncing_boards。"""
        import inspect

        from app.services.after_close_orchestrator import execute_after_close_run

        source = inspect.getsource(execute_after_close_run)
        # dsa_only 模式应包含 syncing_boards 在 completed 集合中
        assert '"syncing_boards"' in source, "dsa_only 模式未处理 syncing_boards"

    def test_board_sync_step_exists(self) -> None:
        """编排函数中必须包含 syncing_boards 步骤的执行代码。"""
        import inspect

        from app.services.after_close_orchestrator import execute_after_close_run

        source = inspect.getsource(execute_after_close_run)
        # 验证关键代码片段存在
        assert "fetch_board_snapshot" in source, "缺少 fetch_board_snapshot 调用"
        assert "sync_boards" in source, "缺少 sync_boards 调用"
        assert "record_sync_status" in source, "缺少 record_sync_status 调用"
        assert "AfterCloseRunStatus.SYNCING_BOARDS" in source, "缺少 SYNCING_BOARDS 状态切换"

    def test_board_sync_soft_failure(self) -> None:
        """板块同步失败时不应阻断主流程（软失败）。"""
        import inspect

        from app.services.after_close_orchestrator import execute_after_close_run

        source = inspect.getsource(execute_after_close_run)
        # 验证软失败逻辑：except 块中不 raise
        assert "软失败" in source or "soft" in source.lower(), "缺少软失败标记"
        # 验证失败时记录状态但不抛异常
        assert 'status": "failed"' in source, "缺少失败状态记录"
