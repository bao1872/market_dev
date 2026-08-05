"""Auction 模式决策（EPIC-05 E05-T09/T10/T11）。

[PRD V2.1 §8.3 / next.md EPIC-05]
- auction 支持 structure_only / hybrid / composite 三种模式。
- composite（E05-T11）：只有全部可发布 anchor 为 composite 且无 failed/stale 才成立。
- hybrid（E05-T10）：部分 chip 可用时，每股 mode + 批次 hybrid + coverage，无伪 composite。
- structure_only（E05-T09）：stock_core 发布后即可发布，无 chip。

本模块为**纯函数**，不连接数据库，可 PURE_UNIT_TEST=1 测试。
决策输入由调用方（auction_anchor_service / orchestrator）从 chip run 状态聚合提供。

决策规则（每股）：
- ANCHOR_MODE_COMPOSITE：该股 chip ready 且无 failed/stale
- ANCHOR_MODE_CHIP：该股有 chip 但非 composite（如 chip 部分可用/含 stale）
- ANCHOR_MODE_STRUCTURE：该股无 chip（仅结构锚点）

批次模式：
- 无任何 chip → structure_only
- 全部可发布 anchor 为 composite 且无 failed/stale、无 structure-only admin 强制 → composite
- 否则 → hybrid（coverage = composite_anchor_count / eligible_count）

禁止“伪 composite”：只要存在 failed/stale chip 或任一 stock 无可发布 composite，
批次模式最多为 hybrid，绝不标 composite。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain_status import (
    ANCHOR_MODE_CHIP,
    ANCHOR_MODE_COMPOSITE,
    ANCHOR_MODE_STRUCTURE,
    AUCTION_MODE_COMPOSITE,
    AUCTION_MODE_HYBRID,
    AUCTION_MODE_STRUCTURE_ONLY,
)


@dataclass(frozen=True)
class AuctionModeDecision:
    """竞价比次模式决策结果。

    - mode: structure_only / hybrid / composite
    - per_stock_modes: {instrument_id: "structure" | "chip" | "composite"}
    - coverage_ratio: composite_anchor_count / eligible_count（hybrid 时为核心指标）
    - structure_anchor_count / chip_anchor_count / composite_anchor_count
    - reason: 决策原因（如 "all_composite" / "partial_chip" / "no_chip"）
    """

    mode: str
    per_stock_modes: dict[Any, str] = field(default_factory=dict)
    coverage_ratio: float = 0.0
    structure_anchor_count: int = 0
    chip_anchor_count: int = 0
    composite_anchor_count: int = 0
    reason: str = ""


def _decide_per_stock_mode(
    *,
    has_chip: bool,
    chip_ready: bool,
    chip_failed: bool,
    chip_stale: bool,
) -> str:
    """每股锚点模式（E05-T10）。

    - composite：chip 存在且 ready 且未 failed/stale
    - chip：chip 存在但非 composite（partially ready / 含 stale / 未完全 ready）
    - structure：无 chip
    """
    if not has_chip:
        return ANCHOR_MODE_STRUCTURE
    if chip_ready and not chip_failed and not chip_stale:
        return ANCHOR_MODE_COMPOSITE
    return ANCHOR_MODE_CHIP


def decide_auction_mode(
    *,
    eligible_instruments: list[Any],
    chip_ready_instruments: set[Any] | None = None,
    failed_instruments: set[Any] | None = None,
    stale_instruments: set[Any] | None = None,
    chip_available: bool = False,
) -> AuctionModeDecision:
    """决定竞价批次模式（E05-T09/T10/T11）。

    Args:
        eligible_instruments: 全部可发布 anchor 的 instrument 列表
        chip_ready_instruments: chip ready 的 instrument 集合
        failed_instruments: chip failed 的 instrument 集合
        stale_instruments: chip stale 的 instrument 集合
        chip_available: 本批次是否有任何 chip 输入（False → forced structure_only）

    Returns:
        AuctionModeDecision
    """
    chip_ready = set(chip_ready_instruments or set())
    failed = set(failed_instruments or set())
    stale = set(stale_instruments or set())

    eligible_set = set(eligible_instruments)
    eligible_count = len(eligible_set)

    per_stock_modes: dict[Any, str] = {}
    composite_count = 0
    chip_count = 0
    structure_count = 0

    for inst in eligible_instruments:
        has_chip = inst in chip_ready or inst in failed or inst in stale
        mode = _decide_per_stock_mode(
            has_chip=has_chip,
            chip_ready=inst in chip_ready,
            chip_failed=inst in failed,
            chip_stale=inst in stale,
        )
        per_stock_modes[inst] = mode
        if mode == ANCHOR_MODE_COMPOSITE:
            composite_count += 1
        elif mode == ANCHOR_MODE_CHIP:
            chip_count += 1
        else:
            structure_count += 1

    coverage_ratio = composite_count / eligible_count if eligible_count > 0 else 0.0

    # 无任何 chip → structure_only
    if not chip_available or (composite_count == 0 and chip_count == 0):
        return AuctionModeDecision(
            mode=AUCTION_MODE_STRUCTURE_ONLY,
            per_stock_modes=per_stock_modes,
            coverage_ratio=0.0,
            structure_anchor_count=structure_count,
            chip_anchor_count=0,
            composite_anchor_count=0,
            reason="no_chip",
        )

    # 存在 failed/stale → 禁止 composite（无伪 composite）
    if failed or stale:
        return AuctionModeDecision(
            mode=AUCTION_MODE_HYBRID,
            per_stock_modes=per_stock_modes,
            coverage_ratio=coverage_ratio,
            structure_anchor_count=structure_count,
            chip_anchor_count=chip_count,
            composite_anchor_count=composite_count,
            reason="hybrid_failed_or_stale",
        )

    # 全部可发布 anchor 为 composite → composite
    if eligible_count > 0 and chip_count == 0 and structure_count == 0 and composite_count == eligible_count:
        return AuctionModeDecision(
            mode=AUCTION_MODE_COMPOSITE,
            per_stock_modes=per_stock_modes,
            coverage_ratio=1.0,
            structure_anchor_count=0,
            chip_anchor_count=0,
            composite_anchor_count=composite_count,
            reason="all_composite",
        )

    # 部分 chip（hybrid）
    return AuctionModeDecision(
        mode=AUCTION_MODE_HYBRID,
        per_stock_modes=per_stock_modes,
        coverage_ratio=coverage_ratio,
        structure_anchor_count=structure_count,
        chip_anchor_count=chip_count,
        composite_anchor_count=composite_count,
        reason="partial_chip",
    )


if __name__ == "__main__":
    # structure_only
    d1 = decide_auction_mode(
        eligible_instruments=["600000", "300369"],
        chip_available=False,
    )
    assert d1.mode == AUCTION_MODE_STRUCTURE_ONLY, d1.mode

    # composite：全部 chip ready，无 failed/stale
    d2 = decide_auction_mode(
        eligible_instruments=["600000", "300369"],
        chip_ready_instruments={"600000", "300369"},
        chip_available=True,
    )
    assert d2.mode == AUCTION_MODE_COMPOSITE, d2.mode
    assert d2.coverage_ratio == 1.0

    # hybrid：部分 chip
    d3 = decide_auction_mode(
        eligible_instruments=["600000", "300369"],
        chip_ready_instruments={"600000"},
        chip_available=True,
    )
    assert d3.mode == AUCTION_MODE_HYBRID, d3.mode
    assert d3.per_stock_modes["600000"] == ANCHOR_MODE_COMPOSITE
    assert d3.per_stock_modes["300369"] == ANCHOR_MODE_STRUCTURE

    # 有 stale → 禁 composite
    d4 = decide_auction_mode(
        eligible_instruments=["600000", "300369"],
        chip_ready_instruments={"600000", "300369"},
        stale_instruments={"300369"},
        chip_available=True,
    )
    assert d4.mode == AUCTION_MODE_HYBRID, d4.mode
    assert "failed_or_stale" in d4.reason

    print("OK: auction mode decision verified")
