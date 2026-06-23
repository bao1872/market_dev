"""全市场 3 周期回补脚本（日线/15min/60min）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.backfill_all_periods

功能：
1. 使用 BarsSchedulerService.backfill_all_instruments 执行全市场回补
2. 日线从 2023-01-01 开始回补
3. 15min/60min 使用 BACKFILL_COUNTS（受 pytdx 服务端限制，实际获取约 2 年数据）
4. 1m 不参与回补（按需从 DB 查询，DB 无数据时策略降级运行）
5. 串行处理（pytdx 不支持并发），带 tqdm 进度条
6. 失败重试 3 次，不中断整体流程
7. 结果输出到日志和 stdout

设计说明：
- pytdx 串行拉取，不支持并发
- upsert 幂等，可重复执行
- 单只失败不中断整体流程，最后汇总失败列表
- 预计耗时约 3-4 小时（8268 只 × 3 周期）
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date

# 确保可以 import app.*
sys.path.insert(0, "/root/web_dev/backend")

from app.services.bars_scheduler_service import BarsSchedulerService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("backfill_all_periods")


async def main() -> None:
    """执行全市场 3 周期回补。"""
    service = BarsSchedulerService()
    start_date = date(2023, 1, 1)

    logger.info("开始全市场 3 周期回补 start_date=%s", start_date)
    result = await service.backfill_all_instruments(start_date=start_date)

    logger.info(
        "全市场回补完成: total=%d succeeded=%d failed=%d period_counts=%s",
        result.total, result.succeeded, result.failed, result.period_counts,
    )

    if result.failed_symbols:
        logger.warning("失败股票列表（前 50 个）: %s", result.failed_symbols[:50])
        logger.warning("失败总数: %d", len(result.failed_symbols))


if __name__ == "__main__":
    asyncio.run(main())
