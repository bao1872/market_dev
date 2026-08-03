"""权限模型 V2 兼容数据迁移 - 只读 dry-run 报告工具（不执行任何写操作）。

功能权限唯一真源 = user_capabilities。本工具：
- 输出迁移前统计（legacy fallback 用户、缺权限用户、冲突等）；
- 打印幂等 backfill 计划（observe_20 → self_selection+market_data；
  research_50 → +research_replay），但**不执行 apply**；
- 支持 canary user_id 单用户预览。

用法（只读）：
    python scripts/capability_backfill_dryrun.py --stats --url <DB_URL>
    python scripts/capability_backfill_dryrun.py --plan --url <DB_URL>
    python scripts/capability_backfill_dryrun.py --plan --canary <user_id> --url <DB_URL>

安全边界：
- 本工具只读，任何情况下不 INSERT/UPDATE/DELETE；
- 默认不连接任何库；显式 --url 才连接（生产禁止，仅用于 CI 临时 PG 或显式授权 dry-run）；
- --apply 未实现（本轮禁止执行 backfill）。
"""
from __future__ import annotations

import argparse
from typing import Any

BACKFILL_MAP: dict[str, tuple[str, ...]] = {
    "observe_20": ("self_selection", "market_data"),
    "research_50": ("self_selection", "market_data", "research_replay"),
}


def _stats(engine: Any) -> dict[str, int]:
    """迁移前只读统计。"""
    from sqlalchemy import text

    res: dict[str, int] = {}
    queries = {
        "active_non_admin_users": (
            "SELECT count(*) FROM users u WHERE u.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
            "WHERE ur.user_id=u.id AND r.name='admin')"
        ),
        "users_with_explicit_capability": (
            "SELECT count(DISTINCT user_id) FROM user_capabilities"
        ),
        "active_non_admin_without_capability": (
            "SELECT count(*) FROM users u WHERE u.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
            "WHERE ur.user_id=u.id AND r.name='admin') "
            "AND NOT EXISTS (SELECT 1 FROM user_capabilities uc WHERE uc.user_id=u.id)"
        ),
        "invite_codes_null_capabilities": (
            "SELECT count(*) FROM invite_codes WHERE capabilities IS NULL"
        ),
        "self_selection_missing_watchlist_limit": (
            "SELECT count(*) FROM user_capabilities WHERE capability='self_selection' "
            "AND watchlist_limit IS NULL"
        ),
        # [权限模型 V2] 补充统计
        "legacy_fallback_users": (
            "SELECT count(*) FROM users u WHERE u.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
            "WHERE ur.user_id=u.id AND r.name='admin') "
            "AND NOT EXISTS (SELECT 1 FROM user_capabilities uc WHERE uc.user_id=u.id) "
            "AND EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id=u.id)"
        ),
        "partial_capability_users": (
            "SELECT count(*) FROM (SELECT user_id FROM user_capabilities "
            "GROUP BY user_id HAVING count(*) < 3) t"
        ),
        "subscription_capability_expiry_conflicts": (
            "SELECT count(*) FROM user_capabilities uc "
            "JOIN subscriptions s ON s.user_id=uc.user_id "
            "WHERE s.expires_at IS NOT NULL AND uc.expires_at IS NOT NULL "
            "AND s.expires_at <> uc.expires_at"
        ),
        "unknown_plan_code": (
            "SELECT count(*) FROM subscriptions s "
            "WHERE s.plan_code NOT IN ('observe_20','research_50')"
        ),
        "expired_but_active_status_abnormal": (
            "SELECT count(*) FROM subscriptions s "
            "WHERE s.status='active' AND s.expires_at IS NOT NULL AND s.expires_at < now()"
        ),
    }
    with engine.connect() as conn:
        for key, q in queries.items():
            res[key] = conn.execute(text(q)).scalar_one()
    return res


def _plan(engine: Any, canary: str | None) -> None:
    """打印幂等 backfill 计划（不执行）。"""
    from sqlalchemy import text

    where = (
        "AND u.id = :uid" if canary else ""
    )
    q = (
        "SELECT u.id, sub.plan_code, sub.status, sub.expires_at "
        "FROM users u JOIN subscriptions sub ON sub.user_id=u.id "
        "WHERE u.status='active' "
        "AND NOT EXISTS (SELECT 1 FROM user_capabilities uc WHERE uc.user_id=u.id) "
        + where
    )
    with engine.connect() as conn:
        params = {"uid": canary} if canary else {}
        rows = conn.execute(text(q), params).all()
    print(f"[plan] 待 backfill（legacy fallback 候选）: {len(rows)} 用户"
          + (f"（canary={canary}）" if canary else ""))
    for r in rows:
        caps = BACKFILL_MAP.get(r.plan_code or "")
        masked_id = f"{str(r.id)[:8]}...{str(r.id)[-4:]}" if r.id else "?"
        plan_code = r.plan_code or "?"
        # 建议到期日 = subscription.expires_at（不缩短）
        suggested_expires = r.expires_at
        # watchlist_limit 来源：observe_20/research_50 → plan 快照（本轮 dry-run 标记 plan_monitor_limit 占位）
        wl_source = "plan_monitor_limit" if plan_code in BACKFILL_MAP else "unknown"
        # skip 原因
        skip_reason = ""
        if plan_code not in BACKFILL_MAP:
            skip_reason = f"未知 plan_code={plan_code}"
        elif r.status != "active":
            skip_reason = f"订阅非 active（{r.status}）"
        print(
            f"  user={masked_id} plan={plan_code} sub_expires={suggested_expires} "
            f"现有权限=[] 建议新增={list(caps or [])} "
            f"建议到期日={suggested_expires} watchlist_limit来源={wl_source} "
            f"skip={skip_reason or '-'}"
        )
    print("[plan] 仅 dry-run，未执行任何写入。apply 未实现（本轮禁止执行）。")


def main() -> int:
    parser = argparse.ArgumentParser(description="capability backfill dry-run（只读）")
    parser.add_argument("--stats", action="store_true", help="输出迁移前统计")
    parser.add_argument("--plan", action="store_true", help="打印幂等 backfill 计划")
    parser.add_argument("--canary", default=None, help="canary user_id（单用户预览）")
    parser.add_argument("--url", default=None, help="只读 DB URL（生产禁止，仅 CI 临时 PG）")
    args = parser.parse_args()

    if not args.url:
        raise SystemExit("缺少 --url。安全边界：本工具只读，生产 DB 禁止连接，仅用于 CI 临时 PG 或显式授权 dry-run。")

    from sqlalchemy import create_engine

    engine = create_engine(args.url)
    if args.stats:
        stats = _stats(engine)
        for k, v in stats.items():
            print(f"  {k} = {v}")
    if args.plan:
        _plan(engine, args.canary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
