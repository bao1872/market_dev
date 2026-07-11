"""qstock 采集适配器（PRD §7.5）。

qstock 只存在于本模块，不成为用户请求链的运行时依赖。
每日收盘后由 board_sync_service 调用，拉取板块目录和成分。

单并发；批量 500～1000；设置连接/读取超时和有限重试。
任一行业/概念目录或成分请求失败，整次同步失败，禁止当成空列表继续。
标准化并去重板块、股票代码。

qstock 内部使用 requests/pandas 同步调用，通过 asyncio.to_thread 包装
避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# qstock 拉取参数（实际使用）
FETCH_TIMEOUT_SECONDS = 30
FETCH_RETRY_COUNT = 2
FETCH_BACKOFF_BASE = 2.0  # 指数退避基数：2s, 4s
BATCH_SIZE = 500


class QStockFetchError(Exception):
    """qstock 拉取失败（目录或成分请求异常）。"""


def _fetch_with_retry(
    qs: Any,
    method_name: str,
    *args: Any,
    label: str = "",
) -> Any:
    """同步执行 qstock 方法，带超时、有限重试和指数退避。

    超时或异常时重试，超过 FETCH_RETRY_COUNT 次后抛出 QStockFetchError。
    """
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRY_COUNT + 1):
        try:
            # qstock 的 ths_index_data / ths_index_stock_data 不直接支持 timeout
            # 使用 func_timeout 包装（qstock 依赖 func-timeout）
            try:
                import func_timeout

                result = func_timeout.func_timeout(
                    FETCH_TIMEOUT_SECONDS,
                    getattr(qs, method_name),
                    args=args,
                )
                return result
            except ImportError:
                # func_timeout 不可用时直接调用（无超时保护，但仍有重试）
                result = getattr(qs, method_name)(*args)
                return result
        except Exception as e:
            last_exc = e
            if attempt < FETCH_RETRY_COUNT:
                backoff = FETCH_BACKOFF_BASE ** attempt
                logger.warning(
                    "qstock %s attempt %d failed: %s, retrying in %.1fs",
                    label or method_name,
                    attempt + 1,
                    e,
                    backoff,
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "qstock %s failed after %d attempts: %s",
                    label or method_name,
                    FETCH_RETRY_COUNT + 1,
                    e,
                )

    raise QStockFetchError(
        f"qstock {label or method_name} failed after {FETCH_RETRY_COUNT + 1} attempts: {last_exc}"
    )


def _fetch_boards_sync(qs: Any) -> list[dict[str, str]]:
    """同步拉取板块目录（行业 + 概念），在 to_thread 中执行。

    任一目录请求失败抛出 QStockFetchError，不返回空列表。
    标准化并去重板块代码。
    """
    boards: list[dict[str, str]] = []
    seen_codes: set[str] = set()

    # 行业板块
    try:
        industry_df = _fetch_with_retry(qs, "ths_index_data", "行业", label="industry_boards")
    except QStockFetchError:
        raise
    if industry_df is not None and not industry_df.empty:
        for _, row in industry_df.iterrows():
            code = str(row.get("code", row.get("板块代码", ""))).strip()
            name = str(row.get("name", row.get("板块名称", ""))).strip()
            if code and name and code not in seen_codes:
                seen_codes.add(code)
                boards.append({"external_code": code, "name": name, "type": "industry"})

    # 概念板块
    try:
        concept_df = _fetch_with_retry(qs, "ths_index_data", "概念", label="concept_boards")
    except QStockFetchError:
        raise
    if concept_df is not None and not concept_df.empty:
        for _, row in concept_df.iterrows():
            code = str(row.get("code", row.get("板块代码", ""))).strip()
            name = str(row.get("name", row.get("板块名称", ""))).strip()
            if code and name and code not in seen_codes:
                seen_codes.add(code)
                boards.append({"external_code": code, "name": name, "type": "concept"})

    logger.info("Fetched %d unique boards from qstock", len(boards))
    return boards


def _fetch_memberships_sync(qs: Any, board_external_code: str) -> list[str]:
    """同步拉取板块成分股，在 to_thread 中执行。

    请求失败抛出 QStockFetchError，不返回空列表。
    标准化并去重股票代码（6 位左填充）。
    """
    df = _fetch_with_retry(
        qs, "ths_index_stock_data", board_external_code,
        label=f"memberships_{board_external_code}",
    )
    if df is None or df.empty:
        logger.warning("Board %s returned empty membership", board_external_code)
        return []

    symbols: list[str] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        code = str(row.get("code", row.get("股票代码", ""))).strip()
        if code:
            # 标准化为 6 位代码
            code = code.zfill(6) if len(code) < 6 else code
            if code not in seen:
                seen.add(code)
                symbols.append(code)

    logger.info("Fetched %d unique members for board %s", len(symbols), board_external_code)
    return symbols


class QStockFetcher:
    """qstock 板块数据拉取适配器。

    实现 BoardFetcher 协议。
    不缓存数据，每次调用实时拉取。
    qstock 同步调用通过 asyncio.to_thread 包装，不阻塞事件循环。
    任一请求失败抛出 QStockFetchError，不返回空数组伪装成功。
    """

    def __init__(self) -> None:
        self._qs: Any = None

    def _ensure_qstock(self) -> Any:
        """延迟导入 qstock，避免模块加载时依赖。"""
        if self._qs is None:
            try:
                import qstock as qs
                self._qs = qs
            except ImportError as e:
                raise ImportError(
                    "qstock is not installed; pip install qstock==1.3.8"
                ) from e
        return self._qs

    async def fetch_boards(self) -> list[dict[str, str]]:
        """拉取板块目录（行业 + 概念）。

        返回 [{external_code, name, type}]。
        type: 'industry' | 'concept'

        任一目录请求失败抛出 QStockFetchError。
        qstock 同步调用通过 asyncio.to_thread 执行，不阻塞事件循环。
        """
        qs = self._ensure_qstock()
        return await asyncio.to_thread(_fetch_boards_sync, qs)

    async def fetch_memberships(
        self, board_external_code: str, board_type: str
    ) -> list[str]:
        """拉取指定板块的成分股代码列表。

        Args:
            board_external_code: qstock 板块代码
            board_type: 'industry' | 'concept'

        Returns:
            股票代码列表（如 ['000001', '000002', ...]）

        请求失败抛出 QStockFetchError。
        qstock 同步调用通过 asyncio.to_thread 执行，不阻塞事件循环。
        """
        qs = self._ensure_qstock()
        return await asyncio.to_thread(
            _fetch_memberships_sync, qs, board_external_code
        )
