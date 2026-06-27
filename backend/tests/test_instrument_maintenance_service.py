"""Instrument 维护服务测试 - 清理长期无日线数据的 active 股票。

测试策略：
- 复用 conftest.py 的 PostgreSQL 测试库 + db_session fixture
- 构造 active 股票 + bars_daily 记录，验证 cleanup_inactive_instruments 行为
- 覆盖场景：
  1. 长期无日线数据的 active 股票被标记为 inactive
  2. 近期有日线数据的 active 股票保持 active
  3. 已 inactive 的股票不动
  4. stale_days 阈值生效
  5. 返回汇总信息（cleaned_count / cleaned_symbols / remaining_active）
  6. 指数类标的（SH000/SZ399）不清理（保留用于指数引用）
  7. dry_run=True 只预览不修改
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.bar import BarDaily


@pytest_asyncio.fixture
async def instruments_with_mixed_bars(db_session: AsyncSession):
    """构造 4 个 active + 1 个 inactive 股票，配合 bars_daily 数据。

    场景（以 today=2026-06-26 为基准）：
    - stock_active_recent: 最近 5 天有日线数据 → 应保留 active
    - stock_active_stale_30d: 最近 35 天无日线数据 → 应清理（stale_days=30）
    - stock_active_stale_60d: 最近 65 天无日线数据 → 应清理（stale_days=30）
    - stock_index_sh: 指数类 SH000001，长期无日线数据 → 不清理（指数保留）
    - stock_inactive: 已 inactive，长期无日线数据 → 不动
    """
    today = date(2026, 6, 26)

    # 1. 近期有数据的 active 股票
    active_recent = Instrument(
        id=uuid.uuid4(),
        symbol="600519",
        name="贵州茅台",
        market="SH",
        status="active",
    )
    db_session.add(active_recent)

    # 2. 35 天前有数据，之后无数据的 active 股票
    active_stale_30d = Instrument(
        id=uuid.uuid4(),
        symbol="600000",
        name="浦发银行",
        market="SH",
        status="active",
    )
    db_session.add(active_stale_30d)

    # 3. 65 天前有数据，之后无数据的 active 股票
    active_stale_60d = Instrument(
        id=uuid.uuid4(),
        symbol="000001",
        name="平安银行",
        market="SZ",
        status="active",
    )
    db_session.add(active_stale_60d)

    # 4. 指数类标的（SH000xxx），长期无数据 → 不清理
    index_sh = Instrument(
        id=uuid.uuid4(),
        symbol="SH000001",
        name="上证指数",
        market="SH",
        status="active",
    )
    db_session.add(index_sh)

    # 5. 已 inactive 的股票
    inactive_stock = Instrument(
        id=uuid.uuid4(),
        symbol="300750",
        name="宁德时代",
        market="SZ",
        status="inactive",
    )
    db_session.add(inactive_stock)

    await db_session.flush()

    # bars_daily 数据
    # active_recent: today - 5 天有数据
    db_session.add(BarDaily(
        instrument_id=active_recent.id,
        trade_date=today - timedelta(days=5),
        open="100.0", high="101.0", low="99.0", close="100.5",
        volume="10000", amount="1000000.0",
    ))
    # active_stale_30d: today - 35 天有数据（超过 stale_days=30 阈值）
    db_session.add(BarDaily(
        instrument_id=active_stale_30d.id,
        trade_date=today - timedelta(days=35),
        open="10.0", high="11.0", low="9.0", close="10.5",
        volume="10000", amount="100000.0",
    ))
    # active_stale_60d: today - 65 天有数据
    db_session.add(BarDaily(
        instrument_id=active_stale_60d.id,
        trade_date=today - timedelta(days=65),
        open="20.0", high="21.0", low="19.0", close="20.5",
        volume="10000", amount="200000.0",
    ))
    # index_sh: 无 bars_daily 数据（指数不写入 bars_daily）
    # inactive_stock: 无 bars_daily 数据
    await db_session.flush()

    yield {
        "today": today,
        "active_recent": active_recent,
        "active_stale_30d": active_stale_30d,
        "active_stale_60d": active_stale_60d,
        "index_sh": index_sh,
        "inactive_stock": inactive_stock,
    }


class TestCleanupInactiveInstruments:
    """cleanup_inactive_instruments 函数测试。"""

    @pytest.mark.asyncio
    async def test_cleanup_marks_stale_active_as_inactive(
        self, db_session, instruments_with_mixed_bars,
    ):
        """长期无日线数据的 active 股票被标记为 inactive。"""
        from app.services.instrument_maintenance_service import cleanup_inactive_instruments

        fixtures = instruments_with_mixed_bars
        result = await cleanup_inactive_instruments(
            db_session, stale_days=30, today=fixtures["today"],
        )

        # 2 个长期无数据的 active 股票被清理
        assert result["cleaned_count"] == 2, (
            f"应清理 2 个长期无数据的 active 股票，实际 {result['cleaned_count']}"
        )
        cleaned_symbols = set(result["cleaned_symbols"])
        assert cleaned_symbols == {"600000", "000001"}, (
            f"清理的股票应为 600000/000001，实际 {cleaned_symbols}"
        )

    @pytest.mark.asyncio
    async def test_cleanup_keeps_recent_active(
        self, db_session, instruments_with_mixed_bars,
    ):
        """近期有日线数据的 active 股票保持 active。"""
        from app.services.instrument_maintenance_service import cleanup_inactive_instruments

        fixtures = instruments_with_mixed_bars
        await cleanup_inactive_instruments(
            db_session, stale_days=30, today=fixtures["today"],
        )

        # 重新查询 active_recent，应保持 active
        await db_session.refresh(fixtures["active_recent"])
        assert fixtures["active_recent"].status == "active", (
            "近期有数据的股票应保持 active"
        )

    @pytest.mark.asyncio
    async def test_cleanup_does_not_touch_already_inactive(
        self, db_session, instruments_with_mixed_bars,
    ):
        """已 inactive 的股票不动。"""
        from app.services.instrument_maintenance_service import cleanup_inactive_instruments

        fixtures = instruments_with_mixed_bars
        result = await cleanup_inactive_instruments(
            db_session, stale_days=30, today=fixtures["today"],
        )

        await db_session.refresh(fixtures["inactive_stock"])
        assert fixtures["inactive_stock"].status == "inactive", (
            "已 inactive 的股票应保持 inactive 不动"
        )
        # inactive_stock 不应出现在 cleaned_symbols 中
        assert "300750" not in result["cleaned_symbols"], (
            "已 inactive 的股票不应被重复清理"
        )

    @pytest.mark.asyncio
    async def test_cleanup_preserves_index_symbols(
        self, db_session, instruments_with_mixed_bars,
    ):
        """指数类标的（SH000/SZ399）不清理，保留用于指数引用。"""
        from app.services.instrument_maintenance_service import cleanup_inactive_instruments

        fixtures = instruments_with_mixed_bars
        result = await cleanup_inactive_instruments(
            db_session, stale_days=30, today=fixtures["today"],
        )

        await db_session.refresh(fixtures["index_sh"])
        assert fixtures["index_sh"].status == "active", (
            "指数类标的（SH000xxx/SZ399xxx）应保留 active 不清理"
        )
        assert "SH000001" not in result["cleaned_symbols"], (
            "指数类标的不应出现在 cleaned_symbols 中"
        )

    @pytest.mark.asyncio
    async def test_cleanup_respects_stale_days_threshold(
        self, db_session, instruments_with_mixed_bars,
    ):
        """stale_days 阈值生效：stale_days=70 时所有 active 股票保留。"""
        from app.services.instrument_maintenance_service import cleanup_inactive_instruments

        fixtures = instruments_with_mixed_bars
        # stale_days=70：65 天前有数据的股票也保留
        result = await cleanup_inactive_instruments(
            db_session, stale_days=70, today=fixtures["today"],
        )

        assert result["cleaned_count"] == 0, (
            f"stale_days=70 时应清理 0 个（所有股票最近 70 天内都有数据），"
            f"实际 {result['cleaned_count']}"
        )

    @pytest.mark.asyncio
    async def test_cleanup_returns_summary_fields(
        self, db_session, instruments_with_mixed_bars,
    ):
        """返回汇总信息包含 cleaned_count / cleaned_symbols / remaining_active。"""
        from app.services.instrument_maintenance_service import cleanup_inactive_instruments

        fixtures = instruments_with_mixed_bars
        result = await cleanup_inactive_instruments(
            db_session, stale_days=30, today=fixtures["today"],
        )

        assert "cleaned_count" in result, "返回结果应包含 cleaned_count"
        assert "cleaned_symbols" in result, "返回结果应包含 cleaned_symbols"
        assert "remaining_active" in result, "返回结果应包含 remaining_active"
        assert isinstance(result["cleaned_symbols"], list)
        assert result["cleaned_count"] == len(result["cleaned_symbols"]), (
            "cleaned_count 应等于 cleaned_symbols 长度"
        )
        # 清理后剩余 active = active_recent + index_sh = 2
        assert result["remaining_active"] == 2, (
            f"清理后剩余 active 应为 2（active_recent + index_sh），"
            f"实际 {result['remaining_active']}"
        )

    @pytest.mark.asyncio
    async def test_cleanup_dry_run_does_not_modify(
        self, db_session, instruments_with_mixed_bars,
    ):
        """dry_run=True 时只返回预览结果，不修改数据库。"""
        from app.services.instrument_maintenance_service import cleanup_inactive_instruments

        fixtures = instruments_with_mixed_bars
        result = await cleanup_inactive_instruments(
            db_session, stale_days=30, today=fixtures["today"], dry_run=True,
        )

        # dry_run 应返回会清理的列表，但不实际修改
        assert result["cleaned_count"] == 2, (
            "dry_run 应预览会清理的 2 个股票"
        )
        # 验证数据库中 status 未变
        await db_session.refresh(fixtures["active_stale_30d"])
        assert fixtures["active_stale_30d"].status == "active", (
            "dry_run=True 时不应修改数据库"
        )


class TestIsStockSymbol:
    """is_stock_symbol 辅助函数测试 - 区分股票 vs 指数/基金/ETF。

    A 股股票代码规则（与 pytdx BestStock 类型='stock' 对齐）：
    - SH 6xxxxx: 上交所 A 股（含 688xxx 科创板）
    - SZ 000xxx: 深交所主板（如 000001 平安银行）
    - SZ 002xxx: 深交所中小板
    - SZ 300xxx: 深交所创业板
    - BJ 8xxxxx / 4xxxxx / 920xxx: 北交所

    排除（不算股票）：
    - SH 000xxx: 上交所指数（如 000001 上证指数，与 SZ 000001 平安银行代码冲突，需 market 区分）
    - SH 5xxxxx: 上交所基金/ETF
    - SH 880xxx: 申万行业指数
    - SH 999xxx: 其他指数
    - SZ 399xxx: 深证指数
    - SZ 159xxx: 深交所 ETF
    - SZ 395xxx: 深证基金
    """

    @pytest.mark.parametrize("symbol, market, expected", [
        # 上交所 A 股
        ("600000", "SH", True),   # 浦发银行
        ("600519", "SH", True),   # 贵州茅台
        ("688981", "SH", True),   # 中芯国际（科创板）
        # 深交所主板/中小板/创业板
        ("000001", "SZ", True),   # 平安银行
        ("000002", "SZ", True),   # 万科A
        ("002415", "SZ", True),   # 海康威视（中小板）
        ("300750", "SZ", True),   # 宁德时代（创业板）
        # [TDD-RED] 深交所主板新代码（001xxx/003xxx）— 当前规则漏掉
        ("001201", "SZ", True),   # 一彬科技（深圳主板新代码）
        ("003000", "SZ", True),   # 三和管桩（深圳主板新代码）
        # [TDD-RED] 深交所创业板新代码（301xxx/302xxx）— 当前规则漏掉
        ("301000", "SZ", True),   # *ST仕净（创业板新代码）
        ("302132", "SZ", True),   # 中航成飞（创业板新代码）
        # 北交所
        ("830799", "BJ", True),   # 北交所股票
        ("430139", "BJ", True),   # 北交所老股票
        ("920819", "BJ", True),   # 北交所新代码
        # === 以下是指数/基金/ETF，应返回 False ===
        # 上交所指数（SH 000xxx）
        ("000001", "SH", False),  # 上证指数（注意：与 SZ 000001 代码相同，靠 market 区分）
        ("000003", "SH", False),  # B股指数
        ("000016", "SH", False),  # 上证50
        ("000300", "SH", False),  # 沪深300
        # 上交所基金/ETF（SH 5xxxxx）
        ("510050", "SH", False),  # 上证50ETF
        ("510300", "SH", False),  # 沪深300ETF
        ("510500", "SH", False),  # 中证500ETF
        ("588000", "SH", False),  # 科创50ETF
        ("519001", "SH", False),  # 银华优势
        # 申万行业指数（SH 880xxx）
        ("801080", "SH", False),  # 电子(申万)
        ("880001", "SH", False),  # 万得全A
        # 其他指数
        ("999997", "SH", False),  # 其他
        # 深证指数（SZ 399xxx）
        ("399001", "SZ", False),  # 深证成指
        ("399006", "SZ", False),  # 创业板指
        ("399300", "SZ", False),  # 深证300
        # 深交所 ETF（SZ 159xxx）
        ("159901", "SZ", False),  # 深100ETF
        ("159915", "SZ", False),  # 创业板ETF
        # 深证基金（SZ 395xxx）
        ("395001", "SZ", False),  # 深证基金
        # [TDD-RED] 北证指数（BJ 899xxx）— 当前规则误匹配
        ("899050", "BJ", False),  # 北证50指数
        ("899601", "BJ", False),  # 北证专精特新指数
    ])
    def test_is_stock_symbol(self, symbol, market, expected):
        """is_stock_symbol 应正确区分股票与指数/基金/ETF。"""
        from app.services.instrument_maintenance_service import is_stock_symbol
        result = is_stock_symbol(symbol, market)
        assert result == expected, (
            f"is_stock_symbol({symbol!r}, {market!r}) 应为 {expected}，实际 {result}"
        )


class TestStockSymbolSqlFilter:
    """stock_symbol_sql_filter 辅助函数测试 - 生成 SQLAlchemy 过滤条件。

    用于 BarsSchedulerService 覆盖率分母与 _get_active_instruments 查询，
    在 SQL 层排除指数/基金/ETF，避免 Python 层过滤的性能开销。
    """

    @pytest.mark.asyncio
    async def test_sql_filter_only_counts_stocks(
        self, db_session,
    ):
        """SQL 过滤后只查到股票，不含指数/基金/ETF。"""
        from app.services.instrument_maintenance_service import stock_symbol_sql_filter
        from app.models.instrument import Instrument
        from sqlalchemy import select, func

        # 构造 3 个 active 标的：1 股票 + 1 指数 + 1 ETF
        stock = Instrument(id=uuid.uuid4(), symbol="600000", name="浦发银行", market="SH", status="active")
        index = Instrument(id=uuid.uuid4(), symbol="000001", name="上证指数", market="SH", status="active")
        etf = Instrument(id=uuid.uuid4(), symbol="510050", name="上证50ETF", market="SH", status="active")
        db_session.add_all([stock, index, etf])
        await db_session.flush()

        # 不加过滤：3 个
        all_result = await db_session.execute(
            select(func.count(Instrument.id)).where(Instrument.status == "active")
        )
        assert all_result.scalar() == 3, "不加过滤应有 3 个 active"

        # 加过滤：只 1 个股票
        filtered_result = await db_session.execute(
            select(func.count(Instrument.id))
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
        )
        assert filtered_result.scalar() == 1, (
            "加 stock_symbol_sql_filter 后应只查到 1 个股票（不含指数/ETF）"
        )

    @pytest.mark.asyncio
    async def test_sql_filter_covers_all_markets(
        self, db_session,
    ):
        """SQL 过滤覆盖 SH/SZ/BJ 三个市场的股票代码模式。"""
        from app.services.instrument_maintenance_service import stock_symbol_sql_filter
        from app.models.instrument import Instrument
        from sqlalchemy import select, func

        stocks = [
            # SH 6xxxxx
            Instrument(id=uuid.uuid4(), symbol="600519", name="贵州茅台", market="SH", status="active"),
            Instrument(id=uuid.uuid4(), symbol="688981", name="中芯国际", market="SH", status="active"),
            # SZ 000xxx / 002xxx / 300xxx
            Instrument(id=uuid.uuid4(), symbol="000001", name="平安银行", market="SZ", status="active"),
            Instrument(id=uuid.uuid4(), symbol="002415", name="海康威视", market="SZ", status="active"),
            Instrument(id=uuid.uuid4(), symbol="300750", name="宁德时代", market="SZ", status="active"),
            # BJ 8xxxxx / 4xxxxx / 920xxx
            Instrument(id=uuid.uuid4(), symbol="830799", name="北交所股票", market="BJ", status="active"),
            Instrument(id=uuid.uuid4(), symbol="430139", name="北交所老股", market="BJ", status="active"),
            Instrument(id=uuid.uuid4(), symbol="920819", name="北交所新代码", market="BJ", status="active"),
            # 排除：指数/ETF
            Instrument(id=uuid.uuid4(), symbol="SH000001", name="上证综指", market="SH", status="active"),
            Instrument(id=uuid.uuid4(), symbol="399001", name="深证成指", market="SZ", status="active"),
            Instrument(id=uuid.uuid4(), symbol="510050", name="上证50ETF", market="SH", status="active"),
        ]
        db_session.add_all(stocks)
        await db_session.flush()

        result = await db_session.execute(
            select(func.count(Instrument.id))
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
        )
        # 8 个股票（SH 2 + SZ 3 + BJ 3），排除 3 个指数/ETF
        assert result.scalar() == 8, (
            "stock_symbol_sql_filter 应查到 8 个股票（SH 2 + SZ 3 + BJ 3），"
            "排除 3 个指数/ETF"
        )
