"""第一金字塔统一快照 Schema（Phase 5B-1 + Gate1 收口）。

PRD20 QM-01 要求个股状态按固定顺序组织：
    趋势 > 结构 > 动量 > 筹码共识

本模块定义唯一权威 DTO `FirstPyramidSnapshot`，单股详情、批量计算、行情列表
和盘后计算层必须复用此结构，不得各自重新拼字段。

约束：
- `ordered_dimensions` 固定为 `["trend", "structure", "momentum", "chip_consensus"]`
- 前三维（trend/structure/momentum）必选；任一缺失必须抛 `ValueError`，不得静默伪造
- `chip_consensus` 允许 `None`（PRD20 QM-40 可选定位）
- 每个事件必须含 `type`、`freshness_bars`；其余字段视事件类型可空
- Gate1：统一 VolumeContext 被趋势/结构/动量复用，禁止各维度重复计算

用法：
    from app.schemas.first_pyramid import FirstPyramidSnapshot
    from app.services.first_pyramid_service import compute_first_pyramid_snapshot

    snapshot = compute_first_pyramid_snapshot(bars, symbol="000001.SZ")
    payload = snapshot.to_dict()

模块自测：
    python -m app.schemas.first_pyramid
"""

from __future__ import annotations

# ruff: noqa: N815 - camelCase 字段为前端 JSON API 契约（tradeDate/inputHash 等）
from typing import Any

from pydantic import BaseModel, Field, model_validator

# 固定维度顺序（PRD20 QM-01；禁止页面或前端动态组合）
ORDERED_DIMENSIONS: tuple[str, ...] = ("trend", "structure", "momentum", "chip_consensus")

# 必选维度（前三维）
REQUIRED_DIMENSIONS: tuple[str, ...] = ("trend", "structure", "momentum")

# 可选维度
OPTIONAL_DIMENSIONS: tuple[str, ...] = ("chip_consensus",)

# 算法版本（每次算法或契约变更时递增）
FIRST_PYRAMID_ALGORITHM_VERSION = "1.1.0-gate1-volume-context"


class VolumeContextSchema(BaseModel):
    """统一量能上下文（Gate1：计算一次，趋势/结构/动量复用）。

    窗口样本不足时各指标为 None，readiness=False。
    """

    volume: float | None = Field(None, description="当日成交量")
    amount: float | None = Field(None, description="当日成交额")
    turnoverRate: float | None = Field(None, description="当日换手率")
    volumeMa20: float | None = Field(None, description="20日平均成交量")
    volumeMa200: float | None = Field(None, description="200日平均成交量")
    volumeRatio20: float | None = Field(None, description="当前量 / 20日均量")
    volumeRatio200: float | None = Field(None, description="当前量 / 200日均量")
    volumePercentile20: float | None = Field(None, description="20日百分位（0-100）")
    volumePercentile200: float | None = Field(None, description="200日百分位（0-100）")
    volumeZscore20: float | None = Field(None, description="20日 z-score")
    volumeZscore200: float | None = Field(None, description="200日 z-score")
    readiness: bool = Field(False, description="True=数据充分；False=窗口不足")
    badge: str | None = Field(None, description="量能徽标：放量/缩量/正常/未知")


class PyramidEvent(BaseModel):
    """金字塔事件（离散、时序、含新鲜度）。

    PRD20 QM-60：连续因子与事件分离；事件必须含发生时间和新鲜度。
    Gate1：事件可携带事件发生 bar 的 VolumeContext。
    """

    type: str = Field(..., description="事件类型，如 BOS / CHoCH / OB_ENTRY / SQZ_OFF / NODE_CROSS_UP")
    direction: str | None = Field(
        None, description="方向：'up' / 'down' / None（部分事件无方向）"
    )
    occurredAt: str | None = Field(
        None, description="事件发生日期（ISO YYYY-MM-DD）；intrabar 事件可空"
    )
    barIndex: int | None = Field(
        None, description="事件在计算序列中的位置索引（0-based）；与 occurredAt 至少一个非空"
    )
    price: float | None = Field(None, description="事件触发价格（可空）")
    freshnessBars: int = Field(
        ..., ge=0, description="事件新鲜度：从事件发生到当前计算 bar 的 bar 数（>=0）"
    )
    volumeContext: VolumeContextSchema | None = Field(
        None, description="事件发生 bar 的量能上下文（Gate1）"
    )
    volumeBadge: str | None = Field(
        None, description="事件量能徽标：放量/缩量/正常/未知（Gate1）"
    )
    extra: dict[str, Any] | None = Field(
        None, description="事件附加字段（如 relativeVolume / boundary 等）"
    )

    @model_validator(mode="after")
    def _check_time_or_index(self) -> PyramidEvent:
        if self.occurredAt is None and self.barIndex is None:
            raise ValueError(
                f"PyramidEvent(type={self.type}) 必须至少包含 occurredAt 或 barIndex"
            )
        return self


class DimensionResult(BaseModel):
    """单维度结果（必选维度 available 必须为 True）。

    PRD20 QM-60：每维包含连续因子、离散事件、状态文本和证据。
    Gate1：每维携带当前 bar 的 VolumeContext（共享计算结果）。
    """

    name: str = Field(..., description="维度名：trend / structure / momentum / chip_consensus")
    available: bool = Field(..., description="该维度是否有数据；必选维度必须为 True")
    continuousFactors: dict[str, Any] = Field(
        default_factory=dict, description="连续因子与特征（标量指标）"
    )
    events: list[PyramidEvent] = Field(
        default_factory=list, description="离散事件列表（按时间升序）"
    )
    statusText: str = Field(..., description="中文状态描述（由结构化结果生成）")
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="证据（如段起止、锚点、参数引用等）"
    )
    volumeContext: VolumeContextSchema | None = Field(
        None, description="当前 bar 的量能上下文（Gate1，共享计算结果）"
    )


class FirstPyramidSnapshot(BaseModel):
    """第一金字塔统一快照（PRD20 QM-01~QM-43、QM-60~QM-62）。

    单股详情、批量计算、行情列表 payload 和盘后 compute 调用必须复用此结构。
    """

    symbol: str = Field(..., description="股票代码")
    tradeDate: str = Field(..., description="交易日（ISO YYYY-MM-DD）")
    orderedDimensions: list[str] = Field(
        default_factory=lambda: list(ORDERED_DIMENSIONS),
        description="固定维度顺序，禁止页面动态组合",
    )
    trend: DimensionResult = Field(..., description="趋势维度（必选）")
    structure: DimensionResult = Field(..., description="结构维度（必选）")
    momentum: DimensionResult = Field(..., description="动量维度（必选）")
    chipConsensus: DimensionResult | None = Field(
        None, description="筹码共识维度（可选；无有效峰时为 None）"
    )
    statusText: str = Field(..., description="金字塔聚合中文状态描述")
    volumeContext: VolumeContextSchema | None = Field(
        None, description="共享量能上下文（Gate1，前端量能水位条数据源）"
    )
    inputHash: str = Field(..., description="OHLCV 输入 hash（用于跨入口一致性校验）")
    parameterHash: str = Field(..., description="参数 hash（含算法版本与固定参数）")
    algorithmVersion: str = Field(
        default=FIRST_PYRAMID_ALGORITHM_VERSION, description="算法版本"
    )

    @model_validator(mode="after")
    def _check_required_dimensions(self) -> FirstPyramidSnapshot:
        """前三维必选，任一 available=False 必须明确错误（不得静默伪造）。

        PRD20 QM-02：趋势、结构、动量为必选维度。
        """
        for name, dim in (
            ("trend", self.trend),
            ("structure", self.structure),
            ("momentum", self.momentum),
        ):
            if not dim.available:
                raise ValueError(
                    f"必选维度 {name} 不可缺失（available=False）；"
                    f"调用方必须传入足够数据，不得静默伪造"
                )
        if list(self.orderedDimensions) != list(ORDERED_DIMENSIONS):
            raise ValueError(
                f"orderedDimensions 必须为 {list(ORDERED_DIMENSIONS)}，"
                f"实际：{self.orderedDimensions}"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为前端 JSON 友好字典（camelCase 字段）。"""
        return self.model_dump(by_alias=False)


# 模块自测：构造最小合法 snapshot
if __name__ == "__main__":
    evt = PyramidEvent(
        type="BOS",
        direction="up",
        occurredAt="2026-07-25",
        barIndex=100,
        price=10.5,
        freshnessBars=2,
    )
    trend = DimensionResult(
        name="trend",
        available=True,
        continuousFactors={"regime_value": 1, "dsa_dir_bars": 60},
        events=[],
        statusText="DSA 趋势上行，连续 60 根",
        evidence={"anchor": "2026-04-01"},
    )
    structure = DimensionResult(
        name="structure",
        available=True,
        continuousFactors={"swing_bias": 1},
        events=[evt],
        statusText="BOS 上行突破，新鲜度 2 根",
    )
    momentum = DimensionResult(
        name="momentum",
        available=True,
        continuousFactors={"squeeze_on": False, "bb_width": 0.12},
        events=[],
        statusText="动量扩张，BB 带宽 0.12",
    )
    snap = FirstPyramidSnapshot(
        symbol="000001.SZ",
        tradeDate="2026-07-25",
        trend=trend,
        structure=structure,
        momentum=momentum,
        chipConsensus=None,
        statusText="趋势上行 + 结构 BOS + 动量扩张",
        inputHash="sha256:dummy",
        parameterHash="sha256:dummy",
    )
    print(f"OK: {snap.symbol} {snap.tradeDate} algo={snap.algorithmVersion}")
    print(f"Ordered: {snap.orderedDimensions}")
    print(f"Status: {snap.statusText}")
