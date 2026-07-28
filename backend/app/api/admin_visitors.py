"""[Gate5] /admin/visitors API - 访问统计只读端点（admin only）。

数据来源：
- 生产环境：GoAccess 容器周期性输出 JSON 报告到 /srv/goaccess/report.json（共享卷）
- 本地开发：报告文件不存在时返回 data_source="empty" + 空数据，前端展示空态

安全设计：
- 仅 admin 角色可访问（require_roles("admin")）
- IP 已由 GoAccess anonymize-ip=true 匿名化（保留前 3 段，末段为 0）
- 敏感 query 参数（token/jwt/password/key）由 nginx log_format 过滤后再写入 access.log
- 不重新解析日志，仅读取 GoAccess 输出的 JSON
- 不缓存响应（每次请求读取最新文件）

GoAccess JSON 报告路径由环境变量 GOACCESS_REPORT_PATH 控制，
默认 /srv/goaccess/report.json（与 docker-compose.prod.yml 挂载一致）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.schemas.visitors import VisitorMetricItem, VisitorReport, VisitorSummary

logger = logging.getLogger("admin_visitors")

router = APIRouter(prefix="/admin", tags=["admin-visitors"])

# GoAccess JSON 报告路径（生产由 docker-compose 挂载；本地不存在则返回空态）
GOACCESS_REPORT_PATH = os.environ.get(
    "GOACCESS_REPORT_PATH",
    "/srv/goaccess/report.json",
)

# 敏感 query 参数黑名单（出现在路径中时，前端展示需脱敏）
SENSITIVE_QUERY_KEYS = {"token", "jwt", "password", "passwd", "key", "secret", "api_key", "access_token"}


def _sanitize_path(path: str) -> str:
    """脱敏路径中的敏感 query 参数（仅保留参数名，不展示值）。

    示例：
    - "/api/v1/users?token=abc123" → "/api/v1/users?token=***"
    - "/market?symbol=600000" → "/market?symbol=600000"（非敏感参数保留）
    """
    if "?" not in path:
        return path
    base, query = path.split("?", 1)
    parts = []
    for kv in query.split("&"):
        if "=" in kv:
            k, _ = kv.split("=", 1)
            if k.lower() in SENSITIVE_QUERY_KEYS:
                parts.append(f"{k}=***")
            else:
                parts.append(kv)
        else:
            parts.append(kv)
    return f"{base}?{'&'.join(parts)}"


def _parse_goaccess_json(raw: dict) -> VisitorReport:
    """解析 GoAccess JSON 报告为 VisitorReport。

    GoAccess JSON 结构（--output-format=json）：
    {
      "data": {
        "visitors": {...},      # UV/IP
        "requests": {...},      # PV/路径
        "referrers": {...},
        "status_codes": {...},
        "browsers": {...},
        "operating_systems": {...},
        "visit_time": {...}
      },
      "generated_at": "2026-07-28T..."
    }

    本函数做字段映射 + Top N 截取 + 脱敏处理。
    GoAccess 不区分 today/7d/30d，需通过 --keep-last=N 天的日志输入控制；
    生产部署时通过三个独立 GoAccess 容器或 cron 脚本分别生成三个时间窗口的报告。
    当前实现：单一报告文件，三个时间窗口返回相同数据（占位），
    生产部署时可通过环境变量切换不同报告文件。
    """
    data = raw.get("data", {})
    generated_at_str = raw.get("generated_at")

    # 解析生成时间
    generated_at: datetime | None = None
    if generated_at_str:
        try:
            generated_at = datetime.fromisoformat(generated_at_str)
        except (ValueError, TypeError):
            generated_at = None

    def _extract_items(section: dict, top_n: int = 10) -> list[VisitorMetricItem]:
        """从 GoAccess 数据段提取 Top N 指标项。"""
        items_obj = section.get("data", []) if isinstance(section, dict) else []
        result: list[VisitorMetricItem] = []
        if not isinstance(items_obj, list):
            return result
        for item in items_obj[:top_n]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("data", item.get("label", "")))
            count = int(item.get("hits") or item.get("count") or 0)
            percentage = item.get("percent")
            result.append(
                VisitorMetricItem(
                    label=_sanitize_path(label) if label.startswith("/") else label,
                    count=count,
                    percentage=float(percentage) if percentage is not None else None,
                )
            )
        return result

    def _extract_count(section: dict, key: str = "total") -> int:
        """从 GoAccess 数据段提取总数。"""
        if not isinstance(section, dict):
            return 0
        # GoAccess 不同版本的 total 字段名不一，尝试多个
        for candidate in ("total", "count", "hits"):
            val = section.get(candidate)
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str) and val.isdigit():
                return int(val)
        # 退化为 items 列表长度
        items = section.get("data", [])
        return len(items) if isinstance(items, list) else 0

    # 提取各维度数据
    visitors_section = data.get("visitors", {})
    requests_section = data.get("requests", {})
    referrers_section = data.get("referrers", {})
    status_section = data.get("status_codes", {})
    browsers_section = data.get("browsers", {})
    os_section = data.get("operating_systems", {})
    time_section = data.get("visit_time", {})

    uv = _extract_count(visitors_section)
    pv = _extract_count(requests_section)

    summary = VisitorSummary(
        pv=pv,
        uv=uv,
        top_pages=_extract_items(requests_section, top_n=10),
        top_referrers=_extract_items(referrers_section, top_n=10),
        status_codes=_extract_items(status_section, top_n=10),
        devices=_extract_items(os_section, top_n=10),
        browsers=_extract_items(browsers_section, top_n=10),
        hourly_trend=_extract_items(time_section, top_n=24),
    )

    # 当前实现：三个时间窗口返回相同数据（生产部署可通过多个报告文件区分）
    return VisitorReport(
        today=summary,
        seven_days=summary.model_copy(deep=True),
        thirty_days=summary.model_copy(deep=True),
        generated_at=generated_at,
        data_source="goaccess_json",
        error_message=None,
    )


@router.get("/visitors", response_model=VisitorReport)
async def get_visitors_report(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> VisitorReport:
    """[Gate5] 查询访问统计报告（admin only）。

    返回今日/7日/30日的 PV/UV、热门页面、来源、状态码、设备/浏览器、时段趋势。
    IP 已匿名化（保留前 3 段），敏感 query 参数已脱敏。

    数据来源：GoAccess 容器输出的 JSON 报告（/srv/goaccess/report.json）。
    本地开发环境无 GoAccess 容器时，返回 data_source="empty" + 空数据。
    """
    report_path = Path(GOACCESS_REPORT_PATH)

    if not report_path.exists():
        logger.info("[Gate5] GoAccess 报告文件不存在: %s（本地开发返回空态）", report_path)
        return VisitorReport(
            data_source="empty",
            error_message=f"GoAccess 报告未生成（路径={report_path}）；生产环境请确认 goaccess 容器已启动",
        )

    try:
        raw_text = report_path.read_text(encoding="utf-8")
        raw_json = json.loads(raw_text)
        return _parse_goaccess_json(raw_json)
    except json.JSONDecodeError as exc:
        logger.warning("[Gate5] GoAccess 报告 JSON 解析失败: %s", exc)
        return VisitorReport(
            data_source="error",
            error_message=f"GoAccess 报告 JSON 解析失败: {exc}",
        )
    except Exception as exc:
        logger.warning("[Gate5] 读取 GoAccess 报告异常: %s", exc)
        return VisitorReport(
            data_source="error",
            error_message=f"读取 GoAccess 报告异常: {exc}",
        )


if __name__ == "__main__":
    # 自测：验证脱敏函数
    assert _sanitize_path("/api/v1/users") == "/api/v1/users"
    assert _sanitize_path("/api/v1/users?token=abc123") == "/api/v1/users?token=***"
    assert _sanitize_path("/market?symbol=600000") == "/market?symbol=600000"
    assert _sanitize_path("/api?token=x&jwt=y&name=z") == "/api?token=***&jwt=***&name=z"
    print("PASS: _sanitize_path")

    # 自测：空报告
    report = VisitorReport(data_source="empty")
    assert report.today.pv == 0
    assert report.today.uv == 0
    assert report.today.top_pages == []
    print("PASS: empty report")

    # 自测：解析空 JSON
    parsed = _parse_goaccess_json({"data": {}, "generated_at": "2026-07-28T10:00:00"})
    assert parsed.data_source == "goaccess_json"
    assert parsed.today.pv == 0
    print("PASS: parse empty GoAccess JSON")

    print("OK")
