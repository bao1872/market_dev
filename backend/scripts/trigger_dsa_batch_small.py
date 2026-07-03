"""触发小批量 DSA batch run（50 只代表性股票）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.trigger_dsa_batch_small

功能：
1. 选择 50 只代表性股票（含 SH/SZ 主板/创业板/科创板）
2. 创建 batch run（run_type=scheduled，避免与已有 manual run 幂等冲突）
3. 执行 run（同步等待完成）
4. 发布结果
5. 输出 run_id 与统计信息

无副作用（除创建 run 和写入 strategy_results 外，不改其他表）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date

from app.db import AsyncSessionLocal
from app.services.strategy_batch_service import StrategyBatchService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trigger_dsa_batch_small")

# 50 只代表性股票 instrument_ids（含 SH/SZ 主板/创业板/科创板）
REPRESENTATIVE_INSTRUMENT_IDS: list[uuid.UUID] = [
    # SZ 主板
    uuid.UUID("da8f815b-59ea-45e5-ade8-80204471d248"),  # 000001 平安银行
    uuid.UUID("cafb08c5-c33e-424a-b2f3-c5d503a2eb90"),  # 000063 中兴通讯
    uuid.UUID("aea2c3f9-2059-4933-925c-6a20ef2f5dcc"),  # 000333 美的集团
    uuid.UUID("785eb773-ed5a-4b27-8b58-e7426742c0cc"),  # 000538 云南白药
    uuid.UUID("ea023bbc-6e3f-4119-8e0f-5a9915ffbb11"),  # 000651 格力电器
    uuid.UUID("74c4cc6d-689c-481c-9cb4-4680f7539a20"),  # 000725 京东方Ａ
    uuid.UUID("521d442e-af90-43e0-b403-b321955750ed"),  # 000858 五粮液
    uuid.UUID("6508bcbd-927f-4c85-aa16-3d4db2aec700"),  # 000999 华润三九
    uuid.UUID("23ab893e-85c1-4e79-9bf5-0aeaf986a180"),  # 002415 海康威视
    # SZ 创业板
    uuid.UUID("4697fd0d-e57c-4012-b803-5bdd4a41aae0"),  # 300015 爱尔眼科
    uuid.UUID("dbb18019-bff6-4bad-84c9-44c2c0d47ea5"),  # 300059 东方财富
    uuid.UUID("b5cbdfbb-e6e7-4fbc-b5d5-c40f54b14705"),  # 300124 汇川技术
    uuid.UUID("dc9e128f-b385-4730-bcf3-70ef3fa8e941"),  # 300142 沃森生物
    uuid.UUID("0e5b521b-c8ff-4ec7-ac2c-7dd6046a0066"),  # 300274 阳光电源
    uuid.UUID("086dc5f3-1497-4569-959d-7ad172a043a7"),  # 300316 晶盛机电
    uuid.UUID("58f3f191-6df4-43e7-9b52-bc9fd2be77aa"),  # 300347 泰格医药
    uuid.UUID("1cc60d7b-8c85-4917-83b0-0a770b98ab09"),  # 300433 蓝思科技
    uuid.UUID("d58e8d73-e50e-4f50-a5d0-a458f25d523c"),  # 300750 宁德时代
    uuid.UUID("67d42eac-6fa5-40d3-82f3-1d67267969af"),  # 300760 迈瑞医疗
    # SH 主板
    uuid.UUID("36467fdb-80da-4d33-8bee-ea1158e3002c"),  # 600009 上海机场
    uuid.UUID("a23608b3-a037-44d9-9ce6-8fe45aa5014f"),  # 600016 民生银行
    uuid.UUID("e19a2d90-925e-4c66-8eb7-b909e6496512"),  # 600030 中信证券
    uuid.UUID("0da3310d-a6b6-428b-b7b7-665f8cc5f716"),  # 600031 三一重工
    uuid.UUID("0e93d44d-5584-4063-a12a-1e57a51ba22d"),  # 600036 招商银行
    uuid.UUID("5f8906da-576e-4324-990c-d0ccd770c48d"),  # 600276 恒瑞医药
    uuid.UUID("7f68fb2e-b681-41d1-b0b3-324c551fe22a"),  # 600519 贵州茅台
    uuid.UUID("0919bcff-5bdf-464b-be3b-96c19a8f908d"),  # 600585 海螺水泥
    uuid.UUID("d9d9215e-bdbd-49b8-bf88-5ced507fbeab"),  # 600690 海尔智家
    uuid.UUID("bc4b5820-7419-4f43-aea9-aa30c97c8952"),  # 600887 伊利股份
    uuid.UUID("a17c39be-8c86-4c07-b42b-f52db8e90f95"),  # 601012 隆基绿能
    uuid.UUID("f3cd69d7-3b94-4462-9d29-6e24f0ba0629"),  # 601088 中国神华
    uuid.UUID("299a6861-33ac-4851-94b4-fb986d58db92"),  # 601288 农业银行
    uuid.UUID("135f39ce-9320-464c-97f0-228f4bd29ee1"),  # 601318 中国平安
    uuid.UUID("c97e1c09-d4dd-498f-a70f-8f8802caa4e4"),  # 601398 工商银行
    uuid.UUID("4c162095-3b37-49d2-8617-fb9bd69fa20a"),  # 601628 中国人寿
    uuid.UUID("4ed1eb3b-d2cb-47c3-8573-9110b276bee9"),  # 601633 长城汽车
    uuid.UUID("90df6240-f0fe-404a-9260-5d0688d3cb36"),  # 601668 中国建筑
    uuid.UUID("e63e289c-3294-4b31-8283-cf8ee7cd7ebb"),  # 601857 中国石油
    uuid.UUID("56b575ff-5e18-41ad-b7a8-454cddf1e592"),  # 601988 中国银行
    # SH 科创板
    uuid.UUID("533ca1ac-43a7-4f11-819e-5329b847634b"),  # 688041 海光信息
    uuid.UUID("c7337628-dfc2-4c78-8173-cccc4bf22b5f"),  # 688111 金山办公
    uuid.UUID("b7660f10-32c4-4843-87fd-54dc32cd750b"),  # 688169 石头科技
    uuid.UUID("411d90cb-c2a9-4eff-ab20-59c069a7071b"),  # 688180 君实生物
    uuid.UUID("f365acf8-46c7-4dee-a55a-32e03b2f7492"),  # 688185 康希诺
    uuid.UUID("e44d1eaa-48df-46d9-9833-8a7c0a4b1982"),  # 688187 时代电气
    uuid.UUID("498f0505-7474-4c2f-8089-5d43ea86535b"),  # 688200 华峰测控
    uuid.UUID("532a329f-3889-417e-9ac3-f0d5f67bda78"),  # 688202 美迪西
    uuid.UUID("6a1ce4eb-6e90-44bb-be04-1a65b96f5b37"),  # 688256 寒武纪
    uuid.UUID("a7581c4d-9642-409c-84fb-7e1898e2cccb"),  # 688981 中芯国际
]

TRADE_DATE = date(2026, 6, 16)
STRATEGY_KEY = "dsa_selector"


async def main() -> None:
    """主流程：创建 → 执行 → 发布。"""
    service = StrategyBatchService()

    async with AsyncSessionLocal() as db:
        # 1. 创建 batch run（run_type=scheduled，避免与已有 manual run 幂等冲突）
        logger.info(
            "创建 batch run: strategy=%s, trade_date=%s, instruments=%d",
            STRATEGY_KEY, TRADE_DATE, len(REPRESENTATIVE_INSTRUMENT_IDS),
        )
        run = await service.create_batch_run(
            db,
            strategy_key=STRATEGY_KEY,
            trade_date=TRADE_DATE,
            run_type="scheduled",
            instrument_ids=REPRESENTATIVE_INSTRUMENT_IDS,
        )
        await db.commit()
        logger.info(
            "batch run 已创建: run_id=%s, status=%s, total=%d",
            run.id, run.status, run.total_instruments,
        )

        # 2. 执行 run（同步等待完成）
        logger.info("开始执行 batch run: run_id=%s", run.id)
        await service.execute_run(db, run.id)
        await db.commit()

        # 重新加载 run 获取最终状态
        from sqlalchemy import select

        from app.models.strategy_run import StrategyRun
        result = await db.execute(select(StrategyRun).where(StrategyRun.id == run.id))
        run = result.scalar_one()
        logger.info(
            "batch run 执行完成: run_id=%s, status=%s, "
            "succeeded=%d, failed=%d, skipped=%d",
            run.id, run.status,
            run.succeeded_count, run.failed_count, run.skipped_count,
        )

        # 3. 发布结果（若 completed 或 partial_failed）
        if run.status in ("completed", "partial_failed"):
            logger.info("发布 run: run_id=%s", run.id)
            run = await service.publish_run(db, run.id)
            await db.commit()
            logger.info(
                "run 已发布: run_id=%s, published_at=%s",
                run.id, run.published_at,
            )
        else:
            logger.warning(
                "run 状态不允许发布（%s），跳过发布步骤", run.status,
            )

    # 4. 输出验证信息
    print("\n=== 验证信息 ===")
    print(f"run_id: {run.id}")
    print(f"status: {run.status}")
    print(f"trade_date: {run.trade_date}")
    print(f"total_instruments: {run.total_instruments}")
    print(f"succeeded_count: {run.succeeded_count}")
    print(f"failed_count: {run.failed_count}")
    print(f"skipped_count: {run.skipped_count}")
    print(f"published_at: {run.published_at}")
    print("\n前端验证 URL: http://43.136.118.82/screener")
    print("API 验证: curl 'http://43.136.118.82/api/strategies/dsa_selector/published-runs'")


if __name__ == "__main__":
    asyncio.run(main())
