#!/usr/bin/env python3
"""文档自动生成脚本 - 从事实源生成数据结构文档与操作手册。

事实源：
- ORM 模型（app.models.bar, app.models.instrument）→ 表结构
- API 路由（app.api.bars）→ API 规格
- 服务配置（bars_metrics, bars_retention, bars_scheduler_service, freshness_sla, reconcile_bars）
  → 监控指标、保留策略、调度配置、SLA、对账参数

Inputs:
    无外部输入，全部从代码事实源提取

Outputs:
    docs/数据结构.md（6 张 bar 表结构 + instruments 表 + 数据流图 + 保留策略）
    docs/操作手册.md（API 规格 + 调度任务 + 监控指标 + 故障排查 + 对账操作 + 保留策略）

How to Run:
    python tools/update_docs.py           # 生成文档
    python tools/update_docs.py --check   # 一致性检查（不写入，仅比对）

Examples:
    python tools/update_docs.py
    python tools/update_docs.py --check

Side Effects:
    生成/覆盖 docs/数据结构.md 与 docs/操作手册.md（--check 模式无副作用）
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import datetime
from typing import Any

# 将 backend 目录加入 sys.path，以便导入 app 模块
_BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
_BACKEND_DIR = os.path.abspath(_BACKEND_DIR)
sys.path.insert(0, _BACKEND_DIR)

from app.models.bar import (  # noqa: E402
    Bar15Min,
    Bar60Min,
    BarDaily,
    BarMinute,
    BarMonthly,
    BarWeekly,
)
from app.models.instrument import Instrument  # noqa: E402

# 项目根目录（docs/ 所在位置）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DOCS_DIR = os.path.join(_PROJECT_ROOT, "docs")
_DB_SCHEMA_PATH = os.path.join(_DOCS_DIR, "数据结构.md")
_OPS_MANUAL_PATH = os.path.join(_DOCS_DIR, "操作手册.md")

# 6 张 bar 表，按周期粒度排序
_BAR_MODELS = [BarDaily, BarMinute, BarWeekly, BarMonthly, Bar15Min, Bar60Min]


# ---------------------------------------------------------------------------
# ORM 模型元数据提取
# ---------------------------------------------------------------------------

def _extract_column_info(col: Any) -> dict:
    """从 SQLAlchemy Column 提取字段元数据。

    Returns:
        dict: name, type, nullable, default, primary_key, foreign_key
    """
    fk_refs = []
    for fk in col.foreign_keys:
        fk_refs.append(f"{fk.column.table.name}.{fk.column.name}")

    return {
        "name": col.name,
        "type": str(col.type),
        "nullable": col.nullable,
        "default": str(col.default.arg) if col.default and col.default.arg is not None else None,
        "primary_key": col.primary_key,
        "foreign_key": ", ".join(fk_refs) if fk_refs else None,
    }


def _extract_table_info(model_cls: Any) -> dict:
    """从 ORM 模型类提取表结构元数据。

    Returns:
        dict: table_name, columns, primary_key, foreign_keys, indexes, docstring
    """
    table = model_cls.__table__
    columns = [_extract_column_info(col) for col in table.columns]
    pk_cols = [c.name for c in table.primary_key.columns]
    indexes = [
        {"name": idx.name, "columns": [c.name for c in idx.columns]}
        for idx in table.indexes
    ]
    fk_constraints = []
    for constraint in table.constraints:
        if hasattr(constraint, "elements") and constraint.__class__.__name__ == "ForeignKeyConstraint":
            for fk in constraint.elements:
                fk_constraints.append({
                    "columns": [col.name for col in constraint.columns],
                    "ref_table": fk.column.table.name,
                    "ref_column": fk.column.name,
                })

    return {
        "table_name": table.name,
        "columns": columns,
        "primary_key": pk_cols,
        "foreign_keys": fk_constraints,
        "indexes": indexes,
        "docstring": model_cls.__doc__ or "",
    }


# ---------------------------------------------------------------------------
# API 路由元数据提取
# ---------------------------------------------------------------------------

def _extract_api_routes() -> list[dict]:
    """从 bars API 路由提取端点元数据。

    Returns:
        list[dict]: path, methods, summary, params
    """
    from app.api.bars import router

    routes = []
    for route in router.routes:
        params = []
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            for dep in dependant.query_params:
                field_info = getattr(dep, "field_info", None)
                default = getattr(field_info, "default", None) if field_info else None
                # required = default 为 None 且非 Optional
                required = getattr(dep, "required", default is None)
                params.append({
                    "name": dep.name,
                    "type": str(getattr(field_info, "annotation", "str") if field_info else "str"),
                    "required": required,
                    "default": default,
                    "description": getattr(field_info, "description", "") if field_info else "",
                })

        routes.append({
            "path": route.path,
            "methods": list(route.methods) if hasattr(route, "methods") else [],
            "summary": getattr(route, "summary", "") or "",
            "params": params,
        })

    return routes


# ---------------------------------------------------------------------------
# 服务配置提取
# ---------------------------------------------------------------------------

def _extract_metrics_info() -> list[dict]:
    """从 bars_metrics 提取 Prometheus 指标定义。

    Returns:
        list[dict]: name, type, help, labelnames
    """
    from app.services.bars_metrics import (
        bars_cache_hits_total,
        bars_cache_misses_total,
        bars_fetch_duration_seconds,
        bars_fetch_total,
        bars_freshness_age_seconds,
        bars_query_duration_seconds,
        bars_query_total,
        bars_retention_deleted_total,
        bars_upsert_records,
        bars_upsert_total,
    )

    def _get_labelnames(metric: Any) -> tuple[str, ...]:
        return getattr(metric, "labelnames", None) or getattr(metric, "_labelnames", ())

    def _get_help(metric: Any) -> str:
        return getattr(metric, "help", "") or getattr(metric, "documentation", "")

    def _get_type(metric: Any) -> str:
        return getattr(metric, "metric_type", metric.__class__.__name__.lower())

    metrics = [
        bars_fetch_total,
        bars_fetch_duration_seconds,
        bars_upsert_total,
        bars_upsert_records,
        bars_query_total,
        bars_query_duration_seconds,
        bars_cache_hits_total,
        bars_cache_misses_total,
        bars_freshness_age_seconds,
        bars_retention_deleted_total,
    ]

    return [
        {
            "name": getattr(m, "name", ""),
            "type": _get_type(m),
            "help": _get_help(m),
            "labelnames": list(_get_labelnames(m)),
        }
        for m in metrics
    ]


def _extract_retention_config() -> list[dict]:
    """从 bars_retention 提取保留策略配置。"""
    from app.services.bars_retention import get_retention_config
    return get_retention_config()


def _extract_scheduler_config() -> dict:
    """从 bars_scheduler_service 提取调度配置。"""
    from app.services.bars_scheduler_service import BarsSchedulerService
    return {
        "daily_counts": BarsSchedulerService.DAILY_COUNTS,
        "backfill_counts": BarsSchedulerService.BACKFILL_COUNTS,
        "max_retries": BarsSchedulerService.MAX_RETRIES,
        "retry_delay": BarsSchedulerService.RETRY_DELAY,
    }


def _extract_sla_config() -> dict:
    """从 freshness_sla 提取 SLA 配置。"""
    # 读取所有 SLA 常量
    import app.services.freshness_sla as sla_mod
    from app.services.freshness_sla import (
        BAR_15MIN_SLA_SECONDS,
        DAILY_SLA_SECONDS,
        MINUTE_SLA_SECONDS,
    )
    sla_consts = {}
    for name in dir(sla_mod):
        if name.endswith("_SLA_SECONDS"):
            sla_consts[name] = getattr(sla_mod, name)
    return {
        "daily_sla_seconds": DAILY_SLA_SECONDS,
        "minute_sla_seconds": MINUTE_SLA_SECONDS,
        "bar_15min_sla_seconds": BAR_15MIN_SLA_SECONDS,
        "all_sla_constants": sla_consts,
    }


def _extract_reconcile_config() -> dict:
    """从 reconcile_bars 提取对账配置。"""
    import app.services.reconcile_bars as recon_mod
    return {
        "mismatch_tolerance": recon_mod._MISMATCH_TOLERANCE,
        "max_mismatch_details": recon_mod._MAX_MISMATCH_DETAILS,
        "default_batch_sample_size": recon_mod._DEFAULT_BATCH_SAMPLE_SIZE,
        "default_batch_days": recon_mod._DEFAULT_BATCH_DAYS,
    }


# ---------------------------------------------------------------------------
# 文档生成：数据结构
# ---------------------------------------------------------------------------

def generate_db_schema_doc() -> str:
    """生成数据结构文档（docs/数据结构.md）。

    内容：6 张 bar 表结构 + instruments 表 + 数据流图 + 保留策略。
    """
    buf = io.StringIO()
    w = buf.write

    w("# 数据结构文档\n\n")
    w(f"> 自动生成 by tools/update_docs.py | 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    w("> 事实源: ORM 模型 (app.models.bar, app.models.instrument) + 服务配置\n\n")
    w("---\n\n")

    # 1. 表结构总览
    w("## 1. 表结构总览\n\n")
    w("| 表名 | 用途 | 主键 | 外键 |\n")
    w("|------|------|------|------|\n")

    all_models = _BAR_MODELS + [Instrument]
    for model_cls in all_models:
        info = _extract_table_info(model_cls)
        pk_str = ", ".join(info["primary_key"])
        fk_str = "; ".join(
            f"{','.join(fk['columns'])} → {fk['ref_table']}.{fk['ref_column']}"
            for fk in info["foreign_keys"]
        ) or "无"
        # 从 docstring 提取用途（第一行）
        purpose = info["docstring"].strip().split("\n")[0] if info["docstring"] else ""
        w(f"| {info['table_name']} | {purpose} | ({pk_str}) | {fk_str} |\n")
    w("\n")

    # 2. 各表详细结构
    w("## 2. 各表详细结构\n\n")

    for model_cls in all_models:
        info = _extract_table_info(model_cls)
        w(f"### {info['table_name']}\n\n")
        if info["docstring"]:
            w(f"{info['docstring'].strip()}\n\n")

        w(f"**主键**: ({', '.join(info['primary_key'])})\n\n")

        if info["foreign_keys"]:
            fk_lines = [
                f"- ({','.join(fk['columns'])}) → {fk['ref_table']}.{fk['ref_column']}"
                for fk in info["foreign_keys"]
            ]
            w("**外键**:\n")
            for line in fk_lines:
                w(line + "\n")
            w("\n")

        if info["indexes"]:
            w("**索引**:\n")
            for idx in info["indexes"]:
                w(f"- {idx['name']}: ({', '.join(idx['columns'])})\n")
            w("\n")

        w("| 字段名 | 类型 | 可空 | 默认值 | 主键 | 外键 | 说明 |\n")
        w("|--------|------|------|--------|------|------|------|\n")

        # 字段说明映射
        field_desc = _get_field_descriptions(info["table_name"])

        for col in info["columns"]:
            nullable = "是" if col["nullable"] else "否"
            default = col["default"] or ""
            pk = "是" if col["primary_key"] else ""
            fk = col["foreign_key"] or ""
            desc = field_desc.get(col["name"], "")
            w(f"| {col['name']} | {col['type']} | {nullable} | {default} | {pk} | {fk} | {desc} |\n")
        w("\n")

    # 3. 数据流图
    w("## 3. 数据流图\n\n")
    w("```mermaid\n")
    w("graph TD\n")
    w("    A[pytdx 数据源] -->|get_*_bars| B[PytdxAdapter]\n")
    w("    B -->|raw_df| C[bar_repository._upsert_*_bars]\n")
    w("    C -->|validate_bars 校验| D{校验通过?}\n")
    w("    D -->|是| E[adj_factor 计算]\n")
    w("    D -->|否| F[跳过写入 + 记录错误]\n")
    w("    E --> G[upsert to PostgreSQL]\n")
    w("    G -->|查询| H[bar_repository._query_*_bars]\n")
    w("    H -->|DataFrame| I[bars_cache Redis 缓存]\n")
    w("    I -->|API 响应| J[GET /api/v1/instruments/&#123;id&#125;/bars]\n")
    w("    G -->|对账| K[reconcile_bars.reconcile_instrument]\n")
    w("    G -->|保留策略| L[bars_retention.apply_retention_policy]\n")
    w("    G -->|新鲜度| M[freshness_sla.check_freshness]\n")
    w("```\n\n")

    # 4. 保留策略
    w("## 4. 保留策略\n\n")
    retention = _extract_retention_config()
    w("| 表名 | 时间列 | 保留期限 | 说明 |\n")
    w("|------|--------|----------|------|\n")
    for r in retention:
        desc = "永久保留" if r["is_permanent"] else f"{r['retention_days']} 天"
        note = "不清理" if r["is_permanent"] else f"清理 {r['time_column']} < cutoff 的数据"
        w(f"| {r['table_name']} | {r['time_column']} | {desc} | {note} |\n")
    w("\n")

    # 5. SLA 配置
    w("## 5. 数据新鲜度 SLA\n\n")
    sla = _extract_sla_config()
    w("| 周期 | SLA 常量 | 秒数 | 说明 |\n")
    w("|------|----------|------|------|\n")
    sla_names = {
        "daily": ("DAILY_SLA_SECONDS", "日线收盘后 30 分钟内更新"),
        "minute": ("MINUTE_SLA_SECONDS", "盘中分钟数据近实时"),
        "15min": ("BAR_15MIN_SLA_SECONDS", "15 分钟线周期结束后 15 分钟内更新"),
    }
    for period, (const_name, desc) in sla_names.items():
        val = sla["all_sla_constants"].get(const_name, "")
        w(f"| {period} | {const_name} | {val} | {desc} |\n")
    # 补充其他 SLA 常量
    w("\n")
    w("**所有 SLA 常量**:\n\n")
    for name, val in sorted(sla["all_sla_constants"].items()):
        w(f"- `{name}` = {val}\n")
    w("\n")

    return buf.getvalue()


def _get_field_descriptions(table_name: str) -> dict:
    """获取字段中文说明（基于表名映射）。"""
    common = {
        "instrument_id": "标的 UUID（FK → instruments.id）",
        "trade_date": "交易日期（日线/周线/月线）",
        "trade_time": "交易时间（分钟线/15min/60min）",
        "open": "开盘价 NUMERIC(20,4)",
        "high": "最高价 NUMERIC(20,4)",
        "low": "最低价 NUMERIC(20,4)",
        "close": "收盘价 NUMERIC(20,4)",
        "volume": "成交量 NUMERIC(20,2)，日线单位为股，周线/月线单位为手",
        "amount": "成交额 NUMERIC(20,2)",
        "adj_factor": "前复权因子 NUMERIC(20,8)，默认 1.0",
    }
    instrument_fields = {
        "id": "UUID 主键（数据库生成 gen_random_uuid()）",
        "symbol": "股票代码（唯一，如 000001）",
        "name": "股票名称",
        "market": "市场（SH/SZ/BJ）",
        "status": "状态（active/delisted/suspended）",
        "listing_date": "上市日期（可空）",
        "created_at": "创建时间戳",
        "updated_at": "更新时间戳",
    }
    if table_name == "instruments":
        return instrument_fields
    return common


# ---------------------------------------------------------------------------
# 文档生成：操作手册
# ---------------------------------------------------------------------------

def generate_ops_manual_doc() -> str:
    """生成操作手册文档（docs/操作手册.md）。

    内容：API 规格 + 调度任务 + 监控指标 + 故障排查 + 对账操作 + 保留策略。
    """
    buf = io.StringIO()
    w = buf.write

    w("# 操作手册\n\n")
    w(f"> 自动生成 by tools/update_docs.py | 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    w("> 事实源: API 路由 (app.api.bars) + 服务配置\n\n")
    w("---\n\n")

    # 1. API 规格
    w("## 1. API 规格\n\n")
    routes = _extract_api_routes()
    for route in routes:
        methods = ", ".join(route["methods"])
        w(f"### {methods} `{route['path']}`\n\n")
        w(f"**摘要**: {route['summary']}\n\n")

        if route["params"]:
            w("**参数**:\n\n")
            w("| 参数名 | 类型 | 必填 | 默认值 | 说明 |\n")
            w("|--------|------|------|--------|------|\n")
            for p in route["params"]:
                required = "是" if p["required"] else "否"
                default = str(p["default"]) if p["default"] is not None else ""
                w(f"| {p['name']} | {p['type']} | {required} | {default} | {p['description']} |\n")
            w("\n")
        else:
            w("无查询参数。\n\n")

    # 2. 调度任务配置
    w("## 2. 调度任务配置\n\n")
    w("### 2.1 行情定时更新\n\n")
    w("- **触发时间**: 每个交易日（周一至周五）16:00\n")
    w("- **Worker 启动**: `WORKER_TYPE=bars_scheduler python -m app.worker`\n")
    w("- **调度框架**: APScheduler (AsyncIOScheduler + CronTrigger)\n")
    w("- **任务 ID**: `bars_refresh_daily`\n")
    w("- **拉取方式**: 串行（pytdx 不支持并发），通过 asyncio.to_thread 桥接\n")
    w("- **失败重试**: 最多 3 次，间隔 5 秒\n\n")

    sched = _extract_scheduler_config()
    w("### 2.2 拉取配置\n\n")
    w("**每日增量更新（DAILY_COUNTS，小 count，约 1.8 小时）**:\n\n")
    w("| 周期 | count |\n|------|-------|\n")
    for period, count in sched["daily_counts"].items():
        w(f"| {period} | {count} |\n")
    w("\n")

    w("**历史回补（BACKFILL_COUNTS，大 count，约 11.1 小时）**:\n\n")
    w("| 周期 | count |\n|------|-------|\n")
    for period, count in sched["backfill_counts"].items():
        w(f"| {period} | {count} |\n")
    w("\n")

    w(f"**重试配置**: MAX_RETRIES={sched['max_retries']}, RETRY_DELAY={sched['retry_delay']}秒\n\n")

    w("### 2.3 保留策略清理\n\n")
    w("- **触发时间**: 每日 02:00（避开交易时间）\n")
    w("- **调度方式**: APScheduler（在 bars_scheduler_service.run_retention_cleanup 中调用）\n")
    w("- **清理逻辑**: DELETE FROM ... WHERE trade_time < :cutoff（向量化删除）\n\n")

    # 3. 监控指标
    w("## 3. 监控指标（Prometheus）\n\n")
    w("指标定义在 `app/services/bars_metrics.py`，共 10 个指标。\n\n")
    metrics = _extract_metrics_info()
    w("| 指标名 | 类型 | 说明 | labels |\n")
    w("|--------|------|------|--------|\n")
    for m in metrics:
        labels = ", ".join(m["labelnames"]) if m["labelnames"] else "无"
        w(f"| {m['name']} | {m['type']} | {m['help']} | {labels} |\n")
    w("\n")
    w("**指标端点**: `GET /metrics`（无需认证）\n\n")

    # 4. 数据对账操作
    w("## 4. 数据对账操作\n\n")
    recon = _extract_reconcile_config()
    w("对账机制定义在 `app/services/reconcile_bars.py`，对比 DB 数据与 pytdx 源数据。\n\n")
    w(f"- **值不一致容差**: {recon['mismatch_tolerance']}\n")
    w(f"- **不一致详情最大保留条数**: {recon['max_mismatch_details']}\n")
    w(f"- **批量对账默认抽样数量**: {recon['default_batch_sample_size']} 只股票\n")
    w(f"- **批量对账默认天数**: {recon['default_batch_days']} 天\n\n")

    w("### 4.1 单只股票对账\n\n")
    w("```python\n")
    w("from app.services.reconcile_bars import reconcile_instrument\n")
    w("from app.db import AsyncSessionLocal\n\n")
    w("async def check_one():\n")
    w("    async with AsyncSessionLocal() as session:\n")
    w("        result = await reconcile_instrument(\n")
    w("            session, instrument_id, symbol, period='d',\n")
    w("            start_date=date(2026, 1, 1), end_date=date(2026, 6, 1)\n")
    w("        )\n")
    w("        print(result)\n")
    w("```\n\n")

    w("### 4.2 批量对账\n\n")
    w("```python\n")
    w("from app.services.reconcile_bars import reconcile_batch\n")
    w("from app.db import AsyncSessionLocal\n\n")
    w("async def check_batch():\n")
    w("    async with AsyncSessionLocal() as session:\n")
    w("        results = await reconcile_batch(session, period='d', days=30)\n")
    w("        for r in results:\n")
    w("            print(f\"{r.symbol}: missing={r.missing_count}, extra={r.extra_count}, mismatch={r.mismatch_count}\")\n")
    w("```\n\n")

    # 5. 保留策略配置
    w("## 5. 保留策略配置\n\n")
    w("保留策略定义在 `app/services/bars_retention.py`。\n\n")
    retention = _extract_retention_config()
    w("| 表名 | 时间列 | 保留期限 | 说明 |\n")
    w("|------|--------|----------|------|\n")
    for r in retention:
        desc = "永久保留" if r["is_permanent"] else f"{r['retention_days']} 天"
        w(f"| {r['table_name']} | {r['time_column']} | {desc} | {'不清理' if r['is_permanent'] else '自动清理过期数据'} |\n")
    w("\n")

    w("### 5.1 手动执行保留策略\n\n")
    w("```python\n")
    w("from app.services.bars_retention import apply_retention_policy\n")
    w("from app.db import AsyncSessionLocal\n\n")
    w("async def cleanup():\n")
    w("    async with AsyncSessionLocal() as session:\n")
    w("        # 预检模式（只统计不删除）\n")
    w("        results = await apply_retention_policy(session, dry_run=True)\n")
    w("        for r in results:\n")
    w("            print(f\"{r.table_name}: 待删除={r.deleted_count}, cutoff={r.cutoff_date}\")\n")
    w("```\n\n")

    # 6. 故障排查
    w("## 6. 故障排查\n\n")
    w("### 6.1 行情数据不显示\n\n")
    w("1. 检查 `GET /api/v1/bars/health` 确认 DB/Redis 连通性\n")
    w("2. 检查对应周期的数据新鲜度（freshness_sla）\n")
    w("3. 若 DB 无数据，检查 pytdx 拉取日志（bars_fetch_total 指标）\n")
    w("4. 若 adj_factor=1.0，需重新拉取日线数据以触发复权因子计算\n\n")

    w("### 6.2 前复权价格异常\n\n")
    w("1. 检查 `bars_daily` 表中 adj_factor 是否为 1.0（默认值）\n")
    w("2. 若 adj_factor=1.0，执行 `POST /api/v1/admin/bars/refresh` 刷新日线\n")
    w("3. 验证 adj_factor 通过 Chanlunpro preclose 公式计算正确\n\n")

    w("### 6.3 定时任务未执行\n\n")
    w("1. 检查 worker 进程: `WORKER_TYPE=bars_scheduler python -m app.worker`\n")
    w("2. 检查 APScheduler 日志: `bars_refresh_daily` 任务是否注册\n")
    w("3. 检查交易日历: 非交易日不触发（is_trading_day_async 判断）\n")
    w("4. 手动触发: `POST /api/v1/admin/bars/refresh`\n\n")

    w("### 6.4 历史数据回补\n\n")
    w("1. 手动触发: `POST /api/v1/admin/bars/backfill?start_date=2023-01-01`\n")
    w("2. 耗时约 11.1 小时（全市场 8000+ 股票 × 5 周期）\n")
    w("3. 回补使用 BACKFILL_COUNTS（大 count）\n")
    w("4. 回补后执行对账验证数据完整性\n\n")

    return buf.getvalue()


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def _write_file(path: str, content: str) -> None:
    """写入文件（自动创建目录）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read_file(path: str) -> str | None:
    """读取文件，不存在时返回 None。"""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def main() -> int:
    """主函数：生成或检查文档。"""
    parser = argparse.ArgumentParser(description="从事实源生成数据结构文档与操作手册")
    parser.add_argument(
        "--check",
        action="store_true",
        help="一致性检查模式：比对现有文档与事实源生成的文档，不一致时返回非 0 退出码",
    )
    args = parser.parse_args()

    print("从事实源提取元数据...")
    db_schema = generate_db_schema_doc()
    ops_manual = generate_ops_manual_doc()
    print(f"  数据结构.md: {len(db_schema)} 字符")
    print(f"  操作手册.md: {len(ops_manual)} 字符")

    if args.check:
        # 一致性检查模式（忽略生成时间戳行，因每次运行时间不同）
        import re

        def _normalize_timestamp(text: str) -> str:
            """将生成时间戳行替换为占位符，避免时间差异导致校验失败。"""
            return re.sub(
                r"生成时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                "生成时间: <NORMALIZED>",
                text,
            )

        print("\n一致性检查模式 (--check)")
        existing_db = _read_file(_DB_SCHEMA_PATH)
        existing_ops = _read_file(_OPS_MANUAL_PATH)

        mismatch = False

        if existing_db is None:
            print(f"  [FAIL] {_DB_SCHEMA_PATH} 不存在")
            mismatch = True
        elif _normalize_timestamp(existing_db) != _normalize_timestamp(db_schema):
            print(f"  [FAIL] {_DB_SCHEMA_PATH} 内容不一致")
            mismatch = True
        else:
            print(f"  [OK] {_DB_SCHEMA_PATH} 一致")

        if existing_ops is None:
            print(f"  [FAIL] {_OPS_MANUAL_PATH} 不存在")
            mismatch = True
        elif _normalize_timestamp(existing_ops) != _normalize_timestamp(ops_manual):
            print(f"  [FAIL] {_OPS_MANUAL_PATH} 内容不一致")
            mismatch = True
        else:
            print(f"  [OK] {_OPS_MANUAL_PATH} 一致")

        if mismatch:
            print("\n一致性检查失败：文档与事实源不一致，请运行 `python tools/update_docs.py` 重建")
            return 1
        print("\n一致性检查通过 ✓")
        return 0

    # 生成模式
    print("\n生成文档...")
    _write_file(_DB_SCHEMA_PATH, db_schema)
    print(f"  [OK] {_DB_SCHEMA_PATH}")
    _write_file(_OPS_MANUAL_PATH, ops_manual)
    print(f"  [OK] {_OPS_MANUAL_PATH}")
    print("\n文档生成完成 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
