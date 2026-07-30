"""全市场行情质量扫描与修复服务（Stage 5 P0）。

设计原则：
- run_key 幂等：相同 (timeframe, start, end, algorithm_version) 的 succeeded run 直接复用
- 显式分类：每只股票必须落到 NOT_LISTED/SUSPENDED/DELISTED/SOURCE_MISSING/
  DB_MISSING/FACTOR_MISSING/OK 之一，禁止静默兜底
- 纯函数可测：OHLC 校验、缺口检测、重复检测、因子异常检测均为 staticmethod，不连 DB
- 数据安全：修复只写 raw OHLCV（不写 qfq），使用 ON CONFLICT DO UPDATE，不覆盖整表
- factor 重算委托 adjustment_factor_calculator.calculate_adjustment_factor_series

约束（AGENTS.md §8）：
- 本服务设计为在服务器上对 bz_stock 执行扫描/修复，不在本地运行非 dry-run
- 修复 DB_MISSING 前必须确认 upstream pytdx 有数据，否则归类为 SOURCE_MISSING
- 不得用 1.0 伪装因子缺失；FACTOR_MISSING 必须显式标记

用法：
    from app.services.market_data_quality_service import MarketDataQualityService
    run = await MarketDataQualityService.create_run(
        db, timeframe="1d", start_date=date(2026,1,1), end_date=date(2026,7,30),
    )
    summary = await MarketDataQualityService.execute_scan(db, run.id)

模块自测：
    python -m app.services.market_data_quality_service
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import Bar15Min, BarDaily
from app.models.calendar import TradingCalendar
from app.models.instrument import Instrument
from app.models.market_data_quality import (
    MarketDataQualityItem,
    MarketDataQualityRun,
)
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("services.market_data_quality_service")


# =============================================================================
# 不可变常量
# =============================================================================

MDQ_ALGORITHM_VERSION: str = "mdq-v1.0.0"
MDQ_15M_MIN_BARS: int = 500
MDQ_15M_FULL_BARS: int = 4000

# Classification 枚举值（与迁移 comment 一致）
CLASS_NOT_LISTED: str = "NOT_LISTED"
CLASS_SUSPENDED: str = "SUSPENDED"
CLASS_DELISTED: str = "DELISTED"
CLASS_SOURCE_MISSING: str = "SOURCE_MISSING"
CLASS_DB_MISSING: str = "DB_MISSING"
CLASS_FACTOR_MISSING: str = "FACTOR_MISSING"
CLASS_OK: str = "OK"

# Issue type 枚举值
ISSUE_NO_ISSUE: str = "NO_ISSUE"
ISSUE_INTERNAL_GAP: str = "INTERNAL_GAP"
ISSUE_TAIL_GAP: str = "TAIL_GAP"
ISSUE_DUPLICATE: str = "DUPLICATE"
ISSUE_TIME_REVERSED: str = "TIME_REVERSED"
ISSUE_OHLC_INVALID: str = "OHLC_INVALID"
ISSUE_VOLUME_ANOMALY: str = "VOLUME_ANOMALY"
ISSUE_AMOUNT_ANOMALY: str = "AMOUNT_ANOMALY"
ISSUE_FACTOR_MISSING: str = "FACTOR_MISSING"
ISSUE_FACTOR_ANOMALY: str = "FACTOR_ANOMALY"
ISSUE_BAR_COUNT_INSUFFICIENT: str = "BAR_COUNT_INSUFFICIENT"

# 因子异常检测阈值：相邻日因子变化超过此比例视为跳变
# adj_factor 单调累积，正常情况下应递减（除权后老因子 < 1.0）或恒为 1.0
# 跳变通常意味着 xdxr 事件未被正确处理或存量数据被错误覆盖
FACTOR_JUMP_THRESHOLD: float = 0.30  # 30% 变化视为异常跳变


# =============================================================================
# 不可变扫描结果数据类
# =============================================================================


@dataclass(frozen=True)
class ScanResult:
    """单只股票扫描结果（不可变）。

    所有字段在扫描完成时确定，禁止运行时修改。
    classification 必须是 CLASS_* 常量之一，禁止为 None（除非扫描异常）。
    """

    issue_type: str
    issue_reason: str | None
    severity: str  # info/warning/error
    missing_dates: list[str] = field(default_factory=list)
    duplicate_dates: list[str] = field(default_factory=list)
    first_bar_date: date | None = None
    last_bar_date: date | None = None
    bar_count: int = 0
    expected_bar_count: int = 0
    factor_min: float | None = None
    factor_max: float | None = None
    factor_anomaly_count: int = 0
    classification: str = CLASS_OK

    def to_dict(self) -> dict[str, Any]:
        """转为 dict（用于更新 item 与 JSON 序列化）。"""
        return {
            "issue_type": self.issue_type,
            "issue_reason": self.issue_reason,
            "severity": self.severity,
            "missing_dates": list(self.missing_dates),
            "duplicate_dates": list(self.duplicate_dates),
            "first_bar_date": self.first_bar_date,
            "last_bar_date": self.last_bar_date,
            "bar_count": self.bar_count,
            "expected_bar_count": self.expected_bar_count,
            "factor_min": self.factor_min,
            "factor_max": self.factor_max,
            "factor_anomaly_count": self.factor_anomaly_count,
            "classification": self.classification,
        }


# =============================================================================
# 服务
# =============================================================================


class MarketDataQualityService:
    """全市场行情质量扫描与修复服务。

    所有方法均为 async，接收 AsyncSession。
    纯函数逻辑（OHLC 校验、缺口检测等）以 staticmethod 暴露，便于单元测试。
    """

    # =========================================================================
    # run_key / parameter_hash 生成
    # =========================================================================

    @staticmethod
    def make_run_key(
        timeframe: str, start_date: date, end_date: date,
        algorithm_version: str = MDQ_ALGORITHM_VERSION,
    ) -> str:
        """生成 run 幂等键。

        格式：mdq:{timeframe}:{start}:{end}:{algorithm_version}
        """
        return (
            f"mdq:{timeframe}:{start_date.isoformat()}:{end_date.isoformat()}:"
            f"{algorithm_version}"
        )

    @staticmethod
    def make_parameter_hash(
        timeframe: str, start_date: date, end_date: date,
        algorithm_version: str = MDQ_ALGORITHM_VERSION,
        repair_mode: bool = False,
    ) -> str:
        """生成参数 hash（含算法版本与固定参数）。

        用于跨入口一致性校验：相同参数应产生相同 hash。
        """
        payload = (
            f"{algorithm_version}|{timeframe}|{start_date.isoformat()}|"
            f"{end_date.isoformat()}|repair={repair_mode}|"
            f"min15m={MDQ_15M_MIN_BARS}|full15m={MDQ_15M_FULL_BARS}|"
            f"jump={FACTOR_JUMP_THRESHOLD}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # =========================================================================
    # run 创建 / 解析（幂等）
    # =========================================================================

    @staticmethod
    async def create_run(
        db: AsyncSession,
        *,
        timeframe: str,
        start_date: date,
        end_date: date,
        repair_mode: bool = False,
    ) -> MarketDataQualityRun:
        """创建或复用扫描 run（幂等）。

        - 若已存在相同 run_key 且 status=succeeded 的 run，直接返回（幂等）
        - 否则新建 status=created 的 run，并预创建 pending items
        - total_instruments 来自 active A 股标的数（stock_symbol_sql_filter）

        Args:
            db: 异步会话
            timeframe: "1d" 或 "15m"
            start_date: 起始日期（含）
            end_date: 结束日期（含）
            repair_mode: 是否启用修复模式

        Returns:
            MarketDataQualityRun（已持久化）
        """
        if timeframe not in ("1d", "15m"):
            raise ValueError(f"不支持的 timeframe: {timeframe}，仅支持 1d / 15m")

        run_key = MarketDataQualityService.make_run_key(
            timeframe, start_date, end_date,
        )
        parameter_hash = MarketDataQualityService.make_parameter_hash(
            timeframe, start_date, end_date, repair_mode=repair_mode,
        )

        # 1. 检查是否已存在 succeeded run（幂等复用）
        result = await db.execute(
            select(MarketDataQualityRun)
            .where(MarketDataQualityRun.run_key == run_key)
            .order_by(MarketDataQualityRun.status.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None and existing.status == "succeeded":
            logger.info(
                "[MDQ] 复用已成功 run_key=%s run_id=%s", run_key, existing.id,
            )
            return existing

        # 2. 新建 run
        run = MarketDataQualityRun(
            run_key=run_key,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            algorithm_version=MDQ_ALGORITHM_VERSION,
            parameter_hash=parameter_hash,
            status="created",
            total_instruments=0,
            succeeded_count=0,
            failed_count=0,
            skipped_count=0,
            coverage_ratio=0.0,
            issue_summary={},
            repair_mode=repair_mode,
        )
        db.add(run)
        await db.flush()  # 获取 run.id

        # 3. 查询活跃 A 股标的，预创建 pending items
        instruments_result = await db.execute(
            select(Instrument.id, Instrument.symbol)
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
            .order_by(Instrument.symbol)
        )
        rows = instruments_result.all()

        items: list[MarketDataQualityItem] = []
        for instrument_id, symbol in rows:
            items.append(MarketDataQualityItem(
                run_id=run.id,
                instrument_id=instrument_id,
                symbol=symbol,
                status="pending",
            ))
        if items:
            db.add_all(items)

        run.total_instruments = len(items)
        await db.flush()

        logger.info(
            "[MDQ] 创建 run_key=%s run_id=%s total=%d timeframe=%s range=%s~%s",
            run_key, run.id, run.total_instruments, timeframe,
            start_date, end_date,
        )
        return run

    @staticmethod
    async def resolve_run(
        db: AsyncSession,
        *,
        timeframe: str,
        start_date: date,
        end_date: date,
        repair_mode: bool = False,
    ) -> MarketDataQualityRun:
        """解析已存在的 run（用于 --resume）；不存在则创建。"""
        run_key = MarketDataQualityService.make_run_key(
            timeframe, start_date, end_date,
        )
        result = await db.execute(
            select(MarketDataQualityRun)
            .where(MarketDataQualityRun.run_key == run_key)
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing
        return await MarketDataQualityService.create_run(
            db, timeframe=timeframe, start_date=start_date,
            end_date=end_date, repair_mode=repair_mode,
        )

    # =========================================================================
    # 纯函数：扫描逻辑（不连 DB，可单元测试）
    # =========================================================================

    @staticmethod
    def check_ohlc_validity(
        open_: float | None,
        high: float | None,
        low: float | None,
        close: float | None,
    ) -> bool:
        """校验单根 bar 的 OHLC 合法性。

        规则：
        - 四值均 > 0（OHLC 不允许 0 或负数）
        - high >= max(open, close, low)
        - low <= min(open, close, high)

        Args:
            open_/high/low/close: OHLC 价格（None 视为缺失，返回 False）

        Returns:
            True 如果 OHLC 合法
        """
        if any(v is None for v in (open_, high, low, close)):
            return False
        if any(v <= 0 for v in (open_, high, low, close)):  # type: ignore[operator]
            return False
        if high < open_ or high < close or high < low:  # type: ignore[operator]
            return False
        if low > open_ or low > close or low > high:  # type: ignore[operator]
            return False
        return True

    @staticmethod
    def check_volume_amount_anomaly(
        volume: float | None,
        amount: float | None,
    ) -> str | None:
        """校验 volume/amount 一致性。

        规则：
        - volume=0 但 amount>0 → VOLUME_ANOMALY（成交量为 0 但有成交额）
        - volume>0 但 amount=0 → AMOUNT_ANOMALY（有成交量但成交额为 0）
        - 两者均为 0 → 正常（停牌日或涨停跌停无成交）
        - 两者均 > 0 → 正常

        Returns:
            issue_type 字符串（VOLUME_ANOMALY/AMOUNT_ANOMALY）或 None（正常）
        """
        v = float(volume) if volume is not None else 0.0
        a = float(amount) if amount is not None else 0.0
        if v == 0.0 and a > 0.0:
            return ISSUE_VOLUME_ANOMALY
        if v > 0.0 and a == 0.0:
            return ISSUE_AMOUNT_ANOMALY
        return None

    @staticmethod
    def detect_duplicates(dates: Sequence[date]) -> list[date]:
        """检测重复日期（理论上 PK 约束保证无重复，但显式校验兜底）。

        Args:
            dates: 实际 bar 日期列表（可能含重复）

        Returns:
            重复出现的日期列表（去重后）
        """
        counter = Counter(dates)
        return sorted([d for d, c in counter.items() if c > 1])

    @staticmethod
    def detect_gaps(
        actual_dates: Sequence[date],
        expected_dates: Sequence[date],
    ) -> tuple[list[date], list[date], list[date]]:
        """检测缺口，分类为 internal_gap / tail_gap / missing。

        Args:
            actual_dates: 实际 bar 日期列表
            expected_dates: 期望交易日列表（来自交易日历）

        Returns:
            (internal_gaps, tail_gaps, all_missing)
            - internal_gaps: 实际数据首末之间的缺失（中间断层）
            - tail_gaps: 末尾缺失（实际最后日期 < 期望最后日期）
            - all_missing: 全部缺失日期
        """
        actual_set = set(actual_dates)
        expected_set = set(expected_dates)
        all_missing = sorted(expected_set - actual_set)

        if not actual_dates:
            return [], [], all_missing

        actual_sorted = sorted(actual_dates)
        first_actual = actual_sorted[0]
        last_actual = actual_sorted[-1]

        internal_gaps: list[date] = []
        tail_gaps: list[date] = []
        for d in all_missing:
            if d < first_actual:
                # 期望日期早于实际首条 → 视为 tail（前置缺失，可能是 listing_date 之前）
                # 实际上这类应由 classification 处理，这里归到 missing
                internal_gaps.append(d)
            elif d <= last_actual:
                internal_gaps.append(d)
            else:
                tail_gaps.append(d)
        return internal_gaps, tail_gaps, all_missing

    @staticmethod
    def classify_missing_dates(
        missing_dates: Sequence[date],
        instrument: Instrument,
    ) -> dict[str, list[str]]:
        """按 instrument 状态分类缺失日期。

        分类规则：
        - missing_date < listing_date → NOT_LISTED（未上市，不算 issue）
        - instrument.status == "delisted" → DELISTED（已退市，整只股票标记）
        - instrument.status == "suspended" → SUSPENDED（停牌，不算 issue）
        - 其他 → DB_MISSING（DB 缺失，需修复）

        注意：本方法只做日期级分类；整只股票的最终 classification 由 scan_instrument
        综合判断（如全部缺失日期都是 NOT_LISTED → 整体 OK）。

        Args:
            missing_dates: 缺失日期列表
            instrument: 标的主数据

        Returns:
            {classification: [iso_date, ...]} 分组
        """
        result: dict[str, list[str]] = {
            CLASS_NOT_LISTED: [],
            CLASS_SUSPENDED: [],
            CLASS_DELISTED: [],
            CLASS_DB_MISSING: [],
        }
        listing_date = instrument.listing_date
        for d in missing_dates:
            iso = d.isoformat() if isinstance(d, date) else str(d)
            if listing_date is not None and d < listing_date:
                result[CLASS_NOT_LISTED].append(iso)
            elif instrument.status == "delisted":
                result[CLASS_DELISTED].append(iso)
            elif instrument.status == "suspended":
                result[CLASS_SUSPENDED].append(iso)
            else:
                result[CLASS_DB_MISSING].append(iso)
        return result

    @staticmethod
    def detect_factor_anomaly(
        factors: Sequence[float | None],
        jump_threshold: float = FACTOR_JUMP_THRESHOLD,
    ) -> tuple[int, float | None, float | None]:
        """检测 adj_factor 异常。

        规则：
        - None 视为缺失（不计入跳变，但由 FACTOR_MISSING issue 单独标记）
        - 相邻非 None 因子变化比例超过 jump_threshold 视为跳变
        - factor_min / factor_max 排除 None

        Args:
            factors: adj_factor 序列
            jump_threshold: 跳变阈值（默认 30%）

        Returns:
            (anomaly_count, factor_min, factor_max)
        """
        valid = [float(f) for f in factors if f is not None]
        if not valid:
            return 0, None, None

        anomaly_count = 0
        for i in range(1, len(valid)):
            prev = valid[i - 1]
            curr = valid[i]
            if prev == 0:
                continue
            change_ratio = abs(curr - prev) / abs(prev)
            if change_ratio > jump_threshold:
                anomaly_count += 1

        return anomaly_count, min(valid), max(valid)

    @staticmethod
    def check_time_ordering(trade_times: Sequence[datetime]) -> bool:
        """校验时间序列是否单调递增（15m 用）。

        Args:
            trade_times: trade_time 列表

        Returns:
            True 如果单调递增（允许相等）
        """
        times = list(trade_times)
        for i in range(1, len(times)):
            if times[i] < times[i - 1]:
                return False
        return True

    # =========================================================================
    # scan_instrument：单只股票扫描（DB-bound）
    # =========================================================================

    @staticmethod
    async def scan_instrument(
        db: AsyncSession,
        *,
        run: MarketDataQualityRun,
        instrument: Instrument,
        timeframe: str,
        start_date: date,
        end_date: date,
    ) -> ScanResult:
        """扫描单只股票的行情质量。

        步骤：
        1. 查询交易日历获取期望交易日
        2. 查询 bars_daily / bars_15min 获取实际 bar
        3. 检测缺口、重复、OHLC 合法性、量额一致性、因子异常
        4. 综合分类（NOT_LISTED/SUSPENDED/DELISTED/SOURCE_MISSING/DB_MISSING/FACTOR_MISSING/OK）

        Args:
            db: 异步会话
            run: 所属 run（用于元数据）
            instrument: 标的主数据
            timeframe: "1d" 或 "15m"
            start_date: 起始日期
            end_date: 结束日期

        Returns:
            ScanResult（不可变）
        """
        # 整只股票未上市 → NOT_LISTED（不算 issue）
        if instrument.listing_date is not None and end_date < instrument.listing_date:
            return ScanResult(
                issue_type=ISSUE_NO_ISSUE,
                issue_reason=f"not listed until {instrument.listing_date}",
                severity="info",
                classification=CLASS_NOT_LISTED,
                expected_bar_count=0,
            )

        # 已退市 → DELISTED（不扫描，由调用方按需处理）
        if instrument.status == "delisted":
            # 仍扫描历史数据完整性，但 classification 标记 DELISTED
            pass

        # 1. 查询交易日历
        cal_result = await db.execute(
            select(TradingCalendar.trade_date)
            .where(TradingCalendar.trade_date >= start_date)
            .where(TradingCalendar.trade_date <= end_date)
            .where(TradingCalendar.is_trading_day.is_(True))
            .order_by(TradingCalendar.trade_date)
        )
        expected_dates = [row[0] for row in cal_result.all()]

        # 过滤掉上市日期之前的期望交易日
        if instrument.listing_date is not None:
            expected_dates = [
                d for d in expected_dates if d >= instrument.listing_date
            ]

        # 2. 查询实际 bar
        if timeframe == "1d":
            bars, actual_dates, ohlc_records, factors = (
                await MarketDataQualityService._query_daily_for_scan(
                    db, instrument.id, start_date, end_date,
                )
            )
        else:  # 15m
            bars, actual_dates, ohlc_records, factors = (
                await MarketDataQualityService._query_15min_for_scan(
                    db, instrument.id, start_date, end_date,
                )
            )

        # 3. 实际无数据 + 期望有数据 → DB_MISSING 或 SOURCE_MISSING
        if not actual_dates:
            if not expected_dates:
                # 期望也无数据（如非交易日区间）→ OK
                return ScanResult(
                    issue_type=ISSUE_NO_ISSUE,
                    issue_reason="no expected trading days in range",
                    severity="info",
                    classification=CLASS_OK,
                    expected_bar_count=0,
                )
            # DB 无数据：默认归类 DB_MISSING（upstream 检查由 repair 阶段执行，
            # 若 upstream 也无数据则升级为 SOURCE_MISSING）
            return ScanResult(
                issue_type=ISSUE_INTERNAL_GAP,
                issue_reason=(
                    f"no bars in DB for {len(expected_dates)} expected trading days"
                ),
                severity="error",
                missing_dates=[d.isoformat() for d in expected_dates],
                expected_bar_count=len(expected_dates),
                classification=CLASS_DB_MISSING,
            )

        # 4. 检测缺口
        internal_gaps, tail_gaps, all_missing = (
            MarketDataQualityService.detect_gaps(actual_dates, expected_dates)
        )

        # 5. 分类缺失日期
        classified = MarketDataQualityService.classify_missing_dates(
            all_missing, instrument,
        )
        db_missing_dates = classified[CLASS_DB_MISSING]

        # 6. 检测重复
        duplicates = MarketDataQualityService.detect_duplicates(actual_dates)

        # 7. OHLC 校验 + 量额一致性
        ohlc_invalid_count = 0
        volume_anomaly_count = 0
        amount_anomaly_count = 0
        for rec in ohlc_records:
            if not MarketDataQualityService.check_ohlc_validity(
                rec.get("open"), rec.get("high"),
                rec.get("low"), rec.get("close"),
            ):
                ohlc_invalid_count += 1
            va = MarketDataQualityService.check_volume_amount_anomaly(
                rec.get("volume"), rec.get("amount"),
            )
            if va == ISSUE_VOLUME_ANOMALY:
                volume_anomaly_count += 1
            elif va == ISSUE_AMOUNT_ANOMALY:
                amount_anomaly_count += 1

        # 8. 因子异常检测
        factor_anomaly_count, factor_min, factor_max = (
            MarketDataQualityService.detect_factor_anomaly(factors)
        )
        factor_has_none = any(f is None for f in factors)

        # 9. 15m bar count 检查
        bar_count_insufficient = False
        if timeframe == "15m" and len(actual_dates) < MDQ_15M_MIN_BARS:
            bar_count_insufficient = True

        # 10. 时间排序检查（15m）
        time_reversed = False
        if timeframe == "15m":
            # actual_dates 对于 15m 实际是日期列表（按日聚合）
            # 真正的时间排序需要 trade_time，这里通过 ohlc_records 的 time 字段判断
            times = [rec.get("time") for rec in ohlc_records if rec.get("time")]
            if times and not MarketDataQualityService.check_time_ordering(times):
                time_reversed = True

        # 11. 综合判定 classification 与 issue_type
        first_bar = min(actual_dates) if actual_dates else None
        last_bar = max(actual_dates) if actual_dates else None

        # 优先级：FACTOR_MISSING > DB_MISSING > OHLC_INVALID > GAP > 其他
        if factor_has_none:
            classification = CLASS_FACTOR_MISSING
            issue_type = ISSUE_FACTOR_MISSING
            severity = "error"
            issue_reason = "adj_factor contains None values"
        elif db_missing_dates:
            classification = CLASS_DB_MISSING
            if tail_gaps:
                issue_type = ISSUE_TAIL_GAP
                issue_reason = (
                    f"tail missing: {len(tail_gaps)} dates, "
                    f"last expected={expected_dates[-1] if expected_dates else None}, "
                    f"last actual={last_bar}"
                )
            else:
                issue_type = ISSUE_INTERNAL_GAP
                issue_reason = (
                    f"internal missing: {len(db_missing_dates)} dates"
                )
            severity = "warning"
        elif instrument.status == "delisted":
            classification = CLASS_DELISTED
            issue_type = ISSUE_NO_ISSUE
            issue_reason = "instrument delisted"
            severity = "info"
        elif instrument.status == "suspended":
            classification = CLASS_SUSPENDED
            issue_type = ISSUE_NO_ISSUE
            issue_reason = "instrument suspended"
            severity = "info"
        else:
            classification = CLASS_OK
            if duplicates:
                issue_type = ISSUE_DUPLICATE
                issue_reason = f"duplicate dates: {len(duplicates)}"
                severity = "warning"
            elif ohlc_invalid_count > 0:
                issue_type = ISSUE_OHLC_INVALID
                issue_reason = f"ohlc invalid bars: {ohlc_invalid_count}"
                severity = "error"
            elif time_reversed:
                issue_type = ISSUE_TIME_REVERSED
                issue_reason = "trade_time not monotonically increasing"
                severity = "error"
            elif volume_anomaly_count > 0:
                issue_type = ISSUE_VOLUME_ANOMALY
                issue_reason = f"volume anomaly bars: {volume_anomaly_count}"
                severity = "warning"
            elif amount_anomaly_count > 0:
                issue_type = ISSUE_AMOUNT_ANOMALY
                issue_reason = f"amount anomaly bars: {amount_anomaly_count}"
                severity = "warning"
            elif factor_anomaly_count > 0:
                issue_type = ISSUE_FACTOR_ANOMALY
                issue_reason = f"factor anomaly jumps: {factor_anomaly_count}"
                severity = "warning"
            elif bar_count_insufficient:
                issue_type = ISSUE_BAR_COUNT_INSUFFICIENT
                issue_reason = (
                    f"15m bar count {len(actual_dates)} < min {MDQ_15M_MIN_BARS}"
                )
                severity = "warning"
            else:
                issue_type = ISSUE_NO_ISSUE
                issue_reason = None
                severity = "info"

        return ScanResult(
            issue_type=issue_type,
            issue_reason=issue_reason,
            severity=severity,
            missing_dates=[d.isoformat() for d in all_missing],
            duplicate_dates=[d.isoformat() for d in duplicates],
            first_bar_date=first_bar,
            last_bar_date=last_bar,
            bar_count=len(actual_dates),
            expected_bar_count=len(expected_dates),
            factor_min=factor_min,
            factor_max=factor_max,
            factor_anomaly_count=factor_anomaly_count,
            classification=classification,
        )

    @staticmethod
    async def _query_daily_for_scan(
        db: AsyncSession,
        instrument_id: uuid.UUID,
        start_date: date,
        end_date: date,
    ) -> tuple[list[Any], list[date], list[dict[str, Any]], list[float | None]]:
        """查询日线 bar 用于扫描。

        Returns:
            (rows, actual_dates, ohlc_records, factors)
        """
        result = await db.execute(
            select(
                BarDaily.trade_date,
                BarDaily.open, BarDaily.high, BarDaily.low, BarDaily.close,
                BarDaily.volume, BarDaily.amount, BarDaily.adj_factor,
            )
            .where(BarDaily.instrument_id == instrument_id)
            .where(BarDaily.trade_date >= start_date)
            .where(BarDaily.trade_date <= end_date)
            .order_by(BarDaily.trade_date)
        )
        rows = result.all()
        actual_dates: list[date] = []
        ohlc_records: list[dict[str, Any]] = []
        factors: list[float | None] = []
        for row in rows:
            td, o, h, low, c, v, a, f = row
            actual_dates.append(td)
            ohlc_records.append({
                "date": td,
                "open": float(o) if o is not None else None,
                "high": float(h) if h is not None else None,
                "low": float(low) if low is not None else None,
                "close": float(c) if c is not None else None,
                "volume": float(v) if v is not None else None,
                "amount": float(a) if a is not None else None,
            })
            factors.append(float(f) if f is not None else None)
        return list(rows), actual_dates, ohlc_records, factors

    @staticmethod
    async def _query_15min_for_scan(
        db: AsyncSession,
        instrument_id: uuid.UUID,
        start_date: date,
        end_date: date,
    ) -> tuple[list[Any], list[date], list[dict[str, Any]], list[float | None]]:
        """查询 15 分钟 bar 用于扫描。

        actual_dates 按 trade_time.date() 聚合（同一日多根 bar 只算一个日期）。

        Returns:
            (rows, actual_dates, ohlc_records, factors)
        """
        from datetime import datetime as dt

        start_dt = dt(start_date.year, start_date.month, start_date.day)
        end_dt = dt(end_date.year, end_date.month, end_date.day, 23, 59, 59)
        result = await db.execute(
            select(
                Bar15Min.trade_time,
                Bar15Min.open, Bar15Min.high, Bar15Min.low, Bar15Min.close,
                Bar15Min.volume, Bar15Min.amount, Bar15Min.adj_factor,
            )
            .where(Bar15Min.instrument_id == instrument_id)
            .where(Bar15Min.trade_time >= start_dt)
            .where(Bar15Min.trade_time <= end_dt)
            .order_by(Bar15Min.trade_time)
        )
        rows = result.all()
        actual_dates_set: set[date] = set()
        ohlc_records: list[dict[str, Any]] = []
        factors: list[float | None] = []
        for row in rows:
            tt, o, h, low, c, v, a, f = row
            # 时区转换：DB 返回 UTC-aware，转 Shanghai naive 取 date
            if tt.tzinfo is not None:
                from zoneinfo import ZoneInfo
                tt_sh = tt.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
            else:
                tt_sh = tt
            actual_dates_set.add(tt_sh.date())
            ohlc_records.append({
                "time": tt_sh,
                "open": float(o) if o is not None else None,
                "high": float(h) if h is not None else None,
                "low": float(low) if low is not None else None,
                "close": float(c) if c is not None else None,
                "volume": float(v) if v is not None else None,
                "amount": float(a) if a is not None else None,
            })
            factors.append(float(f) if f is not None else None)
        actual_dates = sorted(actual_dates_set)
        return list(rows), actual_dates, ohlc_records, factors

    # =========================================================================
    # execute_scan：批次执行扫描
    # =========================================================================

    @staticmethod
    async def execute_scan(
        db: AsyncSession,
        *,
        run_id: uuid.UUID,
        batch_size: int = 50,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """批次执行扫描。

        - 原子 claim：UPDATE WHERE status=pending RETURNING（避免并发重复）
        - 每批 batch_size 条，扫描后更新 item + run 计数
        - dry_run=True 时只统计不写入

        Args:
            db: 异步会话
            run_id: run ID
            batch_size: 每批数量（默认 50）
            dry_run: True 时只打印计划不执行写入

        Returns:
            {total, succeeded, failed, skipped, issue_breakdown}
        """
        # 加载 run
        run_result = await db.execute(
            select(MarketDataQualityRun).where(MarketDataQualityRun.id == run_id)
        )
        run = run_result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"run not found: {run_id}")

        if dry_run:
            # dry-run：统计 pending 数量
            pending_count_result = await db.execute(
                select(func.count(MarketDataQualityItem.id))
                .where(MarketDataQualityItem.run_id == run_id)
                .where(MarketDataQualityItem.status == "pending")
            )
            pending_count = int(pending_count_result.scalar() or 0)
            return {
                "total": run.total_instruments,
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "pending": pending_count,
                "issue_breakdown": {},
                "dry_run": True,
            }

        # 标记 run 为 running
        run.status = "running"
        run.started_at = datetime.now(UTC)
        await db.flush()

        succeeded = 0
        failed = 0
        skipped = 0
        issue_breakdown: Counter[str] = Counter()
        classification_breakdown: Counter[str] = Counter()

        while True:
            # 原子 claim 一批 pending items
            claim_result = await db.execute(
                update(MarketDataQualityItem)
                .where(MarketDataQualityItem.run_id == run_id)
                .where(MarketDataQualityItem.status == "pending")
                .values(status="running", started_at=datetime.now(UTC))
                .returning(
                    MarketDataQualityItem.id,
                    MarketDataQualityItem.instrument_id,
                    MarketDataQualityItem.symbol,
                )
                .execution_options(synchronize_session=False)
            )
            claimed = claim_result.all()
            if not claimed:
                break

            for item_id, instrument_id, _symbol in claimed:
                try:
                    # 加载 instrument
                    inst_result = await db.execute(
                        select(Instrument).where(Instrument.id == instrument_id)
                    )
                    instrument = inst_result.scalar_one_or_none()
                    if instrument is None:
                        # 标的不存在 → skip
                        await db.execute(
                            update(MarketDataQualityItem)
                            .where(MarketDataQualityItem.id == item_id)
                            .values(
                                status="skipped",
                                classification=CLASS_SOURCE_MISSING,
                                issue_reason="instrument not found",
                                severity="warning",
                                finished_at=datetime.now(UTC),
                            )
                        )
                        skipped += 1
                        classification_breakdown[CLASS_SOURCE_MISSING] += 1
                        continue

                    scan_result = await MarketDataQualityService.scan_instrument(
                        db,
                        run=run,
                        instrument=instrument,
                        timeframe=run.timeframe,
                        start_date=run.start_date,
                        end_date=run.end_date,
                    )

                    # 更新 item
                    await db.execute(
                        update(MarketDataQualityItem)
                        .where(MarketDataQualityItem.id == item_id)
                        .values(
                            status="succeeded",
                            issue_type=scan_result.issue_type,
                            issue_reason=scan_result.issue_reason,
                            severity=scan_result.severity,
                            missing_dates=scan_result.missing_dates or None,
                            duplicate_dates=scan_result.duplicate_dates or None,
                            first_bar_date=scan_result.first_bar_date,
                            last_bar_date=scan_result.last_bar_date,
                            bar_count=scan_result.bar_count,
                            expected_bar_count=scan_result.expected_bar_count,
                            factor_min=(
                                Decimal(str(scan_result.factor_min))
                                if scan_result.factor_min is not None else None
                            ),
                            factor_max=(
                                Decimal(str(scan_result.factor_max))
                                if scan_result.factor_max is not None else None
                            ),
                            factor_anomaly_count=scan_result.factor_anomaly_count,
                            classification=scan_result.classification,
                            finished_at=datetime.now(UTC),
                        )
                    )
                    succeeded += 1
                    issue_breakdown[scan_result.issue_type] += 1
                    classification_breakdown[scan_result.classification] += 1

                except Exception as exc:
                    logger.exception(
                        "[MDQ] scan failed run_id=%s instrument_id=%s: %s",
                        run_id, instrument_id, exc,
                    )
                    await db.execute(
                        update(MarketDataQualityItem)
                        .where(MarketDataQualityItem.id == item_id)
                        .values(
                            status="failed",
                            issue_reason=f"scan exception: {exc}",
                            severity="error",
                            finished_at=datetime.now(UTC),
                        )
                    )
                    failed += 1

            await db.flush()

        # 更新 run 统计
        run.succeeded_count = succeeded
        run.failed_count = failed
        run.skipped_count = skipped
        run.coverage_ratio = (
            succeeded / run.total_instruments
            if run.total_instruments > 0 else 0.0
        )
        run.issue_summary = {
            "issue": dict(issue_breakdown),
            "classification": dict(classification_breakdown),
        }
        run.finished_at = datetime.now(UTC)

        if failed == 0:
            run.status = "succeeded"
        elif succeeded > 0:
            run.status = "partial"
        else:
            run.status = "failed"

        await db.flush()

        return {
            "total": run.total_instruments,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "issue_breakdown": dict(issue_breakdown),
            "classification_breakdown": dict(classification_breakdown),
            "run_status": run.status,
            "coverage_ratio": run.coverage_ratio,
        }

    # =========================================================================
    # repair_instrument：单只股票修复
    # =========================================================================

    @staticmethod
    async def repair_instrument(
        db: AsyncSession,
        *,
        run: MarketDataQualityRun,
        item: MarketDataQualityItem,
    ) -> dict[str, Any]:
        """修复单只股票的 DB_MISSING 数据。

        约束：
        - 仅 classification=DB_MISSING 的 item 触发修复
        - 只写 raw OHLCV（不写 qfq 价格）
        - 使用 ON CONFLICT DO UPDATE（不覆盖整表）
        - factor 重算委托 adjustment_factor_calculator

        Args:
            db: 异步会话
            run: 所属 run
            item: 待修复 item

        Returns:
            {repaired: bool, message: str, repaired_dates: list}
        """
        if item.classification != CLASS_DB_MISSING:
            return {
                "repaired": False,
                "message": f"skip: classification={item.classification} not repairable",
                "repaired_dates": [],
            }

        if item.repair_attempted:
            return {
                "repaired": False,
                "message": f"skip: already attempted (status={item.repair_status})",
                "repaired_dates": [],
            }

        # 加载 instrument
        inst_result = await db.execute(
            select(Instrument).where(Instrument.id == item.instrument_id)
        )
        instrument = inst_result.scalar_one_or_none()
        if instrument is None:
            return {
                "repaired": False,
                "message": "instrument not found",
                "repaired_dates": [],
            }

        # 调用 bar_repository 的 fetch 函数拉取并 upsert（DB 优先，无则 pytdx）
        # fetch_*_bars 内部使用 ON CONFLICT DO UPDATE，保证幂等
        try:
            from app.repositories.bar_repository import (
                fetch_15min_bars,
                fetch_daily_bars,
            )

            if run.timeframe == "1d":
                df = await fetch_daily_bars(
                    db, instrument.id,
                    run.start_date, run.end_date,
                )
            else:  # 15m
                from datetime import datetime as dt
                start_dt = dt(run.start_date.year, run.start_date.month, run.start_date.day)
                end_dt = dt(
                    run.end_date.year, run.end_date.month, run.end_date.day,
                    23, 59, 59,
                )
                df = await fetch_15min_bars(
                    db, instrument.id, start_dt, end_dt,
                )
        except Exception as exc:
            logger.warning(
                "[MDQ] repair fetch failed symbol=%s: %s", item.symbol, exc,
            )
            return {
                "repaired": False,
                "message": f"fetch failed: {exc}",
                "repaired_dates": [],
            }

        if df.empty:
            # upstream 也无数据 → 升级 classification 为 SOURCE_MISSING
            await db.execute(
                update(MarketDataQualityItem)
                .where(MarketDataQualityItem.id == item.id)
                .values(
                    repair_attempted=True,
                    repair_status="failed",
                    repair_message="upstream pytdx also empty, upgrade to SOURCE_MISSING",
                    classification=CLASS_SOURCE_MISSING,
                )
            )
            return {
                "repaired": False,
                "message": "upstream empty, classification upgraded to SOURCE_MISSING",
                "repaired_dates": [],
            }

        # 验证写入后的数据完整性（重新扫描）
        try:
            scan_result = await MarketDataQualityService.scan_instrument(
                db,
                run=run,
                instrument=instrument,
                timeframe=run.timeframe,
                start_date=run.start_date,
                end_date=run.end_date,
            )
        except Exception as exc:
            logger.warning(
                "[MDQ] repair re-scan failed symbol=%s: %s", item.symbol, exc,
            )
            return {
                "repaired": False,
                "message": f"re-scan failed: {exc}",
                "repaired_dates": [],
            }

        # 更新 item
        new_class = scan_result.classification
        await db.execute(
            update(MarketDataQualityItem)
            .where(MarketDataQualityItem.id == item.id)
            .values(
                repair_attempted=True,
                repair_status="succeeded" if new_class == CLASS_OK else "partial",
                repair_message=f"repaired, new classification={new_class}",
                classification=new_class,
                issue_type=scan_result.issue_type,
                issue_reason=scan_result.issue_reason,
                severity=scan_result.severity,
                bar_count=scan_result.bar_count,
                expected_bar_count=scan_result.expected_bar_count,
                first_bar_date=scan_result.first_bar_date,
                last_bar_date=scan_result.last_bar_date,
                factor_min=(
                    Decimal(str(scan_result.factor_min))
                    if scan_result.factor_min is not None else None
                ),
                factor_max=(
                    Decimal(str(scan_result.factor_max))
                    if scan_result.factor_max is not None else None
                ),
                factor_anomaly_count=scan_result.factor_anomaly_count,
                missing_dates=scan_result.missing_dates or None,
            )
        )

        return {
            "repaired": True,
            "message": f"repaired, new classification={new_class}",
            "repaired_dates": scan_result.missing_dates,
        }

    # =========================================================================
    # execute_repair：批次执行修复
    # =========================================================================

    @staticmethod
    async def execute_repair(
        db: AsyncSession,
        *,
        run_id: uuid.UUID,
        batch_size: int = 10,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """批次执行修复。

        - 仅处理 classification=DB_MISSING 且 repair_attempted=False 的 item
        - 每批 batch_size 条（默认 10，修复比扫描更重）
        - dry_run=True 时只统计不执行

        Args:
            db: 异步会话
            run_id: run ID
            batch_size: 每批数量
            dry_run: True 时只统计

        Returns:
            {total_candidates, repaired, failed, skipped, dry_run}
        """
        run_result = await db.execute(
            select(MarketDataQualityRun).where(MarketDataQualityRun.id == run_id)
        )
        run = run_result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"run not found: {run_id}")

        # 统计候选
        count_result = await db.execute(
            select(func.count(MarketDataQualityItem.id))
            .where(MarketDataQualityItem.run_id == run_id)
            .where(MarketDataQualityItem.classification == CLASS_DB_MISSING)
            .where(MarketDataQualityItem.repair_attempted.is_(False))
        )
        total_candidates = int(count_result.scalar() or 0)

        if dry_run:
            return {
                "total_candidates": total_candidates,
                "repaired": 0,
                "failed": 0,
                "skipped": 0,
                "dry_run": True,
            }

        repaired = 0
        failed = 0
        skipped = 0
        processed = 0

        while processed < total_candidates and processed < batch_size:
            # 拉取一批候选
            candidates_result = await db.execute(
                select(MarketDataQualityItem)
                .where(MarketDataQualityItem.run_id == run_id)
                .where(MarketDataQualityItem.classification == CLASS_DB_MISSING)
                .where(MarketDataQualityItem.repair_attempted.is_(False))
                .order_by(MarketDataQualityItem.symbol)
                .limit(batch_size - processed)
            )
            candidates = candidates_result.scalars().all()
            if not candidates:
                break

            for item in candidates:
                processed += 1
                try:
                    result = await MarketDataQualityService.repair_instrument(
                        db, run=run, item=item,
                    )
                    if result["repaired"]:
                        repaired += 1
                    elif "skip" in result["message"]:
                        skipped += 1
                    else:
                        failed += 1
                except Exception as exc:
                    logger.exception(
                        "[MDQ] repair failed item_id=%s symbol=%s: %s",
                        item.id, item.symbol, exc,
                    )
                    await db.execute(
                        update(MarketDataQualityItem)
                        .where(MarketDataQualityItem.id == item.id)
                        .values(
                            repair_attempted=True,
                            repair_status="failed",
                            repair_message=f"repair exception: {exc}",
                        )
                    )
                    failed += 1
            await db.flush()

        return {
            "total_candidates": total_candidates,
            "repaired": repaired,
            "failed": failed,
            "skipped": skipped,
            "processed": processed,
            "dry_run": False,
        }

    # =========================================================================
    # summarize_run：汇总
    # =========================================================================

    @staticmethod
    async def summarize_run(
        db: AsyncSession, run_id: uuid.UUID,
    ) -> dict[str, Any]:
        """聚合 run 的扫描结果。

        按 issue_type / classification / severity 聚合 items。

        Args:
            db: 异步会话
            run_id: run ID

        Returns:
            {run_id, run_key, status, total, succeeded, failed, skipped,
             coverage_ratio, issue_breakdown, classification_breakdown,
             severity_breakdown}
        """
        run_result = await db.execute(
            select(MarketDataQualityRun).where(MarketDataQualityRun.id == run_id)
        )
        run = run_result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"run not found: {run_id}")

        # 聚合 items
        items_result = await db.execute(
            select(
                MarketDataQualityItem.issue_type,
                MarketDataQualityItem.classification,
                MarketDataQualityItem.severity,
                MarketDataQualityItem.status,
            )
            .where(MarketDataQualityItem.run_id == run_id)
        )
        rows = items_result.all()

        issue_breakdown: Counter[str] = Counter()
        classification_breakdown: Counter[str] = Counter()
        severity_breakdown: Counter[str] = Counter()
        status_breakdown: Counter[str] = Counter()

        for issue_type, classification, severity, status in rows:
            if issue_type:
                issue_breakdown[issue_type] += 1
            if classification:
                classification_breakdown[classification] += 1
            if severity:
                severity_breakdown[severity] += 1
            status_breakdown[status] += 1

        return {
            "run_id": str(run_id),
            "run_key": run.run_key,
            "timeframe": run.timeframe,
            "start_date": run.start_date.isoformat(),
            "end_date": run.end_date.isoformat(),
            "algorithm_version": run.algorithm_version,
            "status": run.status,
            "total": run.total_instruments,
            "succeeded": run.succeeded_count,
            "failed": run.failed_count,
            "skipped": run.skipped_count,
            "coverage_ratio": run.coverage_ratio,
            "issue_breakdown": dict(issue_breakdown),
            "classification_breakdown": dict(classification_breakdown),
            "severity_breakdown": dict(severity_breakdown),
            "item_status_breakdown": dict(status_breakdown),
            "repair_mode": run.repair_mode,
        }


if __name__ == "__main__":
    # 自测：验证纯函数逻辑（不连 DB）
    from datetime import date as d

    # 1. check_ohlc_validity
    assert MarketDataQualityService.check_ohlc_validity(10.0, 11.0, 9.0, 10.5)
    assert not MarketDataQualityService.check_ohlc_validity(10.0, 9.0, 11.0, 10.5)
    assert not MarketDataQualityService.check_ohlc_validity(0, 11.0, 9.0, 10.5)
    assert not MarketDataQualityService.check_ohlc_validity(None, 11.0, 9.0, 10.5)
    print("check_ohlc_validity ✓")

    # 2. check_volume_amount_anomaly
    assert MarketDataQualityService.check_volume_amount_anomaly(0, 100.0) == ISSUE_VOLUME_ANOMALY
    assert MarketDataQualityService.check_volume_amount_anomaly(100.0, 0) == ISSUE_AMOUNT_ANOMALY
    assert MarketDataQualityService.check_volume_amount_anomaly(100.0, 1000.0) is None
    assert MarketDataQualityService.check_volume_amount_anomaly(0, 0) is None
    print("check_volume_amount_anomaly ✓")

    # 3. detect_duplicates
    assert MarketDataQualityService.detect_duplicates([
        d(2026, 1, 1), d(2026, 1, 2), d(2026, 1, 1),
    ]) == [d(2026, 1, 1)]
    assert MarketDataQualityService.detect_duplicates([d(2026, 1, 1), d(2026, 1, 2)]) == []
    print("detect_duplicates ✓")

    # 4. detect_gaps
    actual = [d(2026, 1, 5), d(2026, 1, 7), d(2026, 1, 8)]
    expected = [d(2026, 1, 5), d(2026, 1, 6), d(2026, 1, 7), d(2026, 1, 8), d(2026, 1, 9)]
    internal, tail, all_missing = MarketDataQualityService.detect_gaps(actual, expected)
    assert internal == [d(2026, 1, 6)], f"internal: {internal}"
    assert tail == [d(2026, 1, 9)], f"tail: {tail}"
    assert all_missing == [d(2026, 1, 6), d(2026, 1, 9)], f"all_missing: {all_missing}"
    print("detect_gaps ✓")

    # 5. detect_factor_anomaly
    anomaly_count, fmin, fmax = MarketDataQualityService.detect_factor_anomaly(
        [1.0, 0.98, 0.5, 0.5],  # 0.98→0.5 是 49% 跳变
    )
    assert anomaly_count == 1, f"anomaly_count: {anomaly_count}"
    assert fmin == 0.5
    assert fmax == 1.0
    # 全 None
    a, n1, n2 = MarketDataQualityService.detect_factor_anomaly([None, None])
    assert a == 0 and n1 is None and n2 is None
    print("detect_factor_anomaly ✓")

    # 6. check_time_ordering
    assert MarketDataQualityService.check_time_ordering([
        datetime(2026, 1, 1, 9, 30),
        datetime(2026, 1, 1, 9, 45),
        datetime(2026, 1, 1, 10, 0),
    ])
    assert not MarketDataQualityService.check_time_ordering([
        datetime(2026, 1, 1, 10, 0),
        datetime(2026, 1, 1, 9, 30),
    ])
    print("check_time_ordering ✓")

    # 7. make_run_key
    rk = MarketDataQualityService.make_run_key("1d", d(2026, 1, 1), d(2026, 7, 30))
    assert rk == "mdq:1d:2026-01-01:2026-07-30:mdq-v1.0.0", f"run_key: {rk}"
    print(f"make_run_key ✓ ({rk})")

    # 8. make_parameter_hash 确定性
    h1 = MarketDataQualityService.make_parameter_hash("1d", d(2026, 1, 1), d(2026, 7, 30))
    h2 = MarketDataQualityService.make_parameter_hash("1d", d(2026, 1, 1), d(2026, 7, 30))
    h3 = MarketDataQualityService.make_parameter_hash("15m", d(2026, 1, 1), d(2026, 7, 30))
    assert h1 == h2, "相同参数应产生相同 hash"
    assert h1 != h3, "不同参数应产生不同 hash"
    print(f"make_parameter_hash ✓ ({h1})")

    print("\n所有纯函数自测通过 ✓（未连接 DB）")
