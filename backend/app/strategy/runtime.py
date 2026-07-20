"""策略运行时抽象基类 - V1.1 策略扩展规范核心接口。

提供：
- MarketDataContext: 市场数据上下文（传递给策略的输入）
- StrategyResult: selector 策略执行结果（含 matched 语义和 metrics 指标）
- MonitorState: monitor 策略当前状态（含 state dict 与版本）
- StrategyEventDraft: monitor 策略检测到的原始事件草稿
- StrategyRuntime: ABC，所有策略运行时的基类（selector/monitor 两种 kind）
- StrategyLoader: 根据策略版本加载对应 runtime

设计说明：
- kind="selector": 按交易日/批次输出每只股票的指标和状态（execute 方法）
- kind="monitor": 按 Bar/事件输出当前状态和原始事件（calculate_state + detect_events 方法）
- 策略不负责用户过滤、组合、冷却、消息模板或飞书发送
- StrategyResult.metrics 必须包含 manifest.outputs 中声明的所有指标
- MonitorState.state 必须包含 manifest.outputs 中声明的所有状态字段
- monitor 方法在基类中默认抛出 NotImplementedError，selector 子类无需实现

参考文档：05_STRATEGY_EXTENSION_SPEC.md
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

import pandas as pd

from app.constants.strategy_keys import DSA_SELECTOR, WATCHLIST_MONITOR
from app.models.strategy import StrategyVersion


@dataclass
class MarketDataContext:
    """市场数据上下文 - 传递给策略运行时的输入。

    包含单只标的的市场数据，策略运行时根据这些数据计算指标和状态。

    [CHANGE-20260720-001 bars_display/bars_daily 分离]
    bars_display 与 bars_daily 必须严格分离，禁止用显示周期回退日线输入：
    - bars_display: 当前页面显示周期 bars（1d/15m/1h/1w/1mo），供 DSA/MACD/SQZMOM
      等当前周期图层使用。切换周期时此字段变化。
    - bars_daily: 真正日线 bars（completed qfq），供 Node/BB/SMC 日线结构算法使用。
      不随页面周期变化，始终为完整日线。
    - bars_15min: Node 成交量分配辅助数据（4000 根 completed qfq 15m）。
    - bars_minute: 盘中事件触发数据（1m），详情页不加载。

    Attributes:
        instrument_id: 标的 UUID（对应 instruments 表主键）
        symbol: 股票代码（如 '600519'，便于日志和调试）
        bars_daily: 真正日线行情 DataFrame（completed qfq），index=DatetimeIndex，
                    columns 含 open/high/low/close/volume/amount/adj_factor；
                    Node/BB/SMC 日线结构只能使用此字段，禁止用 bars_display 回退。
        bars_display: 当前显示周期 bars DataFrame（可选），供 DSA/MACD/SQZMOM
                      等当前周期图层使用；None 时回退到 bars_daily。
        display_timeframe: 当前显示周期（"1d"|"15m"|"1h"|"1w"|"1mo"），与 bars_display 对应。
        bars_minute: 分钟线行情 DataFrame（可选，monitor 策略使用）
        bars_15min: 15 分钟线行情 DataFrame（可选，Volume Node Monitor
                    的 node_ltf=15m 低周期数据，供 compute_indicators 使用）
        adj_factor: 复权因子 DataFrame，columns=[trade_date, adj_factor]
        trade_date: 交易日（策略计算的截止日期）
    """

    instrument_id: UUID
    symbol: str
    bars_daily: pd.DataFrame
    bars_display: pd.DataFrame | None = None
    display_timeframe: str | None = None
    bars_minute: pd.DataFrame | None = None
    bars_15min: pd.DataFrame | None = None
    adj_factor: pd.DataFrame | None = None
    trade_date: date | None = None
    bar_time: datetime | None = None


@dataclass
class StrategyResult:
    """策略执行结果 - 单只标的在一个交易日的计算输出。

    Attributes:
        instrument_id: 标的 UUID
        strategy_version_id: 策略版本 UUID
        trade_date: 交易日
        matched: 是否命中（selector 语义：是否满足选股条件）
        metrics: 指标字典（key=指标名, value=数值/字符串/布尔）
                 必须包含 manifest.outputs 中声明的所有指标
        calculation_id: 计算批次 ID（用于追溯同一次运行中的多个结果）
    """

    instrument_id: UUID
    strategy_version_id: UUID
    trade_date: date
    matched: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    calculation_id: str | None = None


@dataclass
class MonitorState:
    """monitor 策略当前状态 - 单只标的在一个 bar 的状态快照。

    对应 monitor_states 表的 payload + 元数据。state 字典存储策略自定义状态，
    必须包含 manifest.outputs 中声明的所有字段（如 current_price/upper_node 等）。

    Attributes:
        instrument_id: 标的 UUID
        strategy_version_id: 策略版本 UUID
        state: 状态字典（JSON 可序列化），含 manifest.outputs 声明的字段
        state_version: 状态 schema 版本（用于状态结构演进，对应 state_schema_version）
        updated_at: 状态更新时间（bar_time）
        calculation_id: 计算批次 ID（幂等标识）
    """

    instrument_id: UUID
    strategy_version_id: UUID
    state: dict[str, Any] = field(default_factory=dict)
    state_version: int = 1
    updated_at: datetime | None = None
    calculation_id: str | None = None


@dataclass
class StrategyEventDraft:
    """monitor 策略检测到的原始事件草稿。

    事件为事实型、可重复回放，不依赖用户状态（V1.1 组合友好要求）。
    由 detect_events 产出，经去重后写入 strategy_events 表。

    Attributes:
        event_type: 事件类型（如 node_cluster_touch，对应 manifest.event_types[].key）
        event_time: 事件发生时间（bar 时间，非消费时间）
        dedupe_key: 去重键（对应 manifest.event_types[].dedupe，如 touch_episode）
        logical_entity: 逻辑实体标识（如 "{instrument_id}:{node_price}"）
        payload: 事件负载（自包含，不依赖外部状态）
        state_ttl_seconds: 状态有效期秒数（对应 manifest.event_types[].state_ttl_seconds）
    """

    event_type: str
    event_time: datetime
    dedupe_key: str
    logical_entity: str
    payload: dict[str, Any] = field(default_factory=dict)
    state_ttl_seconds: int = 120


class StrategyRuntime(ABC):
    """策略运行时抽象基类。

    所有策略运行时必须继承此类并实现 initialize 和 execute 方法。
    子类通过 kind 类属性声明策略类型（selector/monitor）。

    生命周期：
    1. StrategyLoader.load(version) 创建 runtime 实例
    2. runtime.initialize(version) 加载策略版本配置
    3. runtime.execute(context) 对每个标的执行策略，返回 StrategyResult
    """

    kind: str = "selector"  # 子类覆盖：selector / monitor

    @abstractmethod
    async def initialize(self, version: StrategyVersion) -> None:
        """加载策略版本配置。

        从 StrategyVersion.manifest 中提取参数、输入要求等配置，
        初始化策略运行时状态。

        Args:
            version: 策略版本 ORM 对象
        """
        raise NotImplementedError

    @abstractmethod
    async def execute(self, context: MarketDataContext) -> StrategyResult | None:
        """执行 selector 策略计算。

        根据 MarketDataContext 中的市场数据计算指标和状态，
        返回标准 StrategyResult。

        Args:
            context: 市场数据上下文

        Returns:
            策略执行结果
        """
        raise NotImplementedError

    async def compute_indicators(self, context: MarketDataContext) -> dict[str, Any]:
        """计算图表指标（供个股详情页面使用）。

        默认实现：调用 execute() 并返回 result.metrics。
        子类可覆盖以提供更轻量的计算（只计算图表需要的指标）。

        Returns:
            指标字典，key=指标名, value=数值或数值列表
        """
        result = await self.execute(context)
        assert result is not None
        return result.metrics

    async def calculate_state(self, context: MarketDataContext) -> MonitorState:
        """计算 monitor 策略当前状态（monitor kind 必须实现）。

        根据 MarketDataContext 中的市场数据（1m bars）计算当前状态，
        返回 MonitorState。state 字典必须包含 manifest.outputs 声明的所有字段。

        selector kind 无需实现（默认抛出 NotImplementedError）。

        Args:
            context: 市场数据上下文（bars_minute 含 1m bars）

        Returns:
            当前 bar 的监控状态
        """
        raise NotImplementedError(
            f"{type(self).__name__} 未实现 calculate_state（非 monitor 策略）"
        )

    async def detect_events(
        self,
        context: MarketDataContext,
        prev_state: MonitorState | None,
        curr_state: MonitorState,
    ) -> list[StrategyEventDraft]:
        """检测 monitor 策略事件（monitor kind 必须实现）。

        对比前一状态与当前状态，检测事件（如 node_cluster_touch）。
        事件为事实型、可重复回放，dedupe_key 保证同一 episode 只触发一次。

        selector kind 无需实现（默认抛出 NotImplementedError）。

        Args:
            context: 市场数据上下文
            prev_state: 前一状态（首个 bar 时为 None）
            curr_state: 当前状态

        Returns:
            检测到的事件草稿列表（可能为空）
        """
        raise NotImplementedError(
            f"{type(self).__name__} 未实现 detect_events（非 monitor 策略）"
        )


class StrategyLoader:
    """策略加载器 - 根据策略版本加载对应的 StrategyRuntime。

    通过 strategy_key 查找注册表中的 entrypoint（module:ClassName），
    动态导入并实例化策略运行时。

    注册表说明：
    - key: strategy_id（manifest 中的 strategy_id 字段）
    - value: entrypoint（module_path:ClassName）
    - 新增策略时在此注册表中添加映射
    """

    # 策略注册表：strategy_id -> entrypoint（module:ClassName）
    _registry: dict[str, str] = {
        DSA_SELECTOR: "app.strategy.selectors.dsa_selector:DSASelector",
        "volume_node_monitor": "app.strategy.monitors.volume_node_monitor:VolumeNodeMonitor",
        "bb_monitor": "app.strategy.monitors.bollinger_monitor:BollingerMonitor",
        WATCHLIST_MONITOR: "app.strategy.monitors.watchlist_monitor:WatchlistMonitor",
    }

    @classmethod
    def register(cls, strategy_id: str, entrypoint: str) -> None:
        """注册策略运行时。

        Args:
            strategy_id: 策略唯一标识（manifest.strategy_id）
            entrypoint: 模块路径与类名（格式：module:ClassName）
        """
        cls._registry[strategy_id] = entrypoint

    @classmethod
    async def load(cls, version: StrategyVersion) -> StrategyRuntime:
        """根据策略版本加载对应的 StrategyRuntime。

        流程：
        1. 从 manifest 中提取 strategy_id
        2. 查找注册表中的 entrypoint
        3. 动态导入模块并实例化策略运行时
        4. 调用 initialize 加载版本配置

        Args:
            version: 策略版本 ORM 对象

        Returns:
            已初始化的 StrategyRuntime 实例

        Raises:
            ValueError: 策略未注册或 entrypoint 格式错误
            ImportError: 模块导入失败
            AttributeError: 类不存在
        """
        manifest = version.manifest
        strategy_id = manifest.get("strategy_id")
        if strategy_id is None:
            raise ValueError(
                f"manifest 缺少 strategy_id 字段, version_id={version.id}"
            )

        entrypoint = cls._registry.get(strategy_id)
        if entrypoint is None:
            raise ValueError(
                f"策略未注册: strategy_id={strategy_id}, "
                f"已注册={list(cls._registry.keys())}"
            )

        if ":" not in entrypoint:
            raise ValueError(
                f"entrypoint 格式错误（应为 module:ClassName）: {entrypoint}"
            )

        module_path, class_name = entrypoint.split(":", 1)
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise ImportError(
                f"策略模块导入失败: module={module_path}, strategy_id={strategy_id}"
            ) from exc

        runtime_class = getattr(module, class_name, None)
        if runtime_class is None:
            raise AttributeError(
                f"策略类不存在: module={module_path}, class={class_name}"
            )

        runtime = runtime_class()
        await runtime.initialize(version)
        return runtime


if __name__ == "__main__":
    # 自测入口：验证 ABC 和 dataclass 定义（无副作用）
    print(f"MarketDataContext fields={[f.name for f in MarketDataContext.__dataclass_fields__.values()]}")
    print(f"StrategyResult fields={[f.name for f in StrategyResult.__dataclass_fields__.values()]}")
    print(f"MonitorState fields={[f.name for f in MonitorState.__dataclass_fields__.values()]}")
    print(f"StrategyEventDraft fields={[f.name for f in StrategyEventDraft.__dataclass_fields__.values()]}")
    print(f"StrategyRuntime.kind={StrategyRuntime.kind}")
    print(f"StrategyLoader._registry={StrategyLoader._registry}")

    # 验证 ABC 不可直接实例化
    try:
        StrategyRuntime()  # type: ignore[abstract]
        raise AssertionError("StrategyRuntime 应为 ABC，不可实例化")
    except TypeError:
        print("StrategyRuntime ABC 不可实例化 ✓")

    # 验证注册表
    assert DSA_SELECTOR in StrategyLoader._registry
    assert "volume_node_monitor" in StrategyLoader._registry
    print(f"{DSA_SELECTOR} + volume_node_monitor 已注册 ✓")

    # 验证 MarketDataContext.bars_15min 字段存在
    assert "bars_15min" in MarketDataContext.__dataclass_fields__
    print("MarketDataContext.bars_15min 字段存在 ✓")

    # 验证 compute_indicators 方法存在（具体方法，非 abstractmethod）
    assert hasattr(StrategyRuntime, "compute_indicators")
    assert not getattr(StrategyRuntime.compute_indicators, "__isabstractmethod__", False)
    print("StrategyRuntime.compute_indicators 方法存在且非抽象 ✓")
    print("OK")
