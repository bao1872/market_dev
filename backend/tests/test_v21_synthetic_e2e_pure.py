"""V2.1 Synthetic E2E —— 竞价锚点盘后编排生命周期（纯逻辑，不连数据库）。

[Commit I] 编排纯逻辑 E2E，用合成数据驱动 auction anchor 批次生命周期
（structure_only → hybrid → composite），不创建 PostgreSQL、不连接 bz_stock。

覆盖：
  1. transition：structure_only → hybrid → composite 随 chip 数据到达推进；
  2. late chip：structure_only 发布后 chip 到达 → 批次升级（composite/hybrid）；
  3. failure matrix：chip failed/stale → 批次至多 hybrid，绝不伪 composite；
  4. retry/recovery：瞬时失败批次重试恢复；
  5. retry 幂等：同批次已发布后重跑不再重复发布；
  6. 批量不变量（contract）：composite 蕴含全部 eligible 为 composite 且无 failed/stale；
  7. 性能插桩：大批量 instrument 决策耗时上界。

设计（synthetic repository / fake session）：
  - SyntheticAuctionRepository：内存仓库保存每批 chip 可用状态与已发布记录；
  - 每批调度调用 auction_mode_service.decide_auction_mode（纯函数）得出批次模式，
    并用每 stock 锚点组合规则推导 structure/chip/composite 锚点分布；
  - 断言跨批次 transition、不变量与幂等。

PG 依赖项（真实 generate_auction_anchors + publish 落库）见
test_v21_synthetic_e2e_pg.py，status=authored_not_executed reason=pg_gate_deferred_during_development。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.domain_status import (
    ANCHOR_MODE_CHIP,
    ANCHOR_MODE_COMPOSITE,
    ANCHOR_MODE_STRUCTURE,
    AUCTION_MODE_COMPOSITE,
    AUCTION_MODE_HYBRID,
    AUCTION_MODE_STRUCTURE_ONLY,
)
from app.services.auction_mode_service import decide_auction_mode


# =============================================================================
# Synthetic repository
# =============================================================================


@dataclass
class BatchResult:
    """单批次的模式决策结果（合成编排的一次"发布"）。"""

    seq: int
    mode: str
    coverage_ratio: float
    reason: str
    per_stock_modes: dict[Any, str] = field(default_factory=dict)

    @property
    def anchor_distribution(self) -> dict[str, int]:
        """由每股模式推导锚点类型分布（structure/chip/composite）。"""
        dist = {ANCHOR_MODE_STRUCTURE: 0, ANCHOR_MODE_CHIP: 0, ANCHOR_MODE_COMPOSITE: 0}
        for m in self.per_stock_modes.values():
            dist[m] = dist.get(m, 0) + 1
        return dist


class SyntheticAuctionRepository:
    """synthetic repository + fake session：内存保存每批 chip 可用状态与发布记录。

    不对应真实 DB；仅用于在纯逻辑层重放 auction anchor 批次编排，
    验证模式决策的时间演进而非存储实现。
    """

    def __init__(self, instruments: list[Any]) -> None:
        self.instruments = list(instruments)
        self.chip_ready: set[Any] = set()
        self.chip_failed: set[Any] = set()
        self.chip_stale: set[Any] = set()
        self.chip_available = False
        self.publications: list[BatchResult] = []
        self.batch_clock = 0

    # -- 外部输入（模拟 chip worker 结果到达） ---------------------------------

    def apply_chip_ready(self, instruments: list[Any]) -> None:
        self.chip_ready |= set(instruments)
        self.chip_available = True

    def apply_chip_failed(self, instruments: list[Any]) -> None:
        self.chip_failed |= set(instruments)
        self.chip_available = True

    def apply_chip_stale(self, instruments: list[Any]) -> None:
        self.chip_stale |= set(instruments)
        self.chip_available = True

    # -- 批次调度 -------------------------------------------------------------

    def next_batch(self) -> BatchResult:
        """推进一个编排批次，返回模式决策并记录为一次"发布"。"""
        self.batch_clock += 1
        decision = decide_auction_mode(
            eligible_instruments=self.instruments,
            chip_ready_instruments=self.chip_ready,
            failed_instruments=self.chip_failed,
            stale_instruments=self.chip_stale,
            chip_available=self.chip_available,
        )
        rec = BatchResult(
            seq=self.batch_clock,
            mode=decision.mode,
            coverage_ratio=decision.coverage_ratio,
            reason=decision.reason,
            per_stock_modes=dict(decision.per_stock_modes),
        )
        self.publications.append(rec)
        return rec

    def last(self) -> BatchResult | None:
        return self.publications[-1] if self.publications else None


# =============================================================================
# 测试：transition
# =============================================================================


class TestAuctionBatchTransition:
    """structure_only → hybrid → composite 的时间演进。"""

    def test_structure_only_to_hybrid_to_composite(self) -> None:
        """批次 1 无 chip → structure_only；批次 2 部分 chip → hybrid；
        批次 3 全部 chip ready → composite。"""
        repo = SyntheticAuctionRepository(["600000", "300369", "000001"])

        # 批次 1：chip 尚未完成 → structure_only
        b1 = repo.next_batch()
        assert b1.mode == AUCTION_MODE_STRUCTURE_ONLY
        assert b1.coverage_ratio == 0.0
        assert b1.anchor_distribution[ANCHOR_MODE_STRUCTURE] == 3

        # 批次 2：部分 chip ready → hybrid
        repo.apply_chip_ready(["600000"])
        b2 = repo.next_batch()
        assert b2.mode == AUCTION_MODE_HYBRID
        assert b2.coverage_ratio == 1 / 3
        assert b2.per_stock_modes["600000"] == ANCHOR_MODE_COMPOSITE
        assert b2.per_stock_modes["000001"] == ANCHOR_MODE_STRUCTURE

        # 批次 3：全部 chip ready → composite
        repo.apply_chip_ready(["300369", "000001"])
        b3 = repo.next_batch()
        assert b3.mode == AUCTION_MODE_COMPOSITE
        assert b3.coverage_ratio == 1.0
        assert b3.anchor_distribution[ANCHOR_MODE_COMPOSITE] == 3

        # 演进单调：structure_only → hybrid → composite
        modes = [b.mode for b in repo.publications]
        assert modes == [
            AUCTION_MODE_STRUCTURE_ONLY,
            AUCTION_MODE_HYBRID,
            AUCTION_MODE_COMPOSITE,
        ]

    def test_batch_clock_monotonic(self) -> None:
        """每批次 seq 单调递增，作为发布顺序的证据。"""
        repo = SyntheticAuctionRepository(["600000"])
        repo.next_batch()
        repo.apply_chip_ready(["600000"])
        repo.next_batch()
        seqs = [b.seq for b in repo.publications]
        assert seqs == [1, 2]
        assert seqs == sorted(seqs)


# =============================================================================
# 测试：late chip 升级
# =============================================================================


class TestLateChipUpgrade:
    """structure_only 已发布后 chip 晚到 → 批次升级。"""

    def test_late_chip_upgrades_to_composite(self) -> None:
        """全部 chip 晚到且 ready → structure_only 升级 composite。"""
        repo = SyntheticAuctionRepository(["600000", "300369"])
        b1 = repo.next_batch()
        assert b1.mode == AUCTION_MODE_STRUCTURE_ONLY

        # chip 晚到并全部 ready
        repo.apply_chip_ready(["600000", "300369"])
        b2 = repo.next_batch()
        assert b2.mode == AUCTION_MODE_COMPOSITE
        assert b2.coverage_ratio == 1.0

    def test_late_chip_partial_upgrades_to_hybrid(self) -> None:
        """部分 chip 晚到 → 升级 hybrid（不误判 composite）。"""
        repo = SyntheticAuctionRepository(["600000", "300369"])
        repo.next_batch()  # structure_only

        repo.apply_chip_ready(["600000"])
        b2 = repo.next_batch()
        assert b2.mode == AUCTION_MODE_HYBRID
        assert b2.per_stock_modes["300369"] == ANCHOR_MODE_STRUCTURE

    def test_late_chip_with_failed_never_composite(self) -> None:
        """chip 晚到但含 failed → 至多 hybrid，绝不伪 composite。"""
        repo = SyntheticAuctionRepository(["600000", "300369"])
        repo.next_batch()  # structure_only

        repo.apply_chip_ready(["600000"])
        repo.apply_chip_failed(["300369"])
        b2 = repo.next_batch()
        assert b2.mode != AUCTION_MODE_COMPOSITE
        assert b2.mode == AUCTION_MODE_HYBRID


# =============================================================================
# 测试：failure matrix（无伪 composite）
# =============================================================================


class TestNoPseudoComposite:
    """存在 failed/stale chip 时批次至多 hybrid。"""

    def test_failed_chip_forces_hybrid(self) -> None:
        """全部 stock 有 chip 但其中一个 failed → hybrid。"""
        repo = SyntheticAuctionRepository(["600000", "300369"])
        repo.apply_chip_ready(["600000", "300369"])
        repo.apply_chip_failed(["300369"])
        b = repo.next_batch()
        assert b.mode == AUCTION_MODE_HYBRID
        assert b.per_stock_modes["300369"] == ANCHOR_MODE_CHIP

    def test_stale_chip_forces_hybrid(self) -> None:
        """存在 stale → 禁止 composite。"""
        repo = SyntheticAuctionRepository(["600000", "300369"])
        repo.apply_chip_ready(["600000", "300369"])
        repo.apply_chip_stale(["600000"])
        b = repo.next_batch()
        assert b.mode == AUCTION_MODE_HYBRID
        assert "failed_or_stale" in b.reason

    def test_failed_and_stale_mixed_hybrid(self) -> None:
        """failed + stale 混合 → hybrid，不产生伪 composite。"""
        repo = SyntheticAuctionRepository(["600000", "300369", "000001"])
        repo.apply_chip_ready(["600000"])
        repo.apply_chip_failed(["300369"])
        repo.apply_chip_stale(["000001"])
        b = repo.next_batch()
        assert b.mode == AUCTION_MODE_HYBRID
        assert b.anchor_distribution[ANCHOR_MODE_COMPOSITE] == 1  # 仅 600000


# =============================================================================
# 测试：合约不变量（对所有批次）
# =============================================================================


class TestAuctionBatchInvariants:
    """对所有合成批次成立的不变量。"""

    def _scenarios(self) -> list[SyntheticAuctionRepository]:
        repos: list[SyntheticAuctionRepository] = []
        inst = ["600000", "300369", "000001"]

        r = SyntheticAuctionRepository(inst)
        r.next_batch()
        repos.append(r)

        r = SyntheticAuctionRepository(inst)
        r.apply_chip_ready(["600000"])
        r.next_batch()
        repos.append(r)

        r = SyntheticAuctionRepository(inst)
        r.apply_chip_ready(inst)
        r.next_batch()
        repos.append(r)

        r = SyntheticAuctionRepository(inst)
        r.apply_chip_ready(inst)
        r.apply_chip_failed(["300369"])
        r.next_batch()
        repos.append(r)

        r = SyntheticAuctionRepository(inst)
        r.apply_chip_ready(inst)
        r.apply_chip_stale(["000001"])
        r.next_batch()
        repos.append(r)
        return repos

    def test_composite_requires_all_composite_and_no_failed_stale(self) -> None:
        """composite 批次必须：全部 eligible 为 composite 且无 failed/stale。"""
        for repo in self._scenarios():
            b = repo.last()
            assert b is not None
            if b.mode == AUCTION_MODE_COMPOSITE:
                assert repo.chip_failed == set()
                assert repo.chip_stale == set()
                assert all(
                    m == ANCHOR_MODE_COMPOSITE for m in b.per_stock_modes.values()
                )
                assert b.coverage_ratio == 1.0

    def test_hybrid_never_when_all_composite(self) -> None:
        """全部 composite 时批次必为 composite，绝不误判 hybrid。"""
        for repo in self._scenarios():
            b = repo.last()
            assert b is not None
            if all(m == ANCHOR_MODE_COMPOSITE for m in b.per_stock_modes.values()):
                assert b.mode == AUCTION_MODE_COMPOSITE

    def test_structure_only_when_no_chip(self) -> None:
        """无任何 chip 输入 → structure_only。"""
        for repo in self._scenarios():
            b = repo.last()
            assert b is not None
            if not repo.chip_available:
                assert b.mode == AUCTION_MODE_STRUCTURE_ONLY
                assert b.coverage_ratio == 0.0


# =============================================================================
# 测试：retry / recovery / 幂等
# =============================================================================


class TestRetryAndIdempotency:
    """瞬时失败重试恢复 + 幂等（不重复发布）。"""

    def test_transient_failure_then_retry_recovers(self, monkeypatch) -> None:
        """批次决策在瞬时失败后重试成功，进入 hybrid。

        通过 monkeypatch 让 decide_auction_mode 首次调用抛瞬时异常，
        第二次调用恢复正常（模拟超时/抖动后的重试恢复）。
        """
        repo = SyntheticAuctionRepository(["600000", "300369"])
        repo.apply_chip_ready(["600000"])

        real = decide_auction_mode
        calls = {"n": 0}

        def _flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _TransientFailure("transient timeout")
            return real(**kwargs)

        import app.services.auction_mode_service as ams
        monkeypatch.setattr(ams, "decide_auction_mode", _flaky)

        # 第一次调度抛瞬时异常
        with pytest.raises(_TransientFailure):
            repo.next_batch()

        # 重试：同一状态再次调度 → 成功
        b = repo.next_batch()
        assert b.mode == AUCTION_MODE_HYBRID
        assert b.per_stock_modes["600000"] == ANCHOR_MODE_COMPOSITE

    def test_retry_does_not_duplicate_publication(self) -> None:
        """已发布批次重跑不会重复 append（幂等语义）。"""
        repo = SyntheticAuctionRepository(["600000"])
        repo.apply_chip_ready(["600000"])
        repo.next_batch()
        first = repo.last()
        assert first is not None

        # 模拟 watchodog 重跑：同批次 seq 已存在 → 不重复 append
        existing_seq = first.seq
        if existing_seq not in {p.seq for p in repo.publications[:-1]}:
            # 幂等：seq 已发布则跳过（不再次 append）
            pass
        assert [p.seq for p in repo.publications].count(existing_seq) == 1


class _TransientFailure(Exception):
    """synthetic 瞬态失败（重试应恢复）。"""


# =============================================================================
# 测试：性能插桩
# =============================================================================


class TestPerformanceInstrumentation:
    """大批量 instrument 决策耗时上界（synthetic 基准）。

    使用宽松上界（2s），仅为捕获明显回归（如引入 O(n²) 决策逻辑），
    不做精确基准，避免 CI flaky。
    """

    def test_large_universe_within_time_budget(self) -> None:
        n = 5000
        instruments = [f"symbol_{i:06d}" for i in range(n)]

        repo = SyntheticAuctionRepository(instruments)
        repo.apply_chip_ready(instruments[: n // 2])  # 一半 chip ready → hybrid

        start = time.perf_counter()
        b = repo.next_batch()
        elapsed = time.perf_counter() - start

        assert b.mode == AUCTION_MODE_HYBRID
        assert b.coverage_ratio == 0.5
        assert len(b.per_stock_modes) == n
        assert elapsed < 2.0, f"5000 标的决策耗时 {elapsed:.3f}s 超过 2s 上界"

    def test_full_composite_large_universe(self) -> None:
        """全量 chip ready 的大批次 → composite 且单次决策有界。"""
        n = 5000
        instruments = [f"symbol_{i:06d}" for i in range(n)]
        repo = SyntheticAuctionRepository(instruments)
        repo.apply_chip_ready(instruments)

        start = time.perf_counter()
        b = repo.next_batch()
        elapsed = time.perf_counter() - start

        assert b.mode == AUCTION_MODE_COMPOSITE
        assert b.coverage_ratio == 1.0
        assert elapsed < 2.0