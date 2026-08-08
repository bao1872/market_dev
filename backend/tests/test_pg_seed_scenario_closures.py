"""PG Seed 六态 canonical 场景闭包**严格**断言（PANJI_REMOTE_VERIFY_DB_TEST=1）。

[审查报告修订 / 六状态事实证明] 本文件替换原「四场景 + 允许多个 closure」的宽松断言。
每个场景有且**仅有一个**预期 closure；不匹配即失败，不再用 `assert closure in (...)`
掩盖语义错位。

在远程验证库（bz_stock_verify_<sha>）已运行 `scripts/verify/seed_v21_verify_data.py
--scenario all` 后，对六个 canonical 场景各自的固定交易日调用真实入口
`ProductReadinessService().collect_states` + `evaluate_closure`，断言：

| 场景                          | 交易日     | 唯一预期 closure          |
|-------------------------------|-----------|---------------------------|
| pending_no_core               | 2026-07-28 | pending                   |
| blocked_mandatory_failure     | 2026-07-29 | blocked                   |
| core_ready_waiting_mandatory  | 2026-07-30 | core_ready                |
| mandatory_ready_enhancing     | 2026-07-31 | mandatory_ready_enhancing |
| degraded_terminal_partial     | 2026-08-03 | degraded_ready            |
| fully_ready_all_fresh         | 2026-08-04 | fully_ready               |

分层设计（审查第七节）：
- **Pure Unit**（tests/test_product_readiness_service.py）：手工 ProductReadinessState[]
  → evaluate_closure，证明状态机本身正确。
- **PG E2E**（本文件）：真实 DB 事实 → collect_states → evaluate_closure，
  证明 Seed 产生的事实与状态机语义一致。
- 两层共享 `tests/readiness_fixtures.py` 的**期望值**（只共享事实与期望，不共享算法）。

本测试**只读**：绝不写库。PURE_UNIT_TEST=1 时 skip。
"""
from __future__ import annotations

import os
from datetime import date

import pytest

from app.domain_status import (
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_MANDATORY_READY_ENHANCING,
    CLOSURE_PENDING,
    READINESS_READY,
    READINESS_READY_REUSED,
)
from app.services.product_readiness_service import (
    ProductReadinessService,
    evaluate_closure,
)

_PURE_UNIT_TEST = os.environ.get("PURE_UNIT_TEST", "0") == "1"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        _PURE_UNIT_TEST,
        reason="PG Seed 闭包场景测试需远程验证库（PANJI_REMOTE_VERIFY_DB_TEST=1）",
    ),
]

# 六态 canonical 场景交易日（与 scripts/verify/seed_v21_verify_data.py
# _SCENARIO_TRADE_DATES 严格一致；任一侧改动必须同步，否则本测试失败）。
SCENARIO_TRADE_DATES: dict[str, date] = {
    "pending_no_core": date(2026, 7, 28),
    "blocked_mandatory_failure": date(2026, 7, 29),
    "core_ready_waiting_mandatory": date(2026, 7, 30),
    "mandatory_ready_enhancing": date(2026, 7, 31),
    "degraded_terminal_partial": date(2026, 8, 3),
    "fully_ready_all_fresh": date(2026, 8, 4),
}

# 唯一预期 closure（与 tests/readiness_fixtures.CANONICAL_SCENARIOS 对齐）。
SCENARIO_EXPECTED_CLOSURE: dict[str, str] = {
    "pending_no_core": CLOSURE_PENDING,
    "blocked_mandatory_failure": CLOSURE_BLOCKED,
    "core_ready_waiting_mandatory": CLOSURE_CORE_READY,
    "mandatory_ready_enhancing": CLOSURE_MANDATORY_READY_ENHANCING,
    "degraded_terminal_partial": CLOSURE_DEGRADED_READY,
    "fully_ready_all_fresh": CLOSURE_FULLY_READY,
}

MANDATORY_PRODUCTS = (
    "daily_facts", "board_facts", "stock_core", "board_aggregation", "review",
)
ENHANCEMENT_PRODUCTS = ("dsa_projection", "chip", "state_events", "auction_anchor")


async def _states(db_session, trade_date: date):
    """真实入口聚合（只读）。"""
    return await ProductReadinessService().collect_states(db_session, trade_date)


def _by_product(states) -> dict:
    return {s.product: s for s in states}


def _diagnose(scenario: str, trade_date: date, states, ev) -> str:
    """失败时输出可定位的结构化诊断（有界，不打印随机 UUID 全量）。"""
    lines = [
        f"scenario={scenario} trade_date={trade_date}",
        f"expected={SCENARIO_EXPECTED_CLOSURE[scenario]} actual={ev.closure}",
        f"mandatory_ready={ev.mandatory_products_ready} "
        f"mandatory_full_fresh={ev.mandatory_products_full_fresh} "
        f"enhancement_terminal={ev.enhancement_jobs_terminal}",
        "nodes:",
    ]
    for s in sorted(states, key=lambda x: x.product):
        lines.append(
            f"  {s.product}: readiness={s.readiness} freshness={s.freshness} "
            f"mandatory={s.is_mandatory} terminal={s.is_terminal} "
            f"consumable={s.is_consumable} fully_fresh={s.is_fully_fresh} "
            f"product_ready={s.is_product_ready} auction_mode={s.auction_mode} "
            f"reason={s.lineage.get('reason_code')}"
        )
    if ev.issues:
        lines.append(f"issues={ev.issues[:5]}")
    return "\n".join(lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", list(SCENARIO_TRADE_DATES))
async def test_pg_seed_scenario_closure_is_exact(db_session, scenario: str) -> None:
    """六态严格一对一断言：每个场景有且仅有一个预期 closure。"""
    trade_date = SCENARIO_TRADE_DATES[scenario]
    states = await _states(db_session, trade_date)
    ev = evaluate_closure(states)
    expected = SCENARIO_EXPECTED_CLOSURE[scenario]
    assert ev.closure == expected, _diagnose(scenario, trade_date, states, ev)


@pytest.mark.asyncio
async def test_pg_seed_all_nine_nodes_present_per_scenario(db_session) -> None:
    """collect_states 必须返回全部九个节点（缺节点会让 closure 判定失真）。"""
    expected_products = set(MANDATORY_PRODUCTS) | set(ENHANCEMENT_PRODUCTS)
    for scenario, trade_date in SCENARIO_TRADE_DATES.items():
        states = await _states(db_session, trade_date)
        got = {s.product for s in states}
        missing = expected_products - got
        assert not missing, f"{scenario}@{trade_date} 缺少节点: {sorted(missing)}"


# ---------------------------------------------------------------------------
# 负例（审查第七节）：证明关键事实缺失会真实降级，而非被宽松断言掩盖
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pg_seed_pending_scenario_has_no_consumable_stock_core(db_session) -> None:
    """负例：pending 场景的 stock_core 必须不可消费（否则 pending 语义是假的）。"""
    trade_date = SCENARIO_TRADE_DATES["pending_no_core"]
    node = _by_product(await _states(db_session, trade_date))["stock_core"]
    assert not node.is_consumable, (
        f"pending_no_core@{trade_date} 的 stock_core 竟然可消费："
        f"readiness={node.readiness} lineage={node.lineage}"
    )


@pytest.mark.asyncio
async def test_pg_seed_blocked_scenario_has_terminal_unavailable_mandatory(db_session) -> None:
    """负例：blocked 场景必须存在 terminal 且不可消费的 mandatory 节点。

    [如实标注] Seed 在 board_facts_runs.error_code 写 EXTERNAL_GATE_UNSATISFIED，
    但 ProductReadinessService._board_facts_state 自行生成 lineage.reason_code=RUN_FAILED
    （service 不读 error_code）。这里断言 service 真实产出的语义，不假设 Seed 的字面值。
    """
    trade_date = SCENARIO_TRADE_DATES["blocked_mandatory_failure"]
    states = await _states(db_session, trade_date)
    blocking = [
        s for s in states
        if s.is_mandatory and s.is_terminal and not s.is_consumable
    ]
    assert blocking, (
        f"blocked_mandatory_failure@{trade_date} 无 terminal+unavailable 的 mandatory 节点，"
        f"blocked 语义不成立\n{_diagnose('blocked_mandatory_failure', trade_date, states, evaluate_closure(states))}"
    )
    assert any(s.product == "board_facts" for s in blocking), (
        f"blocked 应由 board_facts 触发，实际阻塞节点={[s.product for s in blocking]}"
    )


@pytest.mark.asyncio
async def test_pg_seed_core_ready_scenario_missing_mandatory_is_not_terminal_failure(
    db_session,
) -> None:
    """负例：core_ready 必须来自 mandatory 未完成（pending），而非 terminal failure。

    若缺失的 mandatory 是 terminal failure，正确 closure 应是 blocked 而非 core_ready。
    """
    trade_date = SCENARIO_TRADE_DATES["core_ready_waiting_mandatory"]
    states = await _states(db_session, trade_date)
    by = _by_product(states)
    assert by["stock_core"].is_consumable, (
        f"core_ready@{trade_date} 的 stock_core 必须可消费：{by['stock_core'].readiness}"
    )
    unfinished = [s for s in states if s.is_mandatory and not s.is_consumable]
    assert unfinished, f"core_ready@{trade_date} 竟然所有 mandatory 都已可消费"
    assert all(not s.is_terminal for s in unfinished), (
        "core_ready 场景存在 terminal failure 的 mandatory 节点，应判 blocked："
        f"{[(s.product, s.readiness, s.lineage.get('reason_code')) for s in unfinished]}"
    )


@pytest.mark.asyncio
async def test_pg_seed_mandatory_ready_enhancing_has_non_terminal_enhancement(
    db_session,
) -> None:
    """负例：mandatory_ready_enhancing 必须真有 enhancement 未终态。"""
    trade_date = SCENARIO_TRADE_DATES["mandatory_ready_enhancing"]
    states = await _states(db_session, trade_date)
    assert all(s.is_consumable for s in states if s.is_mandatory), (
        f"mandatory_ready_enhancing@{trade_date} 的 mandatory 未全部可消费"
    )
    non_terminal = [s for s in states if not s.is_mandatory and not s.is_terminal]
    assert non_terminal, (
        f"mandatory_ready_enhancing@{trade_date} 无未终态 enhancement，"
        "该 closure 语义不成立"
    )


@pytest.mark.asyncio
async def test_pg_seed_degraded_scenario_enhancements_all_terminal_but_not_all_ready(
    db_session,
) -> None:
    """负例：degraded_ready 必须是「enhancement 全终态但非全 truly ready」。"""
    trade_date = SCENARIO_TRADE_DATES["degraded_terminal_partial"]
    states = await _states(db_session, trade_date)
    enh = [s for s in states if not s.is_mandatory]
    assert enh, f"degraded@{trade_date} 无 enhancement 节点"
    assert all(s.is_terminal for s in enh), (
        f"degraded@{trade_date} enhancement 未全部终态："
        f"{[(s.product, s.readiness) for s in enh if not s.is_terminal]}"
    )
    not_truly_ready = [
        s for s in enh
        if s.is_product_ready is False
        or (s.product == "auction_anchor" and s.auction_mode != "composite")
    ]
    assert not_truly_ready, (
        f"degraded@{trade_date} enhancement 全部 truly ready，应判 fully_ready 而非 degraded"
    )


@pytest.mark.asyncio
async def test_pg_seed_fully_ready_scenario_is_all_fresh_and_composite(db_session) -> None:
    """负例：fully_ready 必须 mandatory 全 fresh + auction composite（约束6 严格）。"""
    trade_date = SCENARIO_TRADE_DATES["fully_ready_all_fresh"]
    states = await _states(db_session, trade_date)
    by = _by_product(states)
    stale = [
        s for s in states
        if s.is_mandatory and not s.is_fully_fresh
    ]
    assert not stale, (
        f"fully_ready@{trade_date} 存在非 fully_fresh 的 mandatory 节点："
        f"{[(s.product, s.readiness, s.freshness) for s in stale]}"
    )
    assert by["board_facts"].readiness == READINESS_READY, (
        "约束6：fully_ready 的 board_facts 必须是 fresh ready（不接受 reused）："
        f"{by['board_facts'].readiness}"
    )
    assert by["board_facts"].readiness != READINESS_READY_REUSED
    auction = by["auction_anchor"]
    assert auction.auction_mode == "composite", (
        f"fully_ready@{trade_date} 的 auction_mode={auction.auction_mode}，必须是 composite"
    )


@pytest.mark.asyncio
async def test_pg_seed_board_aggregation_pointer_lineage_matches_stock_core(
    db_session,
) -> None:
    """lineage 关系断言（审查第七节：比较关系而非随机 UUID）。

    board_aggregation 的 source_core_run_id 必须等于同日 stock_core 的 pointer data_run_id，
    否则说明跨 universe 拼接（约束5 违规）。
    """
    for scenario in (
        "mandatory_ready_enhancing", "degraded_terminal_partial", "fully_ready_all_fresh",
    ):
        trade_date = SCENARIO_TRADE_DATES[scenario]
        by = _by_product(await _states(db_session, trade_date))
        agg, core = by.get("board_aggregation"), by.get("stock_core")
        if agg is None or core is None or not agg.is_consumable:
            continue
        agg_source = agg.lineage.get("source_core_run_id")
        core_pointer = (
            core.lineage.get("pointer_data_run_id") or core.lineage.get("run_id")
        )
        if agg_source is None or core_pointer is None:
            continue
        assert str(agg_source) == str(core_pointer), (
            f"{scenario}@{trade_date} board_aggregation.source_core_run_id={agg_source} "
            f"≠ stock_core pointer={core_pointer}（跨 universe 拼接，约束5 违规）"
        )
