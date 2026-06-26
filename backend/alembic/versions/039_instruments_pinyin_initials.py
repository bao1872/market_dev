"""039 instruments add pinyin_initials column + index

Revision ID: 039_instruments_pinyin_initials
Revises: 038_scheduler_job_run_key_partial_index
Create Date: 2026-06-26

变更内容：
- instruments 表新增 pinyin_initials VARCHAR(20) 列（可空）
- 新增 ix_instruments_pinyin_initials 索引，支持拼音首字母前缀搜索

设计说明（advice.md 第六节）：
- 添加自选弹窗提示"代码 / 名称 / 拼音"，但原表无拼音字段，拼音搜索不生效
- 在主数据同步时生成拼音首字母（如 '东睦股份' -> 'dmgf'）并落库，避免每次搜索实时转拼音
- 历史数据通过 scripts/backfill_pinyin_initials.py 一次性回补
- 列设为可空，保证迁移期间不阻塞写入；回补完成后建议 NOT NULL（本迁移暂不强制）
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "039_instruments_pinyin_initials"
down_revision: str | None = "038_scheduler_job_run_key_partial_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Instrument] - 新增拼音首字母列（可空，兼容回补前历史数据）
    op.add_column(
        "instruments",
        sa.Column("pinyin_initials", sa.String(length=20), nullable=True),
    )
    # [Instrument] - 拼音首字母前缀搜索索引
    op.create_index(
        "ix_instruments_pinyin_initials",
        "instruments",
        ["pinyin_initials"],
    )


def downgrade() -> None:
    op.drop_index("ix_instruments_pinyin_initials", table_name="instruments")
    op.drop_column("instruments", "pinyin_initials")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "039_instruments_pinyin_initials"
    assert down_revision == "038_scheduler_job_run_key_partial_index"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
