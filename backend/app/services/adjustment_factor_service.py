"""统一复权因子服务（CHANGE-20260717-002）。

唯一复权服务，负责：
1. 获取权威日线因子序列（从 bars_daily.adj_factor）
2. 应用前复权（qfq = raw × factor(bar_date) / factor(as_of)）
3. 公司行为变化时重建完整因子序列并原子 upsert
4. 检测公司行为变化（xdxr fingerprint 对比）

分层约束：
- 通过 bar_repository 公开别名（get_adj_factor_series / rebuild_adj_factors）访问 DB，
  禁止直接导入 bar_repository 私有函数（_get_adj_factor_df / _calculate_adj_factor）
- 通过 adj_factor 公开 API（apply_adj_factor_with_as_of）应用复权
- MDAS 是唯一调用本服务的行情出口；业务/API/指标/任务不得直接调用本服务绕过 MDAS

How to Run:
    python -m app.services.adjustment_factor_service    # 自测：验证函数签名与基础逻辑
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pytdx_adapter import get_pytdx_adapter
from app.repositories.bar_repository import (
    get_adj_factor_series,
    rebuild_adj_factors,
)
from app.services.adj_factor import (
    apply_adj_factor,
    apply_adj_factor_intraday,
    apply_adj_factor_with_as_of,
)

if TYPE_CHECKING:
    from app.core.pytdx_adapter import PytdxAdapter

logger = logging.getLogger("services.adjustment_factor_service")

# MDAS 缓存键前缀（与 market_data_aggregation_service._REDIS_CACHE_PREFIX 一致）
_MDAS_CACHE_PREFIX = "mdas"
# 公司行为 fingerprint 存储前缀
_FP_PREFIX = "adj_factor_fp"


class AdjustmentFactorService:
    """统一复权因子服务。

    MDAS 通过本服务获取因子序列和应用复权，禁止业务层直接调用 bar_repository
    私有行情/复权函数（架构测试 test_mdas_singleton.py 强制约束）。
    """

    async def get_factor_series(
        self,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        as_of: date | None = None,
    ) -> pd.DataFrame:
        """获取权威日线因子序列。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            as_of: 复权锚点日期（None=全量；date=只返回 trade_date <= as_of 的因子）

        Returns:
            DataFrame: columns=[trade_date, adj_factor]，按 trade_date 排序；
                       无数据时返回空 DataFrame（调用方应标记 degraded）
        """
        return await get_adj_factor_series(session, instrument_id, as_of=as_of)

    def apply_qfq(
        self,
        bars_df: pd.DataFrame,
        factor_df: pd.DataFrame,
        as_of: date | None = None,
        intraday: bool = False,
    ) -> pd.DataFrame:
        """应用前复权（qfq = raw × factor(bar_date) / factor(as_of)）。

        Args:
            bars_df: K 线数据，index 为 DatetimeIndex，含 OHLC 列
            factor_df: 复权因子，columns=[trade_date, adj_factor]
            as_of: 复权锚点日期（None=最新，向后兼容；date=point-in-time，禁止未来泄漏）
            intraday: True 为分钟线（按交易日映射），False 为日线/周线/月线

        Returns:
            前复权后的 DataFrame（volume 不变，OHLC 调整）
        """
        if as_of is not None:
            return apply_adj_factor_with_as_of(
                bars_df, factor_df, as_of=as_of, intraday=intraday
            )
        # as_of=None：向后兼容，委托旧 API
        if intraday:
            return apply_adj_factor_intraday(bars_df, factor_df)
        return apply_adj_factor(bars_df, factor_df)

    async def rebuild_factor_series(
        self,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        symbol: str,
        earliest_affected: date,
        adapter: PytdxAdapter | None = None,
    ) -> int:
        """公司行为变化时重建完整因子序列并原子 upsert。

        重建成功后精确失效该股票的下游缓存（FR-11）：
        - MDAS 缓存（Redis mdas:{instrument_id}:*）
        - bars 缓存（Redis bars:{instrument_id}:*，默认禁用时返回 0）
        - indicator 缓存（Redis indicator:{instrument_id}:*）

        不失效（依赖 TTL 自然过期或重算）：
        - 监控 Profile 缓存（in-process，跨 worker 无法精确失效；TTL 300s）
        - Capture 缓存（filesystem，per-event key，新事件自然 miss；TTL 600s）
        - Snapshot（DB 存储，由 after_close 流水线重算，schema_version bump 保证旧快照不可见）

        失败时 re-raise（不吞没，不伪装成功）。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            symbol: 股票代码（用于 pytdx xdxr_info）
            earliest_affected: 最早受影响日期（从此日起重算）
            adapter: pytdx 适配器（None 用模块单例）

        Returns:
            更新的记录数

        Raises:
            Exception: 重算或 upsert 失败时 re-raise
        """
        count = await rebuild_adj_factors(
            session, instrument_id, symbol, earliest_affected, adapter
        )
        # [FR-11] 精确失效该股票下游缓存：MDAS + bars + indicator
        invalidated = await self._invalidate_downstream_caches(instrument_id)
        logger.info(
            "rebuild_factor_series 完成 instrument_id=%s records=%d 缓存失效: %s",
            instrument_id, count, invalidated,
        )
        return count

    async def _invalidate_downstream_caches(
        self, instrument_id: uuid.UUID
    ) -> dict[str, int]:
        """[FR-11] 因子变化后精确失效该股票的下游缓存。

        失效范围（按依赖顺序，精确到 instrument_id）：
        - mdas: 行情聚合层缓存（sync Redis，与现有实现一致）
        - bars: 原始行情响应缓存（async，默认禁用时返回 0）
        - indicator: 指标计算结果缓存（async，TTL 300s）

        单层失效失败不阻塞其他层（缓存 TTL 会自然过期）。

        Returns:
            dict: 各缓存层删除的键数量 {mdas, bars, indicator}
        """
        result: dict[str, int] = {"mdas": 0, "bars": 0, "indicator": 0}

        # 1. MDAS 缓存（sync Redis，与现有实现一致）
        result["mdas"] = self._invalidate_mdas_cache(instrument_id)

        # 2. bars 缓存（async，默认禁用时返回 0）
        try:
            from app.services.bars_cache import invalidate_bars_cache
            result["bars"] = await invalidate_bars_cache(instrument_id)
        except Exception as exc:
            logger.warning(
                "bars 缓存失效失败 instrument_id=%s: %s（缓存 TTL 会自然过期）",
                instrument_id, exc,
            )

        # 3. indicator 缓存（async，TTL 300s）
        try:
            from app.services.indicator_cache import invalidate as invalidate_indicator
            result["indicator"] = await invalidate_indicator(instrument_id)
        except Exception as exc:
            logger.warning(
                "indicator 缓存失效失败 instrument_id=%s: %s（缓存 TTL 会自然过期）",
                instrument_id, exc,
            )

        total = sum(result.values())
        if total > 0:
            logger.info(
                "下游缓存失效 instrument_id=%s mdas=%d bars=%d indicator=%d",
                instrument_id, result["mdas"], result["bars"], result["indicator"],
            )
        return result

    def _invalidate_mdas_cache(self, instrument_id: uuid.UUID) -> int:
        """失效该股票的 MDAS 缓存（scan + delete mdas:{instrument_id}:*）。

        Returns:
            删除的缓存键数量
        """
        try:
            from app.core.redis_client import get_sync_redis
            client = get_sync_redis()
            pattern = f"{_MDAS_CACHE_PREFIX}:{instrument_id}:*"
            deleted = 0
            for key in client.scan_iter(match=pattern, count=100):
                client.delete(key)
                deleted += 1
            if deleted > 0:
                logger.info(
                    "MDAS 缓存失效 instrument_id=%s deleted=%d", instrument_id, deleted
                )
            return deleted
        except Exception as exc:
            logger.warning(
                "MDAS 缓存失效失败 instrument_id=%s: %s（缓存 TTL 会自然过期）",
                instrument_id, exc,
            )
            return 0

    async def detect_company_action_change(
        self,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        symbol: str,
        adapter: PytdxAdapter | None = None,
    ) -> date | None:
        """检测公司行为集合（xdxr category=1 事件）是否变化。

        通过 fingerprint（事件集合的 SHA256）对比 Redis 存储的上次 fingerprint。
        若变化或无存储记录，返回最早事件日期（用于 rebuild_factor_series 的 earliest_affected）；
        若未变化，返回 None。

        Args:
            session: 异步 DB 会话（保留以备未来扩展，如从 DB 读取已存事件）
            instrument_id: 标的 UUID
            symbol: 股票代码
            adapter: pytdx 适配器（None 用模块单例）

        Returns:
            最早受影响日期（需重建因子）或 None（无变化）
        """
        pytdx = adapter or get_pytdx_adapter()
        try:
            xdxr_df = pytdx.get_xdxr_info(symbol)
        except Exception as exc:
            logger.warning(
                "detect_company_action_change 获取 xdxr 失败 symbol=%s: %s", symbol, exc,
            )
            return None

        if xdxr_df is None or xdxr_df.empty:
            # 无除权除息事件，fingerprint 为空串
            current_fp = ""
            earliest = None
        else:
            exc_events = xdxr_df[xdxr_df["category"] == 1].copy()
            if exc_events.empty:
                current_fp = ""
                earliest = None
            else:
                exc_events = exc_events.sort_values("date")
                # fingerprint = SHA256 of (date, fenhong, songzhuangu, peigu, peigujia) 拼接
                fp_parts = []
                for _, row in exc_events.iterrows():
                    fp_parts.append(
                        f"{row['date']}|{row.get('fenhong', 0)}|"
                        f"{row.get('songzhuangu', 0)}|"
                        f"{row.get('peigu', 0)}|{row.get('peigujia', 0)}"
                    )
                current_fp = hashlib.sha256(
                    "\n".join(fp_parts).encode("utf-8")
                ).hexdigest()[:16]
                earliest = exc_events["date"].iloc[0].date() if hasattr(
                    exc_events["date"].iloc[0], "date"
                ) else pd.Timestamp(exc_events["date"].iloc[0]).date()

        # 对比 Redis 存储的上次 fingerprint
        last_fp = self._get_stored_fingerprint(instrument_id)
        if last_fp == current_fp:
            logger.debug(
                "detect_company_action_change 无变化 instrument_id=%s", instrument_id
            )
            return None

        # fingerprint 变化或首次记录：存储新 fingerprint，返回最早事件日期
        self._store_fingerprint(instrument_id, current_fp)
        logger.info(
            "detect_company_action_change 检测到变化 instrument_id=%s earliest=%s",
            instrument_id, earliest,
        )
        return earliest

    def _get_stored_fingerprint(self, instrument_id: uuid.UUID) -> str | None:
        """从 Redis 读取上次公司行为 fingerprint。"""
        try:
            from app.core.redis_client import get_sync_redis
            client = get_sync_redis()
            key = f"{_FP_PREFIX}:{instrument_id}"
            raw = client.get(key)
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return raw
        except Exception as exc:
            logger.debug("读取 fingerprint 失败 instrument_id=%s: %s", instrument_id, exc)
            return None

    def _store_fingerprint(self, instrument_id: uuid.UUID, fingerprint: str) -> None:
        """存储公司行为 fingerprint 到 Redis（无 TTL，长期保留）。"""
        try:
            from app.core.redis_client import get_sync_redis
            client = get_sync_redis()
            key = f"{_FP_PREFIX}:{instrument_id}"
            client.set(key, fingerprint)
        except Exception as exc:
            logger.warning(
                "存储 fingerprint 失败 instrument_id=%s: %s", instrument_id, exc
            )

    def _delete_fingerprint(self, instrument_id: uuid.UUID) -> None:
        """[CHANGE-20260717-002 SSOT] 删除存储的 fingerprint。

        detect_company_action_change 检测到变化时会立即存储新 fingerprint，
        若后续 rebuild_factor_series 失败，必须调用本方法回滚 fingerprint，
        保证下次运行重新检测并重建（避免因子永久停留在旧值）。
        """
        try:
            from app.core.redis_client import get_sync_redis
            client = get_sync_redis()
            key = f"{_FP_PREFIX}:{instrument_id}"
            client.delete(key)
        except Exception as exc:
            logger.warning(
                "删除 fingerprint 失败 instrument_id=%s: %s", instrument_id, exc
            )


if __name__ == "__main__":
    # 自测入口：验证函数签名与基础逻辑（不连 DB/网络，无副作用）
    import inspect

    logging.basicConfig(level=logging.INFO)

    service = AdjustmentFactorService()

    # 1. 验证方法签名（bound method 不含 self）
    sig_get = inspect.signature(service.get_factor_series)
    assert list(sig_get.parameters.keys()) == ["session", "instrument_id", "as_of"], \
        f"get_factor_series 参数不匹配: {list(sig_get.parameters.keys())}"

    sig_apply = inspect.signature(service.apply_qfq)
    assert list(sig_apply.parameters.keys()) == ["bars_df", "factor_df", "as_of", "intraday"], \
        f"apply_qfq 参数不匹配: {list(sig_apply.parameters.keys())}"

    sig_rebuild = inspect.signature(service.rebuild_factor_series)
    assert list(sig_rebuild.parameters.keys()) == [
        "session", "instrument_id", "symbol", "earliest_affected", "adapter"
    ], f"rebuild_factor_series 参数不匹配: {list(sig_rebuild.parameters.keys())}"

    sig_detect = inspect.signature(service.detect_company_action_change)
    assert list(sig_detect.parameters.keys()) == [
        "session", "instrument_id", "symbol", "adapter"
    ], f"detect_company_action_change 参数不匹配: {list(sig_detect.parameters.keys())}"
    print("方法签名校验 ✓")

    # 2. 验证 apply_qfq（as_of=None 向后兼容）
    bars_df = pd.DataFrame({
        "open": [20.0, 10.0],
        "high": [20.5, 10.2],
        "low": [19.8, 9.8],
        "close": [20.0, 10.0],
        "volume": [100000, 200000],
    }, index=pd.to_datetime(["2026-06-16", "2026-06-17"]))
    bars_df.index.name = "bar_time"

    factor_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17"]),
        "adj_factor": [0.5, 1.0],
    })

    # as_of=None（向后兼容，denominator=latest_adj=1.0）
    qfq_none = service.apply_qfq(bars_df, factor_df, as_of=None, intraday=False)
    assert abs(float(qfq_none.loc[pd.Timestamp("2026-06-16"), "close"]) - 10.0) < 1e-6, \
        f"as_of=None 06-16 qfq 应=10.0, got {qfq_none.loc[pd.Timestamp('2026-06-16'), 'close']}"
    print("apply_qfq as_of=None 向后兼容 ✓")

    # as_of=2026-06-16（point-in-time，无未来泄漏）
    qfq_asof = service.apply_qfq(
        bars_df, factor_df, as_of=date(2026, 6, 16), intraday=False
    )
    assert abs(float(qfq_asof.loc[pd.Timestamp("2026-06-16"), "close"]) - 20.0) < 1e-6, \
        f"as_of=06-16 06-16 qfq 应=20.0（无未来泄漏）, got {qfq_asof.loc[pd.Timestamp('2026-06-16'), 'close']}"
    print("apply_qfq as_of=06-16 无未来泄漏 ✓")

    # 3. 验证空因子（degraded 场景）
    empty_factor = pd.DataFrame(columns=["trade_date", "adj_factor"])
    qfq_empty = service.apply_qfq(bars_df, empty_factor, as_of=None, intraday=False)
    pd.testing.assert_frame_equal(qfq_empty, bars_df)
    print("空因子原样返回（degraded）✓")

    print("OK")
