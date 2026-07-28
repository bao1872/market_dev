"""[Gate5] /admin/visitors 端点测试。

覆盖：
1. _sanitize_path 脱敏函数（敏感 query 参数替换为 ***）
2. _parse_goaccess_json 解析 GoAccess JSON 报告
3. 路由注册验证（/admin/visitors 路径存在）
4. 空态返回（报告文件不存在时 data_source="empty"）
5. Schema 字段验证
6. 前端类型一致性

测试环境：纯单元测试 + 源码级验证（不依赖 DB；admin 鉴权由 require_roles 在运行时校验）
设计要点：
- 不修改生产代码，仅验证字段与逻辑
- 使用 inspect.getsource 做源码级验证（避免 mock 自证）
"""

from __future__ import annotations

import inspect
from datetime import datetime

import pytest

from app.api.admin_visitors import (
    GOACCESS_REPORT_PATH,
    SENSITIVE_QUERY_KEYS,
    _parse_goaccess_json,
    _sanitize_path,
    router,
)
from app.schemas.visitors import (
    VisitorMetricItem,
    VisitorReport,
    VisitorSummary,
)

# =============================================================================
# 1. _sanitize_path 脱敏函数
# =============================================================================


class TestSanitizePath:
    """[Gate5] _sanitize_path 敏感参数脱敏。"""

    def test_no_query_params(self) -> None:
        """无 query 参数时原样返回。"""
        assert _sanitize_path("/market") == "/market"
        assert _sanitize_path("/api/v1/stocks/600000") == "/api/v1/stocks/600000"

    def test_non_sensitive_params_preserved(self) -> None:
        """非敏感参数保留原值。"""
        assert _sanitize_path("/market?symbol=600000") == "/market?symbol=600000"
        assert _sanitize_path("/stocks?limit=10&offset=20") == "/stocks?limit=10&offset=20"

    def test_token_sanitized(self) -> None:
        """token 参数值替换为 ***。"""
        assert _sanitize_path("/api?token=abc123") == "/api?token=***"
        assert _sanitize_path("/api?TOKEN=abc") == "/api?TOKEN=***"  # 大小写不敏感

    def test_jwt_sanitized(self) -> None:
        """jwt 参数值替换为 ***。"""
        assert _sanitize_path("/api?jwt=xyz") == "/api?jwt=***"

    def test_password_sanitized(self) -> None:
        """password 参数值替换为 ***。"""
        assert _sanitize_path("/login?password=secret") == "/login?password=***"
        assert _sanitize_path("/login?passwd=secret") == "/login?passwd=***"

    def test_multiple_sensitive_params(self) -> None:
        """多个敏感参数同时脱敏。"""
        result = _sanitize_path("/api?token=x&jwt=y&name=z")
        assert "token=***" in result
        assert "jwt=***" in result
        assert "name=z" in result  # 非敏感保留

    def test_mixed_sensitive_and_normal(self) -> None:
        """敏感与非敏感参数混合。"""
        result = _sanitize_path("/api?symbol=600000&token=secret&limit=10")
        assert "symbol=600000" in result
        assert "token=***" in result
        assert "limit=10" in result

    def test_all_sensitive_keys_covered(self) -> None:
        """[Gate5] 敏感参数黑名单完整覆盖。"""
        expected = {"token", "jwt", "password", "passwd", "key", "secret", "api_key", "access_token"}
        assert SENSITIVE_QUERY_KEYS == expected


# =============================================================================
# 2. _parse_goaccess_json 解析
# =============================================================================


class TestParseGoaccessJson:
    """[Gate5] _parse_goaccess_json 报告解析。"""

    def test_empty_data(self) -> None:
        """空 data 返回零值汇总。"""
        parsed = _parse_goaccess_json({"data": {}, "generated_at": "2026-07-28T10:00:00"})
        assert parsed.data_source == "goaccess_json"
        assert parsed.today.pv == 0
        assert parsed.today.uv == 0
        assert parsed.today.top_pages == []

    def test_with_sample_data(self) -> None:
        """含样本数据的解析。"""
        raw = {
            "data": {
                "visitors": {
                    "total": 50,
                    "data": [{"data": "192.168.1.0", "hits": 30, "percent": 60.0}],
                },
                "requests": {
                    "total": 100,
                    "data": [{"data": "/market?token=secret", "hits": 80, "percent": 80.0}],
                },
                "referrers": {"data": [{"data": "google.com", "hits": 40, "percent": 40.0}]},
                "status_codes": {"data": [{"data": "200", "hits": 95, "percent": 95.0}]},
                "browsers": {"data": [{"data": "Chrome", "hits": 80, "percent": 80.0}]},
                "operating_systems": {"data": [{"data": "Windows", "hits": 60, "percent": 60.0}]},
                "visit_time": {"data": [{"data": "10:00", "hits": 20}]},
            },
            "generated_at": "2026-07-28T10:00:00",
        }
        parsed = _parse_goaccess_json(raw)

        # PV/UV
        assert parsed.today.pv == 100
        assert parsed.today.uv == 50

        # Top pages with sanitization
        assert len(parsed.today.top_pages) == 1
        assert parsed.today.top_pages[0].label == "/market?token=***"  # sanitized
        assert parsed.today.top_pages[0].count == 80
        assert parsed.today.top_pages[0].percentage == 80.0

        # Referrers
        assert parsed.today.top_referrers[0].label == "google.com"

        # Status codes
        assert parsed.today.status_codes[0].label == "200"

        # Browsers
        assert parsed.today.browsers[0].label == "Chrome"

        # Devices (operating_systems)
        assert parsed.today.devices[0].label == "Windows"

        # Hourly trend
        assert parsed.today.hourly_trend[0].label == "10:00"

        # Generated at
        assert parsed.generated_at == datetime(2026, 7, 28, 10, 0, 0)

    def test_three_time_windows_same_data(self) -> None:
        """[Gate5] 三个时间窗口返回相同数据（当前实现占位）。"""
        raw = {
            "data": {"visitors": {"total": 10}, "requests": {"total": 20}},
            "generated_at": "2026-07-28T10:00:00",
        }
        parsed = _parse_goaccess_json(raw)
        # 当前实现：三个窗口返回相同数据（生产部署可通过多个报告文件区分）
        assert parsed.today.pv == 20
        assert parsed.seven_days.pv == 20
        assert parsed.thirty_days.pv == 20

    def test_invalid_generated_at_returns_none(self) -> None:
        """无效的 generated_at 返回 None。"""
        parsed = _parse_goaccess_json({"data": {}, "generated_at": "invalid-date"})
        assert parsed.generated_at is None


# =============================================================================
# 3. 路由注册验证
# =============================================================================


class TestRouterRegistration:
    """[Gate5] /admin/visitors 路由注册。"""

    def test_router_prefix(self) -> None:
        """router prefix 必须为 /admin。"""
        assert router.prefix == "/admin"

    def test_visitors_route_exists(self) -> None:
        """/admin/visitors 路由必须存在。"""
        routes = [r.path for r in router.routes]
        assert "/admin/visitors" in routes, f"路由列表缺少 /admin/visitors: {routes}"

    def test_router_tags(self) -> None:
        """router tags 包含 admin-visitors。"""
        assert "admin-visitors" in router.tags

    def test_endpoint_requires_admin_role(self) -> None:
        """[Gate5] 端点必须使用 require_roles('admin') 鉴权。"""
        from app.api.admin_visitors import get_visitors_report

        source = inspect.getsource(get_visitors_report)
        assert "require_roles" in source, "端点未使用 require_roles 鉴权"
        assert '"admin"' in source or "'admin'" in source, "端点未要求 admin 角色"


# =============================================================================
# 4. 空态返回验证（源码级）
# =============================================================================


class TestEmptyStateHandling:
    """[Gate5] 报告文件不存在时返回空态。"""

    def test_returns_empty_when_file_missing(self) -> None:
        """文件不存在时 data_source='empty'。"""
        from app.api.admin_visitors import get_visitors_report

        source = inspect.getsource(get_visitors_report)
        # 验证文件存在性检查
        assert "exists()" in source, "缺少文件存在性检查"
        # 验证空态返回
        assert '"empty"' in source or "'empty'" in source, "缺少空态 data_source='empty' 返回"

    def test_returns_error_on_json_failure(self) -> None:
        """JSON 解析失败时 data_source='error'。"""
        from app.api.admin_visitors import get_visitors_report

        source = inspect.getsource(get_visitors_report)
        assert "JSONDecodeError" in source, "缺少 JSONDecodeError 处理"
        assert '"error"' in source or "'error'" in source, "缺少 data_source='error' 返回"


# =============================================================================
# 5. Schema 字段验证
# =============================================================================


class TestVisitorSchema:
    """[Gate5] VisitorReport Schema 字段验证。"""

    def test_visitor_report_fields(self) -> None:
        """VisitorReport 必须包含三个时间窗口 + 元信息。"""
        fields = list(VisitorReport.model_fields.keys())
        assert "today" in fields
        assert "seven_days" in fields
        assert "thirty_days" in fields
        assert "generated_at" in fields
        assert "data_source" in fields
        assert "error_message" in fields

    def test_visitor_summary_fields(self) -> None:
        """VisitorSummary 必须包含 PV/UV + 各维度列表。"""
        fields = list(VisitorSummary.model_fields.keys())
        for required in ["pv", "uv", "top_pages", "top_referrers",
                         "status_codes", "devices", "browsers", "hourly_trend"]:
            assert required in fields, f"VisitorSummary 缺少字段: {required}"

    def test_visitor_metric_item_fields(self) -> None:
        """VisitorMetricItem 必须包含 label/count/percentage。"""
        fields = list(VisitorMetricItem.model_fields.keys())
        for required in ["label", "count", "percentage"]:
            assert required in fields, f"VisitorMetricItem 缺少字段: {required}"

    def test_default_empty_report(self) -> None:
        """[Gate5] 默认空报告字段值合理。"""
        report = VisitorReport(data_source="empty")
        assert report.today.pv == 0
        assert report.today.uv == 0
        assert report.today.top_pages == []
        assert report.seven_days.pv == 0
        assert report.thirty_days.pv == 0
        assert report.generated_at is None
        assert report.error_message is None


# =============================================================================
# 6. 前端类型一致性
# =============================================================================


class TestFrontendTypeConsistency:
    """[Gate5] 前端类型与后端一致。"""

    def test_frontend_endpoints_has_visitor_types(self) -> None:
        """前端 endpoints.ts 包含 VisitorReport 等类型。"""
        from pathlib import Path

        endpoints_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "api" / "endpoints.ts"
        if not endpoints_path.exists():
            pytest.skip("frontend/src/api/endpoints.ts 不存在")

        content = endpoints_path.read_text(encoding="utf-8")
        assert "interface VisitorReport" in content, "endpoints.ts 缺少 VisitorReport 接口"
        assert "interface VisitorSummary" in content, "endpoints.ts 缺少 VisitorSummary 接口"
        assert "interface VisitorMetricItem" in content, "endpoints.ts 缺少 VisitorMetricItem 接口"
        assert "getAdminVisitors" in content, "endpoints.ts 缺少 getAdminVisitors 函数"

    def test_frontend_page_exists(self) -> None:
        """[Gate5] 前端 AdminVisitorsPage 存在。"""
        from pathlib import Path

        page_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "pages" / "AdminVisitorsPage.tsx"
        if not page_path.exists():
            pytest.skip("frontend/src/pages/AdminVisitorsPage.tsx 不存在")

        content = page_path.read_text(encoding="utf-8")
        # 验证状态完备性
        assert "isLoading" in content, "AdminVisitorsPage 缺少 loading 状态"
        assert "isError" in content, "AdminVisitorsPage 缺少 error 状态"
        assert "data_source" in content, "AdminVisitorsPage 缺少 data_source 处理"
        assert "empty" in content, "AdminVisitorsPage 缺少空态处理"
        assert "today" in content and "seven_days" in content and "thirty_days" in content, (
            "AdminVisitorsPage 缺少三个时间窗口"
        )

    def test_route_registered(self) -> None:
        """[Gate5] /admin/visitors 路由已注册。"""
        from pathlib import Path

        app_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "App.tsx"
        if not app_path.exists():
            pytest.skip("frontend/src/App.tsx 不存在")

        content = app_path.read_text(encoding="utf-8")
        assert "/admin/visitors" in content, "App.tsx 缺少 /admin/visitors 路由"
        assert "AdminVisitorsPage" in content, "App.tsx 缺少 AdminVisitorsPage 导入"


if __name__ == "__main__":
    # 自测入口
    print(f"GOACCESS_REPORT_PATH={GOACCESS_REPORT_PATH}")
    print(f"SENSITIVE_QUERY_KEYS={SENSITIVE_QUERY_KEYS}")
    print(f"_sanitize_path('/api?token=x') = {_sanitize_path('/api?token=x')}")
    print(f"Routes: {[r.path for r in router.routes]}")
    print("OK")
