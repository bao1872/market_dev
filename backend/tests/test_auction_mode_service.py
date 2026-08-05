"""[V2.1 EPIC-05] Auction 模式决策纯函数单元测试。

运行（纯单元，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_auction_mode_service.py -q -p no:cacheprovider
"""

from __future__ import annotations

from app.domain_status import (
    ANCHOR_MODE_CHIP,
    ANCHOR_MODE_COMPOSITE,
    ANCHOR_MODE_STRUCTURE,
    AUCTION_MODE_COMPOSITE,
    AUCTION_MODE_HYBRID,
    AUCTION_MODE_STRUCTURE_ONLY,
)
from app.services.auction_mode_service import decide_auction_mode

_INSTRUMENTS = ["600000", "300369", "000001"]


def test_structure_only_when_no_chip():
    """无 chip 输入 → structure_only。"""
    d = decide_auction_mode(eligible_instruments=_INSTRUMENTS, chip_available=False)
    assert d.mode == AUCTION_MODE_STRUCTURE_ONLY
    assert d.coverage_ratio == 0.0
    assert all(m == ANCHOR_MODE_STRUCTURE for m in d.per_stock_modes.values())


def test_composite_when_all_ready():
    """全部可发布 anchor 为 composite 且无 failed/stale → composite。"""
    d = decide_auction_mode(
        eligible_instruments=_INSTRUMENTS,
        chip_ready_instruments=set(_INSTRUMENTS),
        chip_available=True,
    )
    assert d.mode == AUCTION_MODE_COMPOSITE
    assert d.coverage_ratio == 1.0
    assert d.composite_anchor_count == len(_INSTRUMENTS)


def test_hybrid_when_partial_chip():
    """部分 chip → hybrid，每股 mode 正确。"""
    d = decide_auction_mode(
        eligible_instruments=_INSTRUMENTS,
        chip_ready_instruments={"600000", "300369"},
        chip_available=True,
    )
    assert d.mode == AUCTION_MODE_HYBRID
    assert d.per_stock_modes["600000"] == ANCHOR_MODE_COMPOSITE
    assert d.per_stock_modes["300369"] == ANCHOR_MODE_COMPOSITE
    assert d.per_stock_modes["000001"] == ANCHOR_MODE_STRUCTURE
    assert d.coverage_ratio == 2 / 3


def test_no_pseudo_composite_when_stale():
    """存在 stale chip → 禁止 composite（无伪 composite），退化为 hybrid。"""
    d = decide_auction_mode(
        eligible_instruments=_INSTRUMENTS,
        chip_ready_instruments=set(_INSTRUMENTS),
        stale_instruments={"000001"},
        chip_available=True,
    )
    assert d.mode == AUCTION_MODE_HYBRID
    assert d.per_stock_modes["000001"] == ANCHOR_MODE_CHIP
    assert "failed_or_stale" in d.reason


def test_no_pseudo_composite_when_failed():
    """存在 failed chip → 禁止 composite。"""
    d = decide_auction_mode(
        eligible_instruments=_INSTRUMENTS,
        chip_ready_instruments={"600000", "300369"},
        failed_instruments={"000001"},
        chip_available=True,
    )
    assert d.mode == AUCTION_MODE_HYBRID
    assert d.per_stock_modes["000001"] == ANCHOR_MODE_CHIP


def test_hybrid_threshold_boundary_pairwise():
    """2 个标的 1 个 chip → hybrid（coverage=0.5），不误判 composite。"""
    d = decide_auction_mode(
        eligible_instruments=["600000", "300369"],
        chip_ready_instruments={"600000"},
        chip_available=True,
    )
    assert d.mode == AUCTION_MODE_HYBRID
    assert d.coverage_ratio == 0.5


def test_empty_eligible_defaults_structure_only():
    """无 eligible 标的 → structure_only，coverage=0。"""
    d = decide_auction_mode(eligible_instruments=[], chip_available=True)
    assert d.mode == AUCTION_MODE_STRUCTURE_ONLY
    assert d.coverage_ratio == 0.0
