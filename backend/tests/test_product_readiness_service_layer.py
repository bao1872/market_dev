"""[V2.1 EPIC-08] ProductReadinessService 动态聚合服务层单元测试（P0 修正版）。

覆盖：
- P0-1：九节点完整纳入（daily_facts/state_events/dsa_projection）
- P0-2：product readiness 由 publication pointer 决定，latest run 单列
- P0-3：terminal 与 consumable 分离（chip 失败不再永久"仍在运行"）
- P0-4：stock_core 未形成 → pending；stock_core ready 但 review 未完成 → core_ready
- [Corrective-3.1 §P1] review 必须读正式 market_review pointer；dsa/state_events
  必须按当前 core run 精确归属，不得误判 ready。

[Corrective-3.1 收口] 旧实现采用"线性 pop(0)" mock，与当前服务层查询结构
（review 先查 FactorPublication(market_review) + MarketReviewRun；dsa 用 db.scalar
计数；state_events 用 db.execute）不一致，导致用例错位失败。本文件改为**按查询
实体模型 + publication_kind 路由**的 _FakeDB，每个用例用节点计划（plan）表达意图，
与真实查询契约对齐且不再脆弱。

运行（纯单元，mock DB，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_product_readiness_service_layer.py -q -p no:cacheprovider
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from sqlalchemy import BinaryExpression
from sqlalchemy.sql.elements import BooleanClauseList

from app.domain_status import (
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_PENDING,
    READINESS_PENDING,
    READINESS_READY,
)
from app.models.auction_anchor_run import AuctionAnchorRun
from app.models.board_facts_run import BoardFactsRun
from app.models.chip_consensus_run import ChipConsensusRun
from app.models.factor_publication import (
    PUBLICATION_KIND_AUCTION_ANCHOR,
    PUBLICATION_KIND_BOARD_FACTS,
    PUBLICATION_KIND_CHIP_CONSENSUS,
    PUBLICATION_KIND_HISTORY_CROSS_SECTION,
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.market_review import MarketReviewRun
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_state_event import StockStateEvent
from app.services.product_readiness_service import (
    ProductReadinessService,
    evaluate_closure,
)
from app.services.review_publication_service import PUBLICATION_KIND_MARKET_REVIEW

_DAILY = PUBLICATION_KIND_HISTORY_CROSS_SECTION
_BOARD_FACTS = PUBLICATION_KIND_BOARD_FACTS
_STOCK_CORE = PUBLICATION_KIND_STOCK_CORE
_BOARD_AGG = PUBLICATION_KIND_MARKET_AGGREGATION
_CHIP = PUBLICATION_KIND_CHIP_CONSENSUS
_AUCTION = PUBLICATION_KIND_AUCTION_ANCHOR
_REVIEW = PUBLICATION_KIND_MARKET_REVIEW

_DRID = "00000000-0000-0000-0000-000000000001"


class _FakeResult:
    """db.execute 的假结果，支持 .all() 返回元组列表。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


# 表名 → ORM 模型类（用于按查询实体路由，避免依赖 _raw_columns/mapper 的内部结构）
_TABLE_TO_MODEL: dict[str, type] = {
    "factor_publications": FactorPublication,
    "board_facts_runs": BoardFactsRun,
    "market_reviews": MarketReviewRun,
    "chip_consensus_runs": ChipConsensusRun,
    "auction_anchor_runs": AuctionAnchorRun,
    "stock_feature_snapshots": StockFeatureSnapshot,
    "stock_state_events": StockStateEvent,
}


def _entity_class(stmt):
    """从 select 语句提取查询的 ORM 实体类。

    本 SQLAlchemy 版本下 select(Model) 的 froms 直接是 Table（无 .entity），
    故用表名映射到已注册模型类。
    """
    for frm in stmt.get_final_froms():
        name = getattr(frm, "name", None)
        if name in _TABLE_TO_MODEL:
            return _TABLE_TO_MODEL[name]
    return None


def _extract_kind(stmt):
    """从 FactorPublication 查询的 where 条件提取 publication_kind 比较值。"""
    return _find_equality_value(stmt, "publication_kind")


def _extract_source_run_filter(stmt):
    """从 state_events 查询的 where 条件提取 source_run_id 比较值（若存在）。"""
    return _find_equality_value(stmt, "source_run_id")


def _find_equality_value(stmt, col_key):
    """遍历 where 条件，找 `Column[col_key] == value` 的比较值。"""
    wc = stmt.whereclause

    def _walk(node):
        if isinstance(node, BinaryExpression):
            left = node.left
            if getattr(left, "key", None) == col_key:
                return getattr(node.right, "value", None)
        if isinstance(node, BooleanClauseList):
            for child in node.clauses:
                found = _walk(child)
                if found is not None:
                    return found
        return None

    return _walk(wc)


class _FakeDB:
    """按查询实体模型 + publication_kind 路由的假 DB。

    plan 结构：
      pubs: dict[kind] -> list[pub|None]  （每个 kind 的 FactorPublication 查询 FIFO）
      runs: dict[model] -> list[run|None]（领域 run 查询 FIFO）
      dsa_counts: (total_int, matched_int)（StockFeatureSnapshot 两次 scalar）
      state_event_rows: list[tuple]（StockStateEvent execute 的 .all() 结果）
    """

    def __init__(self, plan):
        self._pubs = plan.get("pubs", {})
        self._runs = plan.get("runs", {})
        self._dsa = list(plan.get("dsa_counts", (10, 10)))
        self._state_events = plan.get("state_event_rows", [])
        self._dsa_idx = 0

    async def scalar(self, stmt):
        ent = _entity_class(stmt)
        if ent is FactorPublication:
            kind = _extract_kind(stmt)
            q = self._pubs.get(kind, [])
            return q.pop(0) if q else None
        if ent is StockFeatureSnapshot:
            val = self._dsa[self._dsa_idx]
            self._dsa_idx += 1
            return val
        q = self._runs.get(ent, [])
        return q.pop(0) if q else None

    async def execute(self, stmt):
        ent = _entity_class(stmt)
        if ent is StockStateEvent:
            # state_events 查询：第一次全量（2 列），第二次按 source_run_id 过滤（3 列）。
            # mock 在 Python 层模拟 source_run_id 过滤，使 matched/total 与真实 SQL 一致。
            core_run = _extract_source_run_filter(stmt)
            cols = list(stmt.selected_columns)
            ncols = len(cols)
            out = []
            for row in self._state_events:
                if core_run is not None and row[1] != core_run:
                    continue
                if ncols == 2:
                    out.append((row[0], row[2]))
                else:
                    out.append((row[0], "v1", row[2]))
            return _FakeResult(out)
        return _FakeResult([])


def _pub(kind=_DAILY, drid=_DRID):
    """非 None 即视为存在发布指针；data_run_id 供领域 run 联查。

    source_core_run_id 供 stock_core lineage 透传，使下游 state_events 的
    source_run_id 匹配检查可对齐。
    """
    return SimpleNamespace(
        data_run_id=drid,
        id=drid,
        publication_kind=kind,
        status="published",
        algorithm_version="v1",
        published_at=__import__("datetime").datetime(2026, 1, 1),
        parameter_hash="ph1",
        coverage_ratio=0.99,
        source_core_run_id="c1",
    )


def _bf_run(status="published", drid=_DRID):
    return SimpleNamespace(id=drid, status=status, data_run_id=drid,
                           source_core_run_id="c1", source_board_run_id="b1",
                           algorithm_version="v1", parameter_hash="ph1",
                           coverage_ratio=0.99, finished_at=None, created_at=None)


def _review_run(status="published", drid=_DRID):
    return SimpleNamespace(id=drid, status=status, data_run_id=drid,
                           algorithm_version="v1", filter_version="fv1",
                           coverage_ratio=0.99, published_at=None,
                           completed_at=None, created_at=None)


def _chip_run(status="succeeded", drid=_DRID):
    return SimpleNamespace(id=drid, status=status, data_run_id=drid,
                           source_core_run_id="c1", source_board_run_id="b1",
                           algorithm_version="v1", parameter_hash="ph1",
                           coverage_ratio=0.99, finished_at=None, created_at=None)


def _auction_run(status="succeeded", drid=_DRID):
    return SimpleNamespace(id=drid, status=status, data_run_id=drid,
                           source_core_run_id="c1", source_board_run_id="b1",
                           algorithm_version="v1", finished_at=None, created_at=None)


def _full_plan(**overrides) -> dict:
    """构造全就绪 plan，所有节点均有正式 pointer 与对应领域 run。"""
    pubs = {
        _DAILY: [_pub(_DAILY)],
        _BOARD_FACTS: [_pub(_BOARD_FACTS)],
        _STOCK_CORE: [_pub(_STOCK_CORE)],
        _BOARD_AGG: [_pub(_BOARD_AGG)],
        _REVIEW: [_pub(_REVIEW)],
        _CHIP: [_pub(_CHIP)],
        _AUCTION: [_pub(_AUCTION)],
    }
    runs = {
        BoardFactsRun: [_bf_run("published")],
        MarketReviewRun: [_review_run("published")],
        ChipConsensusRun: [_chip_run("succeeded")],
        AuctionAnchorRun: [_auction_run("succeeded")],
    }
    plan = {
        "pubs": pubs,
        "runs": runs,
        "dsa_counts": (10, 10),
        # 默认 state_events 存在且归属当前 core run（_DRID == stock_core pointer_data_run_id），
        # 使 enhancement 全 terminal
        "state_event_rows": [("candidate", _DRID, 5)],
    }
    plan.update(overrides)
    return plan


async def _evaluate(plan):
    db = _FakeDB(plan)
    service = ProductReadinessService()
    return await service.evaluate_for_trade_date(db, date(2026, 8, 4))


# ----------------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------------

async def test_full_chain_fully_ready():
    """九节点全部就绪 → fully_ready。"""
    ev = await _evaluate(_full_plan())
    assert ev.closure == CLOSURE_FULLY_READY
    assert ev.mandatory_products_ready is True
    assert ev.mandatory_products_full_fresh is True
    assert ev.enhancement_jobs_terminal is True


async def test_board_facts_reused_degrades():
    """P0-7：board_facts 指针 data run 为 reused_previous → ready_reused → degraded_ready。"""
    plan = _full_plan()
    plan["runs"][BoardFactsRun] = [_bf_run("reused_previous")]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_DEGRADED_READY
    assert ev.mandatory_products_ready is True
    assert any(i["code"] == "NOT_FULLY_FRESH" for i in ev.issues)


async def test_board_facts_unavailable_blocks():
    """P0-2：board_facts 无指针且 latest run failed → unavailable → blocked。"""
    plan = _full_plan()
    plan["pubs"][_BOARD_FACTS] = [None]          # 无 board_facts pointer
    plan["runs"][BoardFactsRun] = [_bf_run("failed")]  # latest run failed
    ev = await _evaluate(plan)
    assert ev.closure == "blocked"
    assert ev.mandatory_products_ready is False
    assert any(i["severity"] == "critical" for i in ev.issues)


async def test_old_pointer_ignores_failed_retry():
    """P0-2：指针存在时，即使有 failed 重试 run，readiness 仍为 ready（latest attempt 单列）。"""
    plan = _full_plan()
    # board_facts 指针仍在（published run），另有 failed 重试 run 不应影响
    plan["runs"][BoardFactsRun] = [_bf_run("published"), _bf_run("failed")]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY


async def test_no_publish_pending():
    """无任何 run/pointer → pending（stock_core 未形成）。"""
    plan = _full_plan()
    for k in plan["pubs"]:
        plan["pubs"][k] = [None]
    plan["runs"] = {m: [None] for m in plan["runs"]}
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_PENDING
    assert ev.mandatory_products_ready is False


async def test_failed_enhancement_terminal_fully_ready():
    """P0-3：chip 失败（terminal+unavailable）→ enhancement_jobs_terminal=True，不阻断。"""
    plan = _full_plan()
    plan["pubs"][_CHIP] = [None]                 # chip 无 pointer
    plan["runs"][ChipConsensusRun] = [_chip_run("failed")]  # latest run failed
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY
    assert ev.enhancement_jobs_terminal is True


async def test_stock_core_pointer_missing_pending():
    """P0-4：stock_core 无 pointer → pending。"""
    plan = _full_plan()
    plan["pubs"][_STOCK_CORE] = [None]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_PENDING


async def test_core_ready_when_review_pending():
    """P0-4：stock_core ready 但 review 未完成 → core_ready（而非 pending）。"""
    plan = _full_plan()
    plan["pubs"][_REVIEW] = [None]               # 无正式 market_review pointer
    plan["runs"][MarketReviewRun] = [_review_run("pending")]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_CORE_READY
    assert ev.mandatory_products_ready is False


def test_pure_evaluator_contract():
    """服务层复用纯评估器（契约一致性）。"""
    from app.services.product_readiness_service import ProductReadinessState

    states = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
    ]
    ev = evaluate_closure(states)
    assert ev.closure == CLOSURE_FULLY_READY
    # 缺少 dsa_projection/state_events（enhancement）不影响 fully_ready（空 enhancement）
    states2 = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_PENDING),
        ProductReadinessState("board_aggregation", READINESS_PENDING),
        ProductReadinessState("review", READINESS_PENDING),
    ]
    ev2 = evaluate_closure(states2)
    assert ev2.closure == CLOSURE_PENDING


# ----------------------------------------------------------------------------
# [Corrective-3.1 §P1] review 精确 pointer / dsa / state_events lineage 用例
# ----------------------------------------------------------------------------

def _issues_by_product(ev, product):
    return [i for i in ev.issues if i.get("product") == product]


async def test_review_requires_market_review_pointer():
    """[Corrective-3.1 §P1] 即使 MarketReviewRun=published，但无 market_review pointer，
    也不得判 fully_ready（降级为 core_ready）。"""
    plan = _full_plan()
    plan["pubs"][_REVIEW] = [None]               # 无正式 pointer
    plan["runs"][MarketReviewRun] = [_review_run("published")]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_CORE_READY
    assert ev.mandatory_products_ready is False


async def test_review_pointer_present_ready():
    """[Corrective-3.1 §P1] 有正式 market_review pointer 且 run 一致 → fully_ready。"""
    plan = _full_plan()
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY


async def test_dsa_lineage_mismatch_not_ready():
    """[Corrective-3.1 §P1] 当日有投影但均不归属当前 core run → degraded_ready，不得 fully_ready。"""
    plan = _full_plan()
    plan["dsa_counts"] = (10, 0)                 # total>0, matched=0
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_DEGRADED_READY


async def test_dsa_exact_match_ready():
    """[Corrective-3.1 §P1] 投影归属当前 core run（matched>0）→ fully_ready（不降级）。"""
    plan = _full_plan()
    plan["dsa_counts"] = (10, 10)
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY


async def test_state_events_lineage_mismatch_not_ready():
    """[Corrective-3.1 §P1] 当日有事件但均不归属当前 core run → degraded_ready，不得 fully_ready。"""
    plan = _full_plan()
    # 事件存在但与当前 core run 不匹配（source_run_id 不同）
    plan["state_event_rows"] = [("candidate", "old_run_id", 5)]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_DEGRADED_READY


async def test_state_events_exact_match_ready():
    """[Corrective-3.1 §P1] 事件归属当前 core run（matched>0）→ fully_ready（不降级）。"""
    plan = _full_plan()
    # 与当前 core run（stock_core pointer_data_run_id = _DRID）匹配
    plan["state_event_rows"] = [("candidate", _DRID, 5)]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "--tb=short", "-p", "no:cacheprovider"])
