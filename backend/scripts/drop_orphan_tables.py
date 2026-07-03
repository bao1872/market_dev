"""一次性脚本：删除项目外孤儿表（A~O 共209张）

用法:
    cd /root/web_dev/backend && python scripts/drop_orphan_tables.py

说明:
    - 项目内 40 张表（ORM 定义）保留
    - 209 张项目外表按来源分类 A~O 删除
    - 使用 DROP TABLE IF EXISTS ... CASCADE
    - 安全检查：确认待删除清单不含项目表
"""
import sys

sys.path.insert(0, ".")
import psycopg

from app.config import get_settings

settings = get_settings()
url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")

# 项目内40张表（保留）
PROJECT_TABLES = {
    "alembic_version", "bars_daily", "bars_15min", "bars_60min", "bars_minute",
    "bars_weekly", "bars_monthly", "capture_jobs", "config_definitions",
    "dsa_backfill_jobs", "dsa_backfill_instrument_progress", "instruments",
    "invite_codes", "invite_redemptions", "job_run_events", "job_runs",
    "memberships", "message_deliveries", "monitor_evaluations", "monitor_states",
    "notification_channels", "notification_messages", "notification_templates",
    "outbox", "roles", "scheduler_job_runs", "stock_memos", "strategy_definitions",
    "strategy_event_recipients", "strategy_events", "strategy_result_metrics",
    "strategy_results", "strategy_run_items", "strategy_runs", "strategy_versions",
    "trading_calendar", "user_roles", "user_watchlist_items", "users", "worker_heartbeats",
}

# 类别A~O共209张待删除表
DROP_TABLES = [
    # A: Metabase (10)
    "metabase_cluster_lock", "metabase_database", "metabase_field", "metabase_field_user_settings",
    "metabase_fieldvalues", "metabase_table", "metabot", "metabot_conversation", "metabot_message", "metabot_prompt",
    # B: Quartz (11)
    "qrtz_blob_triggers", "qrtz_calendars", "qrtz_cron_triggers", "qrtz_fired_triggers", "qrtz_job_details",
    "qrtz_locks", "qrtz_paused_trigger_grps", "qrtz_scheduler_state", "qrtz_simple_triggers",
    "qrtz_simprop_triggers", "qrtz_triggers",
    # C: Liquibase (1)
    "databasechangelog",
    # D: 项目历史废弃 (14)
    "selection_plans", "selection_plan_members", "selection_plan_revisions", "selection_plan_runs",
    "selection_plan_results", "selection_member_conditions", "selection_result_evidence",
    "monitoring_plans", "monitoring_plan_members", "monitoring_plan_revisions", "monitoring_plan_states",
    "monitoring_state_evidence", "composite_monitor_events", "composite_event_evidence",
    # E: 废弃策略选股 (17)
    "atr_rope_factors", "atr_rope_features", "atr_rope_selection", "atr_week_selection",
    "bbmacd_week_selection", "c2_strategy_selections", "dsa_selection", "limit_up_selection",
    "limit_up_signals", "node_selection", "pa_selection", "sr_selection", "stop_loss_predictions",
    "stop_loss_selection", "tick_cache", "tick_selection", "vwap_selection",
    # F: 其他应用股票数据 (17)
    "stock_adj_factor", "stock_anomaly_signals", "stock_anomaly_signals_rolling",
    "stock_dsa_vreversal_results", "stock_financial_score_pool", "stock_financial_summary",
    "stock_holder_quality_portrait", "stock_k_data", "stock_market_data_cache", "stock_pools",
    "stock_selected_picks", "stock_selection_results", "stock_sentiment_posts",
    "stock_top10_holder_eval_scores_tushare", "stock_top10_holder_profiles_tushare",
    "stock_top10_holders_tushare", "stock_watchlist",
    # G: 其他应用核心认证 (15)
    "action", "api_key", "application_permissions_revision", "audit_log", "auth_identity",
    "core_session", "core_user", "login_history", "permissions", "permissions_group",
    "permissions_group_membership", "permissions_revision", "secret", "table_privileges", "tenant",
    # H: 其他应用Dashboard (13)
    "dashboard_bookmark", "dashboard_favorite", "dashboard_tab", "dashboardcard_series",
    "parameter_card", "pulse", "pulse_card", "pulse_channel", "pulse_channel_recipient",
    "report_card", "report_cardfavorite", "report_dashboard", "report_dashboardcard",
    # I: 其他应用Workspace (18)
    "transform", "transform_job", "transform_job_run", "transform_job_transform_tag",
    "transform_run", "transform_run_cancelation", "transform_tag", "transform_transform_tag",
    "workspace", "workspace_graph", "workspace_input", "workspace_input_external",
    "workspace_log", "workspace_merge", "workspace_merge_transform", "workspace_output",
    "workspace_output_external", "workspace_transform",
    # J: 其他应用通知系统 (5)
    "notification", "notification_card", "notification_handler", "notification_recipient", "notification_subscription",
    # K: 其他应用Collection/Forum (15)
    "bookmark_ordering", "card_bookmark", "card_label", "collection", "collection_bookmark",
    "collection_permission_graph_revision", "comment", "comment_reaction", "document",
    "document_bookmark", "forum_blogger", "forum_post", "forum_recommendation", "glossary", "timeline",
    # L: 其他应用Query/Metric (16)
    "dependency", "dimension", "field_usage", "measure", "metric", "metric_important_field",
    "model_index", "model_index_value", "native_query_snippet", "query", "query_action",
    "query_cache", "query_execution", "query_field", "query_table", "revision",
    # M: 其他应用Analysis/Factor (13)
    "analysis_finding", "analysis_finding_error", "concept_signals", "concept_signals_rolling",
    "event_definition", "event_factor_map", "event_trigger", "factor_definition",
    "factor_return_dataset", "factor_value", "theme_membership_snapshot", "theme_signals", "theme_signals_rolling",
    # N: 其他应用Financial (4)
    "financial_quarterly_data", "financial_scores", "holder_trade_records", "holder_trade_stats",
    # O: 其他应用杂项 (35)
    "backfill_batch_log", "cache_config", "channel", "channel_template", "cloud_migration",
    "content_translation", "data_edit_undo_chain", "data_permissions", "db_router",
    "http_action", "implicit_action", "instrument_snapshot", "label", "market_index_bar",
    "moderation_review", "persisted_info", "premium_features_token_cache", "python_library",
    "recent_views", "remote_sync_object", "remote_sync_task", "sandboxes", "saved_filters",
    "search_index__jejfwh7fotgr5m24qzoyt", "search_index__sk_ngkr0ktumqur8izppi",
    "search_index__v6jus6gi0_k0yrxvgxjki", "search_index__vm3ysrnv55fc7oqxhguhh",
    "search_index_metadata", "segment", "semantic_search_token_tracking", "sequences",
    "setting", "support_access_grant_log", "task_history", "task_run", "test",
    "user_key_value", "user_parameter_value", "view_log",
]


def main():
    # 安全检查：确认待删除表无项目表
    overlap = set(DROP_TABLES) & PROJECT_TABLES
    if overlap:
        print(f"[ERROR] 待删除清单中包含项目表: {overlap}")
        sys.exit(1)

    print(f"[安全检查通过] 待删除 {len(DROP_TABLES)} 张表，无项目表混入")
    print(f"项目内保留 {len(PROJECT_TABLES)} 张表")

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            # 查询实际存在的表
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            existing = {r[0] for r in cur.fetchall()}
            to_drop = [t for t in DROP_TABLES if t in existing]
            missing = [t for t in DROP_TABLES if t not in existing]
            print(f"实际存在待删除: {len(to_drop)} 张")
            if missing:
                print(f"已不存在(跳过): {len(missing)} 张")

            dropped = []
            failed = []
            for t in to_drop:
                try:
                    cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
                    dropped.append(t)
                except Exception as e:
                    failed.append((t, str(e)))
                    conn.rollback()

            conn.commit()
            print(f"[删除完成] 成功删除 {len(dropped)} 张表")
            if failed:
                print(f"[失败] {len(failed)} 张:")
                for t, e in failed:
                    print(f"  - {t}: {e}")


if __name__ == "__main__":
    main()
