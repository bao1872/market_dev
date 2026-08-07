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
    CLOSURE_MANDATORY_READY_ENHANCING,
    CLOSURE_PENDING,
    READINESS_DEGRADED,
    READINESS_PENDING,
    READINESS_READY,
)
from app.models.auction import (
    AuctionAnchorPublication,
    AuctionAnchorSnapshot,
)
from app.models.board_analysis_snapshot import BoardAnalysisRun
from app.models.board_facts_run import BoardFactsRun
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
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_state_event import StockStateEvent
from app.models.strategy_run import StrategyResult, StrategyRun, StrategyRunItem
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
    "board_analysis_runs": BoardAnalysisRun,
    "market_review_runs": MarketReviewRun,
    "scheduler_job_runs": SchedulerJobRun,
    "stock_chip_consensus_snapshots": StockChipConsensusSnapshot,
    "auction_anchor_publications": AuctionAnchorPublication,
    "auction_anchor_snapshots": AuctionAnchorSnapshot,
    "stock_feature_snapshots": StockFeatureSnapshot,
    "stock_state_events": StockStateEvent,
    "strategy_runs": StrategyRun,
    "strategy_run_items": StrategyRunItem,
}


def _entity_class(stmt):
    """从 select 语句提取查询的 ORM 实体类。

    本 SQLAlchemy 版本下 select(Model) 的 froms 直接是 Table（无 .entity），
    故用表名映射到已注册模型类。对 join 语句（如 matched 查询
    StrategyRunItem JOIN StrategyResult），递归展开 Join.left/right 取首个已知表，
    以便 _FakeDB 正确路由。
    """

    def _first_model(frm):
        if frm is None:
            return None
        name = getattr(frm, "name", None)
        if name in _TABLE_TO_MODEL:
            return _TABLE_TO_MODEL[name]
        left = getattr(frm, "left", None)
        right = getattr(frm, "right", None)
        if left is not None or right is not None:
            return _first_model(left) or _first_model(right)
        return None

    for frm in stmt.get_final_froms():
        model = _first_model(frm)
        if model is not None:
            return model
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
        # [required compatibility projection identity] projection run 的正式终态
        #（published/completed → READY 满足 run_terminal；None 默认 published）
        self._dsa_run_status = plan.get("dsa_run_status")
        # [required compatibility projection identity] run/item 计数一致性：默认与 eligible
        # 一致（count_mismatch=False）；传入不同值可测试 PROJECTION_RUN_ITEM_COUNT_MISMATCH。
        self._dsa_total = plan.get("dsa_total_instruments")
        # [current-lineage validation] 当前上游 pointer 的 pub 对象（供 _current_*_data_run_id）。
        # 默认与 _full_plan 的 stock_core/market_aggregation pointer 一致（data_run_id=_DRID）。
        self._current_pubs = plan.get("current_pubs", {})

    async def scalar(self, stmt):
        ent = _entity_class(stmt)
        if ent is FactorPublication:
            kind = _extract_kind(stmt)
            # [current-lineage validation] _current_*_data_run_id 查询带 superseded_by.is_(None)，
            # 应返回当前（非 superseded）上游 pointer pub 对象（data_run_id 供归属校验）。
            if "superseded_by" in str(stmt.whereclause):
                return self._current_pubs.get(kind)
            q = self._pubs.get(kind, [])
            return q.pop(0) if q else None
        if ent is StrategyRun:
            # [required compatibility projection identity] _count_dsa_projections 查投影 run：
            #   select(StrategyRun).where(input_overrides source_core_run_id == core).limit(1)
            # 当 dsa_counts.eligible > 0 时返回当前 core 的投影 run，否则 None（无投影）。
            # 默认 status=published（正式终态，满足 READY 的 run_terminal 条件）。
            if (self._dsa + [0, 0])[0] > 0:
                total = (
                    self._dsa_total if self._dsa_total is not None
                    else self._dsa[0]
                )
                return SimpleNamespace(
                    id="proj-run-1",
                    total_instruments=total,
                    status=self._dsa_run_status or "published",
                    strategy_version_id="sv1",
                    input_overrides={
                        "strategy_key": "dsa_selector",
                        "source_core_run_id": "c1",
                        "requirement": "required_compatibility",
                    },
                )
            return None
        if ent is StrategyResult:
            # [required compatibility projection identity] matched 查询经
            #   StrategyRunItem.result_id → StrategyResult.id join（select_from(StrategyRunItem)
            #   .join(StrategyResult, ...)），_entity_class 可能解析出 StrategyResult。
            #   matched = eligible 中 succeeded + result_id 非空且 result lineage 一致的
            #   distinct instrument 数（真实投影产物存在性）。
            eligible, matched = (self._dsa + [0, 0])[:2]
            return matched
        if ent is StrategyRunItem:
            # _count_dsa_projections 对 projection run 发两个 count：
            #   - eligible：仅按 run_id（distinct instrument）
            #   - matched：额外 status=='succeeded' AND result_id.is_not(None)
            # status 值是参数化（不在 whereclause 文本），故用 result_id 关键字区分 matched。
            eligible, matched = (self._dsa + [0, 0])[:2]
            wc = str(stmt.whereclause)
            if "result_id" in wc:
                return matched
            return eligible
        if ent is StockFeatureSnapshot:
            # [legacy] 仅 state_events 仍用 StockFeatureSnapshot scalar 计数。
            # _count_dsa_projections 已改为查 StrategyRun/StrategyRunItem，不再走本分支。
            wc = str(stmt.whereclause)
            eligible, matched = (self._dsa + [0, 0])[:2]
            if "?" in wc:
                return matched
            if "source_run_id" in wc:
                return eligible
            return eligible  # day_total：简化取 eligible universe 规模
        q = self._runs.get(ent, [])
        return q.pop(0) if q else None

    async def scalars(self, stmt):
        """_count_dsa_projections stale 计算：当日所有 dsa_selector projection runs。

        简化：返回空（无其他 core 残留 → stale=0）。matched=0 时 _dsa_projection_state
        仍走 eligible>0 分支判 LINEAGE_MISMATCH，不依赖 stale。
        返回带 .all() 的 ScalarResult 兼容对象（真实 session.scalars() 语义）。
        """
        return SimpleNamespace(all=lambda: [])

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


def _review_run(status="published", drid=_DRID, source_core=_DRID, source_board=_DRID):
    return SimpleNamespace(id=drid, status=status, data_run_id=drid,
                           algorithm_version="v1", filter_version="fv1",
                           coverage_ratio=0.99, published_at=None,
                           completed_at=None, created_at=None,
                           source_core_run_id=source_core,
                           source_board_run_id=source_board)


def _board_agg_run(status="succeeded", drid=_DRID, source_core=_DRID):
    """market_aggregation pointer 指向的 BoardAnalysisRun（source_core 默认=当前 stock_core）。"""
    return SimpleNamespace(
        id=drid, status=status, data_run_id=drid,
        source_core_run_id=source_core,
        source_board_run_id="b1",
        algorithm_version="v1", coverage_ratio=0.99,
        published_at=None, created_at=None,
    )


def _chip_job(status="succeeded", drid=_DRID, chip_status=None,
              expected=10, succeeded=10):
    """正式 chip 产物：SchedulerJobRun(after_close_chip_consensus) + metadata_json。

    chip_status 由 chip worker 写入 metadata_json，是产品级状态真源；
    StockChipConsensusSnapshot 只做真实产物/lineage 对账（此处以计数表达）。
    """
    import json as _json

    if chip_status is None:
        chip_status = status  # succeeded→succeeded；failed→failed
    # chip worker 实际写入的分母键是 total_count（见 app/worker.py），
    # expected_count 仅历史兼容；两者同时给出以贴近生产元数据。
    meta = _json.dumps({
        "chip_status": chip_status,
        "total_count": expected,
        "expected_count": expected,
        "succeeded_count": succeeded,
        "core_run_id": drid,
    })
    return SimpleNamespace(
        id=drid,
        job_name="after_close_chip_consensus",
        business_date=date(2026, 8, 4).isoformat(),
        status=status,
        metadata_json=meta,
        created_at=None,
    )


def _auction_pub(status_snap="succeeded", drid=_DRID, snap_id="snap1", coverage=0.99):
    """正式 auction 产物：AuctionAnchorPublication（最新，superseded_by=None）。"""
    return SimpleNamespace(
        id=drid,
        trade_date=date(2026, 8, 4),
        superseded_by=None,
        snapshot_id=snap_id,
        source_core_run_id=drid,
        coverage_ratio=coverage,
        algorithm_version="v1",
        published_at=__import__("datetime").datetime(2026, 1, 1),
    )


def _auction_snap(status="succeeded", snap_id="snap1",
                  composite=5, chip=5, structure=10):
    """AuctionAnchorSnapshot：status 表达产品级状态（succeeded/partial/structure_only/...）。"""
    return SimpleNamespace(
        id=snap_id,
        status=status,
        composite_anchor_count=composite,
        chip_anchor_count=chip,
        structure_anchor_count=structure,
        error_message=None,
    )


def _full_plan(**overrides) -> dict:
    """构造全就绪 plan，所有节点均有正式 pointer 与对应领域 run。"""
    pubs = {
        _DAILY: [_pub(_DAILY)],
        _BOARD_FACTS: [_pub(_BOARD_FACTS)],
        _STOCK_CORE: [_pub(_STOCK_CORE)],
        _BOARD_AGG: [_pub(_BOARD_AGG)],
        _REVIEW: [_pub(_REVIEW)],
        # chip 正式产物为 SchedulerJobRun + StockChipConsensusSnapshot，
        # 不再经 FactorPublication(CHIP_CONSENSUS) pointer；故此处置 None，
        # 由 job 元数据驱动 readiness。
        _CHIP: [None],
        _AUCTION: [None],
    }
    runs = {
        BoardFactsRun: [_bf_run("published")],
        BoardAnalysisRun: [_board_agg_run("succeeded", source_core=_DRID)],
        MarketReviewRun: [_review_run("published")],
        SchedulerJobRun: [_chip_job("succeeded")],
        StockChipConsensusSnapshot: [10],  # 真实 snapshot 行数（lineage 对账）
        AuctionAnchorPublication: [_auction_pub("succeeded")],
        AuctionAnchorSnapshot: [_auction_snap("succeeded")],
    }
    # [current-lineage validation] 当前上游 pointer（供 _current_*_data_run_id）。
    # 默认与 _full_plan 的 stock_core/market_aggregation pointer 一致（data_run_id=_DRID），
    # 使 board_aggregation/review 的 source 归属校验通过。
    current_pubs = {
        _STOCK_CORE: _pub(_STOCK_CORE),
        _BOARD_AGG: _pub(_BOARD_AGG),
    }
    plan = {
        "pubs": pubs,
        "runs": runs,
        "current_pubs": current_pubs,
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


async def _collect_product(plan, product: str):
    """取单个产品的 ProductReadinessState（闭包结果不含产品级明细）。"""
    db = _FakeDB(plan)
    service = ProductReadinessService()
    states = await service.collect_states(db, date(2026, 8, 4))
    return next(s for s in states if s.product == product)


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
    """P0-3/P0-1：chip 失败（terminal+unavailable）→ 不阻断 mandatory chain，但 chip 非真正就绪，
    闭包为 degraded_ready（不得误判 fully_ready）。"""
    plan = _full_plan()
    plan["pubs"][_CHIP] = [None]                 # chip 无 publication pointer
    plan["runs"][SchedulerJobRun] = [_chip_job("failed", chip_status="failed")]  # job failed
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_DEGRADED_READY
    assert ev.mandatory_products_full_fresh is True
    assert ev.enhancement_jobs_terminal is True


async def test_chip_succeeded_but_counts_incomplete_is_degraded():
    """Case A：job=succeeded、chip_status=succeeded，但 5100/5200 计数不完整
    → chip 必须 degraded，绝不能因两个字符串状态就判定 full/ready。"""
    def _plan():
        # _FakeDB 以 pop(0) 消费队列，每次评估须用全新 plan。
        p = _full_plan()
        p["pubs"][_CHIP] = [None]
        p["runs"][SchedulerJobRun] = [
            _chip_job("succeeded", chip_status="succeeded", expected=5200, succeeded=5100)
        ]
        return p

    chip = await _collect_product(_plan(), "chip")
    assert chip.is_product_ready is False
    assert chip.readiness == READINESS_DEGRADED
    ev = await _evaluate(_plan())
    assert ev.closure == CLOSURE_DEGRADED_READY


async def test_auction_partial_snapshot_yields_hybrid():
    """Case B：AuctionAnchorSnapshot.status=partial 且 coverage>0
    → publication 可形成，auction 推导为 hybrid（degraded 而非 unavailable）。"""
    plan = _full_plan()
    plan["runs"][AuctionAnchorSnapshot] = [
        _auction_snap("partial", composite=60, chip=60, structure=40)
    ]
    auction = await _collect_product(plan, "auction_anchor")
    assert auction.auction_mode == "hybrid"
    assert auction.readiness == READINESS_DEGRADED


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
    """[Corrective-3.1 §P1 / Phase 4] 当日有投影但均不归属当前 core run → 非 fully_ready。

    dsa_projection lineage mismatch → 非 terminal 非 ready → enhancement 未全部终态，
    六态下为 mandatory_ready_enhancing（核心就绪、增强推进中），不得 fully_ready。
    """
    plan = _full_plan()
    plan["dsa_counts"] = (10, 0)                 # total>0, matched=0
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_MANDATORY_READY_ENHANCING


async def test_dsa_exact_match_ready():
    """[Corrective-3.1 §P1] 投影归属当前 core run（matched>0）→ fully_ready（不降级）。"""
    plan = _full_plan()
    plan["dsa_counts"] = (10, 10)
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY


async def test_dsa_run_item_count_mismatch_not_ready():
    """[required compatibility projection identity] eligible != StrategyRun.total_instruments →
    PROJECTION_RUN_ITEM_COUNT_MISMATCH → 不得 READY（数据缺陷，阻断闭包）。

    六态下为 mandatory_ready_enhancing（增强未全部终态），不得 fully_ready。
    """
    plan = _full_plan()
    plan["dsa_counts"] = (10, 10)
    plan["dsa_total_instruments"] = 12  # eligible(10) != total_instruments(12)
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_MANDATORY_READY_ENHANCING


async def test_dsa_non_terminal_run_status_not_ready():
    """[required compatibility projection identity] projection run 未达正式终态
    （如 running/queued/partial）→ run_terminal=False → 不得 READY。

    即使 matched==eligible，只要 run 非 published，enhancement 不 terminal。
    """
    plan = _full_plan()
    plan["dsa_counts"] = (10, 10)
    plan["dsa_run_status"] = "running"  # 未正式发布 → 不 READY
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_MANDATORY_READY_ENHANCING


async def test_dsa_completed_but_not_published_not_ready():
    """[required compatibility projection identity] completed 表示已计算但待发布，
    不得 READY；仅 published 才算正式终态。

    即使 matched==eligible 且 coverage 达标，只要 run.status == "completed"（未发布），
    enhancement 不 terminal → 不得 fully_ready。
    """
    plan = _full_plan()
    plan["dsa_counts"] = (10, 10)
    plan["dsa_run_status"] = "completed"  # 已计算但未 publish → 不 READY
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_MANDATORY_READY_ENHANCING


async def test_state_events_lineage_mismatch_not_ready():
    """[Corrective-3.1 §P1 / Phase 4] 当日有事件但均不归属当前 core run → 非 fully_ready。

    state_events lineage mismatch → 非 terminal 非 ready → enhancement 未全部终态，
    六态下为 mandatory_ready_enhancing，不得 fully_ready。
    """
    plan = _full_plan()
    # 事件存在但与当前 core run 不匹配（source_run_id 不同）
    plan["state_event_rows"] = [("candidate", "old_run_id", 5)]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_MANDATORY_READY_ENHANCING


async def test_state_events_exact_match_ready():
    """[Corrective-3.1 §P1] 事件归属当前 core run（matched>0）→ fully_ready（不降级）。"""
    plan = _full_plan()
    # 与当前 core run（stock_core pointer_data_run_id = _DRID）匹配
    plan["state_event_rows"] = [("candidate", _DRID, 5)]
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY


async def test_board_aggregation_exact_lineage_ready():
    """[current-lineage validation] market_aggregation pointer 指向的 BoardAnalysisRun
    source_core_run_id == 当前 stock_core pointer.data_run_id → 就绪。
    """
    plan = _full_plan()  # board run source_core=_DRID == current stock_core(_DRID)
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY


async def test_board_aggregation_stale_lineage_not_ready():
    """[current-lineage validation] market_aggregation pointer 指向的 BoardAnalysisRun
    source_core_run_id 与当前 stock_core pointer 不一致（如 Core 重跑后 board 基于旧 Core）
    → BOARD_AGGREGATION_LINEAGE_MISMATCH，不得 READY。

    board_aggregation 是 mandatory 节点，lineage mismatch 阻断闭包 → 不得 fully_ready。
    """
    plan = _full_plan()
    # board run 基于旧 Core（source_core 与当前 stock_core _DRID 不一致）
    plan["runs"] = {**plan["runs"], BoardAnalysisRun: [_board_agg_run("succeeded", source_core="old_core_id")]}
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_CORE_READY  # mandatory board_aggregation 未就绪

    # 且状态本身不得 READY（lineage mismatch → degraded, non-terminal）
    # 注：_evaluate 会 pop 共享 plan 的 FIFO，需用 fresh plan 断言直接状态。
    plan2 = _full_plan()
    plan2["runs"] = {**plan2["runs"], BoardAnalysisRun: [_board_agg_run("succeeded", source_core="old_core_id")]}
    db = _FakeDB(plan2)
    service = ProductReadinessService()
    st = await service._board_aggregation_state(db, date(2026, 8, 4))
    assert st.readiness == READINESS_DEGRADED
    assert st.lineage.get("reason_code") == "BOARD_AGGREGATION_LINEAGE_MISMATCH"


async def test_review_exact_lineage_ready():
    """[current-lineage validation] market_review pointer 指向的 MarketReviewRun
    source_core_run_id == 当前 stock_core pointer 且 source_board_run_id == 当前
    market_aggregation pointer → 就绪。
    """
    plan = _full_plan()  # review run source_core=_DRID, source_board=_DRID 与当前指针一致
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_FULLY_READY


async def test_review_stale_lineage_not_ready():
    """[current-lineage validation] market_review pointer 指向的 MarketReviewRun
    source_core_run_id 与当前 stock_core 不一致 → REVIEW_LINEAGE_MISMATCH，不得 READY。

    review 是 mandatory 节点，lineage mismatch 阻断闭包 → 不得 fully_ready。
    """
    plan = _full_plan()
    plan["runs"] = {
        **plan["runs"],
        MarketReviewRun: [_review_run("published", source_core="old_core_id", source_board=_DRID)],
    }
    ev = await _evaluate(plan)
    assert ev.closure == CLOSURE_CORE_READY  # mandatory review 未就绪

    # 且状态本身不得 READY（review lineage mismatch → degraded, non-terminal）
    # 注：_evaluate 会 pop 共享 plan 的 FIFO，需用 fresh plan 断言直接状态。
    plan2 = _full_plan()
    plan2["runs"] = {
        **plan2["runs"],
        MarketReviewRun: [_review_run("published", source_core="old_core_id", source_board=_DRID)],
    }
    db = _FakeDB(plan2)
    service = ProductReadinessService()
    st = await service._review_state(db, date(2026, 8, 4))
    assert st.readiness == READINESS_DEGRADED
    assert st.lineage.get("reason_code") == "REVIEW_LINEAGE_MISMATCH"


# =============================================================================
# [AUD-10 2026-08-07] 产品三分类：mandatory / required_compatibility / enhancement
# =============================================================================


def test_product_classification_is_three_way():
    """九节点必须被划入三类，且分类互斥、并集完整。"""
    from app.services.product_readiness_service import (
        ENHANCEMENT_PRODUCTS,
        MANDATORY_PRODUCTS,
        NINE_NODES,
        REQUIRED_COMPATIBILITY_PRODUCTS,
    )

    assert REQUIRED_COMPATIBILITY_PRODUCTS == {"dsa_projection"}, (
        "dsa_projection 是 stock_core 的派生兼容投影，既非可选增强、也不阻断核心"
    )
    # 互斥
    assert not (MANDATORY_PRODUCTS & REQUIRED_COMPATIBILITY_PRODUCTS)
    assert not (MANDATORY_PRODUCTS & ENHANCEMENT_PRODUCTS)
    assert not (REQUIRED_COMPATIBILITY_PRODUCTS & ENHANCEMENT_PRODUCTS)
    # 完整：仍是九节点
    assert len(NINE_NODES) == 9
    assert NINE_NODES == (
        MANDATORY_PRODUCTS | REQUIRED_COMPATIBILITY_PRODUCTS | ENHANCEMENT_PRODUCTS
    )
    # dsa_projection 已移出 enhancement
    assert "dsa_projection" not in ENHANCEMENT_PRODUCTS


def test_classify_product_mapping():
    """classify_product 对三类与未登记产品的映射。"""
    from app.services.product_readiness_service import (
        PRODUCT_CLASS_ENHANCEMENT,
        PRODUCT_CLASS_MANDATORY,
        PRODUCT_CLASS_REQUIRED_COMPATIBILITY,
        classify_product,
    )

    assert classify_product("stock_core") == PRODUCT_CLASS_MANDATORY
    assert classify_product("review") == PRODUCT_CLASS_MANDATORY
    assert (
        classify_product("dsa_projection") == PRODUCT_CLASS_REQUIRED_COMPATIBILITY
    )
    assert classify_product("chip") == PRODUCT_CLASS_ENHANCEMENT
    assert classify_product("auction_anchor") == PRODUCT_CLASS_ENHANCEMENT
    # 未登记产品按最保守处理：enhancement（不阻断核心）
    assert classify_product("some_future_product") == PRODUCT_CLASS_ENHANCEMENT


async def test_required_compatibility_ready_true_when_all_ready():
    """全部产品就绪 → required_compatibility_ready=True 且 fully_ready。"""
    ev = await _evaluate(_full_plan())
    assert ev.closure == CLOSURE_FULLY_READY
    assert ev.required_compatibility_ready is True


async def test_required_compatibility_not_ready_is_attributable():
    """[AUD-10 核心] dsa_projection 未就绪时：

    1. 不得 fully_ready；
    2. required_compatibility_ready=False（可与"增强未就绪"区分）；
    3. issues 中给出专属归因 code，而非混在泛化的增强降级里。
    """
    plan = _full_plan()
    # dsa 投影已终态但 lineage 不匹配（matched=0）→ 兼容输出未就绪
    plan["dsa_counts"] = (10, 0)

    ev = await _evaluate(plan)

    assert ev.closure != CLOSURE_FULLY_READY, (
        "必需兼容输出未就绪时不得宣称完整就绪"
    )
    assert ev.required_compatibility_ready is False, (
        "必须能与'增强产品未就绪'区分开"
    )
    codes = {i.get("code") for i in ev.issues}
    assert "REQUIRED_COMPATIBILITY_NOT_READY" in codes, (
        f"必须给出专属归因 code，实际 issues codes={codes}"
    )


async def test_closure_enum_values_unchanged():
    """[AUD-10] 三分类不得引入新的 closure 取值（避免破坏既有消费方）。"""
    from app import domain_status

    allowed = {
        domain_status.CLOSURE_BLOCKED,
        domain_status.CLOSURE_PENDING,
        domain_status.CLOSURE_CORE_READY,
        domain_status.CLOSURE_MANDATORY_READY_ENHANCING,
        domain_status.CLOSURE_DEGRADED_READY,
        domain_status.CLOSURE_FULLY_READY,
    }
    # 遍历若干典型场景，确认闭包取值始终落在既有集合内
    for plan_factory in (_full_plan,):
        ev = await _evaluate(plan_factory())
        assert ev.closure in allowed


def test_schema_required_compatibility_ready_default_is_fail_safe():
    """[Phase4.1 corrective] requiredCompatibilityReady 默认必须为 fail-safe False：
    调用方若漏填该字段，不得自动报告为 ready（否则会掩盖 dsa_projection 等
    required_compatibility 产品缺失）。验证 schema 默认值与构造语义。"""
    from app.schemas.product_readiness import ProductReadinessResponse

    # 1) 构造时不显式传 requiredCompatibilityReady —— 默认必须是 False
    resp = ProductReadinessResponse(
        tradeDate="2026-08-05",
        closure="fully_ready",
        productionClosure="fully_ready",
    )
    assert resp.requiredCompatibilityReady is False, (
        "schema 默认必须为 False（fail-safe），调用方漏填时禁止自动报告 ready"
    )

    # 2) 显式 False 也合法（缺兼容产品时如实反映）
    resp2 = ProductReadinessResponse(
        tradeDate="2026-08-05",
        closure="degraded_ready",
        productionClosure="degraded_ready",
        requiredCompatibilityReady=False,
    )
    assert resp2.requiredCompatibilityReady is False

    # 3) 仅当调用方显式 True 时才为 ready（service 层始终显式赋值）
    resp3 = ProductReadinessResponse(
        tradeDate="2026-08-05",
        closure="fully_ready",
        productionClosure="fully_ready",
        requiredCompatibilityReady=True,
    )
    assert resp3.requiredCompatibilityReady is True


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "--tb=short", "-p", "no:cacheprovider"])
