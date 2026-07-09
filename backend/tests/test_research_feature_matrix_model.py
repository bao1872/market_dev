"""research_feature_matrix 模型测试。

验证内容：
1. ResearchFeatureMatrixRun / ResearchFeatureMatrixRow ORM 模型列定义
2. 33 个 feature 列与 feature_causality_registry.db_column() 1:1 对应
3. 索引存在（unique run_key, unique instrument_id+trade_date, 各 btree index）
4. 状态机常量
5. registry dotted key 与 model column 命名映射一致

用法：
    cd backend && APP_ENV=test pytest tests/test_research_feature_matrix_model.py -v
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

from app.models._table_meta import table_indexes
from app.models.research_feature_matrix import (
    ALL_STATUSES,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    ResearchFeatureMatrixRow,
    ResearchFeatureMatrixRun,
)
from app.research.feature_causality_registry import build_default_registry


def _get_unique_constraint(table: object, name: str) -> UniqueConstraint | None:
    """从 table.constraints 中查找指定名称的 UniqueConstraint。"""
    for c in table.constraints:  # type: ignore[attr-defined]
        if isinstance(c, UniqueConstraint) and c.name == name:
            return c
    return None


# ===== 1. Run 表模型 =====


def test_run_table_name() -> None:
    """run 表名应为 research_feature_matrix_runs。"""
    assert ResearchFeatureMatrixRun.__tablename__ == "research_feature_matrix_runs"


def test_run_table_has_16_columns() -> None:
    """run 表应有 16 列（id + 14 metadata + updated_at）。"""
    cols = [c.name for c in ResearchFeatureMatrixRun.__table__.columns]
    assert len(cols) == 16, f"run 表应有 16 列，实际 {len(cols)}: {cols}"


def test_run_table_required_columns() -> None:
    """run 表必需列全部存在。"""
    cols = {c.name for c in ResearchFeatureMatrixRun.__table__.columns}
    required = {
        "id", "run_key", "month", "start_date", "end_date", "status",
        "instruments_count", "trade_dates_count", "rows_count", "failed_count",
        "duration_seconds", "started_at", "finished_at",
        "metadata_json", "created_at", "updated_at",
    }
    missing = required - cols
    assert not missing, f"run 表缺少列: {missing}"


def test_run_table_indexes() -> None:
    """run 表索引：unique(run_key) + index(month) + index(status)。"""
    idx_names = {i.name for i in table_indexes(ResearchFeatureMatrixRun) if i.name}
    assert "ix_research_matrix_runs_month" in idx_names
    assert "ix_research_matrix_runs_status" in idx_names


def test_run_key_unique_constraint_is_unique() -> None:
    """run_key 必须有 UniqueConstraint。"""
    uc = _get_unique_constraint(ResearchFeatureMatrixRun.__table__, "uq_research_matrix_runs_run_key")
    assert uc is not None, "未找到 uq_research_matrix_runs_run_key 唯一约束"
    assert [c.name for c in uc.columns] == ["run_key"]


# ===== 2. Row 表模型 =====


def test_row_table_name() -> None:
    """row 表名应为 research_feature_matrix_rows。"""
    assert ResearchFeatureMatrixRow.__tablename__ == "research_feature_matrix_rows"


def test_row_table_has_39_columns() -> None:
    """row 表应有 39 列（5 metadata + 33 feature + 1 created_at）。"""
    cols = [c.name for c in ResearchFeatureMatrixRow.__table__.columns]
    assert len(cols) == 39, f"row 表应有 39 列，实际 {len(cols)}"


def test_row_table_metadata_columns() -> None:
    """row 表 metadata 列：id, run_id, instrument_id, symbol, trade_date, created_at。"""
    cols = {c.name for c in ResearchFeatureMatrixRow.__table__.columns}
    metadata = {"id", "run_id", "instrument_id", "symbol", "trade_date", "created_at"}
    missing = metadata - cols
    assert not missing, f"row 表缺少 metadata 列: {missing}"


def test_row_table_has_33_feature_columns() -> None:
    """row 表应有 33 个 feature 列（排除 6 个 metadata/created_at 列后剩余）。"""
    cols = [c.name for c in ResearchFeatureMatrixRow.__table__.columns]
    non_feature = {"id", "run_id", "instrument_id", "symbol", "trade_date", "created_at"}
    feature_cols = [c for c in cols if c not in non_feature]
    assert len(feature_cols) == 33, (
        f"应有 33 个 feature 列，实际 {len(feature_cols)}: {feature_cols}"
    )


def test_row_table_indexes() -> None:
    """row 表索引：3 个 btree index (trade_date, instrument_id, run_id)。"""
    idx_names = {i.name for i in table_indexes(ResearchFeatureMatrixRow) if i.name}
    assert "ix_research_matrix_rows_trade_date" in idx_names
    assert "ix_research_matrix_rows_instrument_id" in idx_names
    assert "ix_research_matrix_rows_run_id" in idx_names


def test_inst_date_unique_constraint_is_unique() -> None:
    """(instrument_id, trade_date) 必须有 UniqueConstraint。"""
    uc = _get_unique_constraint(ResearchFeatureMatrixRow.__table__, "uq_research_matrix_rows_inst_date")
    assert uc is not None, "未找到 uq_research_matrix_rows_inst_date 唯一约束"
    assert [c.name for c in uc.columns] == ["instrument_id", "trade_date"]


def test_run_id_foreign_key() -> None:
    """run_id 必须有外键约束指向 research_feature_matrix_runs.id。"""
    fks = list(ResearchFeatureMatrixRow.__table__.foreign_keys)
    assert len(fks) == 1, f"应有 1 个 FK，实际 {len(fks)}"
    fk = fks[0]
    assert fk.parent.name == "run_id", f"FK 父列应为 run_id，实际 {fk.parent.name}"
    assert fk.column.table.name == "research_feature_matrix_runs", (
        f"FK 应指向 runs 表，实际 {fk.column.table.name}"
    )
    assert fk.column.name == "id", f"FK 应指向 id 列，实际 {fk.column.name}"


# ===== 3. registry ↔ model 1:1 映射 =====


def test_registry_columns_match_model_feature_columns() -> None:
    """registry.db_columns() 必须与 row 表 feature 列完全一致。"""
    reg = build_default_registry()
    registry_cols = set(reg.db_columns())

    row_cols = {c.name for c in ResearchFeatureMatrixRow.__table__.columns}
    non_feature = {"id", "run_id", "instrument_id", "symbol", "trade_date", "created_at"}
    model_feature_cols = row_cols - non_feature

    assert registry_cols == model_feature_cols, (
        f"registry ↔ model 不一致:\n"
        f"  registry_only={registry_cols - model_feature_cols}\n"
        f"  model_only={model_feature_cols - registry_cols}"
    )


def test_each_namespace_has_correct_count_in_model() -> None:
    """model 中每个 namespace 的列数应与 registry 一致。"""
    reg = build_default_registry()
    row_cols = {c.name for c in ResearchFeatureMatrixRow.__table__.columns}

    for ns in ["causal", "confirmed_delay", "hindsight", "label"]:
        registry_ns_cols = {s.db_column for s in reg.by_namespace(ns)}
        model_ns_cols = {c for c in row_cols if c.startswith(f"{ns}_")}
        assert registry_ns_cols == model_ns_cols, (
            f"namespace={ns} 不一致: "
            f"registry_only={registry_ns_cols - model_ns_cols}, "
            f"model_only={model_ns_cols - registry_ns_cols}"
        )


def test_dotted_key_maps_to_underscore_column() -> None:
    """registry dotted key（causal.atr）映射为 model 下划线列（causal_atr）。"""
    reg = build_default_registry()
    row_cols = {c.name for c in ResearchFeatureMatrixRow.__table__.columns}

    sample_keys = [
        "causal.atr",
        "causal.bb_percent_b",
        "confirmed_delay.confirmed_swing_high",
        "hindsight.dsa_finalized_segment",
        "hindsight.node_cluster_label",
        "label.future_return_10d",
    ]
    for key in sample_keys:
        spec = reg.get(key)
        assert spec is not None, f"registry 缺少 {key}"
        assert spec.db_column in row_cols, (
            f"registry key {key} -> db_column {spec.db_column} 不在 model 中"
        )


# ===== 4. 状态机常量 =====


def test_status_constants() -> None:
    """状态机常量定义正确。"""
    assert STATUS_RUNNING == "running"
    assert STATUS_SUCCEEDED == "succeeded"
    assert STATUS_FAILED == "failed"
    assert ALL_STATUSES == {"running", "succeeded", "failed"}


# ===== 5. 表结构特征 =====


def test_no_gin_index_on_metadata_json() -> None:
    """metadata_json 不得有 GIN 索引（轻量宽表设计）。"""
    for idx in table_indexes(ResearchFeatureMatrixRun):
        for col in idx.columns:
            assert col.name != "metadata_json", (
                f"metadata_json 不应出现在索引 {idx.name} 中"
            )


def test_no_payload_jsonb_column_in_rows() -> None:
    """row 表不得有完整 JSONB payload 列（避免 EAV）。"""
    from sqlalchemy.dialects.postgresql import JSONB

    row_cols = ResearchFeatureMatrixRow.__table__.columns
    jsonb_cols = [c.name for c in row_cols if isinstance(c.type, JSONB)]
    assert jsonb_cols == [], (
        f"row 表不得有 JSONB payload 列，发现: {jsonb_cols}"
    )


def test_all_feature_columns_nullable() -> None:
    """所有 33 个 feature 列必须 nullable=True（warmup 期可为 NULL）。"""
    non_feature = {"id", "run_id", "instrument_id", "symbol", "trade_date", "created_at"}
    for col in ResearchFeatureMatrixRow.__table__.columns:
        if col.name in non_feature:
            continue
        assert col.nullable, f"feature 列 {col.name} 必须 nullable=True"
