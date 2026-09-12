"""BusinessDateAdjustmentContext —— 盘中只读复权 overlay（Stage B2B）。

问题：除权日开盘前，``bars_daily.adj_factor`` 仍是**旧坐标**（最新 factor 日期=昨日）。
若盘前提前把它改写成除权后坐标，现有 MDAS 的 denominator 也取同一个最新 factor，
``qfq = raw × 0.5 / 0.5 = raw`` —— 除权效果被完全抵消。

因此本模块**只做内存 overlay，绝不写库**：

    fresh XDXR（force_refresh=True）
        +
    completed raw daily（只读 raw close，不用 DB adj_factor）
        ↓  calculate_adjustment_factor_series(effective_as_of=business_date)  ← 唯一 calculator
    business-date factor series
        +
    synthetic business_date anchor = 1.0
        ↓  BusinessDateAdjustmentContext
    AdjustmentFactorService.apply_qfq(as_of=business_date)                  ← 唯一 qfq 公式

数学合同（10送10，business_date=2026-09-12，9/11 raw close=20）::

    overlay: 9/11 factor = 0.5 ；9/12 synthetic factor = 1.0
    daily/15m qfq = 20 × 0.5 / 1.0 = 10
    realtime raw quote = 10（business-date qfq 坐标即 raw 坐标）→ quote_qfq_ratio = 1
    ⇒ 历史 daily / 历史 15m / 当前 quote 处于同一价格坐标

fail-closed 合同：

- freshness 无法证明 → **raise** :class:`BusinessDateAdjustmentUnavailableError`，
  绝不返回 ``freshness_proven=False`` 让调用方误用。
- raw daily 未覆盖到 ``expected_completed_through`` / 出现未来 raw / XDXR 强制刷新失败
  / factor 计算数据缺口 → 全部 fail-closed。

本模块**不**触碰 canonical 状态：不 commit、不 UPDATE bars_daily、不调用
``rebuild_adj_factors`` / ``rebuild_factor_series`` / ``detect_company_action_change``
/ ``_store_fingerprint``。

How to Run:
    python -m app.services.business_date_adjustment_context    # 自测（纯计算，不连库）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.factor_contract import FACTOR_ALGORITHM_VERSION
from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.repositories.bar_repository import get_raw_daily_close_series
from app.services.adjustment_factor_calculator import (
    AdjustmentFactorDataError,
    calculate_adjustment_factor_series,
    corporate_action_fingerprint,
)
from app.services.adjustment_factor_service import AdjustmentFactorService

logger = logging.getLogger("services.business_date_adjustment_context")

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# business-date anchor 必须精确为 1.0（浮点容差）
_UNITY_TOLERANCE = 1e-9


class BusinessDateAdjustmentUnavailableError(RuntimeError):
    """business-date 复权坐标无法被证明（fail-closed）。

    why：盘中 crossing 判定必须建立在**可证明**的价格坐标上。若 freshness 无法证明
    （raw daily 滞后 / XDXR 拉取失败 / 数据缺口），返回一个「不可信但看起来正常」的
    context 会让下游在错误坐标上判断穿越。因此一律 raise。
    """

    def __init__(
        self,
        *,
        symbol: str,
        business_date: date,
        reason: str,
        cause: Exception | None = None,
    ) -> None:
        self.symbol = symbol
        self.business_date = business_date
        self.reason = reason
        self.cause = cause

        super().__init__(
            "business-date adjustment unavailable "
            f"symbol={symbol} business_date={business_date} reason={reason}"
        )


@dataclass(frozen=True)
class BusinessDateAdjustmentContext:
    """business-date 只读复权坐标（内存 overlay，不持久化）。

    ``factor_df`` 是**推导出来的** overlay（含 synthetic business-date anchor=1.0），
    与 canonical ``bars_daily.adj_factor`` 无关，也不得写回。
    """

    instrument_id: uuid.UUID
    symbol: str

    business_date: date
    expected_completed_through: date
    latest_raw_trade_date: date

    factor_freshness_date: date

    factor_source_fingerprint: str
    factor_hash: str
    context_hash: str

    denominator_factor: Decimal
    quote_factor: Decimal
    quote_qfq_ratio: Decimal

    synthetic_anchor: bool

    factor_df: pd.DataFrame

    built_at: datetime

    freshness_proven: bool = True
    degraded_reason: str | None = None


def _compute_factor_hash(factor_df: pd.DataFrame) -> str:
    """稳定 factor 序列 hash（跨进程/跨次构造一致）。"""
    if factor_df.empty:
        return ""

    canonical = factor_df[["trade_date", "adj_factor"]].copy()
    canonical["trade_date"] = pd.to_datetime(canonical["trade_date"]).dt.strftime("%Y-%m-%d")
    canonical["adj_factor"] = canonical["adj_factor"].map(lambda v: format(float(v), ".12g"))

    payload = "\n".join(
        f"{row.trade_date}|{row.adj_factor}" for row in canonical.itertuples(index=False)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _compute_context_hash(
    *,
    business_date: date,
    expected_completed_through: date,
    latest_raw_trade_date: date,
    factor_source_fingerprint: str,
    factor_hash: str,
) -> str:
    """复权坐标的版本身份。

    为什么要独立于 15m/daily source hash：除权日早盘「15m completed source 没变，
    但复权坐标变了」——只看 source hash 会漏掉重算。Node target version 必须包含本 hash。
    """
    payload = "|".join([
        FACTOR_ALGORITHM_VERSION,
        business_date.isoformat(),
        expected_completed_through.isoformat(),
        latest_raw_trade_date.isoformat(),
        factor_source_fingerprint,
        factor_hash,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_RAW_DAILY_REQUIRED_COLUMNS = ("datetime", "close")


def _validate_raw_daily_input(
    raw_daily: pd.DataFrame,
    *,
    symbol: str,
    business_date: date,
) -> pd.DataFrame:
    """校验 raw daily 是**可信**输入（fail-closed），返回规范化副本。

    为什么必须在这里拒绝：``BarDaily.close`` 在 ORM 中是 ``nullable=True`` 的，
    ``NULL`` 是 schema 合法状态。若把 NaN 送进 calculator，事件日的 prev_close 会
    传播 NaN，最终**静默跳过实际除权调整**并产出看似正常的 factor=1.0 ——
    这与「坐标不可证明就必须 fail-closed」直接冲突。

    拒绝：NULL/NaN/inf/<=0 的 close，以及无法解析的 trade_date。
    """
    if not set(_RAW_DAILY_REQUIRED_COLUMNS).issubset(raw_daily.columns):
        raise BusinessDateAdjustmentUnavailableError(
            symbol=symbol,
            business_date=business_date,
            reason="raw_daily_invalid_schema",
        )

    validated = raw_daily.copy()
    validated["datetime"] = pd.to_datetime(validated["datetime"], errors="coerce")
    validated["close"] = pd.to_numeric(validated["close"], errors="coerce")

    invalid_date = validated["datetime"].isna()
    invalid_close = validated["close"].map(
        lambda value: (
            not pd.notna(value)
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        )
    )

    if bool(invalid_date.any()):
        raise BusinessDateAdjustmentUnavailableError(
            symbol=symbol,
            business_date=business_date,
            reason="raw_daily_invalid_trade_date",
        )

    if bool(invalid_close.any()):
        raise BusinessDateAdjustmentUnavailableError(
            symbol=symbol,
            business_date=business_date,
            reason="raw_daily_invalid_close",
        )

    return validated


def _assert_context_integrity(context: BusinessDateAdjustmentContext) -> None:
    """消费前校验 context 的 payload identity 未被破坏（fail-closed）。

    ``@dataclass(frozen=True)`` 只冻结属性绑定，**不冻结内部 DataFrame**：
    ``context.factor_df.loc[0, "adj_factor"] = 123`` 是合法操作，会让
    ``factor_hash`` 与实际 factor payload 分叉。而下一阶段 Node Target version
    直接依赖 ``context_hash``：若「版本号不变」但真实坐标变了，
    「目标版本不变就不触发假 crossing」的前提会整体失效。

    因此发现分叉只能 raise，**禁止**重算/覆盖 hash 或接受新 DataFrame。
    """
    current_factor_hash = _compute_factor_hash(context.factor_df)
    if current_factor_hash != context.factor_hash:
        raise BusinessDateAdjustmentUnavailableError(
            symbol=context.symbol,
            business_date=context.business_date,
            reason="context_factor_hash_mismatch",
        )

    expected_context_hash = _compute_context_hash(
        business_date=context.business_date,
        expected_completed_through=context.expected_completed_through,
        latest_raw_trade_date=context.latest_raw_trade_date,
        factor_source_fingerprint=context.factor_source_fingerprint,
        factor_hash=current_factor_hash,
    )
    if expected_context_hash != context.context_hash:
        raise BusinessDateAdjustmentUnavailableError(
            symbol=context.symbol,
            business_date=context.business_date,
            reason="context_hash_mismatch",
        )


class BusinessDateAdjustmentService:
    """构建 / 消费 business-date 复权坐标（只读 overlay）。"""

    async def build_business_date_adjustment_context(
        self,
        session: AsyncSession,
        *,
        instrument_id: uuid.UUID,
        symbol: str,
        business_date: date,
        expected_completed_through: date,
        adapter: PytdxAdapter | None = None,
    ) -> BusinessDateAdjustmentContext:
        """构建 business-date 复权坐标（不写库）。

        Args:
            session: 异步 DB 会话（只读查询 raw daily）。
            instrument_id: 标的 UUID。
            symbol: 股票代码。
            business_date: 目标业务日（synthetic anchor 日期）。
            expected_completed_through: 调用方由**市场日历**算出的「已完成 raw daily 应覆盖到」
                的交易日。builder 不自己推 ``business_date - 1``（周末/节假日会错）。
            adapter: pytdx 适配器（None 用模块单例）。

        Returns:
            :class:`BusinessDateAdjustmentContext`（``freshness_proven=True``）。

        Raises:
            BusinessDateAdjustmentUnavailableError: 任一 freshness 前提不成立。
        """
        # 1. raw completed daily（只读；刻意不用 DB adj_factor = 旧坐标）
        raw_daily = await get_raw_daily_close_series(
            session, instrument_id, end_date=business_date,
        )
        if raw_daily.empty:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol, business_date=business_date, reason="raw_daily_empty",
            )

        # 1b. raw daily 必须是**可信**输入：close 为 NULL（schema 允许）/ NaN / inf / <=0
        # 一律 fail-closed。必须在访问 XDXR provider **之前**完成 —— raw 已知不可信时
        # 不得再发起远端 freshness 请求。
        raw_daily = _validate_raw_daily_input(
            raw_daily,
            symbol=symbol,
            business_date=business_date,
        )

        latest_raw_trade_date = pd.to_datetime(raw_daily["datetime"]).max().date()

        if latest_raw_trade_date > business_date:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol,
                business_date=business_date,
                reason="raw_daily_future_leak",
            )

        if latest_raw_trade_date != expected_completed_through:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol,
                business_date=business_date,
                reason="raw_daily_not_completed_through_expected_date",
            )

        # 2. fresh XDXR 是硬门：必须 force_refresh（Stage A 冻结的 fail-closed 合同）
        pytdx = adapter or get_pytdx_adapter()
        try:
            xdxr_df = await asyncio.to_thread(
                pytdx.get_xdxr_info, symbol, force_refresh=True,
            )
        except Exception as exc:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol,
                business_date=business_date,
                reason="xdxr_force_refresh_failed",
                cause=exc,
            ) from exc

        # 3. 只读 identity：直接调用纯函数，**不**触碰 fingerprint 状态
        factor_source_fingerprint, _ = corporate_action_fingerprint(
            xdxr_df,
            effective_as_of=business_date,
        )

        # 4. 唯一 calculator 生成 overlay（effective_as_of=business_date）
        try:
            factors = calculate_adjustment_factor_series(
                raw_daily,
                xdxr_df,
                effective_as_of=business_date,
            )
        except AdjustmentFactorDataError as exc:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol,
                business_date=business_date,
                reason=exc.degraded_reason,
                cause=exc,
            ) from exc
        except Exception as exc:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol,
                business_date=business_date,
                reason="factor_calculation_failed",
                cause=exc,
            ) from exc

        if len(factors) != len(raw_daily):
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol,
                business_date=business_date,
                reason="factor_length_mismatch",
            )

        # 5. factor overlay（内存）
        factor_df = pd.DataFrame({
            "trade_date": pd.to_datetime(raw_daily["datetime"]),
            "adj_factor": factors,
        })
        factor_df = (
            factor_df.sort_values("trade_date")
            .drop_duplicates("trade_date", keep="last")
            .reset_index(drop=True)
        )

        # 6. synthetic business-date anchor（今天还没有 raw bar 时）
        business_ts = pd.Timestamp(business_date)
        synthetic_anchor = latest_raw_trade_date < business_date
        if synthetic_anchor:
            anchor = pd.DataFrame({
                "trade_date": [business_ts],
                "adj_factor": [1.0],
            })
            factor_df = pd.concat([factor_df, anchor], ignore_index=True)
            factor_df = (
                factor_df.sort_values("trade_date")
                .drop_duplicates("trade_date", keep="last")
                .reset_index(drop=True)
            )

        # 7. business-date anchor 必须为 1.0（不偷偷覆盖错误 factor）
        business_rows = factor_df[factor_df["trade_date"] == business_ts]
        if business_rows.empty:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol,
                business_date=business_date,
                reason="business_date_anchor_missing",
            )
        business_factor = float(business_rows["adj_factor"].iloc[-1])
        if abs(business_factor - 1.0) > _UNITY_TOLERANCE:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=symbol,
                business_date=business_date,
                reason="business_date_anchor_not_unity",
            )

        factor_hash = _compute_factor_hash(factor_df)
        context_hash = _compute_context_hash(
            business_date=business_date,
            expected_completed_through=expected_completed_through,
            latest_raw_trade_date=latest_raw_trade_date,
            factor_source_fingerprint=factor_source_fingerprint,
            factor_hash=factor_hash,
        )

        logger.info(
            "build_business_date_adjustment_context symbol=%s business_date=%s "
            "latest_raw=%s synthetic_anchor=%s factor_hash=%s context_hash=%s",
            symbol, business_date, latest_raw_trade_date, synthetic_anchor,
            factor_hash, context_hash,
        )

        return BusinessDateAdjustmentContext(
            instrument_id=instrument_id,
            symbol=symbol,
            business_date=business_date,
            expected_completed_through=expected_completed_through,
            latest_raw_trade_date=latest_raw_trade_date,
            factor_freshness_date=business_date,
            factor_source_fingerprint=factor_source_fingerprint,
            factor_hash=factor_hash,
            context_hash=context_hash,
            # synthetic anchor = 1.0 ⇒ denominator=1；当前 raw quote 本身就是 business-date qfq 坐标
            denominator_factor=Decimal("1"),
            quote_factor=Decimal("1"),
            quote_qfq_ratio=Decimal("1"),
            synthetic_anchor=synthetic_anchor,
            factor_df=factor_df.copy(),
            built_at=datetime.now(_SHANGHAI_TZ),
            freshness_proven=True,
            degraded_reason=None,
        )

    def apply_context_qfq(
        self,
        bars_df: pd.DataFrame,
        context: BusinessDateAdjustmentContext,
        *,
        intraday: bool,
    ) -> pd.DataFrame:
        """用 context 的 overlay 对 bars 做前复权。

        **唯一复权公式仍由 :meth:`AdjustmentFactorService.apply_qfq` 执行**；
        本模块只负责提供 overlay factor 序列与 ``as_of``。禁止在这里自己写价格乘法。
        """
        if not context.freshness_proven:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=context.symbol,
                business_date=context.business_date,
                reason="context_freshness_not_proven",
            )

        # 消费前校验 payload identity：frozen 只冻结属性绑定，不冻结内部 DataFrame
        _assert_context_integrity(context)

        return AdjustmentFactorService().apply_qfq(
            bars_df,
            context.factor_df,
            as_of=context.business_date,
            intraday=intraday,
        )

    def quote_qfq_price(
        self,
        raw_price: float | Decimal,
        context: BusinessDateAdjustmentContext,
    ) -> Decimal:
        """把实时 raw quote 映射到 business-date qfq 坐标。

        synthetic anchor 的定义保证 ``quote_qfq_ratio == 1``（当前 raw quote 本身就是
        当前 qfq 坐标），因此这里**不查 factor_df**，只做一次可信比例乘法。
        """
        if not context.freshness_proven:
            raise BusinessDateAdjustmentUnavailableError(
                symbol=context.symbol,
                business_date=context.business_date,
                reason="context_freshness_not_proven",
            )

        # quote 虽然 ratio=1，但它宣称与「该 context 的历史 target」同坐标；
        # payload 若被篡改，该 identity 声明即失效，必须 fail-closed。
        _assert_context_integrity(context)

        return Decimal(str(raw_price)) * context.quote_qfq_ratio


if __name__ == "__main__":
    # 自测：验证 hash 稳定性与 quote 映射（不连 DB / 不调 provider）
    logging.basicConfig(level=logging.INFO)

    fdf = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-09-10", "2026-09-11", "2026-09-12"]),
        "adj_factor": [0.5, 0.5, 1.0],
    })
    h1 = _compute_factor_hash(fdf)
    h2 = _compute_factor_hash(fdf.copy())
    assert h1 == h2 and len(h1) == 16, f"factor hash 应稳定且 16 位: {h1} {h2}"

    c1 = _compute_context_hash(
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
        latest_raw_trade_date=date(2026, 9, 11),
        factor_source_fingerprint="abc",
        factor_hash=h1,
    )
    assert len(c1) == 16

    print(f"factor_hash={h1} context_hash={c1}")
    print("OK")
