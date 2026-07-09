"""SQLAlchemy __table__ metadata type-safe accessors.

mypy sees Model.__table__ as `FromClause` (no indexes/constraints) or
`Iterable[NamedColumn[Any]]` (no columns). These helpers use isinstance
narrowing to `Table` which has all attributes.
"""

from sqlalchemy.sql.schema import Table

from app.models.base import Base


def table_indexes(model: type[Base]) -> list:
    """获取模型的 __table__.indexes，类型安全。"""
    table = model.__table__
    if isinstance(table, Table):
        return list(table.indexes)
    return []


def table_constraints(model: type[Base]) -> list:
    """获取模型的 __table__.constraints，类型安全。"""
    table = model.__table__
    if isinstance(table, Table):
        return list(table.constraints)
    return []


def table_columns(model: type[Base]) -> list:
    """获取模型的 __table__.columns，类型安全。"""
    table = model.__table__
    if isinstance(table, Table):
        return list(table.columns)
    return []
