"""Split market_review (复盘) out of market_data (data-only).

[PANJI-REVIEW-CAPABILITY-SPLIT] 将「复盘」从 market_data 拆分为独立 capability。

本迁移是纯数据迁移（DATA migration），不修改任何表结构 / 列定义：
user_capabilities.capability 为 VARCHAR(32)，'market_review' 已在容量内；
invite_codes.capabilities 为 JSONB，无需 DDL。

upgrade（数据拆分，两个独立 part）：

  Part 1 — user_capabilities（既有授权行）：
    仅对**当前仍然有效**的真实授予 market_data 行补一条 market_review：
      * 过滤：capability='market_data' AND expires_at > now()
              AND source IS DISTINCT FROM 'admin_revoke'
        （source IS NULL 视为真实授予；admin_revoke tombstone 不放行；
          已过期 market_data 不补 —— 与冻结合同一致）
      * 同 user 已有 market_review 行时由 NOT EXISTS 跳过，不覆盖、不重复。
      * granted_at / expires_at 原样复制；watchlist_limit / granted_by 置 NULL；
        source = 'migration_review_split'。

  Part 2 — invite_codes（历史未兑换邀请码一次性兼容）：
    「复盘」历史上由 market_data 承载，因此迁移时刻仍为 status='unused' 且
    capabilities 数组同时满足「含 market_data / 不含 market_review」的历史邀请码，
    一次性追加一条 market_review 条目。
      * 期限单位不篡改：market_data 用 days=30 → review days=30；
        market_data 用 months=2 → review months=2。
      * 不复制 watchlist_limit（仅 self_selection 使用）。
      * status IN ('used','revoked') 的邀请码**一律不改**（历史兑换事实不可改写）。
      * capabilities IS NULL 的旧邀请码不制造 JSON —— 它继续走 legacy plan fallback
        （observe_20/research_50 已天然包含 market_review），无需数据改动。
    这是**一次性**兼容，绝不回退为运行时隐式继承（见 subscription_service
    ._grant_capabilities_from_invite：声明即授予，不推导附加权限）。

downgrade（回退到旧三权限可执行状态，**数据破坏性，见下**）：

  旧代码不认识 'market_review'。为让 downgrade 后的旧代码可正常执行：

  Part 1 — user_capabilities：
    **删除全部** capability='market_review' 行（不论 source 是
    'migration_review_split' 还是 admin_grant / invite_code）。
    这是本迁移的**数据破坏性**部分：管理员在升级后直接授予的 market_review
    授权会在 downgrade 时一并丢失，且不会恢复。这是「回退到旧 3 权限模型」的
    必然结果，不可逆，需在 downgrade 前评估。

  Part 2 — invite_codes：
    仅处理 status='unused' 的邀请码：从 capabilities 数组中移除所有 market_review
    条目。若移除后数组为空（原本是 market_review-only 的邀请码），则该邀请码会变成
    「可兑换但无权限」，因此使用**现有状态机**将其置为 status='revoked'（不引入新状态）。
    status IN ('used','revoked') 的邀请码一律不改（历史 JSON 不重写）。

不触碰任何其他表，不重写历史兑换记录，不改 subscription / plan 商业模型。
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "097_split_market_review_capability"
down_revision = "096_retire_legacy_market_review"
branch_labels = None
depends_on = None

# 本迁移写入 market_review 行时使用的来源标记
_MIGRATION_SOURCE = "migration_review_split"

_CAP_MARKET_DATA = "market_data"
_CAP_MARKET_REVIEW = "market_review"


def upgrade() -> None:
    # --- Part 1: user_capabilities — 仅 ACTIVE 的真实 market_data 补 market_review ---
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
              AND uc.expires_at > now()
              AND uc.source IS DISTINCT FROM 'admin_revoke'
              AND NOT EXISTS (
                SELECT 1 FROM user_capabilities uc2
                WHERE uc2.user_id = uc.user_id
                  AND uc2.capability = 'market_review'
              )
            """
        ).bindparams(migration_source=_MIGRATION_SOURCE)
    )

    # --- Part 2: invite_codes — 历史 unused 邀请码一次性补齐 market_review ---
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, capabilities::text AS caps_text
            FROM invite_codes
            WHERE status = 'unused'
              AND capabilities IS NOT NULL
              AND jsonb_typeof(capabilities) = 'array'
            """
        )
    ).fetchall()

    for invite_id, caps_text in rows:
        caps = _parse_caps(caps_text)
        market_data_entry = _find_cap(caps, _CAP_MARKET_DATA)
        if market_data_entry is None:
            continue
        if _find_cap(caps, _CAP_MARKET_REVIEW) is not None:
            continue  # 已声明 market_review，不重复追加
        new_caps = [*caps, _build_review_entry(market_data_entry)]
        bind.execute(
            sa.text(
                "UPDATE invite_codes SET capabilities = CAST(:caps AS jsonb) WHERE id = :invite_id"
            ),
            {"caps": json.dumps(new_caps, ensure_ascii=False), "invite_id": invite_id},
        )


def downgrade() -> None:
    bind = op.get_bind()

    # --- Part 1: user_capabilities — 删除全部 market_review（数据破坏性，见 docstring） ---
    op.execute(sa.text("DELETE FROM user_capabilities WHERE capability = 'market_review'"))

    # --- Part 2: invite_codes — unused 邀请码移除 market_review；空数组则置 revoked ---
    rows = bind.execute(
        sa.text(
            """
            SELECT id, capabilities::text AS caps_text
            FROM invite_codes
            WHERE status = 'unused'
              AND capabilities IS NOT NULL
              AND jsonb_typeof(capabilities) = 'array'
              AND capabilities @> '[{"capability": "market_review"}]'::jsonb
            """
        )
    ).fetchall()

    for invite_id, caps_text in rows:
        caps = _parse_caps(caps_text)
        remaining = [c for c in caps if not _is_cap(c, _CAP_MARKET_REVIEW)]
        if remaining:
            bind.execute(
                sa.text(
                    "UPDATE invite_codes SET capabilities = CAST(:caps AS jsonb) "
                    "WHERE id = :invite_id"
                ),
                {"caps": json.dumps(remaining, ensure_ascii=False), "invite_id": invite_id},
            )
        else:
            # 移除后无任何 capability → 不能留下「可兑换但无权限」的邀请码，
            # 用现有状态机置为 revoked（不引入新状态，不重写 used/revoked 历史）。
            bind.execute(
                sa.text(
                    "UPDATE invite_codes "
                    "SET capabilities = CAST(:caps AS jsonb), status = 'revoked' "
                    "WHERE id = :invite_id"
                ),
                {"caps": json.dumps([], ensure_ascii=False), "invite_id": invite_id},
            )


# --------------------------------------------------------------------------- #
# helpers（纯函数，便于单测；不依赖驱动对 JSONB 的解码行为）
# --------------------------------------------------------------------------- #
def _parse_caps(caps_text: str | None) -> list[dict]:
    """把 invite_codes.capabilities 的文本形态解析为 list[dict]。"""
    if not caps_text:
        return []
    parsed = json.loads(caps_text)
    if not isinstance(parsed, list):
        return []
    return [c for c in parsed if isinstance(c, dict)]


def _is_cap(entry: object, capability: str) -> bool:
    return isinstance(entry, dict) and entry.get("capability") == capability


def _find_cap(caps: list[dict], capability: str) -> dict | None:
    for entry in caps:
        if _is_cap(entry, capability):
            return entry
    return None


def _build_review_entry(market_data_entry: dict) -> dict:
    """按 market_data 条目的期限单位原样生成 market_review 条目。

    期限单位**不篡改**：原条目用 ``months`` 则 review 也用 ``months``；
    否则用 ``days``（缺省 1 天）。不复制 watchlist_limit。
    """
    if "months" in market_data_entry:
        return {"capability": _CAP_MARKET_REVIEW, "months": market_data_entry["months"]}
    return {"capability": _CAP_MARKET_REVIEW, "days": market_data_entry.get("days", 1)}
