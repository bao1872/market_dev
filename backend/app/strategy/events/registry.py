"""V1.1 事件注册表 - 升级版，支持 state_ttl_seconds 和 allowed_roles 声明。

从 ref/交易/event_lib/registry.py 迁移并升级。

核心升级：
1. register_event 新增 state_ttl_seconds 和 allowed_roles 参数
2. 新增 detect_to_drafts：将检测信号转为 StrategyEventDraft 列表（自包含 payload）
3. 保留 detect_panel：向后兼容，返回 0/1 标记列 DataFrame

设计说明：
- 本文件只存元数据和检测逻辑，不重新计算因子。
- 事件检测只能基于标准化因子列触发（接收 factors_df）。
- 禁异常吞没：检测失败时补上下文后 re-raise。

Usage:
    from app.strategy.events.registry import register_event, detect_to_drafts

    register_event(
        name="evt_dsa_dir_flip_up",
        category="趋势事件",
        detect_func=_detect_dsa_dir_flip_up,
        required_factors=["dsa_dir"],
        description="DSA方向翻转为上升",
        direction="positive",
        is_core=True,
        state_ttl_seconds=3600,
        allowed_roles=["TRIGGER", "CONFIRM"],
    )
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pandas as pd

from app.strategy.events.base import (
    EventRole,
    StrategyEventDraft,
    build_dedupe_key,
)

logger = logging.getLogger("event_registry")

# 全局事件注册表
EVENT_REGISTRY: dict[str, dict[str, Any]] = {}

# 默认状态有效期（秒）
DEFAULT_STATE_TTL_SECONDS = 3600
# 默认允许角色
DEFAULT_ALLOWED_ROLES = [EventRole.OBSERVE]


def register_event(
    name: str,
    category: str,
    detect_func: Callable[[pd.DataFrame], pd.Series],
    required_factors: list[str],
    description: str,
    direction: str = "neutral",
    is_core: bool = False,
    outputs_strength: bool = False,
    state_ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS,
    allowed_roles: list[str] | None = None,
) -> None:
    """注册一个事件到全局注册表（V1.1 升级版）。

    Args:
        name: 事件列名（唯一标识，建议前缀 evt_）
        category: 事件类别，如'趋势事件'、'量能事件'等
        detect_func: 检测函数，接收 factors_df 返回 Series（0/1 或布尔值）
        required_factors: 依赖的因子列名列表
        description: 事件描述
        direction: 事件方向，'positive'/'negative'/'neutral'
        is_core: 是否核心事件
        outputs_strength: 是否输出强度列（evt_*_strength）
        state_ttl_seconds: 状态有效期（秒），超时后状态机窗口过期
        allowed_roles: 允许的角色列表（TRIGGER/CONFIRM/VETO/OBSERVE），
                       None 则默认 [OBSERVE]

    Raises:
        ValueError: 事件已注册或角色非法
    """
    if name in EVENT_REGISTRY:
        raise ValueError(f"事件 '{name}' 已注册")

    roles = allowed_roles if allowed_roles is not None else list(DEFAULT_ALLOWED_ROLES)
    # 校验角色合法
    for role in roles:
        if role not in EventRole.ALL_ROLES:
            raise ValueError(f"事件 '{name}' 非法角色: {role}，合法角色: {EventRole.ALL_ROLES}")

    EVENT_REGISTRY[name] = {
        "name": name,
        "category": category,
        "detect": detect_func,
        "required_factors": required_factors,
        "description": description,
        "direction": direction,
        "is_core": is_core,
        "outputs_strength": outputs_strength,
        "state_ttl_seconds": state_ttl_seconds,
        "allowed_roles": roles,
    }


def list_all() -> list[dict[str, Any]]:
    """返回所有已注册事件的元数据列表。"""
    return list(EVENT_REGISTRY.values())


def list_by_category(category: str) -> list[dict[str, Any]]:
    """按类别返回事件元数据列表。"""
    return [e for e in EVENT_REGISTRY.values() if e["category"] == category]


def get_event(name: str) -> dict[str, Any]:
    """获取单个事件的元数据。

    Raises:
        KeyError: 事件未注册
    """
    if name not in EVENT_REGISTRY:
        raise KeyError(f"事件 '{name}' 未注册")
    return EVENT_REGISTRY[name]


def detect_panel(
    factors_df: pd.DataFrame,
    categories: list[str] | None = None,
    event_names: list[str] | None = None,
    exclude: list[str] | None = None,
) -> pd.DataFrame:
    """统一检测入口（向后兼容）。遍历注册表，基于因子列触发事件。

    Args:
        factors_df: 包含因子列的 DataFrame
        categories: 按类别筛选
        event_names: 按名称筛选
        exclude: 排除的事件名列表

    Returns:
        包含所有事件标记列的 DataFrame（0/1）
    """
    result = factors_df.copy()
    exclude = exclude or []

    for name, meta in EVENT_REGISTRY.items():
        if name in exclude:
            continue
        if categories and meta["category"] not in categories:
            continue
        if event_names and name not in event_names:
            continue

        missing = [f for f in meta["required_factors"] if f not in result.columns]
        if missing:
            result[name] = 0
            if meta["outputs_strength"]:
                result[f"{name}_strength"] = 0.0
            continue

        try:
            detected = meta["detect"](result)
            if isinstance(detected, pd.Series):
                result[name] = detected.astype(int)
            elif isinstance(detected, pd.DataFrame):
                for col in detected.columns:
                    result[col] = detected[col]
        except Exception as e:
            raise RuntimeError(f"检测事件 '{name}' 失败: {e}") from e

    return result


def detect_to_drafts(
    factors_df: pd.DataFrame,
    *,
    strategy_version_id: str,
    instrument_id: str,
    categories: list[str] | None = None,
    event_names: list[str] | None = None,
    exclude: list[str] | None = None,
    snapshot_builder: Callable[[pd.DataFrame, int], dict[str, Any]] | None = None,
) -> list[StrategyEventDraft]:
    """检测事件并生成 StrategyEventDraft 列表（V1.1 核心入口）。

    遍历注册表，对每个触发的事件生成自包含的 StrategyEventDraft。
    payload 从 factors_df 当前行提取（自包含，不依赖外部状态）。

    Args:
        factors_df: 包含因子列的 DataFrame（index 为时间）
        strategy_version_id: 策略版本 ID（用于构建 dedupe_key）
        instrument_id: 股票 ID（用于构建 dedupe_key 和 logical_entity）
        categories: 按类别筛选
        event_names: 按名称筛选
        exclude: 排除的事件名列表
        snapshot_builder: 快照构建函数 (factors_df, row_idx) -> dict，
                          None 则使用默认快照（提取当行因子值）

    Returns:
        StrategyEventDraft 列表（每个触发的事件一个 draft）

    Raises:
        RuntimeError: 检测失败时补上下文后 re-raise
    """
    drafts: list[StrategyEventDraft] = []
    exclude = exclude or []

    for name, meta in EVENT_REGISTRY.items():
        if name in exclude:
            continue
        if categories and meta["category"] not in categories:
            continue
        if event_names and name not in event_names:
            continue

        missing = [f for f in meta["required_factors"] if f not in factors_df.columns]
        if missing:
            continue

        try:
            detected = meta["detect"](factors_df)
        except Exception as e:
            raise RuntimeError(f"检测事件 '{name}' 失败: {e}") from e

        if not isinstance(detected, pd.Series):
            continue

        # 找到触发位置（值为 1/True 的行）
        triggered_mask = detected.astype(int) == 1
        if not triggered_mask.any():
            continue

        triggered_indices = factors_df.index[triggered_mask.values]

        for idx in triggered_indices:
            # 获取当行数据
            row = factors_df.loc[idx]
            row_pos = factors_df.index.get_loc(idx)

            # 构建自包含 payload（提取当行因子值）
            payload = _build_payload(name, meta, row)

            # 构建快照（冻结事件发生时的上下文）
            if snapshot_builder is not None:
                snapshot = snapshot_builder(factors_df, row_pos)
            else:
                snapshot = _build_default_snapshot(factors_df, row_pos)

            # 构建去重键
            dedupe_key = build_dedupe_key(
                strategy_version_id, instrument_id, idx, name
            )

            draft = StrategyEventDraft(
                event_type=name,
                event_time=idx,
                dedupe_key=dedupe_key,
                logical_entity=instrument_id,
                payload=payload,
                snapshot=snapshot,
                state_ttl_seconds=meta["state_ttl_seconds"],
                allowed_roles=list(meta["allowed_roles"]),
            )
            drafts.append(draft)

    return drafts


def _build_payload(
    event_name: str,
    meta: dict[str, Any],
    row: pd.Series,
) -> dict[str, Any]:
    """构建自包含 payload（从当行因子值提取）。

    payload 包含事件类型、方向、依赖因子值，确保自包含不依赖外部状态。

    Args:
        event_name: 事件名
        meta: 事件元数据
        row: 触发行的因子数据

    Returns:
        payload 字典
    """
    payload: dict[str, Any] = {
        "event_type": event_name,
        "direction": meta["direction"],
        "category": meta["category"],
        "description": meta["description"],
    }
    # 提取依赖因子值（确保 payload 自包含）
    for factor in meta["required_factors"]:
        if factor in row.index:
            val = row[factor]
            # 转换 numpy 类型为 Python 原生类型
            if hasattr(val, "item"):
                payload[factor] = val.item()
            else:
                payload[factor] = val
    return payload


def _build_default_snapshot(
    factors_df: pd.DataFrame,
    row_pos: int,
) -> dict[str, Any]:
    """构建默认快照（提取当行及前 N 行因子值）。

    快照冻结事件发生时的完整上下文，用于证据回溯。
    默认提取当行 + 前 5 行的因子值。

    Args:
        factors_df: 因子 DataFrame
        row_pos: 触发行位置

    Returns:
        快照字典
    """
    window_start = max(0, row_pos - 5)
    window = factors_df.iloc[window_start : row_pos + 1]

    # 转换为可序列化的字典
    snapshot: dict[str, Any] = {}
    for col in window.columns:
        vals = window[col].tolist()
        # 转换 numpy 类型
        snapshot[col] = [
            v.item() if hasattr(v, "item") else (None if pd.isna(v) else v)
            for v in vals
        ]
    snapshot["_window_size"] = len(window)
    return snapshot


if __name__ == "__main__":
    # 自测入口：验证注册表基础功能（无副作用）
    import pandas as pd

    # 1. 注册一个测试事件
    def _test_detect(factors_df: pd.DataFrame) -> pd.Series:
        return (factors_df["close"] > 10).astype(int)

    register_event(
        name="evt_test_smoke",
        category="测试事件",
        detect_func=_test_detect,
        required_factors=["close"],
        description="测试事件",
        direction="positive",
        state_ttl_seconds=1800,
        allowed_roles=[EventRole.TRIGGER],
    )
    meta = get_event("evt_test_smoke")
    assert meta["state_ttl_seconds"] == 1800
    assert meta["allowed_roles"] == [EventRole.TRIGGER]
    print(f"注册表元数据 ✓: ttl={meta['state_ttl_seconds']}, roles={meta['allowed_roles']}")

    # 2. detect_to_drafts
    df = pd.DataFrame(
        {"close": [5.0, 11.0, 9.0, 12.0]},
        index=pd.to_datetime(["2026-06-18 10:00", "2026-06-18 10:01", "2026-06-18 10:02", "2026-06-18 10:03"]),
    )
    drafts = detect_to_drafts(
        df,
        strategy_version_id="v1",
        instrument_id="600519",
    )
    assert len(drafts) == 2, f"应检测到 2 个事件，实际 {len(drafts)}"
    assert drafts[0].event_type == "evt_test_smoke"
    assert drafts[0].state_ttl_seconds == 1800
    assert drafts[0].allowed_roles == [EventRole.TRIGGER]
    assert "close" in drafts[0].payload
    print(f"detect_to_drafts ✓: 检测到 {len(drafts)} 个事件")
    print(f"  draft[0].payload={drafts[0].payload}")
    print(f"  draft[0].dedupe_key={drafts[0].dedupe_key}")

    # 3. 清理测试事件
    del EVENT_REGISTRY["evt_test_smoke"]
    print("OK")
