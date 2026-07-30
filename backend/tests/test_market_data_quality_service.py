"""全市场行情质量扫描与修复服务测试（Stage 5 P0）。

测试覆盖：
1. 纯函数：OHLC 校验、量额一致性、重复检测、缺口检测、因子异常检测、时间排序
2. run_key / parameter_hash 生成与幂等性
3. classify_missing_dates 分类逻辑
4. scan_instrument 综合 classification（NOT_LISTED/SUSPENDED/DELISTED/DB_MISSING/SOURCE_MISSING/FACTOR_MISSING/OK）
5. PG 集成测试（仅 CI 环境，本地 PURE_UNIT_TEST=1 自动 skip）

测试策略：
- 纯单元测试：使用 mock DB 查询结果，验证扫描逻辑
- PG 集成测试：使用 db_session fixture，验证完整 scan + repair 流程

运行：
    # 本地纯单元测试
    PURE_UNIT_TEST=1 pytest tests/test_market_data_quality_service.py -v

    # CI 集成测试
    APP_ENV=test TEST_DATABASE_URL=postgresql://...pytest_test \\
    GITHUB_ACTIONS=true pytest tests/test_market_data_quality_service.py -v
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

# CI 环境标识（与 conftest.py 一致）
_CI_ENV = (
    os.environ.get("GITHUB_ACTIONS", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_CI_DB_TEST", "").lower() in ("1", "true", "yes")
)
_PURE_UNIT = os.environ.get("PURE_UNIT_TEST", "").lower() in ("1", "true", "yes")

from app.models.instrument import Instrument  # noqa: E402
from app.services.market_data_quality_service import (  # noqa: E402
    CLASS_DB_MISSING,
    CLASS_DELISTED,
    CLASS_FACTOR_MISSING,
    CLASS_NOT_LISTED,
    CLASS_OK,
    CLASS_SOURCE_MISSING,
    CLASS_SUSPENDED,
    ISSUE_AMOUNT_ANOMALY,
    ISSUE_BAR_COUNT_INSUFFICIENT,
    ISSUE_DUPLICATE,
    ISSUE_FACTOR_ANOMALY,
    ISSUE_FACTOR_MISSING,
    ISSUE_INTERNAL_GAP,
    ISSUE_NO_ISSUE,
    ISSUE_OHLC_INVALID,
    ISSUE_TAIL_GAP,
    ISSUE_TIME_REVERSED,
    ISSUE_VOLUME_ANOMALY,
    MDQ_15M_MIN_BARS,
    MDQ_ALGORITHM_VERSION,
    MarketDataQualityService,
    ScanResult,
)

# =============================================================================
# helpers
# =============================================================================


def _make_instrument(
    symbol: str = "600000",
    market: str = "SH",
    status: str = "active",
    listing_date: date | None = date(2020, 1, 1),
) -> Instrument:
    """构造测试用 Instrument。"""
    return Instrument(
        id=uuid.uuid4(),
        symbol=symbol,
        name=f"测试股票{symbol}",
        market=market,
        status=status,
        listing_date=listing_date,
    )


# =============================================================================
# 1. check_ohlc_validity 纯函数测试
# =============================================================================


class TestCheckOhlcValidity:
    """OHLC 合法性校验。"""

    def test_valid_ohlc(self):
        """正常 OHLC：high>=max(open,close,low), low<=min(open,close,high)。"""
        assert MarketDataQualityService.check_ohlc_validity(10.0, 11.0, 9.0, 10.5)
        assert MarketDataQualityService.check_ohlc_validity(10.0, 10.0, 10.0, 10.0)

    def test_high_less_than_open(self):
        """high < open 非法。"""
        assert not MarketDataQualityService.check_ohlc_validity(11.0, 10.0, 9.0, 10.5)

    def test_high_less_than_close(self):
        """high < close 非法。"""
        assert not MarketDataQualityService.check_ohlc_validity(10.0, 10.5, 9.0, 11.0)

    def test_high_less_than_low(self):
        """high < low 非法。"""
        assert not MarketDataQualityService.check_ohlc_validity(10.0, 9.0, 11.0, 10.5)

    def test_low_greater_than_open(self):
        """low > open 非法。"""
        assert not MarketDataQualityService.check_ohlc_validity(9.0, 11.0, 10.0, 10.5)

    def test_zero_or_negative(self):
        """OHLC 含 0 或负数非法。"""
        assert not MarketDataQualityService.check_ohlc_validity(0, 11.0, 9.0, 10.5)
        assert not MarketDataQualityService.check_ohlc_validity(10.0, -1.0, 9.0, 10.5)
        assert not MarketDataQualityService.check_ohlc_validity(10.0, 11.0, 0, 10.5)

    def test_none_values(self):
        """OHLC 含 None 非法。"""
        assert not MarketDataQualityService.check_ohlc_validity(None, 11.0, 9.0, 10.5)
        assert not MarketDataQualityService.check_ohlc_validity(10.0, None, 9.0, 10.5)
        assert not MarketDataQualityService.check_ohlc_validity(10.0, 11.0, None, 10.5)
        assert not MarketDataQualityService.check_ohlc_validity(10.0, 11.0, 9.0, None)


# =============================================================================
# 2. check_volume_amount_anomaly 纯函数测试
# =============================================================================


class TestCheckVolumeAmountAnomaly:
    """volume/amount 一致性校验。"""

    def test_both_positive(self):
        """volume>0 且 amount>0 正常。"""
        assert MarketDataQualityService.check_volume_amount_anomaly(100.0, 1000.0) is None

    def test_both_zero(self):
        """volume=0 且 amount=0 正常（停牌日）。"""
        assert MarketDataQualityService.check_volume_amount_anomaly(0, 0) is None
        assert MarketDataQualityService.check_volume_amount_anomaly(0.0, 0.0) is None

    def test_volume_zero_amount_positive(self):
        """volume=0 但 amount>0 → VOLUME_ANOMALY。"""
        assert MarketDataQualityService.check_volume_amount_anomaly(0, 100.0) == ISSUE_VOLUME_ANOMALY

    def test_volume_positive_amount_zero(self):
        """volume>0 但 amount=0 → AMOUNT_ANOMALY。"""
        assert MarketDataQualityService.check_volume_amount_anomaly(100.0, 0) == ISSUE_AMOUNT_ANOMALY

    def test_none_values_treated_as_zero(self):
        """None 视为 0。"""
        assert MarketDataQualityService.check_volume_amount_anomaly(None, 100.0) == ISSUE_VOLUME_ANOMALY
        assert MarketDataQualityService.check_volume_amount_anomaly(100.0, None) == ISSUE_AMOUNT_ANOMALY
        assert MarketDataQualityService.check_volume_amount_anomaly(None, None) is None


# =============================================================================
# 3. detect_duplicates 纯函数测试
# =============================================================================


class TestDetectDuplicates:
    """重复日期检测。"""

    def test_no_duplicates(self):
        """无重复。"""
        dates = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        assert MarketDataQualityService.detect_duplicates(dates) == []

    def test_with_duplicates(self):
        """有重复，返回去重后的重复日期。"""
        dates = [
            date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 1),
            date(2026, 1, 3), date(2026, 1, 2),
        ]
        result = MarketDataQualityService.detect_duplicates(dates)
        assert result == [date(2026, 1, 1), date(2026, 1, 2)]

    def test_empty(self):
        """空列表。"""
        assert MarketDataQualityService.detect_duplicates([]) == []

    def test_single(self):
        """单元素。"""
        assert MarketDataQualityService.detect_duplicates([date(2026, 1, 1)]) == []


# =============================================================================
# 4. detect_gaps 纯函数测试（internal vs tail）
# =============================================================================


class TestDetectGaps:
    """缺口检测：internal_gap vs tail_gap。"""

    def test_no_gaps(self):
        """无缺口。"""
        actual = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        expected = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        internal, tail, all_missing = MarketDataQualityService.detect_gaps(actual, expected)
        assert internal == []
        assert tail == []
        assert all_missing == []

    def test_internal_gap(self):
        """中间缺口（首末之间缺一天）。"""
        actual = [date(2026, 1, 1), date(2026, 1, 3)]
        expected = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        internal, tail, all_missing = MarketDataQualityService.detect_gaps(actual, expected)
        assert internal == [date(2026, 1, 2)]
        assert tail == []
        assert all_missing == [date(2026, 1, 2)]

    def test_tail_gap(self):
        """末尾缺口（实际最后日期 < 期望最后日期）。"""
        actual = [date(2026, 1, 1), date(2026, 1, 2)]
        expected = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
        internal, tail, all_missing = MarketDataQualityService.detect_gaps(actual, expected)
        assert internal == []
        assert tail == [date(2026, 1, 3)]
        assert all_missing == [date(2026, 1, 3)]

    def test_both_internal_and_tail(self):
        """同时有中间缺口和末尾缺口。"""
        actual = [date(2026, 1, 1), date(2026, 1, 3)]
        expected = [
            date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
            date(2026, 1, 4),
        ]
        internal, tail, all_missing = MarketDataQualityService.detect_gaps(actual, expected)
        assert internal == [date(2026, 1, 2)]
        assert tail == [date(2026, 1, 4)]
        assert all_missing == [date(2026, 1, 2), date(2026, 1, 4)]

    def test_empty_actual(self):
        """实际无数据：所有期望都算 missing，归到 internal。"""
        actual: list[date] = []
        expected = [date(2026, 1, 1), date(2026, 1, 2)]
        internal, tail, all_missing = MarketDataQualityService.detect_gaps(actual, expected)
        # 空实际时全部归 internal（首末不可判断）
        assert internal == []
        assert tail == []
        assert all_missing == [date(2026, 1, 1), date(2026, 1, 2)]


# =============================================================================
# 5. detect_factor_anomaly 纯函数测试
# =============================================================================


class TestDetectFactorAnomaly:
    """因子异常检测。"""

    def test_no_anomaly(self):
        """无跳变（正常累积因子）。"""
        factors = [1.0, 0.98, 0.97, 0.96]
        count, fmin, fmax = MarketDataQualityService.detect_factor_anomaly(factors)
        assert count == 0
        assert fmin == 0.96
        assert fmax == 1.0

    def test_with_jump(self):
        """有跳变（0.98→0.5 是 49% 变化）。"""
        factors = [1.0, 0.98, 0.5, 0.5]
        count, fmin, fmax = MarketDataQualityService.detect_factor_anomaly(factors)
        assert count == 1
        assert fmin == 0.5
        assert fmax == 1.0

    def test_all_none(self):
        """全 None。"""
        count, fmin, fmax = MarketDataQualityService.detect_factor_anomaly([None, None, None])
        assert count == 0
        assert fmin is None
        assert fmax is None

    def test_with_some_none(self):
        """部分 None（不计入跳变，min/max 排除 None）。"""
        factors = [1.0, None, 0.5]
        count, fmin, fmax = MarketDataQualityService.detect_factor_anomaly(factors)
        # None 被跳过，1.0→0.5 是 50% 跳变
        assert count == 1
        assert fmin == 0.5
        assert fmax == 1.0

    def test_all_unit(self):
        """全 1.0（无事件股票）。"""
        factors = [1.0, 1.0, 1.0]
        count, fmin, fmax = MarketDataQualityService.detect_factor_anomaly(factors)
        assert count == 0
        assert fmin == 1.0
        assert fmax == 1.0

    def test_empty(self):
        """空列表。"""
        count, fmin, fmax = MarketDataQualityService.detect_factor_anomaly([])
        assert count == 0
        assert fmin is None
        assert fmax is None


# =============================================================================
# 6. check_time_ordering 纯函数测试
# =============================================================================


class TestCheckTimeOrdering:
    """时间排序校验。"""

    def test_monotonic_increasing(self):
        """单调递增。"""
        times = [
            datetime(2026, 1, 1, 9, 30),
            datetime(2026, 1, 1, 9, 45),
            datetime(2026, 1, 1, 10, 0),
        ]
        assert MarketDataQualityService.check_time_ordering(times)

    def test_with_reverse(self):
        """含逆序。"""
        times = [
            datetime(2026, 1, 1, 10, 0),
            datetime(2026, 1, 1, 9, 30),
        ]
        assert not MarketDataQualityService.check_time_ordering(times)

    def test_equal_allowed(self):
        """允许相等。"""
        times = [
            datetime(2026, 1, 1, 9, 30),
            datetime(2026, 1, 1, 9, 30),
        ]
        assert MarketDataQualityService.check_time_ordering(times)

    def test_single(self):
        """单元素。"""
        assert MarketDataQualityService.check_time_ordering([datetime(2026, 1, 1, 9, 30)])

    def test_empty(self):
        """空列表。"""
        assert MarketDataQualityService.check_time_ordering([])


# =============================================================================
# 7. classify_missing_dates 分类测试
# =============================================================================


class TestClassifyMissingDates:
    """缺失日期分类逻辑。"""

    def test_not_listed(self):
        """上市日期之前的缺失 → NOT_LISTED。"""
        inst = _make_instrument(listing_date=date(2026, 6, 15))
        missing = [date(2026, 6, 10), date(2026, 6, 12)]
        result = MarketDataQualityService.classify_missing_dates(missing, inst)
        assert result[CLASS_NOT_LISTED] == ["2026-06-10", "2026-06-12"]
        assert result[CLASS_DB_MISSING] == []

    def test_delisted(self):
        """已退市标的的缺失 → DELISTED。"""
        inst = _make_instrument(status="delisted", listing_date=date(2020, 1, 1))
        missing = [date(2026, 6, 10)]
        result = MarketDataQualityService.classify_missing_dates(missing, inst)
        assert result[CLASS_DELISTED] == ["2026-06-10"]
        assert result[CLASS_DB_MISSING] == []

    def test_suspended(self):
        """停牌标的的缺失 → SUSPENDED。"""
        inst = _make_instrument(status="suspended", listing_date=date(2020, 1, 1))
        missing = [date(2026, 6, 10)]
        result = MarketDataQualityService.classify_missing_dates(missing, inst)
        assert result[CLASS_SUSPENDED] == ["2026-06-10"]
        assert result[CLASS_DB_MISSING] == []

    def test_db_missing(self):
        """活跃标的上市后的缺失 → DB_MISSING。"""
        inst = _make_instrument(status="active", listing_date=date(2020, 1, 1))
        missing = [date(2026, 6, 10)]
        result = MarketDataQualityService.classify_missing_dates(missing, inst)
        assert result[CLASS_DB_MISSING] == ["2026-06-10"]

    def test_mixed(self):
        """混合：部分上市前、部分上市后。"""
        inst = _make_instrument(status="active", listing_date=date(2026, 6, 15))
        missing = [date(2026, 6, 10), date(2026, 6, 20)]
        result = MarketDataQualityService.classify_missing_dates(missing, inst)
        assert result[CLASS_NOT_LISTED] == ["2026-06-10"]
        assert result[CLASS_DB_MISSING] == ["2026-06-20"]

    def test_no_listing_date(self):
        """listing_date=None 时所有缺失归 DB_MISSING（活跃）。"""
        inst = _make_instrument(status="active", listing_date=None)
        missing = [date(2026, 6, 10)]
        result = MarketDataQualityService.classify_missing_dates(missing, inst)
        assert result[CLASS_DB_MISSING] == ["2026-06-10"]
        assert result[CLASS_NOT_LISTED] == []

    def test_empty(self):
        """空缺失列表。"""
        inst = _make_instrument()
        result = MarketDataQualityService.classify_missing_dates([], inst)
        assert all(v == [] for v in result.values())


# =============================================================================
# 8. run_key / parameter_hash 测试
# =============================================================================


class TestRunKeyAndHash:
    """run_key 与 parameter_hash 生成。"""

    def test_run_key_format(self):
        """run_key 格式：mdq:{timeframe}:{start}:{end}:{algorithm_version}:scan。"""
        rk = MarketDataQualityService.make_run_key(
            "1d", date(2026, 1, 1), date(2026, 7, 30),
        )
        # [P0-5] 新格式末尾追加 :scan
        assert rk == "mdq:1d:2026-01-01:2026-07-30:mdq-v1.0.0:scan"

    def test_run_key_different_timeframe(self):
        """不同 timeframe 产生不同 run_key。"""
        rk_1d = MarketDataQualityService.make_run_key("1d", date(2026, 1, 1), date(2026, 7, 30))
        rk_15m = MarketDataQualityService.make_run_key("15m", date(2026, 1, 1), date(2026, 7, 30))
        assert rk_1d != rk_15m

    def test_parameter_hash_deterministic(self):
        """相同参数产生相同 hash。"""
        h1 = MarketDataQualityService.make_parameter_hash(
            "1d", date(2026, 1, 1), date(2026, 7, 30),
        )
        h2 = MarketDataQualityService.make_parameter_hash(
            "1d", date(2026, 1, 1), date(2026, 7, 30),
        )
        assert h1 == h2

    def test_parameter_hash_different_params(self):
        """不同参数产生不同 hash。"""
        h1 = MarketDataQualityService.make_parameter_hash(
            "1d", date(2026, 1, 1), date(2026, 7, 30),
        )
        h2 = MarketDataQualityService.make_parameter_hash(
            "15m", date(2026, 1, 1), date(2026, 7, 30),
        )
        h3 = MarketDataQualityService.make_parameter_hash(
            "1d", date(2026, 1, 1), date(2026, 7, 30), repair_mode=True,
        )
        assert h1 != h2
        assert h1 != h3

    def test_algorithm_version_constant(self):
        """算法版本常量正确。"""
        assert MDQ_ALGORITHM_VERSION == "mdq-v1.0.0"

    # [P0-5] 新增：verification run_key 必须不同于 scan run_key
    def test_verification_run_key_differs_from_scan(self):
        """verification 模式 run_key 必须与 scan 不同。"""
        scan_rk = MarketDataQualityService.make_run_key(
            "1d", date(2026, 1, 1), date(2026, 7, 30), mode="scan",
        )
        # 用 source_repair_run_id
        src_id = uuid.uuid4()
        ver_rk_src = MarketDataQualityService.make_run_key(
            "1d", date(2026, 1, 1), date(2026, 7, 30),
            mode="verification", source_repair_run_id=src_id,
        )
        assert ver_rk_src != scan_rk
        assert "verification" in ver_rk_src
        assert str(src_id) in ver_rk_src

        # 用 verification_seq
        ver_rk_seq = MarketDataQualityService.make_run_key(
            "1d", date(2026, 1, 1), date(2026, 7, 30),
            mode="verification", verification_seq=3,
        )
        assert ver_rk_seq != scan_rk
        assert "verification" in ver_rk_seq
        assert "seq=3" in ver_rk_seq

    def test_verification_parameter_hash_differs_from_scan(self):
        """verification 模式 parameter_hash 必须与 scan 不同。"""
        h_scan = MarketDataQualityService.make_parameter_hash(
            "1d", date(2026, 1, 1), date(2026, 7, 30), mode="scan",
        )
        h_ver = MarketDataQualityService.make_parameter_hash(
            "1d", date(2026, 1, 1), date(2026, 7, 30),
            mode="verification",
            source_repair_run_id=uuid.uuid4(),
        )
        assert h_ver != h_scan

    def test_verification_mode_requires_source_or_seq(self):
        """verification 模式必须提供 source_repair_run_id 或 verification_seq。"""
        import asyncio

        db = MagicMock()
        with pytest.raises(ValueError, match="verification 模式必须提供"):
            asyncio.get_event_loop().run_until_complete(
                MarketDataQualityService.create_run(
                    db, timeframe="1d",
                    start_date=date(2026, 1, 1), end_date=date(2026, 7, 30),
                    mode="verification",  # 缺 source_repair_run_id 和 verification_seq
                )
            )

    def test_invalid_mode_rejected(self):
        """非法 mode 抛 ValueError。"""
        import asyncio

        db = MagicMock()
        with pytest.raises(ValueError, match="不支持的 mode"):
            asyncio.get_event_loop().run_until_complete(
                MarketDataQualityService.create_run(
                    db, timeframe="1d",
                    start_date=date(2026, 1, 1), end_date=date(2026, 7, 30),
                    mode="invalid_mode",
                )
            )


# =============================================================================
# 9. ScanResult 数据类测试
# =============================================================================


class TestScanResult:
    """ScanResult 不可变数据类。"""

    def test_to_dict(self):
        """to_dict 包含所有字段。"""
        sr = ScanResult(
            issue_type=ISSUE_NO_ISSUE,
            issue_reason=None,
            severity="info",
            classification=CLASS_OK,
            bar_count=100,
            expected_bar_count=100,
        )
        d = sr.to_dict()
        assert d["issue_type"] == ISSUE_NO_ISSUE
        assert d["classification"] == CLASS_OK
        assert d["bar_count"] == 100
        assert d["missing_dates"] == []
        assert d["first_bar_date"] is None

    def test_frozen(self):
        """ScanResult 不可变。"""
        sr = ScanResult(
            issue_type=ISSUE_NO_ISSUE, issue_reason=None, severity="info",
        )
        with pytest.raises(AttributeError):
            sr.issue_type = ISSUE_INTERNAL_GAP  # type: ignore[misc]


# =============================================================================
# 10. scan_instrument 综合 classification 测试（mock DB）
# =============================================================================


class TestScanInstrumentClassification:
    """scan_instrument 综合 classification 逻辑（mock DB 查询）。

    通过 mock db.execute 返回不同的查询结果，验证各种 classification 路径。
    """

    def _make_mock_db(
        self,
        calendar_dates: list[date],
        bar_rows: list[tuple] | None,
        timeframe: str = "1d",
    ) -> MagicMock:
        """构造 mock AsyncSession。

        Args:
            calendar_dates: 交易日历返回的日期列表
            bar_rows: bars_daily/bars_15min 返回的行列表（None 表示无数据）
            timeframe: 影响查询调用顺序
        """
        db = MagicMock()

        # 模拟 execute 返回不同结果（按调用顺序）
        # scan_instrument 1d 调用顺序：
        #   1. select TradingCalendar → 返回 calendar_dates
        #   2. _query_daily_for_scan → 返回 bar_rows
        results = []

        # calendar 查询结果
        cal_result = MagicMock()
        cal_result.all.return_value = [(d,) for d in calendar_dates]
        results.append(cal_result)

        # bar 查询结果
        bar_result = MagicMock()
        bar_result.all.return_value = bar_rows or []
        results.append(bar_result)

        db.execute = AsyncMock(side_effect=results)
        return db

    @pytest.mark.asyncio
    async def test_not_listed_before_listing_date(self):
        """标的在扫描区间结束前未上市 → NOT_LISTED。"""
        db = MagicMock()
        inst = _make_instrument(listing_date=date(2026, 12, 31))
        run = MagicMock()
        run.timeframe = "1d"
        run.start_date = date(2026, 1, 1)
        run.end_date = date(2026, 7, 30)

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 7, 30),
        )
        # 不应查询 DB（提前返回）
        assert result.classification == CLASS_NOT_LISTED
        assert result.issue_type == ISSUE_NO_ISSUE
        assert result.expected_bar_count == 0

    @pytest.mark.asyncio
    async def test_db_missing_no_bars(self):
        """DB 无数据但有期望交易日 → DB_MISSING。"""
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5), date(2026, 1, 6)],
            bar_rows=None,
        )
        inst = _make_instrument(listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"
        run.start_date = date(2026, 1, 1)
        run.end_date = date(2026, 1, 31)

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_DB_MISSING
        assert result.issue_type == ISSUE_INTERNAL_GAP
        assert result.severity == "error"
        assert len(result.missing_dates) == 2

    @pytest.mark.asyncio
    async def test_ok_complete_bars(self):
        """完整数据无问题 → OK。"""
        # 构造 3 个交易日的完整 bar 数据
        bar_rows = [
            (date(2026, 1, 5), Decimal("10"), Decimal("11"), Decimal("9"),
             Decimal("10.5"), Decimal("100"), Decimal("1000"), Decimal("1.0")),
            (date(2026, 1, 6), Decimal("10.5"), Decimal("11"), Decimal("10"),
             Decimal("10.8"), Decimal("200"), Decimal("2000"), Decimal("1.0")),
        ]
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5), date(2026, 1, 6)],
            bar_rows=bar_rows,
        )
        inst = _make_instrument(listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_OK
        assert result.issue_type == ISSUE_NO_ISSUE
        assert result.bar_count == 2
        assert result.expected_bar_count == 2

    @pytest.mark.asyncio
    async def test_factor_missing_none_factor(self):
        """adj_factor 含 None → FACTOR_MISSING。"""
        bar_rows = [
            (date(2026, 1, 5), Decimal("10"), Decimal("11"), Decimal("9"),
             Decimal("10.5"), Decimal("100"), Decimal("1000"), None),
            (date(2026, 1, 6), Decimal("10.5"), Decimal("11"), Decimal("10"),
             Decimal("10.8"), Decimal("200"), Decimal("2000"), Decimal("1.0")),
        ]
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5), date(2026, 1, 6)],
            bar_rows=bar_rows,
        )
        inst = _make_instrument(listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_FACTOR_MISSING
        assert result.issue_type == ISSUE_FACTOR_MISSING
        assert result.severity == "error"

    @pytest.mark.asyncio
    async def test_internal_gap_db_missing(self):
        """中间有缺口（DB 缺失）→ DB_MISSING + INTERNAL_GAP。"""
        # 期望 3 天，实际只有第 1、3 天
        bar_rows = [
            (date(2026, 1, 5), Decimal("10"), Decimal("11"), Decimal("9"),
             Decimal("10.5"), Decimal("100"), Decimal("1000"), Decimal("1.0")),
            (date(2026, 1, 7), Decimal("10.5"), Decimal("11"), Decimal("10"),
             Decimal("10.8"), Decimal("200"), Decimal("2000"), Decimal("1.0")),
        ]
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)],
            bar_rows=bar_rows,
        )
        inst = _make_instrument(listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_DB_MISSING
        assert result.issue_type == ISSUE_INTERNAL_GAP
        assert result.severity == "warning"

    @pytest.mark.asyncio
    async def test_tail_gap_db_missing(self):
        """末尾缺口 → DB_MISSING + TAIL_GAP。"""
        # 期望 3 天，实际只有前 2 天
        bar_rows = [
            (date(2026, 1, 5), Decimal("10"), Decimal("11"), Decimal("9"),
             Decimal("10.5"), Decimal("100"), Decimal("1000"), Decimal("1.0")),
            (date(2026, 1, 6), Decimal("10.5"), Decimal("11"), Decimal("10"),
             Decimal("10.8"), Decimal("200"), Decimal("2000"), Decimal("1.0")),
        ]
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)],
            bar_rows=bar_rows,
        )
        inst = _make_instrument(listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_DB_MISSING
        assert result.issue_type == ISSUE_TAIL_GAP

    @pytest.mark.asyncio
    async def test_delisted_classification(self):
        """已退市 + 完整数据 → DELISTED + NO_ISSUE。"""
        bar_rows = [
            (date(2026, 1, 5), Decimal("10"), Decimal("11"), Decimal("9"),
             Decimal("10.5"), Decimal("100"), Decimal("1000"), Decimal("1.0")),
        ]
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5)],
            bar_rows=bar_rows,
        )
        inst = _make_instrument(status="delisted", listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_DELISTED
        assert result.issue_type == ISSUE_NO_ISSUE

    @pytest.mark.asyncio
    async def test_suspended_classification(self):
        """停牌 + 完整数据 → SUSPENDED + NO_ISSUE。"""
        bar_rows = [
            (date(2026, 1, 5), Decimal("10"), Decimal("11"), Decimal("9"),
             Decimal("10.5"), Decimal("100"), Decimal("1000"), Decimal("1.0")),
        ]
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5)],
            bar_rows=bar_rows,
        )
        inst = _make_instrument(status="suspended", listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_SUSPENDED
        assert result.issue_type == ISSUE_NO_ISSUE

    @pytest.mark.asyncio
    async def test_ohlc_invalid(self):
        """OHLC 非法 → OK + OHLC_INVALID。"""
        # high < open
        bar_rows = [
            (date(2026, 1, 5), Decimal("11"), Decimal("10"), Decimal("9"),
             Decimal("10.5"), Decimal("100"), Decimal("1000"), Decimal("1.0")),
        ]
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5)],
            bar_rows=bar_rows,
        )
        inst = _make_instrument(listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_OK
        assert result.issue_type == ISSUE_OHLC_INVALID
        assert result.severity == "error"

    @pytest.mark.asyncio
    async def test_factor_anomaly(self):
        """因子跳变 → OK + FACTOR_ANOMALY。"""
        # 1.0 → 0.5 是 50% 跳变
        bar_rows = [
            (date(2026, 1, 5), Decimal("10"), Decimal("11"), Decimal("9"),
             Decimal("10.5"), Decimal("100"), Decimal("1000"), Decimal("1.0")),
            (date(2026, 1, 6), Decimal("10.5"), Decimal("11"), Decimal("10"),
             Decimal("10.8"), Decimal("200"), Decimal("2000"), Decimal("0.5")),
        ]
        db = self._make_mock_db(
            calendar_dates=[date(2026, 1, 5), date(2026, 1, 6)],
            bar_rows=bar_rows,
        )
        inst = _make_instrument(listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_OK
        assert result.issue_type == ISSUE_FACTOR_ANOMALY
        assert result.factor_anomaly_count == 1

    @pytest.mark.asyncio
    async def test_no_expected_days_ok(self):
        """区间内无期望交易日 → OK。"""
        db = self._make_mock_db(
            calendar_dates=[],  # 无期望交易日
            bar_rows=None,
        )
        inst = _make_instrument(listing_date=date(2020, 1, 1))
        run = MagicMock()
        run.timeframe = "1d"

        result = await MarketDataQualityService.scan_instrument(
            db, run=run, instrument=inst,
            timeframe="1d", start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        assert result.classification == CLASS_OK
        assert result.expected_bar_count == 0


# =============================================================================
# 11. 幂等性测试（mock DB）
# =============================================================================


class TestIdempotency:
    """run_key 幂等性测试。"""

    @pytest.mark.asyncio
    async def test_create_run_returns_existing_succeeded(self):
        """已存在 succeeded run 时直接复用。"""
        from app.models.market_data_quality import MarketDataQualityRun

        existing_run = MarketDataQualityRun(
            id=uuid.uuid4(),
            # [P0-5] run_key 新格式含 :scan 后缀
            run_key="mdq:1d:2026-01-01:2026-07-30:mdq-v1.0.0:scan",
            timeframe="1d",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 7, 30),
            algorithm_version=MDQ_ALGORITHM_VERSION,
            parameter_hash="abc",
            status="succeeded",
            total_instruments=100,
            succeeded_count=100,
        )

        db = MagicMock()
        # 第一次 execute 返回已存在 run
        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = existing_run
        db.execute = AsyncMock(return_value=existing_result)

        result = await MarketDataQualityService.create_run(
            db, timeframe="1d",
            start_date=date(2026, 1, 1), end_date=date(2026, 7, 30),
        )
        assert result is existing_run
        assert result.status == "succeeded"

    @pytest.mark.asyncio
    async def test_create_run_invalid_timeframe(self):
        """非法 timeframe 抛 ValueError。"""
        db = MagicMock()
        with pytest.raises(ValueError, match="不支持的 timeframe"):
            await MarketDataQualityService.create_run(
                db, timeframe="1h",
                start_date=date(2026, 1, 1), end_date=date(2026, 7, 30),
            )


# =============================================================================
# 12. 常量与枚举值测试
# =============================================================================


class TestConstants:
    """常量与枚举值正确性。"""

    def test_algorithm_version(self):
        assert MDQ_ALGORITHM_VERSION == "mdq-v1.0.0"

    def test_15m_thresholds(self):
        assert MDQ_15M_MIN_BARS == 500
        assert MDQ_15M_MIN_BARS < 4000

    def test_classification_constants(self):
        """所有 classification 常量值唯一。"""
        classes = {
            CLASS_NOT_LISTED, CLASS_SUSPENDED, CLASS_DELISTED,
            CLASS_SOURCE_MISSING, CLASS_DB_MISSING, CLASS_FACTOR_MISSING, CLASS_OK,
        }
        assert len(classes) == 7

    def test_issue_type_constants(self):
        """所有 issue_type 常量值唯一。"""
        issues = {
            ISSUE_NO_ISSUE, ISSUE_INTERNAL_GAP, ISSUE_TAIL_GAP, ISSUE_DUPLICATE,
            ISSUE_TIME_REVERSED, ISSUE_OHLC_INVALID, ISSUE_VOLUME_ANOMALY,
            ISSUE_AMOUNT_ANOMALY, ISSUE_FACTOR_MISSING, ISSUE_FACTOR_ANOMALY,
            ISSUE_BAR_COUNT_INSUFFICIENT,
        }
        assert len(issues) == 11


# =============================================================================
# 13. [P0-5] execute_repair 全批次完成 + resolve_last_completed_trading_day
# =============================================================================


class TestExecuteRepairFullBatch:
    """[P0-5] execute_repair 必须处理全部 eligible items，禁止只处理 batch_size 条。

    旧 BUG：`while processed < total_candidates and processed < batch_size` 把
    batch_size 当作总数上限，导致 batch_size=10 默认下只修复 10 条后停止。

    修复后：`while True: ... if not candidates: break`，循环到无未处理候选为止。
    batch_size 仅控制单批拉取数量（吞吐批次），不限制总量。
    """

    @pytest.mark.asyncio
    async def test_repair_processes_all_eligible_items(self):
        """25 条 DB_MISSING + batch_size=10 → 必须处理全部 25 条。"""
        from app.models.market_data_quality import (
            MarketDataQualityItem,
        )

        # 构造 1 个 run + 25 个 DB_MISSING items
        run_id = uuid.uuid4()
        run = MagicMock()
        run.id = run_id
        run.timeframe = "1d"
        run.start_date = date(2026, 1, 1)
        run.end_date = date(2026, 7, 30)

        items = [
            MarketDataQualityItem(
                id=uuid.uuid4(),
                run_id=run_id,
                instrument_id=uuid.uuid4(),
                symbol=f"{i:06d}",
                status="succeeded",  # 扫描已完成
                classification=CLASS_DB_MISSING,
                repair_attempted=False,
            )
            for i in range(25)
        ]

        # mock db.execute 按调用顺序返回不同结果
        db = MagicMock()
        # 1. select run
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = run
        # 2. count(DB_MISSING, repair_attempted=False) → 25 (初始)
        count_result = MagicMock()
        count_result.scalar.return_value = 25
        # 3. 每批 select items 返回 batch_size=10 条（共 3 批：10+10+5）
        # 4. repair_instrument 内部会调用 db.execute（update item）

        # 简化：mock repair_instrument 为成功，避免内部 db.execute
        # 直接 patch MarketDataQualityService.repair_instrument
        original_repair = MarketDataQualityService.repair_instrument

        call_count = {"n": 0}

        async def _mock_repair(db_arg, *, run, item):
            call_count["n"] += 1
            return {
                "repaired": True,
                "message": "repaired",
                "repaired_dates": [],
            }

        # 模拟 batch 查询：第 1 批返回前 10，第 2 批返回中间 10，第 3 批返回最后 5，第 4 批空
        # 每批查询返回 MagicMock with scalars().all()
        def _make_batch_result(batch_items):
            r = MagicMock()
            scalars_mock = MagicMock()
            scalars_mock.all.return_value = batch_items
            r.scalars.return_value = scalars_mock
            return r

        batch1 = _make_batch_result(items[0:10])
        batch2 = _make_batch_result(items[10:20])
        batch3 = _make_batch_result(items[20:25])
        batch_empty = _make_batch_result([])

        # mock db.execute：按调用顺序
        # 调用顺序：
        # 1. select run → run_result
        # 2. count → count_result
        # 3. select batch1 → batch1
        # 4. (内部 repair_instrument 不调用 db，已 mock)
        # 5. flush (AsyncMock)
        # 6. select batch2 → batch2
        # 7. flush
        # 8. select batch3 → batch3
        # 9. flush
        # 10. select batch_empty → batch_empty (break)
        db.execute = AsyncMock(side_effect=[
            run_result, count_result,
            batch1, batch2, batch3, batch_empty,
        ])
        db.flush = AsyncMock()

        try:
            MarketDataQualityService.repair_instrument = staticmethod(_mock_repair)
            result = await MarketDataQualityService.execute_repair(
                db, run_id=run_id, batch_size=10, dry_run=False,
            )
        finally:
            MarketDataQualityService.repair_instrument = original_repair

        # 断言：必须处理全部 25 条
        assert call_count["n"] == 25, (
            f"应处理 25 条，实际处理 {call_count['n']}（旧 BUG 会只处理 10 条）"
        )
        assert result["processed"] == 25
        assert result["repaired"] == 25
        assert result["total_candidates"] == 25  # 初始候选数
        assert result["dry_run"] is False

    @pytest.mark.asyncio
    async def test_repair_dry_run_does_not_process(self):
        """dry_run=True 只统计不处理。"""
        run_id = uuid.uuid4()
        run = MagicMock()
        run.id = run_id
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = run

        count_result = MagicMock()
        count_result.scalar.return_value = 30

        db = MagicMock()
        db.execute = AsyncMock(side_effect=[run_result, count_result])

        result = await MarketDataQualityService.execute_repair(
            db, run_id=run_id, batch_size=10, dry_run=True,
        )
        assert result["dry_run"] is True
        assert result["total_candidates"] == 30
        assert result["repaired"] == 0
        assert "processed" not in result or result.get("processed", 0) == 0

    @pytest.mark.asyncio
    async def test_repair_zero_candidates_exits_cleanly(self):
        """无候选时立即退出，不报错。"""
        run_id = uuid.uuid4()
        run = MagicMock()
        run.id = run_id
        run_result = MagicMock()
        run_result.scalar_one_or_none.return_value = run

        count_result = MagicMock()
        count_result.scalar.return_value = 0  # 无候选

        # 当 total_candidates=0 且 dry_run=False，仍会进入 while 循环
        # 第一批 select 返回空 → break
        empty_batch = MagicMock()
        empty_scalars = MagicMock()
        empty_scalars.all.return_value = []
        empty_batch.scalars.return_value = empty_scalars

        db = MagicMock()
        db.execute = AsyncMock(side_effect=[run_result, count_result, empty_batch])
        db.flush = AsyncMock()

        result = await MarketDataQualityService.execute_repair(
            db, run_id=run_id, batch_size=10, dry_run=False,
        )
        assert result["processed"] == 0
        assert result["repaired"] == 0
        assert result["total_candidates"] == 0


class TestResolveLastCompletedTradingDay:
    """[P0-5] resolve_last_completed_trading_day 测试。

    修复后 end_date 必须为"已收盘的最近一个交易日"：
    - 16:00 CST 之前 today 未收盘 → 取前一交易日
    - 16:00 CST 之后 today 是交易日 → 取 today
    - today 非交易日 → 取之前最近交易日
    - 跨午夜场景：用户本机时区非 CST，today 在 CST 还是昨日
    """

    @pytest.mark.asyncio
    async def test_returns_last_trading_day_before_today_when_pre_close(self):
        """16:00 CST 前 → 取 < today 的最近交易日。"""
        from app.services.market_data_quality_service import (
            resolve_last_completed_trading_day,
        )

        # mock db.execute 返回 2026-07-29（today=2026-07-30 周四，假设 14:00 CST）
        db = MagicMock()
        result_mock = MagicMock()
        result_mock.first.return_value = (date(2026, 7, 29),)
        db.execute = AsyncMock(return_value=result_mock)

        # now_cst=14:00 < 16:00 → 查询 < today 的交易日
        now_cst = datetime(2026, 7, 30, 14, 0, 0)
        result = await resolve_last_completed_trading_day(
            db, today=date(2026, 7, 30), now_cst=now_cst,
        )
        assert result == date(2026, 7, 29)

    @pytest.mark.asyncio
    async def test_returns_today_when_post_close_and_trading_day(self):
        """16:00 CST 后 + today 是交易日 → 取 today。"""
        from app.services.market_data_quality_service import (
            resolve_last_completed_trading_day,
        )

        db = MagicMock()
        result_mock = MagicMock()
        result_mock.first.return_value = (date(2026, 7, 30),)
        db.execute = AsyncMock(return_value=result_mock)

        # now_cst=17:00 >= 16:00 → 查询 <= today 的交易日
        now_cst = datetime(2026, 7, 30, 17, 0, 0)
        result = await resolve_last_completed_trading_day(
            db, today=date(2026, 7, 30), now_cst=now_cst,
        )
        assert result == date(2026, 7, 30)

    @pytest.mark.asyncio
    async def test_returns_previous_trading_day_when_today_is_weekend(self):
        """today 是周末 → 取最近交易日（如周五）。"""
        from app.services.market_data_quality_service import (
            resolve_last_completed_trading_day,
        )

        # 2026-07-25 是周六
        db = MagicMock()
        result_mock = MagicMock()
        result_mock.first.return_value = (date(2026, 7, 24),)  # 周五
        db.execute = AsyncMock(return_value=result_mock)

        # now_cst=17:00，today=周六非交易日 → 查询 <= today 的最近交易日
        now_cst = datetime(2026, 7, 25, 17, 0, 0)
        result = await resolve_last_completed_trading_day(
            db, today=date(2026, 7, 25), now_cst=now_cst,
        )
        assert result == date(2026, 7, 24)

    @pytest.mark.asyncio
    async def test_returns_none_when_calendar_empty(self):
        """交易日历无数据 → 返回 None（CLI 回退到 today）。"""
        from app.services.market_data_quality_service import (
            resolve_last_completed_trading_day,
        )

        db = MagicMock()
        result_mock = MagicMock()
        result_mock.first.return_value = None
        db.execute = AsyncMock(return_value=result_mock)

        now_cst = datetime(2026, 7, 30, 17, 0, 0)
        result = await resolve_last_completed_trading_day(
            db, today=date(2026, 7, 30), now_cst=now_cst,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cross_midnight_non_cst_timezone(self):
        """跨午夜场景：用户本机时间已到次日，但 CST 仍是当日 14:00。

        用户本机 today=2026-07-31，但 CST 仍是 07-30 14:00（未收盘）。
        应查 < today=07-31 的交易日（但 07-30 未收盘不能算）。
        由于 today 参数是 07-31，< 07-31 会返回 07-30；但 07-30 未收盘，
        应返回 07-29。
        """
        from app.services.market_data_quality_service import (
            resolve_last_completed_trading_day,
        )

        db = MagicMock()
        # 返回 07-29（< 07-31 的最近交易日，跳过未收盘的 07-30）
        result_mock = MagicMock()
        result_mock.first.return_value = (date(2026, 7, 29),)
        db.execute = AsyncMock(return_value=result_mock)

        # now_cst.hour=14 < 16 → 查 < today 的交易日
        # today=07-31（用户本机），< 07-31 最近的交易日是 07-30，
        # 但 07-30 在 CST 视角未收盘 → 应返回 07-29（mock 控制）
        now_cst = datetime(2026, 7, 30, 14, 0, 0)  # CST 仍是 07-30 14:00
        result = await resolve_last_completed_trading_day(
            db, today=date(2026, 7, 31), now_cst=now_cst,
        )
        assert result == date(2026, 7, 29)


# =============================================================================
# 13. PG 集成测试（仅 CI 环境）
# =============================================================================


@pytest.mark.skipif(
    not _CI_ENV,
    reason="PostgreSQL 集成测试只在 CI 临时 Postgres 容器中运行；本地请用 PURE_UNIT_TEST=1",
)
class TestPGIntegration:
    """PG 集成测试：完整 scan + repair 流程。

    只在 CI 环境（GITHUB_ACTIONS=true 或 PANJI_CI_DB_TEST=1）运行。
    本地 PURE_UNIT_TEST=1 时自动 skip。
    """

    @pytest.mark.asyncio
    async def test_create_run_and_scan(self, db_session):
        """创建 run + 扫描一只股票的完整流程。"""
        from app.models.bar import BarDaily
        from app.models.calendar import TradingCalendar
        from app.models.market_data_quality import MarketDataQualityItem

        # 1. 准备测试数据
        inst = Instrument(
            id=uuid.uuid4(),
            symbol="600000",
            name="测试股票",
            market="SH",
            status="active",
            listing_date=date(2020, 1, 1),
        )
        db_session.add(inst)
        await db_session.flush()

        # 添加交易日历
        for d in [date(2026, 1, 5), date(2026, 1, 6)]:
            db_session.add(TradingCalendar(
                trade_date=d,
                is_trading_day=True,
                market="A",
                source="MANUAL_OVERRIDE",
                status="OPEN",
            ))

        # 添加完整 bar 数据
        db_session.add(BarDaily(
            instrument_id=inst.id,
            trade_date=date(2026, 1, 5),
            open=Decimal("10"), high=Decimal("11"), low=Decimal("9"),
            close=Decimal("10.5"), volume=Decimal("100"), amount=Decimal("1000"),
            adj_factor=Decimal("1.0"),
        ))
        db_session.add(BarDaily(
            instrument_id=inst.id,
            trade_date=date(2026, 1, 6),
            open=Decimal("10.5"), high=Decimal("11"), low=Decimal("10"),
            close=Decimal("10.8"), volume=Decimal("200"), amount=Decimal("2000"),
            adj_factor=Decimal("1.0"),
        ))
        await db_session.flush()

        # 2. 创建 run（注意：stock_symbol_sql_filter 会过滤，需用真实 A 股代码）
        run = await MarketDataQualityService.create_run(
            db_session,
            timeframe="1d",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
        assert run.total_instruments >= 1
        assert run.status == "created"

        # 3. 验证预创建 items
        items_result = await db_session.execute(
            select(MarketDataQualityItem)
            .where(MarketDataQualityItem.run_id == run.id)
        )
        items = items_result.scalars().all()
        assert len(items) >= 1
        assert all(item.status == "pending" for item in items)

    @pytest.mark.asyncio
    async def test_execute_scan_dry_run(self, db_session):
        """execute_scan dry-run 模式只统计不写入。"""
        inst = Instrument(
            id=uuid.uuid4(),
            symbol="000001",
            name="测试股票",
            market="SZ",
            status="active",
            listing_date=date(2020, 1, 1),
        )
        db_session.add(inst)
        await db_session.flush()

        run = await MarketDataQualityService.create_run(
            db_session,
            timeframe="1d",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )

        result = await MarketDataQualityService.execute_scan(
            db_session, run_id=run.id, batch_size=50, dry_run=True,
        )
        assert result["dry_run"] is True
        assert "pending" in result
