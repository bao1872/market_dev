"""Node Cluster 唯一业务入口（计算内核）— 三链同核 SSOT。

本模块是 Node Cluster 的唯一业务入口。所有业务模块（盘后 feature_snapshot、
详情 indicator/VolumeNodeMonitor、盘中 monitor_batch_service）必须通过本 engine
计算 Volume Profile / Peak Node / 状态派生 / 穿越检测，禁止直接调用底层
`compute_unified_volume_profile` / `compute_volume_profile` / `_detect_nodes` /
`extract_nearest_nodes`。

合同三层（缺一不可）：
- `app.constants.indicator_contract`: 数值参数真源（根数/行数/阈值/TTL）
- `app.contracts.indicator_semantics`: 语义合同（输入口径/过滤规则/输出口径/指纹）
- `app.services.node_cluster_engine`: 计算内核（本文件，唯一业务入口）

不可变结果（frozen dataclass）：
- `NodeClusterProfileResult`: Profile 计算结果（100 行 profile + 全部 peak + POC/VAH/VAL + 诊断）
- `NodeClusterPriceState`: 价格对应状态（upper/lower node + position + zone）

核心函数：
- `compute_node_cluster_profile(...)`: 唯一 Profile 计算入口
- `derive_state_for_price(profile, price)`: 从 Profile 派生状态（不重算 Profile）
- `detect_crossover_signals(profile, prev_close, cur_close)`: 1m 穿越检测
- `build_engine_cache_key(...)`: engine 缓存键（含 algorithm_version + fingerprint + as_of + hash）
- `profile_to_dict(profile)`: 序列化（供 snapshot 落库）

缓存策略：
- engine 自身不缓存（保持纯函数语义）
- 调用方（monitor_batch_service）按 `build_engine_cache_key` 缓存 `NodeClusterProfileResult`
- 缓存键含 algorithm_version + contract_fingerprint + as_of + daily_hash + 15m_hash
- 合同/参数变化自动失效（指纹/hash 变化使键不同）

用法：
    from app.services.node_cluster_engine import (
        compute_node_cluster_profile,
        derive_state_for_price,
        detect_crossover_signals,
        NodeClusterProfileResult,
        NodeClusterPriceState,
    )
    profile = compute_node_cluster_profile(daily_bars, bars_15m, adjustment_as_of="2026-07-18")
    state = derive_state_for_price(profile, current_price)
    signals = detect_crossover_signals(profile, prev_close, cur_close)

模块自测：
    python -m app.services.node_cluster_engine
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.contracts.indicator_semantics import (
    NODE_CLUSTER_ADJUSTMENT_MODE,
    NODE_CLUSTER_ALGORITHM_VERSION,
    NODE_CLUSTER_COMPLETED_ONLY,
    NODE_CLUSTER_CONTRACT_FINGERPRINT,
    NODE_CLUSTER_OUTPUT_SCHEMA_VERSION,
)
from app.constants.indicator_contract import VP_ROWS
# [CHANGE-20260718-004 Node Cluster engine] 只有本 engine 可导入底层 VP 模块。
# 业务模块禁止直接 from ...unified_volume_profile import compute_unified_volume_profile。
# 架构守护测试 test_node_cluster_architecture.py 强制此约束。
from app.strategy_assets.algorithms.features.unified_volume_profile import (
    NodeClusterBarsResult,
    UnifiedVolumeProfileResult,
    compute_unified_volume_profile,
    prepare_node_cluster_bars,
)

logger = logging.getLogger(__name__)

# 事件类型（与 volume_node_monitor 保持一致，禁止散落硬编码）
EVENT_TYPE_NODE_CLUSTER_TOUCH = "node_cluster_touch"


# =============================================================================
# 不可变结果数据类
# =============================================================================


@dataclass(frozen=True)
class NodeClusterProfileResult:
    """Node Cluster Profile 计算结果（不可变）。

    所有字段在构造时确定，禁止运行时修改。`profile_hash` 用于三链一致性断言：
    同 stock/as_of/输入 → profile_hash 必须完全一致。

    Attributes:
        algorithm_version: 算法版本（来自 indicator_semantics）
        output_schema_version: 输出 schema 版本
        contract_fingerprint: 合同指纹（语义变更时 bump，自动失效缓存）
        profile_rows: 完整 100 行 VP 价格档位快照（含 is_peak/is_poc/is_value_area）
        peak_rows: 全部 Peak 节点快照（含 VA 外 Peak，禁止过滤）
        all_peak_prices: 全部 Peak 价格列表（含 VA 外）
        poc_price: POC 价格（float 或 None）
        vah_price: 价值区域高位价格
        val_price: 价值区域低位价格
        price_step: VP 价格档位步长
        lowest_price: VP 价格范围最低价
        highest_price: VP 价格范围最高价
        daily_source_hash: 250 根日线内容 hash
        bars_15m_source_hash: 4000 根 15m 内容 hash
        adj_factor_hash: 复权因子 hash（可选，盘后链传入）
        adjustment_as_of: 复权锚点业务日（ISO 字符串，可选）
        daily_bars_count: 实际日线根数（诊断）
        bars_15m_count: 实际 15m 根数（诊断）
        profile_hash: 100 行 profile 内容 hash（三链一致性断言用）
    """

    algorithm_version: str
    output_schema_version: int
    contract_fingerprint: str
    profile_rows: list[dict[str, Any]]
    peak_rows: list[dict[str, Any]]
    all_peak_prices: list[float]
    poc_price: float | None
    vah_price: float | None
    val_price: float | None
    price_step: float | None
    lowest_price: float | None
    highest_price: float | None
    daily_source_hash: str
    bars_15m_source_hash: str
    adj_factor_hash: str | None
    adjustment_as_of: str | None
    daily_bars_count: int
    bars_15m_count: int
    profile_hash: str
    # 私有：底层 VP 结果（供 derive_state_for_price / detect_crossover_signals 复用，禁止业务模块访问）
    _vp_result: UnifiedVolumeProfileResult = field(default=None, repr=False, compare=False)

    # [CHANGE-20260718-004 Section 2.6] 鸭子类型适配器：让 monitor_chart_renderer.render_monitoring_chart
    # 可直接消费 NodeClusterProfileResult（按 profile.profile_df/.peak_df/.price_step 鸭子类型访问，
    # 见 monitor_chart_renderer.py:107-108）。renderer 零改动；架构守护测试仍约束"只有 engine 导入底层 VP"。
    @property
    def profile_df(self) -> pd.DataFrame:
        """完整 profile 行 DataFrame（鸭子类型兼容 UnifiedVolumeProfileResult.profile_df）。

        供 monitor_chart_renderer 等历史调用方按鸭子类型访问，避免在共享模块外重建 DataFrame。
        """
        if self._vp_result is None:
            return pd.DataFrame()
        return self._vp_result.profile_df

    @property
    def peak_df(self) -> pd.DataFrame | None:
        """Peak Node DataFrame（鸭子类型兼容 UnifiedVolumeProfileResult.peak_df）。

        供 monitor_chart_renderer.render_monitoring_chart 等历史调用方按鸭子类型访问。
        """
        if self._vp_result is None:
            return None
        return self._vp_result.peak_df


@dataclass(frozen=True)
class NodeClusterPriceState:
    """Node Cluster 价格对应状态（不可变）。

    由 `derive_state_for_price` 从 Profile 派生，不重算 Profile。

    Attributes:
        current_price: 当前价格
        upper_node: 上方最近 Peak Node（dict 或 None）
        lower_node: 下方最近 Peak Node（dict 或 None）
        position_0_1: VP 全区间相对位置 [0, 1]
        poc_node: POC 节点（dict 或 None）
        last_touched_node: 触碰节点（dict 或 None）
    """

    current_price: float
    upper_node: dict[str, float] | None
    lower_node: dict[str, float] | None
    position_0_1: float | None
    poc_node: dict[str, float] | None
    last_touched_node: dict[str, float] | None

    def to_dict(self) -> dict[str, Any]:
        """转换为 dict（与 volume_node_monitor.yaml outputs 对齐）。"""
        return {
            "current_price": self.current_price,
            "upper_node": self.upper_node,
            "lower_node": self.lower_node,
            "position_0_1": self.position_0_1,
            "poc_price": self.poc_node,
            "last_touched_node": self.last_touched_node,
        }


# =============================================================================
# 内部辅助函数
# =============================================================================


def _compute_dataframe_hash(df: pd.DataFrame) -> str:
    """计算 DataFrame 内容 hash（基于 index + OHLCV 列）。

    用于 source_hash 诊断字段和缓存键。同输入 → 同 hash（确定性）。

    Args:
        df: OHLCV DataFrame（DatetimeIndex）

    Returns:
        16 字符 hex hash（SHA256 前 16 字符）
    """
    if df is None or df.empty:
        return "empty"
    try:
        # 取 index + OHLCV 列，转 string 后 hash
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        if not cols:
            return "no_ohlcv"
        # index 转 ISO 字符串 + 数值列转 string
        index_str = pd.Series(df.index.astype(str)).str.cat(sep=",")
        values_str = df[cols].astype(str).agg(",".join, axis=1).str.cat(sep="|")
        content = f"{index_str}#{values_str}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:
        logger.warning("DataFrame hash 计算失败: %s", exc)
        return "hash_error"


def _finite_or_none(v: float) -> float | None:
    """NaN/Inf 转 None，保证 JSON 可序列化。"""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _profile_rows_to_list(profile_df: pd.DataFrame | None) -> list[dict[str, Any]]:
    """将 profile_df 转换为 list[dict]（含 is_peak/is_poc/is_value_area）。"""
    if profile_df is None or profile_df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in profile_df.iterrows():
        rows.append({
            "price_low": round(float(row["price_low"]), 4),
            "price_high": round(float(row["price_high"]), 4),
            "price_mid": round(float(row["price_mid"]), 4),
            "bullish_volume": float(row["bullish_volume"]),
            "bearish_volume": float(row["bearish_volume"]),
            "total_volume": float(row["total_volume"]),
            "is_peak": bool(row["is_peak"]),
            "is_poc": bool(row["is_poc"]),
            "is_value_area": bool(row["is_value_area"]),
        })
    return rows


def _peak_rows_to_list(peak_df: pd.DataFrame | None) -> list[dict[str, Any]]:
    """将 peak_df 转换为 list[dict]（含 VA 外 Peak，禁止过滤）。"""
    if peak_df is None or peak_df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in peak_df.iterrows():
        rows.append({
            "price_mid": round(float(row["price_mid"]), 4),
            "bullish_volume": float(row["bullish_volume"]),
            "bearish_volume": float(row["bearish_volume"]),
            "total_volume": float(row["total_volume"]),
            "is_peak": bool(row["is_peak"]),
        })
    return rows


def _compute_profile_hash(profile_rows: list[dict[str, Any]]) -> str:
    """计算 100 行 profile 内容 hash（三链一致性断言用）。

    同 stock/as_of/输入 → profile_hash 必须完全一致。
    """
    if not profile_rows:
        return "empty"
    # 稳定序列化：按 key 排序
    # 注意：必须用列表推导 [...] 而非生成器 (...)，否则 repr(generator) 含内存地址
    # 导致 profile_hash 非确定性（三链一致性断言失败）。
    content = repr([tuple(sorted(r.items())) for r in profile_rows])
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# =============================================================================
# 核心函数：唯一 Profile 计算入口
# =============================================================================


def compute_node_cluster_profile(
    daily_bars: pd.DataFrame,
    bars_15m: pd.DataFrame,
    *,
    adjustment_as_of: str | None = None,
    adj_factor_hash: str | None = None,
) -> NodeClusterProfileResult:
    """计算 Node Cluster Profile（三链唯一业务入口）。

    内部调用 `prepare_node_cluster_bars` + `compute_unified_volume_profile`，
    构造不可变 `NodeClusterProfileResult`。

    语义合同（来自 indicator_semantics.py，禁止弱化）：
    - 1d 最近 250 根已完成 qfq 日线决定价格范围
    - 15m 最近 4000 根已完成 qfq bar 分配成交量
    - Peak 搜索域为完整 100 行 Profile
    - value_area_filters_peaks = False（VA 外 Peak 有效）
    - nearest node 来自全部 Peak（含 VA 外）

    Args:
        daily_bars: 日线 OHLCV DataFrame（DatetimeIndex，completed qfq）
        bars_15m: 15m OHLCV DataFrame（DatetimeIndex，completed qfq）
        adjustment_as_of: 复权锚点业务日（ISO 字符串，盘后链传入）
        adj_factor_hash: 复权因子 hash（盘后链传入，用于诊断）

    Returns:
        NodeClusterProfileResult（不可变）

    Raises:
        RuntimeError: 底层 VP 计算失败
    """
    # 统一输入准备（_dedupe_sort_tail 到 250/4000/2）
    prepared: NodeClusterBarsResult = prepare_node_cluster_bars(
        daily_bars, bars_15m, pd.DataFrame()  # 1m 不参与 Profile 计算
    )
    daily_prepared = prepared.daily
    bars_15m_prepared = prepared.bars_15m

    if daily_prepared is None or daily_prepared.empty or len(daily_prepared) < 10:
        # 数据不足，返回空 Profile（保持不可变结构）
        return NodeClusterProfileResult(
            algorithm_version=NODE_CLUSTER_ALGORITHM_VERSION,
            output_schema_version=NODE_CLUSTER_OUTPUT_SCHEMA_VERSION,
            contract_fingerprint=NODE_CLUSTER_CONTRACT_FINGERPRINT,
            profile_rows=[],
            peak_rows=[],
            all_peak_prices=[],
            poc_price=None,
            vah_price=None,
            val_price=None,
            price_step=None,
            lowest_price=None,
            highest_price=None,
            daily_source_hash=_compute_dataframe_hash(daily_prepared),
            bars_15m_source_hash=_compute_dataframe_hash(bars_15m_prepared),
            adj_factor_hash=adj_factor_hash,
            adjustment_as_of=adjustment_as_of,
            daily_bars_count=int(len(daily_prepared)) if daily_prepared is not None else 0,
            bars_15m_count=int(len(bars_15m_prepared)) if bars_15m_prepared is not None else 0,
            profile_hash="empty",
            _vp_result=None,
        )

    # 调用底层 VP（只有本 engine 可调用）
    profile_df = bars_15m_prepared if (bars_15m_prepared is not None and not bars_15m_prepared.empty) else None
    try:
        vp_result: UnifiedVolumeProfileResult = compute_unified_volume_profile(
            daily_prepared, profile_df=profile_df, main_period="day",
        )
    except Exception as exc:
        raise RuntimeError(
            f"node_cluster_engine: compute_unified_volume_profile 失败: {exc}"
        ) from exc

    # 提取字段（VA 外 Peak 保留，禁止过滤）
    profile_rows_list = _profile_rows_to_list(vp_result.profile_df)
    peak_rows_list = _peak_rows_to_list(vp_result.peak_rows)
    all_peak_prices = list(vp_result.all_peak_prices)  # 含 VA 外

    # 计算 source hash 和 profile hash（三链一致性断言用）
    daily_hash = _compute_dataframe_hash(daily_prepared)
    bars_15m_hash = _compute_dataframe_hash(bars_15m_prepared)
    profile_hash = _compute_profile_hash(profile_rows_list)

    return NodeClusterProfileResult(
        algorithm_version=NODE_CLUSTER_ALGORITHM_VERSION,
        output_schema_version=NODE_CLUSTER_OUTPUT_SCHEMA_VERSION,
        contract_fingerprint=NODE_CLUSTER_CONTRACT_FINGERPRINT,
        profile_rows=profile_rows_list,
        peak_rows=peak_rows_list,
        all_peak_prices=all_peak_prices,
        poc_price=_finite_or_none(vp_result.poc_price),
        vah_price=_finite_or_none(vp_result.vah_price),
        val_price=_finite_or_none(vp_result.val_price),
        price_step=_finite_or_none(vp_result.price_step),
        lowest_price=_finite_or_none(vp_result.lowest_price),
        highest_price=_finite_or_none(vp_result.highest_price),
        daily_source_hash=daily_hash,
        bars_15m_source_hash=bars_15m_hash,
        adj_factor_hash=adj_factor_hash,
        adjustment_as_of=adjustment_as_of,
        daily_bars_count=int(len(daily_prepared)),
        bars_15m_count=int(len(bars_15m_prepared)) if bars_15m_prepared is not None else 0,
        profile_hash=profile_hash,
        _vp_result=vp_result,
    )


# =============================================================================
# 单周期 VP（非 Node Cluster，仅供 structural_factor_service 15m secondary）
# =============================================================================


def compute_single_period_volume_profile(
    bars: pd.DataFrame, *, main_period: str = "day",
) -> UnifiedVolumeProfileResult:
    """计算单周期 Volume Profile（非 Node Cluster）。

    本函数仅供 `structural_factor_service._compute_cost_position_factors` 计算
    `secondary.15m.timeframe_volume_profile`（单周期 15m VP，显式非 Node Cluster）使用。
    Node Cluster 三链（盘后 primary / 详情 / 监控）必须使用 `compute_node_cluster_profile`。

    内部调用 `compute_unified_volume_profile(bars, profile_df=None, main_period)`，
    保持与原 `structural_factor_service` 单周期 VP 调用完全一致的语义（profile_df=None
    时 main_period 不影响结果，见 _prepare_profile_bars 早返回路径）。

    本函数是 engine 唯一暴露的非 Node Cluster VP 入口，确保 `structural_factor_service`
    不直接导入 `unified_volume_profile`（架构守护 test_node_cluster_architecture 强制）。

    Args:
        bars: 单周期 OHLCV DataFrame（DatetimeIndex）
        main_period: 主周期标识（与原调用一致默认 "day"；profile_df=None 时不影响结果）

    Returns:
        UnifiedVolumeProfileResult（底层 VP 结果，含 nearest_nodes/position_0_1 方法）

    Raises:
        RuntimeError: 底层 VP 计算失败
    """
    try:
        return compute_unified_volume_profile(
            bars, profile_df=None, main_period=main_period,
        )
    except Exception as exc:
        raise RuntimeError(
            f"node_cluster_engine.compute_single_period_volume_profile 失败: {exc}"
        ) from exc


# =============================================================================
# 状态派生（不重算 Profile）
# =============================================================================


def derive_state_for_price(
    profile: NodeClusterProfileResult, price: float,
) -> NodeClusterPriceState:
    """从 Profile 派生指定价格的状态（不重算 Profile）。

    复用底层 `UnifiedVolumeProfileResult.state_for_price`，包装为不可变
    `NodeClusterPriceState`。

    Args:
        profile: NodeClusterProfileResult（含 _vp_result）
        price: 当前价格

    Returns:
        NodeClusterPriceState（不可变）

    Raises:
        RuntimeError: profile._vp_result 为 None（数据不足或构造异常）
    """
    if profile._vp_result is None:
        raise RuntimeError(
            "node_cluster_engine.derive_state_for_price: profile._vp_result 为 None"
            "（可能数据不足，无法派生状态）"
        )
    state_dict = profile._vp_result.state_for_price(float(price))
    return NodeClusterPriceState(
        current_price=state_dict["current_price"],
        upper_node=state_dict["upper_node"],
        lower_node=state_dict["lower_node"],
        position_0_1=state_dict["position_0_1"],
        poc_node=state_dict["poc_price"],
        last_touched_node=state_dict["last_touched_node"],
    )


# =============================================================================
# 穿越检测（1m prev_close / cur_close）
# =============================================================================


def detect_crossover_signals(
    profile: NodeClusterProfileResult,
    prev_close: float,
    cur_close: float,
) -> list[dict[str, Any]]:
    """1m 穿越检测（公式与 volume_node_monitor._detect_node_crossover_signals 完全一致）。

    逻辑：遍历 profile.all_peak_prices（含 VA 外），检测价格穿越：
        (prev_close <= peak_price < cur_close) or (cur_close <= peak_price < prev_close)

    Args:
        profile: NodeClusterProfileResult（含 all_peak_prices）
        prev_close: 1m 前一根 bar 收盘价
        cur_close: 1m 当前 bar 收盘价

    Returns:
        信号列表，每项含 trigger_type/price/cluster_price/boundary/dev_pct
    """
    cluster_prices = profile.all_peak_prices
    if not cluster_prices:
        return []

    prev_close_f = float(prev_close)
    cur_close_f = float(cur_close)

    signals: list[dict[str, Any]] = []
    for cp in cluster_prices:
        peak_cross = (prev_close_f <= cp < cur_close_f) or (cur_close_f <= cp < prev_close_f)
        if peak_cross:
            dev_pct = (cur_close_f - cp) / cp * 100 if cp != 0 else 0.0
            signals.append({
                "trigger_type": EVENT_TYPE_NODE_CLUSTER_TOUCH,
                "price": cur_close_f,
                "cluster_price": cp,
                "boundary": cp,
                "dev_pct": round(dev_pct, 4),
            })

    return signals


# =============================================================================
# 缓存键构造（调用方使用，engine 自身不缓存）
# =============================================================================


def build_engine_cache_key(
    instrument_id: str,
    profile: NodeClusterProfileResult,
) -> str:
    """构造 engine 缓存键（调用方使用，engine 自身不缓存）。

    键含 algorithm_version + contract_fingerprint + as_of + daily_hash + 15m_hash，
    合同/参数/输入变化自动失效。

    Args:
        instrument_id: 标的 ID（字符串）
        profile: NodeClusterProfileResult（从中提取 hash 和版本字段）

    Returns:
        缓存键字符串
    """
    return (
        f"node_cluster_profile:{instrument_id}"
        f":{profile.algorithm_version}"
        f":{profile.contract_fingerprint}"
        f":as_of={profile.adjustment_as_of or 'latest'}"
        f":daily={profile.daily_source_hash}"
        f":15m={profile.bars_15m_source_hash}"
    )


# =============================================================================
# 序列化（供 snapshot 落库）
# =============================================================================


def profile_to_dict(profile: NodeClusterProfileResult) -> dict[str, Any]:
    """将 NodeClusterProfileResult 序列化为 dict（供 snapshot structural_payload 落库）。

    输出结构：
    ```
    {
        "algorithm_version": "nc-v1",
        "output_schema_version": 1,
        "contract_fingerprint": "nc-cf-v1",
        "profile_rows": [...],   # 100 行
        "peak_rows": [...],      # 全部 Peak（含 VA 外）
        "all_peak_prices": [...],
        "poc_price": float | None,
        "vah_price": float | None,
        "val_price": float | None,
        "price_step": float | None,
        "lowest_price": float | None,
        "highest_price": float | None,
        "daily_source_hash": str,
        "bars_15m_source_hash": str,
        "adj_factor_hash": str | None,
        "adjustment_as_of": str | None,
        "daily_bars_count": int,
        "bars_15m_count": int,
        "profile_hash": str,
    }
    ```
    """
    return {
        "algorithm_version": profile.algorithm_version,
        "output_schema_version": profile.output_schema_version,
        "contract_fingerprint": profile.contract_fingerprint,
        "profile_rows": list(profile.profile_rows),
        "peak_rows": list(profile.peak_rows),
        "all_peak_prices": list(profile.all_peak_prices),
        "poc_price": profile.poc_price,
        "vah_price": profile.vah_price,
        "val_price": profile.val_price,
        "price_step": profile.price_step,
        "lowest_price": profile.lowest_price,
        "highest_price": profile.highest_price,
        "daily_source_hash": profile.daily_source_hash,
        "bars_15m_source_hash": profile.bars_15m_source_hash,
        "adj_factor_hash": profile.adj_factor_hash,
        "adjustment_as_of": profile.adjustment_as_of,
        "daily_bars_count": profile.daily_bars_count,
        "bars_15m_count": profile.bars_15m_count,
        "profile_hash": profile.profile_hash,
    }


def state_to_dict(state: NodeClusterPriceState) -> dict[str, Any]:
    """将 NodeClusterPriceState 序列化为 dict（与 volume_node_monitor.yaml outputs 对齐）。"""
    return state.to_dict()


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 自测入口：验证 engine 计算与状态查询（无副作用，不写库表）
    rng = np.random.default_rng(42)
    n = 100
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0.0, 0.005, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.005, 0.02, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.005, 0.02, n))
    volume = rng.lognormal(mean=13, sigma=0.35, size=n).astype(int)
    dates = pd.date_range("2026-01-01", periods=n, freq="D")

    bars_daily = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )

    # 15m bars（合成，4 倍日线根数）
    n_15m = n * 4
    close_15m = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n_15m)))
    open_15m = np.r_[close_15m[0], close_15m[:-1]] * (1 + rng.normal(0.0, 0.003, n_15m))
    high_15m = np.maximum(open_15m, close_15m) * (1 + rng.uniform(0.002, 0.01, n_15m))
    low_15m = np.minimum(open_15m, close_15m) * (1 - rng.uniform(0.002, 0.01, n_15m))
    volume_15m = rng.lognormal(mean=11, sigma=0.3, size=n_15m).astype(int)
    dates_15m = pd.date_range("2026-01-01", periods=n_15m, freq="15min")
    bars_15m = pd.DataFrame(
        {"open": open_15m, "high": high_15m, "low": low_15m, "close": close_15m, "volume": volume_15m},
        index=dates_15m,
    )

    profile = compute_node_cluster_profile(
        bars_daily, bars_15m, adjustment_as_of="2026-04-10",
    )
    assert isinstance(profile, NodeClusterProfileResult)
    assert profile.algorithm_version == NODE_CLUSTER_ALGORITHM_VERSION
    assert profile.contract_fingerprint == NODE_CLUSTER_CONTRACT_FINGERPRINT
    assert profile.output_schema_version == NODE_CLUSTER_OUTPUT_SCHEMA_VERSION
    print(f"algorithm_version={profile.algorithm_version} ✓")
    print(f"contract_fingerprint={profile.contract_fingerprint} ✓")
    print(f"POC={profile.poc_price} VAH={profile.vah_price} VAL={profile.val_price} ✓")
    print(f"profile_rows count={len(profile.profile_rows)} (期望 {VP_ROWS}) ✓")
    print(f"peak_rows count={len(profile.peak_rows)} ✓")
    print(f"all_peak_prices count={len(profile.all_peak_prices)} ✓")
    print(f"daily_source_hash={profile.daily_source_hash} ✓")
    print(f"bars_15m_source_hash={profile.bars_15m_source_hash} ✓")
    print(f"profile_hash={profile.profile_hash} ✓")

    # 状态派生
    current_price = float(bars_daily["close"].iloc[-1])
    state = derive_state_for_price(profile, current_price)
    assert isinstance(state, NodeClusterPriceState)
    assert state.current_price == round(current_price, 4)
    print(f"state.position_0_1={state.position_0_1} ✓")

    # 穿越检测（构造一个穿越场景）
    if profile.all_peak_prices:
        peak_price = profile.all_peak_prices[0]
        # prev_close 略低于 peak，cur_close 略高于 peak → 触发
        signals = detect_crossover_signals(profile, peak_price - 0.1, peak_price + 0.1)
        assert len(signals) >= 1, "应触发穿越信号"
        print(f"crossover signals={len(signals)} ✓")

    # 缓存键
    cache_key = build_engine_cache_key("test-instrument", profile)
    assert NODE_CLUSTER_ALGORITHM_VERSION in cache_key
    assert NODE_CLUSTER_CONTRACT_FINGERPRINT in cache_key
    assert profile.daily_source_hash in cache_key
    print(f"cache_key={cache_key[:80]}... ✓")

    # 序列化
    profile_dict = profile_to_dict(profile)
    assert "algorithm_version" in profile_dict
    assert "profile_hash" in profile_dict
    print(f"profile_to_dict keys={len(profile_dict)} ✓")

    print("OK")