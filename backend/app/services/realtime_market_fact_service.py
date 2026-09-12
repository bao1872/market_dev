"""盘中实时行情事实服务（Stage G3）。

核心契约：
1. 唯一主源：pytdx（get_security_quotes_with_provenance，80只/批，TCP极速，毫秒级响应）；
2. 备用源：Eastmoney push2.eastmoney.com 实时快照（pytdx 明确失败时受控降级；BJ 北交所 pytdx 原生不支持，直接进入备用源）；
3. 内存价格追踪器（PriceTracker）：维护上一次快照与本次快照价格 [P_last, P_curr]，供 Crossing 判定；
4. 不做任何指标重算，只生产轻量、高保真的实时行情事实。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.core.pytdx_adapter import PytdxAdapter, PytdxSourceError, connect_pytdx
from app.services.realtime_market_snapshot_provider import (
    SnapshotProviderError,
    fetch_realtime_a_share_snapshot,
)

logger = logging.getLogger(__name__)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_PYTDX_BATCH_SIZE = 80


@dataclass(frozen=True)
class RealtimeQuote:
    """标准盘中实时行情快照事实（raw，不复权）。"""

    symbol: str
    price: float
    last_close: float
    open_price: float
    high: float
    low: float
    volume: float  # 统一为 canonical 股（手 × 100）
    amount: float  # 元
    captured_at: datetime
    source: str  # "pytdx" | "eastmoney"


class PriceTracker:
    """标的连续快照价格区间跟踪器（内存单例或按监控实例持有）。

    每次更新价格时，返回上一次快照价格与本次快照价格形成的价格区间 [price_last, price_curr]。
    对于首次出现的标的，price_last 初始化为本次价格（区间长度为 0），避免由 0 跃迁触发伪穿透。
    """

    def __init__(self) -> None:
        self._last_prices: dict[str, float] = {}

    def update_price(self, symbol: str, current_price: float) -> tuple[float, float]:
        """记录当前价并返回 (price_last, price_curr)。"""
        if symbol not in self._last_prices:
            self._last_prices[symbol] = current_price
            return current_price, current_price

        p_last = self._last_prices[symbol]
        self._last_prices[symbol] = current_price
        return p_last, current_price

    def get_last_price(self, symbol: str) -> float | None:
        return self._last_prices.get(symbol)

    def set_last_price(self, symbol: str, price: float) -> None:
        self._last_prices[symbol] = price

    def clear(self) -> None:
        self._last_prices.clear()

    def reset(self, symbol: str | None = None) -> None:
        """重置指定标的或全部标的前次价格。"""
        if symbol is None:
            self._last_prices.clear()
        else:
            self._last_prices.pop(symbol, None)


def resolve_snapshot_price_range(
    price_tracker: PriceTracker,
    symbol: str,
    current_price: float,
    *,
    prev_node_version: object = None,
    prev_smc_version: object = None,
    curr_node_version: object = None,
    curr_smc_version: object = None,
    persisted_price: float | None = None,
) -> tuple[float, float]:
    """[G7 唯一生命周期 owner] 解析本轮连续快照价格区间 ``[P_last, P_curr]``。

    **生产（MonitorBatchService）与极速旁路（WatchlistRealtimeMonitorService）必须
    共用这唯一一套规则**，禁止两侧各自定义生命周期语义 —— 否则修复只会落在没有成为
    production owner 的那一侧。

    规则（严格按序）：
    1. **Target Set Version 变更**（XDXR 除权除息 / 日线滚动 / 坐标重算）→ 首帧强制
       ``(p, p)``。严禁拿旧复权坐标的 ``P_last`` 去扫描新坐标 TargetSet，
       否则会制造完全假的穿透。
    2. **内存无价格**（进程重启 / 首次执行）且持久化价与当前版本一致 →
       用持久化的 ``current_price`` 恢复 ``P_last``，支持重启窗口期的
       catch-up 穿透（重启前 100、重启期间涨到 105、target=103 必须能命中）。
    3. 其余情况 → 由 :class:`PriceTracker` 连续推进快照区间。
    """
    is_node_ver_changed = (
        prev_node_version is not None and curr_node_version != prev_node_version
    )
    is_smc_ver_changed = prev_smc_version is not None and curr_smc_version != prev_smc_version

    if is_node_ver_changed or is_smc_ver_changed:
        price_tracker.set_last_price(symbol, current_price)
        return current_price, current_price

    if price_tracker.get_last_price(symbol) is None:
        same_version = (
            (prev_node_version == curr_node_version if curr_node_version else True)
            and (prev_smc_version == curr_smc_version if curr_smc_version else True)
        )
        if persisted_price is not None and same_version:
            try:
                restored = float(persisted_price)
            except (TypeError, ValueError):
                restored = None
            if restored is not None:
                price_tracker.set_last_price(symbol, current_price)
                return restored, current_price

    return price_tracker.update_price(symbol, current_price)


class RealtimeMarketFactService:
    """盘中行情事实引擎。"""

    def __init__(self, price_tracker: PriceTracker | None = None) -> None:
        self.price_tracker = price_tracker or PriceTracker()

    @staticmethod
    def _is_bj_symbol(symbol: str) -> bool:
        """判定是否为北交所股票代码（920xxx, 43xxxx, 83xxxx, 87xxxx 等）。"""
        return symbol.startswith(("920", "43", "83", "87", "88"))

    async def fetch_quotes(
        self,
        symbols: Sequence[str],
        *,
        adapter: PytdxAdapter | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, RealtimeQuote]:
        """批量获取指定标的的实时行情事实。

        数据源优先级：
        1. SH / SZ 标的：pytdx 主源（80只/批，并发/顺序获取）；
        2. BJ 标的：pytdx 不支持，直接走 Eastmoney 备用源；
        3. 若 pytdx 失败抛出 PytdxSourceError：降级走 Eastmoney 备用源。

        Returns:
            {symbol: RealtimeQuote}
        """
        if not symbols:
            return {}

        results: dict[str, RealtimeQuote] = {}
        now = datetime.now(_SHANGHAI_TZ)

        # 统一规范化为 symbol 字符串
        norm_symbols: list[str] = [
            s.symbol if hasattr(s, "symbol") else str(s)
            for s in symbols
        ]
        # 去重保持顺序
        seen: set[str] = set()
        clean_symbols = [s for s in norm_symbols if not (s in seen or seen.add(s))]

        sh_sz_symbols: list[str] = []
        bj_symbols: list[str] = []

        for s in clean_symbols:
            if self._is_bj_symbol(s):
                bj_symbols.append(s)
            else:
                sh_sz_symbols.append(s)

        # 1. 抓取 SH / SZ（优先 pytdx 主源）
        failed_sh_sz: list[str] = []
        if sh_sz_symbols:
            if adapter is not None:
                pytdx_quotes, failed = await self._fetch_via_pytdx(adapter, sh_sz_symbols, now)
                results.update(pytdx_quotes)
                failed_sh_sz.extend(failed)
            else:
                try:
                    with connect_pytdx() as connected_adapter:
                        pytdx_quotes, failed = await self._fetch_via_pytdx(
                            connected_adapter, sh_sz_symbols, now
                        )
                        results.update(pytdx_quotes)
                        failed_sh_sz.extend(failed)
                except Exception as exc:
                    logger.warning("[MARKET-FACT] pytdx 连接失败，整批转入备用源: %s", exc)
                    failed_sh_sz.extend(sh_sz_symbols)

        # 2. 备用源：Eastmoney（用于 BJ 标的以及 pytdx 失败的 SH/SZ 标的）
        fallback_targets = set(bj_symbols + failed_sh_sz)
        if fallback_targets:
            logger.info(
                "[MARKET-FACT] 请求 Eastmoney 备用源: %d 只标的 (BJ=%d, fallback=%d)",
                len(fallback_targets),
                len(bj_symbols),
                len(failed_sh_sz),
            )
            em_quotes = await self._fetch_via_eastmoney(client, fallback_targets, now)
            results.update(em_quotes)

        return results

    async def _fetch_via_pytdx(
        self,
        adapter: PytdxAdapter,
        symbols: list[str],
        now: datetime,
    ) -> tuple[dict[str, RealtimeQuote], list[str]]:
        """分批通过 pytdx 获取实时 quote。"""
        quotes: dict[str, RealtimeQuote] = {}
        failed_symbols: list[str] = []

        for idx in range(0, len(symbols), _PYTDX_BATCH_SIZE):
            chunk = symbols[idx : idx + _PYTDX_BATCH_SIZE]
            try:
                # pytdx adapter 是同步 IO，通过 to_thread 避免阻塞 event loop
                raw_rows, _ = await asyncio.to_thread(
                    adapter.get_security_quotes_with_provenance, chunk
                )
                chunk_found: set[str] = set()
                for row in raw_rows:
                    code = str(row.get("code") or "")
                    if not code:
                        continue
                    price = float(row.get("price") or 0.0)
                    last_close = float(row.get("last_close") or 0.0)
                    # 停牌或未产生成交时，当前价为 0，回退至前收盘价
                    effective_price = price if price > 0.0 else last_close

                    vol_lots = float(row.get("vol") or 0.0)
                    volume_shares = vol_lots * 100.0  # 手转股
                    amount = float(row.get("amount") or 0.0)

                    quotes[code] = RealtimeQuote(
                        symbol=code,
                        price=effective_price,
                        last_close=last_close,
                        open_price=float(row.get("open") or 0.0),
                        high=float(row.get("high") or 0.0),
                        low=float(row.get("low") or 0.0),
                        volume=volume_shares,
                        amount=amount,
                        captured_at=now,
                        source="pytdx",
                    )
                    chunk_found.add(code)

                # 记录该批次中未返回 quote 的标的
                for s in chunk:
                    if s not in chunk_found:
                        failed_symbols.append(s)

            except (PytdxSourceError, Exception) as exc:
                logger.warning(
                    "[MARKET-FACT] pytdx batch %d~%d 获取失败，转备用源: %s",
                    idx,
                    idx + len(chunk),
                    exc,
                )
                failed_symbols.extend(chunk)

        return quotes, failed_symbols

    async def _fetch_via_eastmoney(
        self,
        client: httpx.AsyncClient | None,
        symbols: set[str],
        now: datetime,
    ) -> dict[str, RealtimeQuote]:
        """通过 Eastmoney 备用源获取实时快照。"""
        quotes: dict[str, RealtimeQuote] = {}
        own_client = False
        active_client = client
        if active_client is None:
            active_client = httpx.AsyncClient(timeout=15.0)
            own_client = True

        try:
            snapshot = await fetch_realtime_a_share_snapshot(
                active_client,
                symbols=symbols,
                now=now,
            )
            for row in snapshot.rows:
                if row.symbol not in symbols:
                    continue
                price = float(row.close) if row.close > 0.0 else float(row.previous_close)
                quotes[row.symbol] = RealtimeQuote(
                    symbol=row.symbol,
                    price=price,
                    last_close=float(row.previous_close),
                    open_price=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    volume=float(row.volume),  # EodSnapshotRow.volume 已经是股
                    amount=float(row.amount),
                    captured_at=snapshot.captured_at,
                    source="eastmoney",
                )
        except (SnapshotProviderError, Exception) as exc:
            logger.error("[MARKET-FACT] Eastmoney 备用源抓取失败: %s", exc)
        finally:
            if own_client:
                await active_client.aclose()

        return quotes


__all__ = [
    "PriceTracker",
    "RealtimeMarketFactService",
    "RealtimeQuote",
    "resolve_snapshot_price_range",
]
