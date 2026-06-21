"""SQLAlchemy ORM 声明基类。

V1.1 各阶段模型统一继承 Base，便于 Alembic autogenerate 与元数据统一管理。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


if __name__ == "__main__":
    # 自测入口：验证 Base 创建与 metadata
    print(f"Base={Base}")
    print(f"metadata.tables={Base.metadata.tables}")
    print("OK")
