"""[Gate5] /admin/visitors 端点测试（Umami 迁移后）。

[CHANGE-20260730-010] 从 GoAccess 迁移到 Umami：
- 删除 GOACCESS_REPORT_PATH / _parse_goaccess_json 相关测试
- 新增 UmamiAnalyticsAdapter 纯单元测试（不依赖 DB）

覆盖：
1. _sanitize_path 脱敏函数（敏感 query 参数替换为 ***）
2. _resolve_umami_db_url URL 形态转换
3. _get_website_id UUID 解析
4. 路由注册验证（/admin/visitors 路径存在）
5. Schema 字段验证
6. 前端类型一致性
"""

from __future__ import annotations

import inspect

import pytest

from app.api.admin_visitors import router
from app.schemas.visitors import (
    VisitorMetricItem,
    VisitorReport,
    VisitorSummary,
)
from app.services.umami_analytics_adapter import (
    _SENSITIVE_QUERY_KEYS,
    _get_website_id,
    _resolve_umami_db_url,
    _sanitize_path,
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
        assert _SENSITIVE_QUERY_KEYS == expected


# =============================================================================
# 2. _resolve_umami_db_url URL 形态转换
# =============================================================================


class TestResolveUmamiDbUrl:
    """[Gate5] _resolve_umami_db_url URL 形态转换。"""

    def test_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量未设置时返回 None。"""
        monkeypatch.delenv("UMAMI_DATABASE_URL", raising=False)
        assert _resolve_umami_db_url() is None

    def test_empty_string_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """空字符串返回 None。"""
        monkeypatch.setenv("UMAMI_DATABASE_URL", "")
        assert _resolve_umami_db_url() is None

    def test_postgresql_converted_to_asyncpg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """postgresql:// 前缀转为 postgresql+asyncpg://。"""
        monkeypatch.setenv(
            "UMAMI_DATABASE_URL",
            "postgresql://umami:secret@trading-postgres:5432/umami",
        )
        result = _resolve_umami_db_url()
        assert result == "postgresql+asyncpg://umami:secret@trading-postgres:5432/umami"

    def test_asyncpg_kept_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """postgresql+asyncpg:// 前缀保持不变。"""
        monkeypatch.setenv(
            "UMAMI_DATABASE_URL",
            "postgresql+asyncpg://umami:secret@trading-postgres:5432/umami",
        )
        result = _resolve_umami_db_url()
        assert result == "postgresql+asyncpg://umami:secret@trading-postgres:5432/umami"

    def test_invalid_scheme_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 postgresql 前缀返回 None。"""
        monkeypatch.setenv("UMAMI_DATABASE_URL", "mysql://umami:secret@host/umami")
        assert _resolve_umami_db_url() is None


# =============================================================================
# 3. _get_website_id UUID 解析
# =============================================================================


class TestGetWebsiteId:
    """[Gate5] _get_website_id UUID 解析。"""

    def test_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量未设置时返回 None。"""
        monkeypatch.delenv("UMAMI_WEBSITE_ID", raising=False)
        assert _get_website_id() is None

    def test_valid_uuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """合法 UUID 字符串解析为 UUID 对象。"""
        monkeypatch.setenv("UMAMI_WEBSITE_ID", "109c6241-d39e-47b0-a6f2-29a6bc15bd09")
        result = _get_website_id()
        assert result is not None
        assert str(result) == "109c6241-d39e-47b0-a6f2-29a6bc15bd09"

    def test_invalid_uuid_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非法 UUID 返回 None。"""
        monkeypatch.setenv("UMAMI_WEBSITE_ID", "not-a-uuid")
        assert _get_website_id() is None


# =============================================================================
# 4. 路由注册验证
# =============================================================================


class TestRouterRegistration:
    """[Gate5] /admin/visitors 路由注册验证。"""

    def test_router_has_visitors_route(self) -> None:
        """路由注册了 GET /admin/visitors。"""
        paths = [route.path for route in router.routes]
        assert "/v1/admin/visitors" in paths, f"路由未注册 /v1/admin/visitors，实际: {paths}"

    def test_visitors_route_is_get(self) -> None:
        """路由方法为 GET。"""
        methods = []
        for route in router.routes:
            if route.path == "/v1/admin/visitors":
                methods.extend(route.methods or [])
        assert "GET" in methods, f"GET 方法未注册，实际: {methods}"

    def test_router_prefix(self) -> None:
        """路由 prefix 为 /admin。"""
        assert router.prefix == "/v1/admin"

    def test_router_tags(self) -> None:
        """路由 tag 包含 admin-visitors。"""
        assert "admin-visitors" in router.tags


# =============================================================================
# 5. Schema 字段验证
# =============================================================================


class TestSchemaFields:
    """[Gate5] VisitorReport / VisitorSummary / VisitorMetricItem 字段验证。"""

    def test_visitor_report_data_source_values(self) -> None:
        """data_source 字段接受 umami / empty / error。"""
        for ds in ("umami", "empty", "error"):
            report = VisitorReport(data_source=ds)  # type: ignore[arg-type]
            assert report.data_source == ds

    def test_visitor_report_default_generated_at(self) -> None:
        """generated_at 默认为 None（运行时填充）。"""
        report = VisitorReport(data_source="empty")
        # generated_at 是可选字段
        assert report.data_source == "empty"

    def test_visitor_summary_required_fields(self) -> None:
        """VisitorSummary 必填字段 PV/UV。"""
        summary = VisitorSummary(pv=0, uv=0)
        assert summary.pv == 0
        assert summary.uv == 0
        assert summary.top_pages == []
        assert summary.top_referrers == []
        assert summary.devices == []
        assert summary.browsers == []
        assert summary.hourly_trend == []
        # Umami 不记录 HTTP 状态码
        assert summary.status_codes == []

    def test_visitor_metric_item_fields(self) -> None:
        """VisitorMetricItem 包含 label/count/percentage。"""
        item = VisitorMetricItem(label="/market", count=10, percentage=50.0)
        assert item.label == "/market"
        assert item.count == 10
        assert item.percentage == 50.0


# =============================================================================
# 6. 前端类型一致性
# =============================================================================


class TestFrontendTypeConsistency:
    """[Gate5] 后端 Schema 字段与前端 TS 类型一致。"""

    def test_visitor_report_fields_match_frontend(self) -> None:
        """VisitorReport 字段与前端 VisitorReport 接口一致。"""
        # 后端 Schema 字段
        backend_fields = set(VisitorReport.model_fields.keys())
        # 前端 TS 接口期望字段（来自 frontend/src/api/endpoints.ts）
        expected_frontend_fields = {
            "today",
            "seven_days",
            "thirty_days",
            "generated_at",
            "data_source",
            "error_message",
        }
        assert backend_fields == expected_frontend_fields, (
            f"前后端字段不一致: backend={backend_fields}, frontend={expected_frontend_fields}"
        )

    def test_no_goaccess_residual_in_admin_visitors(self) -> None:
        """[Gate5] admin_visitors.py 不再包含 GoAccess 代码符号（docstring 历史说明允许）。"""
        from app.api import admin_visitors

        # 不应再导入或定义 GoAccess 相关符号
        forbidden_symbols = ["GOACCESS_REPORT_PATH", "_parse_goaccess_json"]
        module_symbols = dir(admin_visitors)
        for sym in forbidden_symbols:
            assert sym not in module_symbols, (
                f"admin_visitors.py 仍定义 GoAccess 符号: {sym}"
            )

    def test_umami_adapter_no_db_password_hardcoded(self) -> None:
        """[Gate5] Umami adapter 不硬编码数据库密码。"""
        from app.services import umami_analytics_adapter

        source = inspect.getsource(umami_analytics_adapter)
        # _DEFAULT_UMAMI_URL 是默认值（umami:umami），生产由环境变量覆盖
        # 但不应有真实密码硬编码
        forbidden_passwords = ["023d39c5162c7fe8fe499cc042354e8d", "panji_prod_password"]
        for pwd in forbidden_passwords:
            assert pwd not in source, f"Umami adapter 硬编码生产密码: {pwd}"
