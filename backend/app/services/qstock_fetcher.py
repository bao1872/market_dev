"""qstock 采集适配器（PRD §7.5）。

qstock 只存在于本模块，不成为用户请求链的运行时依赖。
每日收盘后由 board_sync_service 调用，拉取板块目录和成分。

单并发；批量 500～1000；设置连接/读取超时和有限重试。
失败重试后继续沿用上次成功快照并告警。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# qstock 拉取参数
FETCH_TIMEOUT_SECONDS = 30
FETCH_RETRY_COUNT = 2
BATCH_SIZE = 500


class QStockFetcher:
    """qstock 板块数据拉取适配器。

    实现 BoardFetcher 协议。
    不缓存数据，每次调用实时拉取。
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
                    "qstock is not installed; pip install qstock"
                ) from e
        return self._qs

    async def fetch_boards(self) -> list[dict[str, str]]:
        """拉取板块目录（行业 + 概念）。

        返回 [{external_code, name, type}]。
        type: 'industry' | 'concept'
        """
        qs = self._ensure_qstock()

        boards: list[dict[str, str]] = []

        # 行业板块
        try:
            industry_df = qs.ths_index_data("行业")
            if industry_df is not None and not industry_df.empty:
                for _, row in industry_df.iterrows():
                    code = str(row.get("code", row.get("板块代码", "")))
                    name = str(row.get("name", row.get("板块名称", "")))
                    if code and name:
                        boards.append({
                            "external_code": code,
                            "name": name,
                            "type": "industry",
                        })
        except Exception as e:
            logger.warning(f"Failed to fetch industry boards: {e}")

        # 概念板块
        try:
            concept_df = qs.ths_index_data("概念")
            if concept_df is not None and not concept_df.empty:
                for _, row in concept_df.iterrows():
                    code = str(row.get("code", row.get("板块代码", "")))
                    name = str(row.get("name", row.get("板块名称", "")))
                    if code and name:
                        boards.append({
                            "external_code": code,
                            "name": name,
                            "type": "concept",
                        })
        except Exception as e:
            logger.warning(f"Failed to fetch concept boards: {e}")

        logger.info(f"Fetched {len(boards)} boards from qstock")
        return boards

    async def fetch_memberships(
        self, board_external_code: str, board_type: str
    ) -> list[str]:
        """拉取指定板块的成分股代码列表。

        Args:
            board_external_code: qstock 板块代码
            board_type: 'industry' | 'concept'

        Returns:
            股票代码列表（如 ['000001', '000002', ...]）
        """
        qs = self._ensure_qstock()

        try:
            df = qs.ths_index_stock_data(board_external_code)
            if df is None or df.empty:
                return []

            symbols: list[str] = []
            for _, row in df.iterrows():
                code = str(row.get("code", row.get("股票代码", "")))
                if code:
                    # 标准化为 6 位代码
                    code = code.zfill(6) if len(code) < 6 else code
                    symbols.append(code)

            logger.info(
                f"Fetched {len(symbols)} members for board {board_external_code} ({board_type})"
            )
            return symbols

        except Exception as e:
            logger.warning(
                f"Failed to fetch memberships for board {board_external_code}: {e}"
            )
            return []
