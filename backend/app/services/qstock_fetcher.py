"""qstock 采集适配器（PRD §7.5）。

QStockFetcher 实现 BoardFetcher 协议，内部委托给 QStockTHSAdapter
（app/services/ths_adapter.py）执行实际的同花顺板块数据拉取。

C10 决策：不使用 qstock 运行时函数（ths_concept_name_code/ths_index_member），
因为它们硬编码 BeautifulSoup(res.text, "lxml") 而 lxml 未声明依赖。
改为独立实现 QStockTHSAdapter，使用 httpx + bs4[html.parser]（不依赖 lxml）。

qstock 1.3.1 仍保留在依赖中，仅用于：
- ths_code_name dict（行业板块目录，硬编码数据，无网络无依赖）
- get_ths_header()（反爬 cookie 生成，py_mini_racer 执行 ths.js）

同步 HTTP 调用通过 asyncio.to_thread 包装，避免阻塞事件循环。
单并发；设置连接/读取超时和有限重试。
任一行业/概念目录或成分请求失败，整次同步失败，禁止当成空列表继续。
"""

from __future__ import annotations

import asyncio
import logging

from app.services.ths_adapter import QStockTHSAdapter, THSAdapterError

logger = logging.getLogger(__name__)

# 保留向后兼容的异常别名（board_sync_service 等模块可能引用 QStockFetchError）
QStockFetchError = THSAdapterError

# 保留向后兼容的常量（测试可能引用）
FETCH_TIMEOUT_SECONDS = 30
FETCH_RETRY_COUNT = 2


class QStockFetcher:
    """qstock 板块数据拉取适配器。

    实现 BoardFetcher 协议（board_sync_service.BoardFetcher）。
    委托 QStockTHSAdapter 执行实际的 HTTP 请求和解析。
    不缓存数据，每次调用实时拉取。
    同步调用通过 asyncio.to_thread 包装，不阻塞事件循环。
    任一请求失败抛出 THSAdapterError（QStockFetchError），不返回空数组伪装成功。
    """

    def __init__(self) -> None:
        self._adapter = QStockTHSAdapter()

    async def fetch_boards(self) -> list[dict[str, str]]:
        """拉取板块目录（行业 + 概念）。

        返回 [{external_code, name, type}]。
        type: 'industry' | 'concept'

        任一目录请求失败抛出 QStockFetchError。
        同步调用通过 asyncio.to_thread 执行，不阻塞事件循环。
        """
        return await asyncio.to_thread(self._adapter.fetch_boards_sync)

    async def fetch_memberships(
        self, board_external_code: str, board_type: str
    ) -> list[str]:
        """拉取指定板块的成分股代码列表。

        Args:
            board_external_code: 同花顺板块代码
            board_type: 'industry' | 'concept'

        Returns:
            股票代码列表（如 ['000001', '000002', ...]）

        请求失败抛出 QStockFetchError。
        同步调用通过 asyncio.to_thread 执行，不阻塞事件循环。
        """
        return await asyncio.to_thread(
            self._adapter.fetch_memberships_sync, board_external_code, board_type
        )
