"""Split market_review (复盘) out of market_data (data-only).

[PANJI-REVIEW-CAPABILITY-SPLIT] 将「复盘」从 market_data 拆分为独立 capability。

本迁移是纯数据迁移（DATA migration），不修改任何表结构 / 列定义：
user_capabilities.capability 为 VARCHAR(32)，'market_review' 已在容量内。

upgrade（数据拆分）：
  - 对每一行「真实授予」的 market_data（排除 admin_revoke 撤销 tombstone），
    若同一 user_id 尚无 market_review 行，则插入一条对应的 market_review 行：
      * granted_at / expires_at 原样复制（保留 active / expired 状态）
      * watchlist_limit = NULL（仅 self_selection 使用）
      * source = 'migration_review_split'
      * granted_by = NULL
  - source IS NULL 三值逻辑安全处理：NULL 视为真实授予（(source IS NULL OR source <> 'admin_revoke')），
    不放行撤销 tombstone。
  - 用 NOT EXISTS 防重复：已存在 market_review 行的用户不再插入。

downgrade（仅回滚本迁移产生的行）：
  - DELETE FROM user_capabilities WHERE capability='market_review' AND source='migration_review_split'
  - 只删除本迁移写入的行；管理员直接授予 / 邀请码兑换产生的 market_review 行不受影响。

不触碰任何其他表，不重写历史兑换记录，不改 subscription / plan 商业模型。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "097_split_market_review_capability"
down_revision = "096_retire_legacy_market_review"
branch_labels = None
depends_on = None

# 本迁移写入 market_review 行时使用的来源标记（downgrade 据此精确回滚）
_MIGRATION_SOURCE = "migration_review_split"


def upgrade() -> None:
    # 数据拆分：market_data（真实授予、非撤销）→ market_review
    op.execute(
        sa.text(
            """
            INSERT INTO user_capabilities
                (user_id, capability, watchlist_limit, granted_at, expires_at, source)
            SELECT
                uc.user_id,
                'market_review',
                NULL,
                uc.granted_at,
                uc.expires_at,
                :migration_source
            FROM user_capabilities uc
            WHERE uc.capability = 'market_data'
              AND (uc.source IS NULL OR uc.source <> 'admin_revoke')
              AND NOT EXISTS (
                SELECT 1 FROM user_capabilities uc2
                WHERE uc2.user_id = uc.user_id
                  AND uc2.capability = 'market_review'
              )
            """
        ).bindparams(migration_source=_MIGRATION_SOURCE)
    )


def downgrade() -> None:
    # 仅删除本迁移产生的 market_review 行；保留管理员 / 邀请码产生的 market_review
    op.execute(
        sa.text(
            """
            DELETE FROM user_capabilities
            WHERE capability = 'market_review'
              AND source = :migration_source
            """
        ).bindparams(migration_source=_MIGRATION_SOURCE)
    )
