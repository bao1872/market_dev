"""Board Sync 绝对门禁 / 相对门禁单元测试（纯函数，不连数据库）。

[Corrective-2 2026-08-05 §10] 从原 test_board_sync.py 拆分出的**单元测试**部分：
- 只含 validate_snapshot 绝对门禁 / 相对门禁纯函数测试
- 本文件保持纯单元；真实 DB 集成测试使用 `postgres` marker 并在远程验证库运行
- 可 PURE_UNIT_TEST=1 运行

DB 集成测试见 test_board_sync_pg.py（使用 db_session fixture，命中 postgres 分类）。
"""

from __future__ import annotations

import pytest

from app.services.board_sync_service import (
    MIN_CONCEPT_COUNT,
    MIN_INDUSTRY_COUNT,
    MIN_RAW_ROWS,
    MIN_RELATION_COUNT,
    StagingValidationError,
    validate_snapshot,
)
from app.services.wencai_board_provider import BoardSnapshot


# 测试快照须满足 provider 合同：board 显式携带 taxonomy/source/taxonomy_version/
# taxonomy_compatibility_key/identity_contract_version（board_sync_service 已禁止回退默认值）。
def _board(
    external_code: str,
    name: str,
    type_: str,
    *,
    hierarchy_level: str = "L1",
) -> dict[str, str]:
    """构造带显式分类学/身份合同字段的 board dict。"""
    return {
        "external_code": external_code,
        "name": name,
        "type": type_,
        "hierarchy_level": hierarchy_level,
        "taxonomy": "wencai",
        "source": "wencai",
        "taxonomy_version": "wencai-hierarchy-v1",
        "taxonomy_compatibility_key": "wencai-board-v1",
        "identity_contract_version": "wencai-identity-v1",
    }


def _make_valid_snapshot(
    num_stocks: int = 5500,
    concepts_per_stock: int = 12,
    num_industries: int = 257,
    num_concepts: int = 388,
) -> BoardSnapshot:
    """构造能通过绝对门禁的 BoardSnapshot。

    默认参数接近生产基线：5537股、257行业、388概念、69737概念关系。
    raw_rows = num_stocks，每股唯一 → code_uniqueness_rate = 1.0
    """
    boards: list[dict[str, str]] = []
    memberships: dict[tuple[str, str], list[str]] = {}

    # 生成行业 boards
    for i in range(num_industries):
        name = f"行业{i}-子类{i % 10}"
        ext_code = f"wc:i:industry_{i:04d}"
        boards.append(_board(ext_code, name, "industry"))
        memberships[(ext_code, "industry")] = []

    # 生成概念 boards
    for i in range(num_concepts):
        ext_code = f"wc:c:concept_{i:04d}"
        boards.append(_board(ext_code, f"概念{i}", "concept"))
        memberships[(ext_code, "concept")] = []

    # 生成股票及其板块归属
    for stock_idx in range(num_stocks):
        symbol = f"{600000 + stock_idx:06d}"

        # 每股分配一个行业（轮询）
        industry_idx = stock_idx % num_industries
        industry_key = (f"wc:i:industry_{industry_idx:04d}", "industry")
        memberships[industry_key].append(symbol)

        # 每股分配多个概念（轮询）
        for c in range(concepts_per_stock):
            concept_idx = (stock_idx * concepts_per_stock + c) % num_concepts
            concept_key = (f"wc:c:concept_{concept_idx:04d}", "concept")
            memberships[concept_key].append(symbol)

    return BoardSnapshot(
        boards=boards,
        memberships=memberships,
        raw_rows=num_stocks,
        unresolved_symbols=[],
    )


# =============================================================================
# 1. 绝对门禁测试（纯函数）
# =============================================================================


class TestValidateSnapshotAbsolute:
    """绝对门禁校验测试。"""

    def test_valid_snapshot_passes(self) -> None:
        snapshot = _make_valid_snapshot()
        stats = validate_snapshot(snapshot)
        assert stats["raw_rows"] >= MIN_RAW_ROWS
        assert stats["industry_count"] >= MIN_INDUSTRY_COUNT
        assert stats["concept_count"] >= MIN_CONCEPT_COUNT
        assert stats["relation_count"] >= MIN_RELATION_COUNT
        assert stats["code_uniqueness_rate"] >= 0.999

    def test_raw_rows_below_minimum_rejected(self) -> None:
        """raw_rows < 5000 拒绝。"""
        snapshot = BoardSnapshot(
            boards=[_board("wc:i:b0", "b", "industry")],
            memberships={("wc:i:b0", "industry"): ["000001"]},
            raw_rows=MIN_RAW_ROWS - 1,
        )
        with pytest.raises(StagingValidationError, match="raw rows"):
            validate_snapshot(snapshot)

    def test_industry_below_minimum_rejected(self) -> None:
        """行业数 < 200 拒绝。"""
        snapshot = _make_valid_snapshot(num_industries=MIN_INDUSTRY_COUNT - 1)
        with pytest.raises(StagingValidationError, match="industry count"):
            validate_snapshot(snapshot)

    def test_concept_below_minimum_rejected(self) -> None:
        """概念数 < 300 拒绝。"""
        snapshot = _make_valid_snapshot(num_concepts=MIN_CONCEPT_COUNT - 1)
        with pytest.raises(StagingValidationError, match="concept count"):
            validate_snapshot(snapshot)

    def test_relation_below_minimum_rejected(self) -> None:
        """关系数 < 60000 拒绝（其它门禁均通过）。"""
        # 5000 唯一股票 → code_uniqueness_rate=1.0；200行业+300概念通过；
        # 每股仅 1 行业 + 1 概念 → 10000 关系 < 60000
        num_stocks = MIN_RAW_ROWS
        boards: list[dict[str, str]] = []
        memberships: dict[tuple[str, str], list[str]] = {}
        for i in range(MIN_INDUSTRY_COUNT):
            ext = f"wc:i:b{i:04d}"
            boards.append(_board(ext, f"b{i}", "industry"))
            memberships[(ext, "industry")] = []
        for i in range(MIN_CONCEPT_COUNT):
            ext = f"wc:c:b{i:04d}"
            boards.append(_board(ext, f"c{i}", "concept"))
            memberships[(ext, "concept")] = []
        for s_idx in range(num_stocks):
            sym = f"{s_idx:06d}"
            memberships[(f"wc:i:b{s_idx % MIN_INDUSTRY_COUNT:04d}", "industry")].append(sym)
            memberships[(f"wc:c:b{s_idx % MIN_CONCEPT_COUNT:04d}", "concept")].append(sym)
        snapshot = BoardSnapshot(
            boards=boards,
            memberships=memberships,
            raw_rows=num_stocks,
        )
        with pytest.raises(StagingValidationError, match="relation count"):
            validate_snapshot(snapshot)


# =============================================================================
# 2. 相对门禁测试（纯函数）
# =============================================================================


class TestValidateSnapshotRelative:
    """相对门禁校验测试。"""

    def test_normal_drop_accepted(self) -> None:
        """下降 ≤20% 接受。"""
        snapshot = _make_valid_snapshot(num_stocks=5000)
        validate_snapshot(
            snapshot,
            prev_stock_count=6000,
            prev_industry_count=300,
            prev_concept_count=450,
            prev_relation_count=80000,
        )

    def test_stock_drop_over_20_percent_rejected(self) -> None:
        """股票数下降 >20% 拒绝（snapshot 通过绝对门禁）。"""
        # 默认 5500 股票通过绝对门禁；prev=8000 → drop=31.25% > 20%
        snapshot = _make_valid_snapshot(num_stocks=5500)
        with pytest.raises(StagingValidationError, match="stock count dropped"):
            validate_snapshot(snapshot, prev_stock_count=8000)

    def test_industry_drop_over_20_percent_rejected(self) -> None:
        snapshot = _make_valid_snapshot(num_industries=200)
        with pytest.raises(StagingValidationError, match="industry count dropped"):
            validate_snapshot(snapshot, prev_industry_count=300)

    def test_concept_drop_over_20_percent_rejected(self) -> None:
        snapshot = _make_valid_snapshot(num_concepts=300)
        with pytest.raises(StagingValidationError, match="concept count dropped"):
            validate_snapshot(snapshot, prev_concept_count=400)

    def test_first_sync_no_drop_check(self) -> None:
        """首次同步（prev=0）不检查相对门禁。"""
        snapshot = _make_valid_snapshot()
        validate_snapshot(
            snapshot,
            prev_stock_count=0,
            prev_industry_count=0,
            prev_concept_count=0,
            prev_relation_count=0,
        )
