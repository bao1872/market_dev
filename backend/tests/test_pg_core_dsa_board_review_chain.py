"""PG integration：Daily → Stock Core / First Pyramid → canonical DSA → DSA projection
→ Board Aggregation → Review 的代表性链路（第二层验证）。

设计目标（三层验证的第二层）：
- 不跑 5200 market-wide（那是最终 full-closure 的职责）；
- 用 deterministic synthetic 构造 5 个结构代表性 board + 成员并集（~180-250 股）+ daily bars（无 15m）；
- 所有正式结果（StockFeatureSnapshot / DSA StrategyResult / BoardAnalysisRun|Snapshot /
  MARKET_AGGREGATION publication / Review run|publication）**必须由正式 producer/service 产生**，
  禁止直接写终态；
- 验证三条 lineage：board.source_core_run_id == stock_core.run_id、
  review.source_core_run_id == stock_core.run_id、review.source_board_run_id == board.run_id；
- DSA 不重新计算（从真实 Core snapshot 的 canonical DSA 做 projection，StrategyRuntime 不应二次执行）。

本文件是 PG 测试：PURE_UNIT 下 skip；PANJI_REMOTE_VERIFY_DB_TEST=1（远程验证库）下执行。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest

from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.models.board_analysis_snapshot import BoardAnalysisRun
from app.models.board_taxonomy import (
    BoardDefinitionVersion,
    BoardMembershipHistory,
    UniverseDefinition,
)
from app.models.factor_publication import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.market_board import MarketBoard
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

REVIEW_METRICS = ["P", "Q", "U", "C", "V"]

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.asyncio,
]

# 测试 trade_date（daily bars 覆盖到此日，≥80 根）
TRADE_DATE = date(2026, 8, 4)

# 6 个结构代表性 board（deterministic synthetic，representative coverage 而非统计学抽样）
# 目标：覆盖大/小 industry、大/小 concept、高 overlap、edge 结构差异，member union 约 100-180。
BOARDS = [
    # (key, type, member_count)
    ("IND_LARGE", "industry", 60),   # 较大行业：多成员聚合
    ("IND_SMALL", "industry", 15),   # 较小行业：小板块
    ("CON_LARGE", "concept", 80),    # 较大概念：概念板块
    ("CON_SMALL", "concept", 12),    # 小概念：稀疏板块
    ("CON_OVERLAP", "concept", 45),  # 高 overlap：一股多板块（成员取自 IND_LARGE/CON_LARGE 子集）
    ("CON_EDGE", "concept", 8),      # edge board：coverage 边界
]

# 价格行为类型：确保不同 price path 均存在，避免第一金字塔因数据单一而"全算成一种状态"
PRICE_PATHS = ["uptrend", "downtrend", "range", "high_vol", "low_vol"]
_PRICE_PATH_ASSIGNMENT = [0, 1, 2, 3, 4]  # 每 5 只循环分配 5 类，deterministic


def _daily_ohlc(index: int, path_type: int) -> dict[str, float]:
    """按 price path 生成 deterministic OHLC。不伪造因子结果，只是 raw 输入。"""
    base = 10.0 + (index % 50)
    step = (index % 90) / 90.0  # 0..1
    if path_type == 0:        # uptrend
        close = base + step * 20.0 + 0.1
    elif path_type == 1:      # downtrend
        close = base - step * 20.0 + 0.1
    elif path_type == 2:      # range
        close = base + (index % 5) * 0.4
    elif path_type == 3:      # high_vol
        close = base + ((index % 9) - 4) * 2.5
    else:                     # low_vol
        close = base + ((index % 5) - 2) * 0.15
    o = close - 0.1
    spread = 1.2 if path_type == 3 else (0.2 if path_type == 4 else 0.5)
    return {
        "open": o,
        "high": max(o, close) + spread,
        "low": min(o, close) - spread,
        "close": close,
        "volume": 100000 + index * 100,
        "amount": 1000000.0 + index * 1000,
        "adj_factor": 1.0,
    }


def _price_path_type(index: int) -> int:
    return _PRICE_PATH_ASSIGNMENT[index % len(PRICE_PATHS)]


async def _ensure_snapshot_run(db, run_id: uuid.UUID, trade_date: date, n: int) -> None:
    await db.execute(
        "INSERT INTO stock_feature_snapshot_runs "
        "(id, trade_date, run_type, status, expected_count, snapshot_count, failed_count, "
        "skipped_count, failure_rate, started_at) "
        "VALUES (:id, :td, 'after_close', 'running', :n, 0, 0, 0, 0.0, now())",
        {"id": str(run_id), "td": trade_date, "n": n},
    )


async def _ensure_strategy_version(db, version_id: uuid.UUID) -> None:
    """建 dsa_selector 的 strategy_versions 行，消除对 Seed 预置 strategy_definitions 的依赖。"""
    await db.execute(
        "INSERT INTO strategy_definitions (id, strategy_key, kind, display_name, environment) "
        "VALUES (gen_random_uuid(), 'dsa_selector', 'selector', 'DSA Selector', 'production') "
        "ON CONFLICT (strategy_key) DO NOTHING",
    )
    await db.flush()
    def_id = (
        await db.execute(
            "SELECT id FROM strategy_definitions WHERE strategy_key='dsa_selector'"
        )
    ).scalar_one()
    await db.execute(
        "INSERT INTO strategy_versions "
        "(id, strategy_definition_id, version, status, manifest, build_hash, released_at) "
        "VALUES (:id, :did, :ver, 'released', '{}', 'build-1', now()) ON CONFLICT (id) DO NOTHING",
        {"id": str(version_id), "did": str(def_id), "ver": f"verify-{uuid.uuid4().hex[:8]}"},
    )


async def _seed_universe(
    db,
    trade_date: date,
) -> dict[str, dict[str, object]]:
    """构造 5 个 board + instruments + daily bars + PIT membership。

    返回 board key → {instrument_ids, id, ...}。
    隔离：savepoint 内清理库中其他 active MarketBoard 与 UniverseDefinition。
    """
    # ---- isolation：只保留本测试的 active MarketBoard 与 UniverseDefinition ----
    await db.execute("DELETE FROM market_boards WHERE is_active = true")
    await db.execute("DELETE FROM universe_definitions")

    # 成员并集（deterministic）：先为每个 board 分配唯一成员，再让 OVERLAP 取前两组子集
    members: dict[str, list[uuid.UUID]] = {}
    all_ids: list[uuid.UUID] = []
    cursor = 0
    for key, _typ, count in BOARDS:
        ids = []
        for _ in range(count):
            inst_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"chain-instr-{cursor}")
            cursor += 1
            ids.append(inst_id)
        members[key] = ids
        all_ids.extend(ids)
    # OVERLAP 用 IND_LARGE/CON_LARGE 前若干成员（跨板块归属，一股多板块），不新增股票
    overlap = members["IND_LARGE"][:25] + members["CON_LARGE"][:20]
    members["CON_OVERLAP"] = list(dict.fromkeys(overlap))
    universe_ids = list(dict.fromkeys(all_ids))

    # ---- instruments ----
    for i, inst_id in enumerate(universe_ids):
        await db.execute(
            "INSERT INTO instruments (id, symbol, name, market, status, listing_date) "
            "VALUES (:id, :symbol, :name, 'cn', 'active', '2010-01-04')",
            {"id": str(inst_id), "symbol": f"{990000 + i:06d}", "name": f"Chain {i}"},
        )

    # ---- daily bars（≥80 根，价格路径覆盖 5 类）----
    days: list[date] = []
    d = trade_date
    while len(days) < 90:
        if d.weekday() < 5:
            days.append(d)
        d = date.fromordinal(d.toordinal() - 1)  # 前一天
    days.reverse()
    for i, inst_id in enumerate(universe_ids):
        path_type = _price_path_type(i)
        for idx, day in enumerate(days):
            o = _daily_ohlc(i + idx, path_type)
            await db.execute(
                "INSERT INTO bars_daily "
                "(instrument_id, trade_date, open, high, low, close, volume, amount, adj_factor) "
                "VALUES (:iid, :td, :o, :h, :l, :c, :v, :a, :af) "
                "ON CONFLICT (instrument_id, trade_date) DO NOTHING",
                {"iid": str(inst_id), "td": day, "o": o["open"], "h": o["high"],
                 "l": o["low"], "c": o["close"], "v": o["volume"], "a": o["amount"],
                 "af": o["adj_factor"]},
            )

    # ---- MarketBoard + PIT BoardDefinitionVersion + BoardMembershipHistory ----
    board_meta: dict[str, dict[str, object]] = {}
    for key, typ, _count in BOARDS:
        board_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"chain-board-{key}")
        await db.execute(
            "INSERT INTO market_boards "
            "(id, external_code, name, type, taxonomy, source, taxonomy_version, "
            "taxonomy_compatibility_key, hierarchy_level, is_active, membership_version) "
            "VALUES (:id, :code, :name, :type, 'wencai', 'wencai', 'wencai-hierarchy-v1', "
            "'wencai-board-v1', 'L1', true, 'chain-membership-v1')",
            {"id": str(board_id), "code": key, "name": f"链测试{key}", "type": typ},
        )
        # PIT definition version
        definition_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"chain-def-{key}")
        await db.execute(
            "INSERT INTO board_definition_versions "
            "(id, board_id, taxonomy, source, taxonomy_version, taxonomy_compatibility_key, "
            "identity_contract_version, board_type, hierarchy_level, membership_version, "
            "effective_from, effective_to, definition_hash) "
            "VALUES (:id, :bid, 'wencai', 'wencai', 'wencai-hierarchy-v1', 'wencai-board-v1', "
            "'wencai-identity-v1', :btype, 'L1', 'chain-membership-v1', "
            "'2026-01-01', NULL, 'chain')",
            {"id": str(definition_id), "bid": str(board_id), "btype": typ},
        )
        # PIT membership history
        for inst_id in members[key]:
            await db.execute(
                "INSERT INTO board_membership_history "
                "(board_definition_version_id, instrument_id, membership_version, "
                "effective_from, effective_to) "
                "VALUES (:did, :iid, 'chain-membership-v1', '2026-01-01', NULL)",
                {"did": str(definition_id), "iid": str(inst_id)},
            )
        board_meta[key] = {"id": board_id, "definition_id": definition_id,
                           "instrument_ids": members[key], "type": typ,
                           "member_count": len(members[key])}
    # Core 计算的 SqlAlchemyReleasedConfigResolver 要求存在 released dsa_selector
    # StrategyVersion（fail-closed，无 released 则拒绝回退代码常量），故在 Core 前预置。
    await _ensure_strategy_version(db, uuid.uuid5(uuid.NAMESPACE_DNS, "chain-dsa-version"))
    await db.commit()
    return board_meta


def _make_savepoint_session_factory(_db_connection):
    """返回绑定 _db_connection 外层事务的 session factory（与 db_session 共享事务）。

    compute_review_core_with_run_items 用独立 session_factory 创建 session 时，若用
    test_async_engine（不同连接）将看不到 db_session savepoint 内未提交的 instruments/
    bars。绑定同一 _db_connection + create_savepoint 使服务在 savepoint 内读写、可回滚。
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    def _f():
        return AsyncSession(
            _db_connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )

    return _f


async def _run_core_chain(db, trade_date: date, instrument_ids: list[uuid.UUID],
                          session_factory):
    """真实 Core 链：create run → compute_review_core_with_run_items → finish → publish。"""
    from app.services.feature_snapshot_service import (
        compute_review_core_with_run_items,
        create_snapshot_run,
        finish_snapshot_run,
    )
    from app.services.factor_publication_service import compute_coverage
    from app.services.stock_core_publication_service import publish_stock_core_atomically
    from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION

    run = await create_snapshot_run(
        db, trade_date, "after_close", expected_count=len(instrument_ids), scope="full"
    )
    snapshot_run_id = run.id
    await db.commit()

    await compute_review_core_with_run_items(
        trade_date=trade_date,
        instrument_ids=instrument_ids,
        snapshot_run_id=snapshot_run_id,
        worker_id="chain-pg",
        algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        session_factory=session_factory,
    )

    run = await db.get(StockFeatureSnapshotRun, snapshot_run_id)
    cov = await compute_coverage(db, snapshot_run_id)
    await finish_snapshot_run(
        db, run, status="succeeded", snapshot_count=cov["succeeded"],
        failed_count=cov["failed"], skipped_count=cov["skipped"],
        expected_count=cov["expected"], metadata={"scope": "full"},
    )
    await db.commit()

    cov2 = await compute_coverage(db, snapshot_run_id)
    await publish_stock_core_atomically(
        db, scope_key="market", trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
        algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        snapshot_run_id=snapshot_run_id, coverage_ratio=cov2["coverage"],
        worker_id="chain-pg",
        lease_epoch=int(datetime.now(timezone.utc).timestamp()),
        eligible_count=cov2.get("expected", 0),
    )
    await db.commit()
    return snapshot_run_id


async def _run_dsa_projection(db, snapshot_run_id: uuid.UUID, instrument_ids: list[uuid.UUID]):
    """真实 DSA projection：project_dsa_batch（从真实 Core snapshot 读 canonical DSA，不重算）。"""
    from app.services.core_artifact_repository import CoreArtifactRepository
    from app.services.strategy_batch_service import persist_precomputed_dsa_results

    dsa_run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    await _ensure_strategy_version(db, version_id)
    await db.execute(
        "INSERT INTO strategy_runs "
        "(id, strategy_version_id, run_type, trade_date, status, total_instruments, "
        "succeeded_count, failed_count, started_at, worker_id, idempotency_key) "
        "VALUES (:id, :vid, 'after_close', :td, 'running', :n, 0, 0, now(), 'w1', :ik) "
        "ON CONFLICT (id) DO NOTHING",
        {"id": str(dsa_run_id), "vid": str(version_id), "td": TRADE_DATE,
         "n": len(instrument_ids), "ik": str(dsa_run_id)},
    )
    for iid in instrument_ids:
        await db.execute(
            "INSERT INTO strategy_run_items "
            "(id, run_id, instrument_id, status, attempt_count, started_at) "
            "VALUES (:id, :rid, :iid, 'pending', 0, now()) ON CONFLICT (id) DO NOTHING",
            {"id": str(uuid.uuid4()), "rid": str(dsa_run_id), "iid": str(iid)},
        )
    await db.commit()

    repo = CoreArtifactRepository(db)
    result = await repo.project_dsa_batch(
        source_core_run_id=snapshot_run_id, dsa_run_id=dsa_run_id,
        trade_date=TRADE_DATE, strategy_version_id=version_id,
        persist_fn=persist_precomputed_dsa_results,
    )
    await db.commit()
    return dsa_run_id, result


async def _assert_lineages(db, snapshot_run_id: uuid.UUID, board_run_id: uuid.UUID,
                           review_run_id: uuid.UUID) -> None:
    """断言三条 lineage。"""
    board_run = await db.get(BoardAnalysisRun, board_run_id)
    assert board_run is not None, "board run 不存在"
    assert str(board_run.source_core_run_id) == str(snapshot_run_id), (
        f"board.source_core_run_id 应等于 stock_core.run_id: "
        f"{board_run.source_core_run_id} != {snapshot_run_id}"
    )
    from app.models.market_review import MarketReviewRun
    review_run = await db.get(MarketReviewRun, review_run_id)
    assert review_run is not None, "review run 不存在"
    assert str(review_run.source_core_run_id) == str(snapshot_run_id), (
        f"review.source_core_run_id 应等于 stock_core.run_id"
    )
    assert str(review_run.source_board_run_id) == str(board_run_id), (
        f"review.source_board_run_id 应等于 board.run_id"
    )


async def _get_market_aggregation_publication(db, trade_date: date):
    row = (
        await db.execute(
            "SELECT id FROM factor_publications "
            "WHERE publication_kind=:kind AND trade_date=:td "
            "AND superseded_by IS NULL ORDER BY published_at DESC LIMIT 1",
            {"kind": PUBLICATION_KIND_MARKET_AGGREGATION, "td": trade_date},
        )
    ).scalar_one_or_none()
    return row


async def _count_snapshots(db, snapshot_run_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                "SELECT count(*) FROM stock_feature_snapshots WHERE source_run_id=:rid",
                {"rid": str(snapshot_run_id)},
            )
        ).scalar_one()
    )


async def _count_dsa_projection_rows(db, dsa_run_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                "SELECT count(*) FROM strategy_run_items WHERE run_id=:rid AND status='succeeded'",
                {"rid": str(dsa_run_id)},
            )
        ).scalar_one()
    )


async def _seed_review_history(db, trade_date: date) -> None:
    """为 market scope 构造 ≥60 个历史日期的 metric observations。

    evaluate_publish_gate 要求 market scope 的 P/Q/U/C/V 五项 normalized_ready（历史≥60 日）。
    load_metric_history 按 scope_type/scope_key/trade_date<target/algorithm_version 读取
    MarketReviewMetricObservation。唯一约束 (review_run_id, scope_type, scope_key, metric_code,
    component_name) 不含 trade_date，故每个历史日期需独立 review_run_id。这里构造 60 个
    历史 review run + 每日期 market scope 5 metric 的 _metric_value 观测。这是"最低必要
    historical observations"（review_bootstrap 的正式产物），不写任何终态。
    """
    days: list[date] = []
    d = trade_date
    while len(days) < 65:  # 多留 5 天余量
        if d.weekday() < 5:
            days.append(d)
        d = date.fromordinal(d.toordinal() - 1)
    days = [x for x in reversed(days) if x < trade_date][-60:]  # 严格早于 target 的最后 60 交易日
    for i, hist_day in enumerate(days):
        hist_run_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"chain-review-hist-{i}")
        await db.execute(
            "INSERT INTO market_review_runs "
            "(id, trade_date, status, algorithm_version, filter_version, baseline_window, "
            "source_core_run_id, source_board_run_id, expected_scope_count, "
            "succeeded_scope_count, failed_scope_count, coverage_ratio, signal_count, "
            "tracking_evaluations, metadata, started_at, completed_at, created_at, updated_at) "
            "VALUES (:id, :td, 'succeeded', :algo, 'v1', 120, NULL, NULL, 0, 0, 0, 1.0, 0, 0, "
            "'{}', now(), now(), now(), now())",
            {"id": str(hist_run_id), "td": hist_day, "algo": REVIEW_ALGORITHM_VERSION},
        )
        for mi, metric in enumerate(REVIEW_METRICS):
            await db.execute(
                "INSERT INTO market_review_metric_observations "
                "(id, review_run_id, trade_date, scope_type, scope_key, metric_code, "
                "component_name, raw_value, denominator, field_source_json, weight_mode, "
                "algorithm_version, input_hash, membership_version, status) "
                "VALUES (:id, :rid, :td, 'market', 'market', :mc, '_metric_value', :rv, NULL, "
                "'{}', 'equal_weight', :algo, 'hist', 'chain-membership-v1', 'normalized_ready')",
                {"id": str(uuid.uuid4()), "rid": str(hist_run_id), "td": hist_day,
                 "mc": metric, "rv": 100 + (i + mi) % 50, "algo": REVIEW_ALGORITHM_VERSION},
            )
    await db.commit()


async def test_pg_chain_scenario_a_happy_path(db_session, _db_connection) -> None:
    """Scenario A：5 board 全 coverage → Core READY / DSA matched==eligible / Board 5/5 /
    MARKET_AGGREGATION published / Review published + 三条 lineage。"""
    sf = _make_savepoint_session_factory(_db_connection)
    board_meta = await _seed_universe(db_session, TRADE_DATE)
    universe_ids = list(dict.fromkeys(
        [i for bm in board_meta.values() for i in bm["instrument_ids"]]
    ))

    # ---- Core ----
    snapshot_run_id = await _run_core_chain(db_session, TRADE_DATE, universe_ids, sf)
    n_core = await _count_snapshots(db_session, snapshot_run_id)
    assert n_core == len(universe_ids), f"Core snapshots {n_core} != eligible {len(universe_ids)}"

    # ---- DSA projection ----
    dsa_run_id, _dsa_result = await _run_dsa_projection(db_session, snapshot_run_id, universe_ids)
    n_dsa = await _count_dsa_projection_rows(db_session, dsa_run_id)
    assert n_dsa == len(universe_ids), f"DSA matched {n_dsa} != eligible {len(universe_ids)}"

    # ---- Board Aggregation ----
    from app.services.board_analysis_service import compute_all_boards
    result = await compute_all_boards(db_session, TRADE_DATE, publish=True)
    await db_session.commit()
    assert result.get("status") == "succeeded", f"Board batch 非 succeeded: {result}"
    assert result.get("succeeded") == len(BOARDS), (
        f"Board succeeded {result.get('succeeded')} != {len(BOARDS)}: {result}"
    )
    assert result.get("coverage_below_threshold", 0) == 0
    assert result.get("published", 0) == len(BOARDS)
    board_run_id = uuid.UUID(str(result["board_analysis_run_id"]))
    # MARKET_AGGREGATION 应存在
    pub_id = await _get_market_aggregation_publication(db_session, TRADE_DATE)
    assert pub_id is not None, "Scenario A 应发布 MARKET_AGGREGATION"

    # ---- Review ----
    # 历史 observations（≥60 日 market scope）须在 compute_run 前就绪，否则
    # evaluate_publish_gate 判 insufficient_history 阻塞发布（review_publication:155-198）。
    await _seed_review_history(db_session, TRADE_DATE)
    from app.services.review_orchestrator_service import compute_run, create_run, publish_run
    # [Phase4.2 corrective] create_run 恢复 baseline 合同：直接返回 MarketReviewRun。
    review_run = await create_run(
        db_session, trade_date=TRADE_DATE, source_core_run_id=snapshot_run_id,
        source_board_run_id=board_run_id, idempotency_key=f"chain-a:{TRADE_DATE}",
    )
    await db_session.commit()
    await compute_run(db_session, review_run)
    await db_session.commit()
    pub, _warn = await publish_run(db_session, review_run, force=False)
    await db_session.commit()
    assert pub is not None, "Scenario A Review 应正式发布"
    assert str(review_run.source_core_run_id) == str(snapshot_run_id)

    await _assert_lineages(db_session, snapshot_run_id, board_run_id, review_run.id)


async def test_pg_chain_scenario_b_coverage_below(db_session, _db_connection) -> None:
    """Scenario B：一个小 board 加非 current-core universe 成员 → coverage_below →
    Board batch partial → 不发布 MARKET_AGGREGATION → Review 不正式发布。"""
    sf = _make_savepoint_session_factory(_db_connection)
    board_meta = await _seed_universe(db_session, TRADE_DATE)

    # core universe = 除 IND_SMALL 外的 4 组并集 + IND_SMALL 前 18 个成员（保持 100% core coverage）
    # 但给 IND_SMALL 追加 2 个"存在于 board membership 但不在 core eligible"的 instrument
    small_ids = board_meta["IND_SMALL"]["instrument_ids"]
    # 追加 2 个非 core 标的（只进 PIT membership，不进 core run）
    extra_ids = [
        uuid.uuid5(uuid.NAMESPACE_DNS, f"chain-noncore-a"),
        uuid.uuid5(uuid.NAMESPACE_DNS, f"chain-noncore-b"),
    ]
    # 把这些 extra 也注册进 instruments + daily bars + IND_SMALL 的 PIT membership
    for i, eid in enumerate(extra_ids):
        await db_session.execute(
            "INSERT INTO instruments (id, symbol, name, market, status, listing_date) "
            "VALUES (:id, :symbol, :name, 'cn', 'active', '2010-01-04')",
            {"id": str(eid), "symbol": f"{990000 + 10000 + i:06d}", "name": f"NonCore {i}"},
        )
        days: list[date] = []
        d = TRADE_DATE
        while len(days) < 90:
            if d.weekday() < 5:
                days.append(d)
            d = date.fromordinal(d.toordinal() - 1)
        days.reverse()
        path_type = _price_path_type(500 + i)
        for idx, day in enumerate(days):
            o = _daily_ohlc(500 + i + idx, path_type)
            await db_session.execute(
                "INSERT INTO bars_daily "
                "(instrument_id, trade_date, open, high, low, close, volume, amount, adj_factor) "
                "VALUES (:iid, :td, :o, :h, :l, :c, :v, :a, :af) "
                "ON CONFLICT (instrument_id, trade_date) DO NOTHING",
                {"iid": str(eid), "td": day, "o": o["open"], "h": o["high"], "l": o["low"],
                 "c": o["close"], "v": o["volume"], "a": o["amount"], "af": o["adj_factor"]},
            )
    # 追加 extra 到 IND_SMALL 的 PIT membership
    for eid in extra_ids:
        await db_session.execute(
            "INSERT INTO board_membership_history "
            "(board_definition_version_id, instrument_id, effective_from, effective_to) "
            "VALUES (:did, :iid, '2026-01-01', NULL)",
            {"did": str(board_meta["IND_SMALL"]["definition_id"]), "iid": str(eid)},
        )
    await db_session.commit()

    # core universe = 除 IND_SMALL extra 外全部（small_ids 仍全含），确保 Core 100% 成功
    universe_ids = list(dict.fromkeys(
        [i for k, bm in board_meta.items() for i in bm["instrument_ids"]]
    ))
    snapshot_run_id = await _run_core_chain(db_session, TRADE_DATE, universe_ids, sf)

    from app.services.board_analysis_service import compute_all_boards
    result = await compute_all_boards(db_session, TRADE_DATE, publish=True)
    await db_session.commit()
    assert result.get("status") == "partial", f"Scenario B 应为 partial: {result}"
    assert result.get("succeeded") == len(BOARDS) - 1, f"应 4 个 board succeeded: {result}"
    assert result.get("coverage_below_threshold", 0) == 1, f"应 1 个 coverage_below: {result}"

    # MARKET_AGGREGATION 不应发布（batch 非 succeeded）
    pub_id = await _get_market_aggregation_publication(db_session, TRADE_DATE)
    assert pub_id is None, "Scenario B 不应发布 MARKET_AGGREGATION"

    # Review 不应正式发布：无合法 market_aggregation pointer（batch partial 未发布），
    # create_run 解析 source_board_run_id 时抛 ReviewOrchestratorError（review_orchestrator:372）。
    from app.services.review_orchestrator_service import ReviewOrchestratorError, create_run
    with pytest.raises(ReviewOrchestratorError):
        await create_run(
            db_session, trade_date=TRADE_DATE, source_core_run_id=snapshot_run_id,
            idempotency_key=f"chain-b:{TRADE_DATE}",
        )
