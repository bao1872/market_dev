"""AC0-AC4 盘后故障闭环契约（纯单元，无 DB）。

覆盖本次事故相关的可观测性修复：
1. resolve_failed_step：从 step_summary 推导失败步骤（后端权威，前端无需猜测）；
2. _compute_watchlist_reason：失败不再回退「未进入 publish」误导文案，
   而是指向真实失败步骤（配合 error_code/error_message 给出可操作信息）；
3. upsert_raw_daily_snapshot 分批改写（asyncpg 32767 参数上限回归）。

注：orchestrator 失败语义（step_summary.refreshing_daily.status=failed、
error_code/error_message 落库、syncing_boards 不伪造）属 postgres 契约，
由 test_after_close_orchestrator* 在注册验证运行时覆盖。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import after_close_pipeline_service as pipeline_mod
from app.services import eod_daily_refresh_service as refresh_mod
from app.services.eod_market_snapshot_provider import EodSnapshotRow

# ---------------------------------------------------------------------------
# resolve_failed_step
# ---------------------------------------------------------------------------


def test_resolve_failed_step_returns_last_failed() -> None:
    summary = {
        "refreshing_daily": {"status": "succeeded"},
        "syncing_boards": {"status": "failed"},
        "checking_coverage": {"status": "pending"},
    }
    assert pipeline_mod.resolve_failed_step(summary) == "syncing_boards"


def test_resolve_failed_step_handles_terminal_statuses() -> None:
    for status in ("unavailable", "timed_out", "interrupted"):
        summary = {"refreshing_daily": {"status": status}}
        assert pipeline_mod.resolve_failed_step(summary) == "refreshing_daily"


def test_resolve_failed_step_prefers_latest_in_order() -> None:
    # 倒序扫描：后失败的步骤优先
    summary = {
        "refreshing_daily": {"status": "failed"},
        "syncing_boards": {"status": "succeeded"},
        "checking_coverage": {"status": "failed"},
    }
    assert pipeline_mod.resolve_failed_step(summary) == "checking_coverage"


def test_resolve_failed_step_none_when_all_ok() -> None:
    summary = {"refreshing_daily": {"status": "succeeded"}}
    assert pipeline_mod.resolve_failed_step(summary) is None


def test_resolve_failed_step_none_on_bad_input() -> None:
    assert pipeline_mod.resolve_failed_step(None) is None
    assert pipeline_mod.resolve_failed_step("not-a-dict") is None


# ---------------------------------------------------------------------------
# _compute_watchlist_reason：admin 可观测性文案
# ---------------------------------------------------------------------------


def test_watchlist_reason_no_publish_fallback_on_failure() -> None:
    # 失败时绝不再出现过时文案「未进入 publish」
    job_run = SimpleNamespace(status="failed", error_message="boom")
    reason = pipeline_mod._compute_watchlist_reason(
        False, job_run, None, False, failed_step="refreshing_daily"
    )
    assert "未进入 publish" not in reason
    assert "刷新日线" in reason


def test_watchlist_reason_points_at_real_failed_step() -> None:
    job_run = SimpleNamespace(status="failed", error_message="x")
    reason = pipeline_mod._compute_watchlist_reason(
        False, job_run, None, False, failed_step="checking_coverage"
    )
    assert "检查覆盖率" in reason
    assert "未进入 publish" not in reason


def test_watchlist_reason_without_failed_step_is_neutral() -> None:
    # 无 failed_step 时退化为中性状态文案，仍不出现 publish 误导
    job_run = SimpleNamespace(status="failed", error_message="x")
    reason = pipeline_mod._compute_watchlist_reason(False, job_run, None, False)
    assert "未进入 publish" not in reason


# ---------------------------------------------------------------------------
# upsert_raw_daily_snapshot：asyncpg 32767 参数上限回归
# ---------------------------------------------------------------------------


def _snap(symbol: str) -> EodSnapshotRow:
    return EodSnapshotRow(
        symbol=symbol,
        name=symbol,
        market="SH",
        updated_at=datetime(2026, 9, 14, 15, 5),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=Decimal("1000"),
        amount=Decimal("10500"),
        previous_close=Decimal("10"),
    )


class _InsertCapturingSession:
    """最小 async session：仅捕获 INSERT 语句，供分批改写断言。"""

    def __init__(self) -> None:
        self.insert_statements: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt: object) -> object:
        from sqlalchemy.sql.expression import Insert

        if isinstance(stmt, Insert):
            self.insert_statements.append(stmt)
        return SimpleNamespace()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_upsert_raw_daily_snapshot_chunks_market_wide_insert() -> None:
    """全市场快照必须分批改写，单条 INSERT 不超 asyncpg 32767 参数上限。"""
    batch = refresh_mod._RAW_DAILY_UPSERT_BATCH_SIZE
    assert batch * 9 <= 32767  # 9 列 × batch 行 <= 32767

    # 全市场量级：约 2.3 个批 → 3 条 INSERT
    total = batch * 2 + 1000
    session = _InsertCapturingSession()
    rows = [(uuid.uuid4(), _snap(f"{600000 + i:06d}")) for i in range(total)]
    written = await refresh_mod.upsert_raw_daily_snapshot(session, date(2026, 9, 14), rows)
    assert written == total
    assert len(session.insert_statements) == 3
