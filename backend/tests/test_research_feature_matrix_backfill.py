"""research_feature_matrix_backfill 脚本测试。

[Blocker Fix] 验证 5 个 blocker 修复：
1. DSA hindsight 不得等于 causal 近似 → test_feature_computer.py 已覆盖
2. Node Cluster phase1 全 NULL → test_feature_computer.py 已覆盖
3. 单只股票失败时 failed_rows == trade_dates_count
4. upsert 异常后 rollback 被调用，后续股票可继续
5. 同 month/scope lock 存在时拒绝启动
6. failed_rate 用 failed_rows / expected_rows → test_research_matrix_writer.py 已覆盖
7. resume 不应破坏已完成 full run 的统计 → test_research_matrix_writer.py 已覆盖

约束：
- 不接入 watchlist_ready
- 不修改 production snapshot
- dry-run 不写库不写文件

用法：
    cd backend && APP_ENV=test pytest tests/test_research_feature_matrix_backfill.py -v
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from scripts.research_feature_matrix_backfill import (
    _process_instrument,
    parse_args,
)

# ===== 1. parse_args 新参数 =====


class TestParseArgs:
    """parse_args 新参数测试（--month / --resume / --export-parquet）。"""

    def test_month_parameter(self) -> None:
        """--month YYYY-MM 解析。"""
        with patch(
            "sys.argv",
            ["research_feature_matrix_backfill", "--month", "2026-01", "--dry-run"],
        ):
            args = parse_args()
        assert args.month == "2026-01"
        assert args.start is None
        assert args.end == "latest"
        assert args.dry_run is True

    def test_month_and_start_mutually_exclusive(self) -> None:
        """--month 与 --start 互斥，同时指定应 SystemExit。"""
        with patch(
            "sys.argv",
            [
                "research_feature_matrix_backfill",
                "--month", "2026-01",
                "--start", "2026-01-01",
            ],
        ), pytest.raises(SystemExit):
            parse_args()

    def test_resume_flag(self) -> None:
        """--resume 标志解析。"""
        with patch(
            "sys.argv",
            ["research_feature_matrix_backfill", "--month", "2026-01", "--resume"],
        ):
            args = parse_args()
        assert args.resume is True

    def test_export_parquet_optional(self) -> None:
        """--export-parquet 可选导出路径。"""
        with patch(
            "sys.argv",
            [
                "research_feature_matrix_backfill",
                "--month", "2026-01",
                "--dry-run",
                "--export-parquet", "/tmp/debug.parquet",
            ],
        ):
            args = parse_args()
        assert args.export_parquet == "/tmp/debug.parquet"

    def test_no_output_parameter_anymore(self) -> None:
        """旧 --output 参数已移除，应 SystemExit。"""
        with patch(
            "sys.argv",
            [
                "research_feature_matrix_backfill",
                "--month", "2026-01",
                "--dry-run",
                "--output", "/tmp/out.parquet",
            ],
        ), pytest.raises(SystemExit):
            parse_args()


# ===== 2. [Blocker 3] 单只股票失败时 failed_rows == trade_dates_count =====


class TestProcessInstrumentFailureRows:
    """[Blocker 3] 单只股票失败时 failed_rows 应等于 trade_dates_count。

    旧实现只计 1 行，但一只股票对应多个交易日行，应全部计为失败。
    """

    @pytest.mark.asyncio
    async def test_bars_insufficient_failed_rows_equals_trade_dates_count(
        self,
    ) -> None:
        """bars 不足时，failed_rows == len(trade_dates)。"""
        db = AsyncMock()
        run_id = uuid.uuid4()
        instrument_id = uuid.uuid4()
        trade_dates = {date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)}

        # mock fetch_daily_bars 返回 None（bars 不足）
        with patch(
            "scripts.research_feature_matrix_backfill.fetch_daily_bars",
            new_callable=AsyncMock,
            return_value=None,
        ):
            rows, failed = await _process_instrument(
                db, run_id, instrument_id, "000001",
                trade_dates, date(2026, 1, 1), date(2026, 1, 31),
            )

        assert rows == 0
        # [Blocker 3] failed 应等于 trade_dates 数量（3），不是 1
        assert failed == 3, (
            f"[Blocker 3] 单股失败 failed_rows 应={len(trade_dates)}，"
            f"实际={failed}"
        )

    @pytest.mark.asyncio
    async def test_features_empty_failed_rows_equals_trade_dates_count(
        self,
    ) -> None:
        """compute_all_features 返回空时，failed_rows == len(trade_dates)。"""
        db = AsyncMock()
        run_id = uuid.uuid4()
        instrument_id = uuid.uuid4()
        trade_dates = {
            date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
            date(2026, 1, 8), date(2026, 1, 9),
        }

        # mock fetch_daily_bars 返回足够长的 bars
        import pandas as pd

        bars = pd.DataFrame(
            {"open": [1.0] * 100, "high": [1.0] * 100, "low": [1.0] * 100,
             "close": [1.0] * 100, "volume": [1000.0] * 100},
            index=pd.date_range("2025-06-01", periods=100, freq="B"),
        )

        # mock compute_all_features 返回空 DataFrame
        empty_df = pd.DataFrame()
        with patch(
            "scripts.research_feature_matrix_backfill.fetch_daily_bars",
            new_callable=AsyncMock,
            return_value=bars,
        ), patch(
            "scripts.research_feature_matrix_backfill.compute_all_features",
            return_value=empty_df,
        ):
            rows, failed = await _process_instrument(
                db, run_id, instrument_id, "000001",
                trade_dates, date(2026, 1, 1), date(2026, 1, 31),
            )

        assert rows == 0
        # [Blocker 3] 5 个 trade_date 都应计为失败
        assert failed == 5, (
            f"[Blocker 3] features 为空时 failed_rows 应={len(trade_dates)}，"
            f"实际={failed}"
        )


# ===== 3. [Blocker 4] upsert 异常后 rollback 被调用 =====


class TestProcessInstrumentRollback:
    """[Blocker 4] upsert 异常时必须 rollback，后续股票可继续。"""

    @pytest.mark.asyncio
    async def test_upsert_exception_triggers_rollback(self) -> None:
        """upsert_rows_batch 抛异常时，db.rollback() 被调用。"""
        db = AsyncMock()
        # rollback 也是 AsyncMock
        db.rollback = AsyncMock()
        run_id = uuid.uuid4()
        instrument_id = uuid.uuid4()
        trade_dates = {date(2026, 1, 5), date(2026, 1, 6)}

        import pandas as pd

        bars = pd.DataFrame(
            {"open": [1.0] * 100, "high": [1.0] * 100, "low": [1.0] * 100,
             "close": [1.0] * 100, "volume": [1000.0] * 100},
            index=pd.date_range("2025-06-01", periods=100, freq="B"),
        )

        # mock compute_all_features 返回有效 DataFrame
        features_df = pd.DataFrame(
            {"causal_atr": [1.0, 2.0]},
            index=pd.date_range("2026-01-05", periods=2, freq="B"),
        )

        # mock upsert_rows_batch 抛异常
        with patch(
            "scripts.research_feature_matrix_backfill.fetch_daily_bars",
            new_callable=AsyncMock,
            return_value=bars,
        ), patch(
            "scripts.research_feature_matrix_backfill.compute_all_features",
            return_value=features_df,
        ), patch(
            "scripts.research_feature_matrix_backfill.upsert_rows_batch",
            new_callable=AsyncMock,
            side_effect=Exception("DB connection lost"),
        ):
            rows, failed = await _process_instrument(
                db, run_id, instrument_id, "000001",
                trade_dates, date(2026, 1, 1), date(2026, 1, 31),
            )

        # [Blocker 4] rollback 必须被调用
        assert db.rollback.called, (
            "[Blocker 4] upsert 异常时 db.rollback() 必须被调用，"
            "防止事务污染后续股票"
        )
        assert rows == 0
        assert failed == 2  # 2 个 trade_date

    @pytest.mark.asyncio
    async def test_rollback_failure_does_not_crash(self) -> None:
        """rollback 本身失败时不抛异常，仍返回 failed_rows。"""
        db = AsyncMock()
        db.rollback = AsyncMock(side_effect=Exception("rollback failed"))
        run_id = uuid.uuid4()
        instrument_id = uuid.uuid4()
        trade_dates = {date(2026, 1, 5)}

        import pandas as pd

        bars = pd.DataFrame(
            {"open": [1.0] * 100, "high": [1.0] * 100, "low": [1.0] * 100,
             "close": [1.0] * 100, "volume": [1000.0] * 100},
            index=pd.date_range("2025-06-01", periods=100, freq="B"),
        )
        features_df = pd.DataFrame(
            {"causal_atr": [1.0]},
            index=pd.date_range("2026-01-05", periods=1, freq="B"),
        )

        with patch(
            "scripts.research_feature_matrix_backfill.fetch_daily_bars",
            new_callable=AsyncMock,
            return_value=bars,
        ), patch(
            "scripts.research_feature_matrix_backfill.compute_all_features",
            return_value=features_df,
        ), patch(
            "scripts.research_feature_matrix_backfill.upsert_rows_batch",
            new_callable=AsyncMock,
            side_effect=Exception("DB error"),
        ):
            # 不应抛异常
            rows, failed = await _process_instrument(
                db, run_id, instrument_id, "000001",
                trade_dates, date(2026, 1, 1), date(2026, 1, 31),
            )

        assert rows == 0
        assert failed == 1


# ===== 4. [Blocker 5] 同 month/scope lock 存在时拒绝启动 =====


class TestLockRejection:
    """[Blocker 5] 同 month/scope 已有 lock 时拒绝启动。"""

    def test_advisory_lock_key_stable(self) -> None:
        """相同 month+scope 生成相同 lock key（跨进程一致）。"""
        from app.research.research_matrix_writer import _advisory_lock_key

        key1 = _advisory_lock_key("2026-01", "full")
        key2 = _advisory_lock_key("2026-01", "full")
        assert key1 == key2, "相同 month+scope 应生成相同 lock key"

        # 不同 scope 应不同
        key3 = _advisory_lock_key("2026-01", "sample_100")
        assert key1 != key3, "不同 scope 应生成不同 lock key"

        # 不同 month 应不同
        key4 = _advisory_lock_key("2026-02", "full")
        assert key1 != key4, "不同 month 应生成不同 lock key"

    def test_lock_file_creation_and_rejection(self, tmp_path) -> None:
        """[Blocker 5] lock file 创建后，第二次创建返回 None（拒绝）。"""
        import os

        # mock tempfile.gettempdir 返回 tmp_path
        with patch(
            "app.research.research_matrix_writer.tempfile.gettempdir",
            return_value=str(tmp_path),
        ):
            from app.research.research_matrix_writer import (
                acquire_lock_file,
                release_lock_file,
            )

            # 第一次创建成功
            lock_path = acquire_lock_file("2026-01", "full")
            assert lock_path is not None
            assert os.path.exists(lock_path)

            # 第二次创建应返回 None（拒绝）
            lock_path2 = acquire_lock_file("2026-01", "full")
            assert lock_path2 is None, (
                "[Blocker 5] 同 month/scope lock file 已存在时应返回 None"
            )

            # 释放后可再次创建
            release_lock_file(lock_path)
            assert not os.path.exists(lock_path)

            lock_path3 = acquire_lock_file("2026-01", "full")
            assert lock_path3 is not None
            release_lock_file(lock_path3)

    def test_lock_file_different_scope_coexist(self, tmp_path) -> None:
        """不同 scope 的 lock file 可共存（不互相阻塞）。"""
        with patch(
            "app.research.research_matrix_writer.tempfile.gettempdir",
            return_value=str(tmp_path),
        ):
            from app.research.research_matrix_writer import (
                acquire_lock_file,
                release_lock_file,
            )

            lock_full = acquire_lock_file("2026-01", "full")
            lock_sample = acquire_lock_file("2026-01", "sample_100")

            assert lock_full is not None
            assert lock_sample is not None, (
                "[Blocker 5] 不同 scope 的 lock file 应可共存"
            )

            release_lock_file(lock_full)
            release_lock_file(lock_sample)

    def test_release_nonexistent_lock_file_no_error(self) -> None:
        """释放不存在的 lock file 不抛异常。"""
        from app.research.research_matrix_writer import release_lock_file

        # 不应抛异常
        release_lock_file("/tmp/nonexistent_lock_file_test.lock")
