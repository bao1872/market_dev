"""复权因子计算纯函数（CHANGE-20260719-001 §1.2）。

提取自 bar_repository._calculate_adj_factor，作为唯一算法实现。
Auditor（只读比较）和 Rebuild（持久化）都调用本函数；禁止两套算法。

设计原则（用户 /goal §1.2 明确要求）：
1. 纯函数：无 IO（不连 DB、不调 pytdx）、无 min_date、无 supplement_df、无 adapter
2. 调用方负责确保 raw_daily_bars 覆盖所有事件日 + 事件日前至少一根 bar
3. 数据缺失抛 AdjustmentFactorDataError（含缺失事件日列表）：
   - Auditor 捕获后标记 degraded_reason="bars_daily_missing_data"
   - Rebuild 捕获后抛异常让上层处理（禁止 1.0 伪装成功）
4. 旧 supplement_df 拉取逻辑取消（仅 _calculate_adj_factor wrapper 保留，给
   _upsert_daily_bars 回补场景使用）：000688 等数据缺失股票会被纯函数
   检测为 degraded，不是 mismatch；§1.3 修复时补齐 bars_daily 后重审

000688 根因（min_date 参数分叉，本次修复）：
- rebuild 路径：min_date=earliest_affected → 过滤掉 earliest_affected 之前的事件
  → 累积因子不完整 → prev_close 查找失败（事件日附近 close 缺失）→ 跳过事件
- expected 路径：min_date=None → 处理全部事件 → 拉 supplement_df 补齐 close
  → prev_close 正确 → 累积因子完整
- 两条路径输出不同 factor，导致 partial_success_still_inconsistent
- 纯函数删除 min_date 参数，统一算法；调用方负责数据完整性

算法（Chanlunpro klines_fq 的 preclose 公式，与原 _calculate_adj_factor 一致）：
1. 筛选 category=1 的除权除息事件，按日期升序
2. 对每个事件日 D，从 raw_daily_bars 查找 D 之前**最后一根实际存在** bar 的 close
   （长期停牌时该 bar 可能远早于 D；不再按相隔日历日推断数据缺口）
3. event_factor = (close_{D-1} × 10 - fenhong + peigu × peigujia)
                  / ((10 + peigu + songzhuangu) × close_{D-1})
4. 累积因子 = 所有晚于该 bar 日期的事件因子乘积
5. 最新日期（无后续事件）adj_factor = 1.0

用法：
    from app.services.adjustment_factor_calculator import (
        calculate_adjustment_factor_series,
        AdjustmentFactorDataError,
    )
    try:
        factors = calculate_adjustment_factor_series(raw_df, xdxr_df)
    except AdjustmentFactorDataError as exc:
        # Auditor: degraded_reason = exc.degraded_reason
        # Rebuild: re-raise（禁止 1.0 伪装）
        ...

模块自测：
    python -m app.services.adjustment_factor_calculator
"""

from __future__ import annotations

import hashlib
import logging
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from app.constants.factor_contract import FACTOR_ALGORITHM_VERSION

logger = logging.getLogger("services.adjustment_factor_calculator")

# 因子累积时的"无效事件"阈值（与原 _calculate_adj_factor 一致）
# event_factor 与 1.0 差距小于此值时视为无效事件，不累积
_UNIT_EVENT_THRESHOLD = 1e-10

# 批量日期映射的内存上限：避免一次性为全量 bars 分配 index/mask/factor 三组数组。
_FACTOR_MAPPING_CHUNK_SIZE = 16_384


@dataclass(frozen=True)
class MissingEventClose:
    """单个事件日数据缺失详情（不可变）。"""

    event_date: date
    # 该事件日的前一交易日应在 close_map 中但缺失
    prev_trade_date: date | None  # None = 事件日早于 raw_daily_bars 最早日期


class AdjustmentFactorDataError(Exception):
    """复权因子计算所需数据缺失（CHANGE-20260719-001 §1.2）。

    纯函数检测到事件日前一交易日 close 缺失时抛出。
    这不是算法不一致（mismatch），而是数据不完整（degraded）。

    Attributes:
        missing_event_dates: 缺失 close 的事件日列表
        degraded_reason: 简明 degraded 原因标识（用于 Auditor 标记）
    """

    def __init__(
        self,
        missing_event_dates: list[date],
        *,
        degraded_reason: str = "bars_daily_missing_data",
    ) -> None:
        self.missing_event_dates = missing_event_dates
        self.degraded_reason = degraded_reason
        dates_str = ", ".join(d.isoformat() for d in missing_event_dates[:5])
        more = (
            f" (共 {len(missing_event_dates)} 个)"
            if len(missing_event_dates) > 5
            else ""
        )
        super().__init__(
            f"复权因子计算数据缺失：事件日前一交易日 close 缺失，"
            f"事件日=[{dates_str}{more}]，degraded_reason={degraded_reason}"
        )


def filter_effective_corporate_actions(
    corporate_actions: pd.DataFrame | None,
    *,
    effective_as_of: date,
) -> pd.DataFrame:
    """唯一的「某个 business date 已生效的公司行为」过滤器。

    effective 定义（项目唯一事实源）::

        category == 1  AND  event_date <= effective_as_of

    future event（``event_date > effective_as_of``）必须被排除：

    - **factor 计算**：否则未生效的除权会提前进入 cumulative factor，
      把最新 raw bar 的价格错误改写成除权后的价格。
    - **fingerprint**：否则 future event 会污染 fingerprint —— 等它真正生效时
      fingerprint 反而没有变化，rebuild 不再触发（永久 stale）。

    Args:
        corporate_actions: xdxr 结果（columns 至少含 date / category）。
        effective_as_of: 有效截止日（含当日）。

    Returns:
        已生效事件（按 date 升序，index 重置）；无有效事件时返回空 DataFrame。
    """
    if corporate_actions is None or corporate_actions.empty:
        return pd.DataFrame()

    actions = corporate_actions.copy()

    if "category" not in actions.columns or "date" not in actions.columns:
        return pd.DataFrame()

    actions["_effective_date"] = pd.to_datetime(actions["date"], errors="coerce")
    # category 可能是 int / float / 数字字符串；非数值一律视为不生效
    category = pd.to_numeric(actions["category"], errors="coerce")

    cutoff = pd.Timestamp(effective_as_of)
    actions = actions[
        (category == 1)
        & actions["_effective_date"].notna()
        & (actions["_effective_date"] <= cutoff)
    ].copy()

    if actions.empty:
        return actions.drop(columns=["_effective_date"], errors="ignore")

    actions["date"] = actions["_effective_date"]
    return (
        actions.drop(columns=["_effective_date"])
        .sort_values("date")
        .reset_index(drop=True)
    )


def corporate_action_fingerprint(
    corporate_actions: pd.DataFrame | None,
    *,
    effective_as_of: date,
) -> tuple[str, date | None]:
    """已生效公司行为集合的 fingerprint 与最早生效日（纯函数）。

    与 :func:`filter_effective_corporate_actions` **共用同一过滤逻辑**，保证
    「进入 factor 的事件集合」与「进入 fingerprint 的事件集合」永远一致 ——
    若两者分叉，就会出现「factor 已变但 fingerprint 未变」的静默 stale。

    字符串格式与历史实现保持一致（``date|fenhong|songzhuangu|peigu|peigujia``，
    ``\\n`` 连接后取 sha256 前 16 位），避免无意义地洗掉已存 Redis fingerprint。

    Returns:
        (fingerprint, earliest_effective_date)；无有效事件时返回 ``("", None)``。
    """
    events = filter_effective_corporate_actions(
        corporate_actions,
        effective_as_of=effective_as_of,
    )

    if events.empty:
        return "", None

    fp_parts: list[str] = []
    for _, row in events.iterrows():
        fp_parts.append(
            f"{row['date']}|{row.get('fenhong', 0)}|"
            f"{row.get('songzhuangu', 0)}|"
            f"{row.get('peigu', 0)}|{row.get('peigujia', 0)}"
        )

    fingerprint = hashlib.sha256(
        "\n".join(fp_parts).encode("utf-8")
    ).hexdigest()[:16]

    earliest = pd.Timestamp(events["date"].iloc[0]).date()
    return fingerprint, earliest


def next_future_corporate_action_date(
    corporate_actions: pd.DataFrame | None,
    *,
    effective_as_of: date,
) -> date | None:
    """返回 ``effective_as_of`` 之后最早的 category=1 公司行为日（未来事件日程）。

    与 :func:`corporate_action_fingerprint` 的 effective 集合**严格区分**：

    - ``event_date <= effective_as_of`` → 已生效，进入 factor / fingerprint；
    - ``event_date > effective_as_of``  → 仅作为「未来刷新日程」，不进入 fingerprint。

    为什么未来事件不能进 fingerprint：fingerprint 在事件真正生效日才应变化；若提前
    进入，生效当天 fingerprint 反而不变，rebuild 永不触发（永久 stale）。本函数把未来
    事件单独抽出来，供下一轮 G1B-3B2 的「已知 next_event_date 到期」刷新信号使用。

    Args:
        corporate_actions: xdxr 结果（columns 至少含 date / category）。
        effective_as_of: 有效截止日（与 fingerprint cutoff 使用同一日期，保证一致）。

    Returns:
        最早未来事件日（``event_date > effective_as_of`` 且 ``category == 1``）；
        无未来事件 / 空 / 缺列 / 日期非法时返回 ``None``。
    """
    if corporate_actions is None or corporate_actions.empty:
        return None

    if "category" not in corporate_actions.columns or "date" not in corporate_actions.columns:
        return None

    frame = corporate_actions.copy()
    dates = pd.to_datetime(frame["date"], errors="coerce")
    categories = pd.to_numeric(frame["category"], errors="coerce")

    future = dates[
        (categories == 1)
        & dates.notna()
        & (dates > pd.Timestamp(effective_as_of))
    ]

    if future.empty:
        return None

    return future.min().date()


def calculate_adjustment_factor_series(
    raw_daily_bars: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    *,
    algorithm_version: str = FACTOR_ALGORITHM_VERSION,
    effective_as_of: date | None = None,
) -> list[float]:
    """计算前复权因子序列（纯函数，无 IO）。

    基于 raw 日线 close + 公司行为（xdxr category=1 事件），按 Chanlunpro
    preclose 公式计算每根 bar 的前复权因子。最新日期 adj_factor=1.0，
    历史 bar 的 adj_factor = 后续所有事件因子乘积。

    纯函数约束：
    - 不连 DB、不调 pytdx、不拉 supplement_df
    - 不接收 min_date/adapter/use_raw_close 参数
    - 数据缺失时抛 AdjustmentFactorDataError（不返回 1.0 伪装）

    Args:
        raw_daily_bars: 必须包含列 ['datetime', 'close']。
            - datetime: pd.Timestamp 或可转换值（事件日 + 前一交易日 close 必须在此 DataFrame 中）
            - close: 收盘价（float）
            调用方负责确保覆盖所有事件日 + 前一交易日（否则抛 AdjustmentFactorDataError）
        corporate_actions: 必须包含列 ['date', 'category', 'fenhong',
            'songzhuangu', 'peigu', 'peigujia']；
            - date: pd.Timestamp 或 datetime
            - category: int（仅处理 category=1 的除权除息事件）
            - fenhong/songzhuangu/peigu/peigujia: float（NaN 视为 0）
            空 DataFrame 时返回全 1.0
        algorithm_version: 算法版本（来自 FACTOR_ALGORITHM_VERSION，用于日志和审计）
        effective_as_of: 有效截止日（含当日）。None 时默认取 ``raw_daily_bars`` 的
            **最新 bar 日期** —— canonical 语义只允许「截至最新 raw bar 已生效」的
            公司行为，future event 绝不提前进入 factor。
            显式传入用于 point-in-time / 盘前 business-date 场景（例如 raw 只到
            9/11，但业务日=9/12，需要纳入 9/12 当日事件）。

    Returns:
        adj_factor 列表，与 raw_daily_bars 行一一对应；
        无事件时返回全 1.0 列表；空 DataFrame 返回空列表

    Raises:
        AdjustmentFactorDataError: 事件日前一交易日 close 不在 raw_daily_bars 中
            （数据缺失，调用方应补齐数据或标记 degraded）

    Examples:
        >>> raw = pd.DataFrame({
        ...     "datetime": pd.to_datetime(["2026-04-23", "2026-04-24"]),
        ...     "close": [40.97, 41.20],
        ... })
        >>> xdxr = pd.DataFrame([{
        ...     "date": pd.Timestamp("2026-04-24"),
        ...     "category": 1, "fenhong": 1.3, "songzhuangu": 0,
        ...     "peigu": 0, "peigujia": 0,
        ... }])
        >>> factors = calculate_adjustment_factor_series(raw, xdxr)
        >>> len(factors) == 2
        True
        >>> abs(factors[1] - 1.0) < 1e-10  # 事件日当天 factor=1.0（无后续事件）
        True
    """
    if raw_daily_bars.empty:
        return []

    default_factors = [1.0] * len(raw_daily_bars)

    if corporate_actions is None or corporate_actions.empty:
        return default_factors

    raw_dates = pd.to_datetime(raw_daily_bars["datetime"], errors="coerce")
    valid_raw_dates = raw_dates.dropna()
    if valid_raw_dates.empty:
        return default_factors

    # canonical 语义：默认只允许「截至最新 raw bar 日期已经生效」的公司行为。
    # 这样即使 caller 不传任何参数（如 canonical rebuild），future XDXR 也不会
    # 提前进入因子 —— 否则最新几根 bar 会被未生效的除权改写。
    latest_raw_bar_date: date = valid_raw_dates.max().date()
    effective_cutoff = effective_as_of or latest_raw_bar_date

    # 唯一的 effective 过滤（category==1 AND event_date <= cutoff）
    exc_events = filter_effective_corporate_actions(
        corporate_actions,
        effective_as_of=effective_cutoff,
    )
    if exc_events.empty:
        return default_factors

    # 构建 close 查找表：date -> close
    # dict(zip(...)) 与逐行赋值保持一致：同一日期重复出现时最后一行生效。
    # 日期和 close 先按列转换，避免在行情行数上执行 iterrows。
    raw_bar_dates = raw_dates.dt.date.to_numpy()
    raw_closes = raw_daily_bars["close"].astype(float).to_numpy()
    close_map = dict(zip(raw_bar_dates, raw_closes, strict=True))

    sorted_close_dates = sorted(close_map.keys())
    earliest_bar_date = sorted_close_dates[0] if sorted_close_dates else None

    # 检测数据缺失：事件日之前是否存在任何 raw bar
    # [CHANGE-20260719-001 §1.3] 修复 000688 误报：
    # - 事件日 <= earliest_bar_date 的事件不影响任何 bar 的因子（bar_date >= earliest_bar_date
    #   >= event_date，event_date 不在 bar_date 之后），直接跳过，不算数据缺失。
    # [F3] 判定只依据「事件日前是否存在最后一根真实 bar」，**不再按相隔日历日**
    #      推断数据是否损坏（原 14 天 _BARS_DAILY_GAP_THRESHOLD_DAYS hard block 已移除）。
    #      长期停牌（如 000032 2014/2017、001331/688689 2026-06）同样应使用该
    #      最后一根实际成交 close，而不是把整个盘后打断。
    missing_event_dates: list[date] = []
    for _, event in exc_events.iterrows():
        event_date = pd.Timestamp(event["date"]).date()
        # 跳过早于等于最早 bar 的事件（不影响任何 bar 因子）
        if earliest_bar_date is not None and event_date <= earliest_bar_date:
            continue
        prev_result = _find_prev_close(event_date, sorted_close_dates, close_map)
        if prev_result is None:
            missing_event_dates.append(event_date)

    if missing_event_dates:
        raise AdjustmentFactorDataError(missing_event_dates)

    # 按日期升序排列事件
    exc_events = exc_events.sort_values("date")

    # 计算每个事件的因子，并构建 (event_date, cumulative_factor) 列表
    # cumulative_factor 表示：日期 < event_date 的 bar 需要乘以该因子
    # 从最新事件向最旧事件累积
    events_with_factor: list[tuple[date, float]] = []
    cumulative = 1.0
    for _, event in exc_events[::-1].iterrows():
        event_date = pd.Timestamp(event["date"]).date()
        # 跳过早于等于最早 bar 的事件（不影响任何 bar 因子，前面已检测）
        if earliest_bar_date is not None and event_date <= earliest_bar_date:
            continue
        # prev_close 必非 None（前面已检测）
        prev_result = _find_prev_close(event_date, sorted_close_dates, close_map)
        assert prev_result is not None, (
            f"prev_close 不应为 None（已检测数据缺失）：event_date={event_date}"
        )
        _, prev_close = prev_result
        if prev_close == 0:
            logger.warning(
                "事件日 %s 前一交易日 close=0（数据异常），跳过该事件", event_date,
            )
            continue

        fenhong = float(event["fenhong"]) if pd.notna(event["fenhong"]) else 0.0
        songzhuangu = (
            float(event["songzhuangu"]) if pd.notna(event["songzhuangu"]) else 0.0
        )
        peigu = float(event["peigu"]) if pd.notna(event["peigu"]) else 0.0
        peigujia = (
            float(event["peigujia"]) if pd.notna(event["peigujia"]) else 0.0
        )

        # Chanlunpro preclose 公式：
        # preclose = (close_{D-1} × 10 - fenhong + peigu × peigujia)
        #           / (10 + peigu + songzhuangu)
        # event_factor = preclose / close_{D-1}
        denominator = 10 + peigu + songzhuangu
        if denominator == 0:
            logger.warning(
                "事件日 %s 除权除息分母为 0（peigu=%s songzhuangu=%s），跳过该事件",
                event_date, peigu, songzhuangu,
            )
            continue

        preclose = (prev_close * 10 - fenhong + peigu * peigujia) / denominator
        event_factor = preclose / prev_close

        # 仅当事件因子不为 1.0 时才累积（避免无意义的事件）
        if abs(event_factor - 1.0) > _UNIT_EVENT_THRESHOLD:
            cumulative *= event_factor
        events_with_factor.append((event_date, cumulative))

    # events_with_factor 按 event_date 降序（最新事件在前）
    # 对每个 bar 日期，adj_factor = bar_date 之后第一个事件的 cumulative_factor
    # 即降序列表中最后一个 event_date > bar_date 的事件
    # 如果没有晚于 bar_date 的事件，adj_factor = 1.0
    adj_factors = _map_cumulative_factors_to_bars(raw_dates, events_with_factor)

    logger.info(
        "计算 adj_factor bars=%d events=%d adj_range=[%.6f, %.6f] algorithm_version=%s",
        len(adj_factors), len(events_with_factor),
        min(adj_factors) if adj_factors else 1.0,
        max(adj_factors) if adj_factors else 1.0,
        algorithm_version,
    )
    return adj_factors


def _find_prev_close(
    target_date: date,
    sorted_close_dates: list[date],
    close_map: dict[date, float],
) -> tuple[date, float] | None:
    """查找 target_date 前一交易日的收盘价及日期（纯函数）。

    在 sorted_close_dates 中找 < target_date 的最大日期，返回 (该日期, close)。
    若无（target_date 早于所有 close_date），返回 None。

    Args:
        target_date: 目标日期（事件日）
        sorted_close_dates: 升序排列的交易日列表
        close_map: 日期 -> 收盘价映射

    Returns:
        (前一交易日日期, 前一交易日收盘价)，或 None（数据缺失）
    """
    insertion_index = bisect_left(sorted_close_dates, target_date)
    if insertion_index == 0:
        return None
    prev_date = sorted_close_dates[insertion_index - 1]
    return (prev_date, close_map[prev_date])


def _map_cumulative_factors_to_bars(
    raw_dates: pd.Series,
    events_with_factor: list[tuple[date, float]],
) -> list[float]:
    """把事件累计因子映射到原始 bar 顺序，保持严格 ``event_date > bar_date``。

    ``events_with_factor`` 由新到旧排列。同日可能存在多条公司行动；旧逐项循环会
    选择该日最后写入、也就是包含同日全部事件的累计值。这里先按日期折叠，再使用
    ``searchsorted(side="right")`` 批量寻找每根 bar 之后的第一个事件。
    """
    if not events_with_factor:
        return [1.0] * len(raw_dates)

    # 反转后按日期升序；同日第一项包含该日全部事件的累计乘积。
    events_ascending = list(reversed(events_with_factor))
    event_factor_by_date: dict[date, float] = {}
    for event_date, cumulative_factor in events_ascending:
        event_factor_by_date.setdefault(event_date, cumulative_factor)

    event_dates = np.asarray(list(event_factor_by_date), dtype="datetime64[D]")
    cumulative_factors = np.asarray(
        list(event_factor_by_date.values()),
        dtype=float,
    )
    bar_dates = raw_dates.to_numpy()
    factors: list[float] = []
    for start in range(0, len(bar_dates), _FACTOR_MAPPING_CHUNK_SIZE):
        chunk_dates = bar_dates[start : start + _FACTOR_MAPPING_CHUNK_SIZE]
        event_indexes = np.searchsorted(event_dates, chunk_dates, side="right")
        chunk_factors = np.ones(len(chunk_dates), dtype=float)
        has_future_event = event_indexes < len(event_dates)
        chunk_factors[has_future_event] = cumulative_factors[
            event_indexes[has_future_event]
        ]
        factors.extend(chunk_factors.tolist())
    return factors


if __name__ == "__main__":
    # 自测：验证纯函数逻辑（不连 DB/网络，无副作用）
    logging.basicConfig(level=logging.INFO)

    # Case 1: 无事件 → 全 1.0
    raw1 = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]),
        "close": [10.0, 10.5, 11.0],
    })
    xdxr1 = pd.DataFrame(columns=["date", "category", "fenhong", "songzhuangu", "peigu", "peigujia"])
    factors1 = calculate_adjustment_factor_series(raw1, xdxr1)
    assert factors1 == [1.0, 1.0, 1.0], f"Case1 应全 1.0: {factors1}"
    print("Case1 无事件全 1.0 ✓")

    # Case 2: 单事件（分红 1.3 元/10 股）
    # close_{D-1}=40.97, fenhong=1.3, songzhuangu=0, peigu=0, peigujia=0
    # preclose = (40.97*10 - 1.3 + 0) / (10 + 0 + 0) = 408.4 / 10 = 40.84
    # event_factor = 40.84 / 40.97 = 0.996826...
    raw2 = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-04-23", "2026-04-24"]),
        "close": [40.97, 41.20],
    })
    xdxr2 = pd.DataFrame([{
        "date": pd.Timestamp("2026-04-24"),
        "category": 1, "fenhong": 1.3, "songzhuangu": 0,
        "peigu": 0, "peigujia": 0,
    }])
    factors2 = calculate_adjustment_factor_series(raw2, xdxr2)
    # 2026-04-23: 事件日在后 → factor = event_factor = 0.996826
    # 2026-04-24: 事件日当天，无后续事件 → factor = 1.0
    expected_factor = (40.97 * 10 - 1.3) / 10 / 40.97
    assert abs(factors2[0] - expected_factor) < 1e-10, (
        f"Case2 04-23 factor 应={expected_factor}, got {factors2[0]}"
    )
    assert abs(factors2[1] - 1.0) < 1e-10, (
        f"Case2 04-24 factor 应=1.0, got {factors2[1]}"
    )
    print(f"Case2 单事件 ✓ factor={factors2}")

    # Case 3: 长 gap（长期停牌）→ 不再抛异常，使用事件日前最后一根实际 bar 的 close
    # [F3] 事件日 2026-04-24 前最后一根 bar 是 2026-01-30（close=26.80），相隔远超
    # 原 14 天阈值；现按「存在最后一根真实 bar 即可」计算因子，不再推断数据缺口。
    raw3 = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-01-30", "2026-06-29"]),
        "close": [26.80, 33.42],
    })
    xdxr3 = xdxr2  # 事件日 2026-04-24, fenhong=1.3
    factors3 = calculate_adjustment_factor_series(raw3, xdxr3)
    expected_factor3 = (26.80 * 10 - 1.3) / 10 / 26.80
    assert abs(factors3[0] - expected_factor3) < 1e-10, (
        f"Case3 应使用 2026-01-30 close=26.80 计算因子，got {factors3[0]}"
    )
    assert abs(factors3[1] - 1.0) < 1e-10, (
        f"Case3 事件日及之后 factor 应=1.0: {factors3[1]}"
    )
    print(f"Case3 长 gap 使用最后实际 bar ✓ factors={factors3}")

    # Case 3b: 事件日 <= earliest_bar_date → 跳过事件（不抛异常）
    # [CHANGE-20260719-001 §1.3] 事件日等于最早 bar 日期时，事件不影响任何 bar
    # （无 bar 在事件日之前），应跳过而非抛异常。
    raw3b = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-04-24"]),
        "close": [41.20],
    })
    xdxr3b = xdxr2  # 事件日 2026-04-24 == earliest_bar_date
    factors3b = calculate_adjustment_factor_series(raw3b, xdxr3b)
    assert factors3b == [1.0], f"Case3b 事件<=earliest_bar 应跳过: {factors3b}"
    print(f"Case3b 事件<=earliest_bar 跳过 ✓ factors={factors3b}")

    # Case 4: 空 raw_df → 空列表
    raw4 = pd.DataFrame(columns=["datetime", "close"])
    factors4 = calculate_adjustment_factor_series(raw4, xdxr2)
    assert factors4 == [], f"Case4 应返回空列表: {factors4}"
    print("Case4 空 raw_df ✓")

    # Case 5: 事件日早于 raw_df 最早日期 → 跳过事件（不抛异常）
    # [CHANGE-20260719-001 §1.3] 事件日 < earliest_bar_date 时，事件不影响任何 bar
    # （所有 bar 都在事件日之后），应跳过而非抛异常。
    raw5 = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-06-16", "2026-06-17"]),
        "close": [10.0, 10.5],
    })
    xdxr5 = pd.DataFrame([{
        "date": pd.Timestamp("2026-06-15"),  # 早于 raw_df 最早日期
        "category": 1, "fenhong": 1.0, "songzhuangu": 0,
        "peigu": 0, "peigujia": 0,
    }])
    # [CHANGE-20260719-001 §1.3] 事件日 2026-06-15 < earliest_bar_date 2026-06-16
    # → 跳过事件，不抛异常，因子全 1.0
    factors5 = calculate_adjustment_factor_series(raw5, xdxr5)
    assert factors5 == [1.0, 1.0], f"Case5 事件<earliest_bar 应跳过: {factors5}"
    print(f"Case5 事件<earliest_bar 跳过 ✓ factors={factors5}")

    # Case 6: 算法版本传参（用于日志）
    factors6 = calculate_adjustment_factor_series(
        raw2, xdxr2, algorithm_version="test-v2"
    )
    assert factors6 == factors2, "Case6 算法版本不影响结果"
    print("Case6 算法版本传参 ✓")

    print("OK")
