"""Round 1 Frozen Dataset 字段清单与 schema 定义（纯本地描述，不连 DB）。

本文件是 Round 1 提取/审计/分析三方共享的 SSOT：
- extractor 按此列写 CSV/Parquet
- integrity audit 按此列做 missing / coverage 检查
- primitive audit 按此列画值域/分布图

重要：此文件不伪造不存在的字段。字段列表来自真实代码审计：
- first_pyramid_service.py::compute_first_pyramid_history daily_state.append()
- FirstPyramidHistoryDailyState ORM 模型
- BarDaily ORM 模型

如果 DB 实际 payload 与本清单不一致，integrity audit 会报告为字段异常。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Frozen Dataset 唯一标识：Round 1–3 原则上共用同一 DATASET_ID
# 实际 dataset_id 由 extractor 在运行时生成（= sha256(manifest) 前 16 位）
# ---------------------------------------------------------------------------

DATASET_CODE = "review-market-observation-r1"
DATASET_VERSION = "1.0"

# 目标历史窗口：最近 120 个完整交易日
TARGET_TRADE_DATE_COUNT = 120

# HISTORY_CONTRACT_VERSION（来自 first_pyramid_service.py L132）
EXPECTED_HISTORY_CONTRACT_VERSION = "review-history-v2"

# FIRST_PYRAMID_CORE_ALGORITHM_VERSION（来自 schemas/first_pyramid.py L494）
EXPECTED_ALGORITHM_VERSION = "1.0.0-core-split"

# ---------------------------------------------------------------------------
# 1. Raw daily_state payload 原子字段清单（按维度分组）
#    来源：compute_first_pyramid_history() 内 daily_state.append({...}) 段
# ---------------------------------------------------------------------------

TREND_FIELDS: tuple[str, ...] = (
    "regime_value",           # int: -1(DOWN)/0(SIDEWAYS)/1(UP) 来自 DSA
    "regime_strength",        # float:  regime 强度（连续值）
    "trend_transition",       # str | None: "UP→SIDEWAYS" 等方向迁移标签
    "dsa_dir_bars",           # int: 当前 regime 方向连续 bar 数
    "dsa_vwap_dev_pct",       # float: 相对 VWAP 偏离百分比
    # segment 原子特征（CHANGE-20260808 扩展，prefix-causal）
    "segment_id",             # int | None
    "segment_direction",      # int | None: -1/0/1
    "segment_start_time",     # str | None: ISO date
    "segment_start_bar_index",  # int | None
    "segment_end_bar_index",  # int | None
    "segment_bars",           # int | None
    "segment_change_pct",     # float | None
    "segment_slope",          # float | None
    "current_vs_prev_volume_mean_ratio",  # float | None: 当前段 vs 前段均量比
    "current_vs_prev_amount_mean_ratio",  # float | None
    "current_segment_volume_mean",  # float | None
    "prev_segment_volume_mean",  # float | None
)

STRUCTURE_FIELDS: tuple[str, ...] = (
    "swing_bias",             # int: -1/0/1 主要结构级别方向
    "internal_bias",          # int: -1/0/1 短线结构级别方向
    "structure_alignment",    # str | None: "共振"/"背离"（或旧 raw aligned/divergent）
    "active_internal_ob_count",  # int: 活跃短线 OB 数量
    "active_swing_ob_count",  # int: 活跃主要 OB 数量
    # latest 结构事件摘要（CHANGE-20260808）
    "latest_bos_direction",   # str | None: "up"/"bullish"/"down"/"bearish"（原始）
    "latest_bos_freshness",   # int | None: 距 BOS 确认 bar 数
    "latest_choch_direction",  # str | None
    "latest_choch_freshness",  # int | None
    "latest_ob_direction",    # str | None
    "latest_ob_freshness",    # int | None
    "latest_ob_structure_level",  # str | None: "swing"/"internal"
    "latest_ob_active",       # bool: True=CREATED/ENTERED; False=MITIGATED
)

MOMENTUM_FIELDS: tuple[str, ...] = (
    "volatility_phase",       # str | None: squeeze/released/normal 或合同对应枚举
    "momentum_direction",     # str | None: expanding/contracting/flat（原始）
    "momentum_change",        # str | None: enhancing/weakening/flat（原始）
    "sqzmom_val",             # float: 挤压动量值（>0=扩张, <0=收缩, 0=平）
    "sqzmom_delta",           # float: sqzmom_val 相较前一 bar 变化量
)

VOLUME_ACTIVITY_FIELDS: tuple[str, ...] = (
    "volume_ratio_20",        # float: 当日量 / 前20日均量（不含当日，历史合同）
    "volume_percentile_20",   # float: 0-100，前20日量百分位（历史合同）
    "volume_zscore_20",       # float | None: 20日 z-score
    # Review 共享 rolling facts（CHANGE-20260808，LIVE/HISTORY parity）
    "review_volume_ratio20",  # float: Review member_fact 同口径
    "review_amount_ratio20",  # float
    "review_volume_percentile20",  # float
    "review_amount_percentile200",  # float
    "price_position_120d",    # float: 0-1，120日 (close-low)/(high-low)
)

READINESS_FIELDS: tuple[str, ...] = (
    "available_bars",         # int: 到该 bar 的可用输入 bar 数
    "trend_ready",            # bool: 趋势因子 ready
    "structure_ready",        # bool: 结构因子 ready
    "momentum_ready",         # bool: 动量因子 ready
    "volume20_ready",         # bool: volume 20 日窗口够
    "volume200_ready",        # bool: volume 200 日窗口够
    # 聚合有效性（3 合 1）
    "core_factor_ready",      # bool: trend + structure + momentum 均 ready
    "history_sufficient",     # bool: available_bars >= _MIN_BARS_FOR_REQUIRED_DIMS
    "valid_for_market_aggregation",  # bool: core + history + warmup 都过
    "invalid_reason",         # str | None: "core_factor_not_ready"/"history_insufficient"/"warmup_period"
)

# daily_state 中 meta 字段（非 payload 内）
DAILY_STATE_META_FIELDS: tuple[str, ...] = (
    "bar_index",              # int: 在 one-pass 计算序列中的索引
    "time",                   # str: ISO YYYY-MM-DD（trade_date）
    "history_contract_version",  # str: 必须 = "review-history-v2"（新行）
)

ALL_STATE_PAYLOAD_FIELDS: tuple[str, ...] = (
    DAILY_STATE_META_FIELDS
    + TREND_FIELDS
    + STRUCTURE_FIELDS
    + MOMENTUM_FIELDS
    + VOLUME_ACTIVITY_FIELDS
    + READINESS_FIELDS
)

# ---------------------------------------------------------------------------
# 2. 外层 DB 列（FirstPyramidHistoryDailyState 表，非 state_payload JSONB）
# ---------------------------------------------------------------------------

DB_OUTER_COLUMNS: tuple[str, ...] = (
    "instrument_id",
    "trade_date",
    "algorithm_version",
    "input_hash",
    "source_history_run_id",
    "history_contract_version",  # 注意此列与 payload 内重名；审计时必须检查两者一致
    # created_at / updated_at 不进 frozen dataset（减少磁盘）
)

# ---------------------------------------------------------------------------
# 3. 必要 BarsDaily facts（最小集合，用于 cross-check price/volume）
# ---------------------------------------------------------------------------

BAR_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
)

# ---------------------------------------------------------------------------
# 4. 最终 Frozen Dataset flat schema
# ---------------------------------------------------------------------------

# Flat = DB 外列 + payload 解包 + (可选) bar facts 交叉列
FROZEN_COLUMNS: tuple[str, ...] = (
    # --- DB outer ---
    "instrument_id",
    "trade_date",
    "algorithm_version",
    "input_hash",
    "source_history_run_id",
    "hc_outer",   # = DB 列 history_contract_version
    "hc_payload",  # = state_payload.history_contract_version
    # --- Payload unpacked (ALL_STATE_PAYLOAD_FIELDS 去掉重复的 history_contract_version) ---
) + tuple(f for f in ALL_STATE_PAYLOAD_FIELDS if f != "history_contract_version") + (
    # --- BarDaily cross-check（左连接，可空） ---
    "bar_open",
    "bar_high",
    "bar_low",
    "bar_close",
    "bar_volume",
    "bar_amount",
    "bar_adj_factor",
)

# 可用于形成状态的分类字段（用于 Transition Audit）
# 字段值域 = 可枚举状态（不是连续值）
CATEGORICAL_STATE_FIELDS: tuple[str, ...] = (
    "regime_value",
    "trend_transition",
    "swing_bias",
    "internal_bias",
    "structure_alignment",
    "volatility_phase",
    "momentum_direction",
    "momentum_change",
    # 派生分类（审计里会先做 canonical 归一，再统计）
)


@dataclass(frozen=True)
class DatasetField:
    name: str
    group: Literal[
        "db_outer", "trend", "structure", "momentum",
        "volume_activity", "readiness", "payload_meta", "bar_cross"
    ]
    expected_type: str          # "int" | "float" | "str" | "bool" | "int|None" ...
    from_payload: bool          # True=从 state_payload 拆；False=DB 外列或 bar
    nullable: bool
    description: str


def build_field_inventory() -> list[DatasetField]:
    """返回完整字段清单，供 field_inventory.json 使用。"""
    inv: list[DatasetField] = []

    for name in DB_OUTER_COLUMNS:
        if name == "history_contract_version":
            key = "hc_outer"
            desc = "DB 外层显式列 history_contract_version（审计时需与 hc_payload 一致）"
        else:
            key = name
            desc = f"first_pyramid_history_daily_state 表列 {name}"
        nullable = name in ("source_history_run_id", "history_contract_version")
        inv.append(DatasetField(
            name=key, group="db_outer", expected_type="str" if "version" in name or "hash" in name or "_id" in name else "str",
            from_payload=False, nullable=nullable, description=desc,
        ))

    inv.append(DatasetField(
        name="hc_payload", group="db_outer", expected_type="str",
        from_payload=True, nullable=True,
        description="state_payload.history_contract_version（审计时需与 hc_outer 一致）",
    ))

    group_map = {
        DAILY_STATE_META_FIELDS: "payload_meta",
        TREND_FIELDS: "trend",
        STRUCTURE_FIELDS: "structure",
        MOMENTUM_FIELDS: "momentum",
        VOLUME_ACTIVITY_FIELDS: "volume_activity",
        READINESS_FIELDS: "readiness",
    }
    for field_tuple, group in group_map.items():
        for name in field_tuple:
            if name == "history_contract_version":
                continue  # 已作为 hc_payload 处理
            nullable = True
            etype = _guess_type(name, group)
            inv.append(DatasetField(
                name=name, group=group, expected_type=etype,
                from_payload=True, nullable=nullable,
                description=f"{group} 维度原子字段 {name}（来自 compute_first_pyramid_history）",
            ))

    for name in BAR_FIELDS:
        inv.append(DatasetField(
            name=f"bar_{name}", group="bar_cross", expected_type="float|None",
            from_payload=False, nullable=True,
            description=f"BarDaily.{name}（交叉校验用，左连接可空）",
        ))

    return inv


def _guess_type(name: str, group: str) -> str:
    if name.endswith("_ready") or name.startswith("valid_for_") or name == "latest_ob_active":
        return "bool"
    if (
        name.endswith("_bars") or name.endswith("_count") or name.endswith("_index")
        or name.endswith("_id") or name == "bar_index"
        or name in ("segment_bars", "segment_direction", "regime_value",
                    "swing_bias", "internal_bias")
    ):
        return "int|None"
    if name in ("time", "segment_start_time", "trend_transition",
                "structure_alignment", "volatility_phase",
                "momentum_direction", "momentum_change",
                "latest_bos_direction", "latest_choch_direction",
                "latest_ob_direction", "latest_ob_structure_level",
                "invalid_reason"):
        return "str|None"
    return "float|None"


if __name__ == "__main__":
    inv = build_field_inventory()
    groups: dict[str, int] = {}
    for f in inv:
        groups[f.group] = groups.get(f.group, 0) + 1
    print(f"Total fields in frozen flat schema: {len(inv)}")
    for g, c in sorted(groups.items()):
        print(f"  {g}: {c}")
    assert len(inv) == len(FROZEN_COLUMNS), (
        f"schema mismatch: inventory={len(inv)} FROZEN_COLUMNS={len(FROZEN_COLUMNS)}"
    )
    print("OK: field_inventory == FROZEN_COLUMNS count")


# ============================================================================
# Helper: canonical schema hash（用于 manifest 漂移检测）
# ============================================================================
import hashlib as _hashlib


def compute_schema_hash() -> str:
    """对 Frozen COLUMNS 有序元组 + version 做 sha256，截取 16 位 hex。

    保证：列顺序/列名/版本号任意变化都会改变 hash。
    """
    payload = (
        DATASET_CODE, DATASET_VERSION,
        str(EXPECTED_ALGORITHM_VERSION), str(EXPECTED_HISTORY_CONTRACT_VERSION),
        TARGET_TRADE_DATE_COUNT,
        "|".join(FROZEN_COLUMNS),
    )
    h = _hashlib.sha256("\n".join(str(x) for x in payload).encode("utf-8")).hexdigest()
    return h[:16]


# ============================================================================
# Helper: Frozen 120 交易日选择 + 验证
# ============================================================================

def build_selected_trade_dates(known_trade_dates_asc, target_count: int = TARGET_TRADE_DATE_COUNT):
    """给定已知交易日（按日期升序），取最近 target_count 个并升序返回。"""
    if not known_trade_dates_asc:
        return []
    asc_sorted = sorted(known_trade_dates_asc)  # 防御性排序
    tail = asc_sorted[-target_count:] if len(asc_sorted) >= target_count else asc_sorted
    return list(tail)


def validate_120_consecutive_trade_dates(selected_dates_asc):
    """返回 {count, start, end, is_exact_target (== TARGET_TRADE_DATE_COUNT)}。"""
    if not selected_dates_asc:
        return {
            "count": 0,
            "start": None,
            "end": None,
            "is_exact_target": False,
        }
    return {
        "count": len(selected_dates_asc),
        "start": str(selected_dates_asc[0]),
        "end": str(selected_dates_asc[-1]),
        "is_exact_target": len(selected_dates_asc) == TARGET_TRADE_DATE_COUNT,
    }


# ============================================================================
# Helper: flatten state_payload（保持原值，不自行改写 canonical 语义）
# ============================================================================

def flatten_state_payload(payload, *, hc_outer: str | None = None) -> dict:
    """把 state_payload JSON dict 摊平到 flat 行；缺失 key → None。

    禁止改写：regime_value=-1/0/1、swing_bias=-1/0/1、structure_alignment=
    "共振"/"背离"、volatility_phase/momentum_direction 都保留原值字符串，
    不做 alias（映射是 adapter 层的职责，实验代码不越权）。
    """
    out: dict = {}
    payload = payload or {}
    # hc_payload 来自 payload.history_contract_version（若有），否则 None
    out["hc_payload"] = payload.get("history_contract_version")
    for key in ALL_STATE_PAYLOAD_FIELDS:
        if key in ("history_contract_version",):
            # 已单独取到 hc_payload；不放入重复 key
            continue
        out[key] = payload.get(key)
    return out
