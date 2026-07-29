"""FactorPublication ORM 模型 - 分层发布指针。

对应迁移 073 中的 factor_publications 表：
- 发布不复制结果，只执行小事务切换指针：旧run → 新run
- 唯一键：(scope_type, scope_key, trade_date, publication_kind)
- 只指向覆盖率门禁通过的不可变 run

publication_kind 枚举：
- stock_core: 单股核心快照发布（market 级覆盖率达到门禁后切换）
- market_aggregation: 市场聚合发布（宽度/行业/事件率等）
- history_cross_section: 历史横截面发布

模块自测：
    python -m app.models.factor_publication
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_constraints, table_indexes
from app.models.base import Base

# 发布类型枚举
PUBLICATION_KIND_STOCK_CORE = "stock_core"
PUBLICATION_KIND_MARKET_AGGREGATION = "market_aggregation"
PUBLICATION_KIND_HISTORY_CROSS_SECTION = "history_cross_section"
ALL_PUBLICATION_KINDS = {
    PUBLICATION_KIND_STOCK_CORE,
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_HISTORY_CROSS_SECTION,
}

# 范围类型枚举
SCOPE_TYPE_MARKET = "market"
SCOPE_TYPE_INSTRUMENT = "instrument"


class FactorPublication(Base):
    """分层发布指针 - 小事务原子切换数据版本。

    使用方式：
    1. 计算完成后，检查 coverage_ratio >= CORE_PUBLICATION_MIN_COVERAGE
    2. 通过后，upsert 一条 FactorPublication 记录
    3. 读请求始终读取 publication pointer 指向的 data_run_id
    4. 指针更新失败只重试指针，不重新计算数据

    兼容策略：
    - 没有 publication pointer 时，API 回退到原来的 latest published run
    - 建立 pointer 后，以 pointer 为唯一事实源
    """

    __tablename__ = "factor_publications"

    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_key", "trade_date", "publication_kind",
            name="uq_factor_publications_scope_date_kind",
        ),
        Index("ix_factor_publications_kind_date", "publication_kind", "trade_date"),
        Index(
            "ix_factor_publications_scope_kind",
            "scope_type", "scope_key", "publication_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    scope_type: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="范围类型：market / instrument",
    )
    scope_key: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="范围键：market 全市场 / instrument_id 单股",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False,
        comment="业务交易日（所有 publication 都按交易日，禁止 NULL 避免普通唯一约束允许多 NULL）",
    )
    publication_kind: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="发布类型：stock_core / market_aggregation / history_cross_section",
    )
    algorithm_version: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="算法版本",
    )
    data_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
        comment="指向的数据 run ID（snapshot_run_id 或 history_run_id）",
    )
    coverage_ratio: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="覆盖率（succeeded / expected）",
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="发布时间",
    )
    metadata_json: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="额外元数据 JSON",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<FactorPublication("
            f"scope_type={self.scope_type!r}, scope_key={self.scope_key!r}, "
            f"trade_date={self.trade_date!r}, "
            f"publication_kind={self.publication_kind!r}, "
            f"data_run_id={self.data_run_id!r})>"
        )


if __name__ == "__main__":
    cols = FactorPublication.__table__.columns
    expected = {
        "id", "scope_type", "scope_key", "trade_date", "publication_kind",
        "algorithm_version", "data_run_id", "coverage_ratio", "published_at",
        "metadata_json", "created_at",
    }
    actual = {c.name for c in cols}
    assert expected == actual, f"字段不匹配: {expected ^ actual}"
    print(f"OK: {FactorPublication.__tablename__} columns verified")

    constraint_names = {
        c.name for c in table_constraints(FactorPublication)
        if hasattr(c, "name") and c.name
    }
    assert "uq_factor_publications_scope_date_kind" in constraint_names, (
        f"缺少唯一约束: {constraint_names}"
    )
    print("unique constraint ✓")

    idx_names = {idx.name for idx in table_indexes(FactorPublication) if idx.name}
    expected_idx = {
        "ix_factor_publications_kind_date",
        "ix_factor_publications_scope_kind",
    }
    assert expected_idx.issubset(idx_names), f"缺少索引: {expected_idx - idx_names}"
    print("indexes ✓")
    print("OK")
