# [PRD §8.4.9 / R14] admin_errors 统一错误模型测试（纯单元，无 DB）
# 验证统一错误结构的稳定字段与新旧错误码双字段兼容。
from __future__ import annotations

from fastapi import status

from app.api.admin_errors import (
    admin_bad_request,
    admin_conflict,
    admin_error,
    admin_not_found,
)


class TestAdminErrorUnifiedFields:
    def test_admin_error_has_unified_fields(self) -> None:
        """admin_error 必须输出稳定字段：detail/message/stable_error_code/error_code/
        reason/severity/retryable/resumable/recommended_action。"""
        exc = admin_error(
            409,
            "after_close_conflict",
            "冲突",
            legacy_error_code="DUPLICATE_RUN",
            severity="warning",
            retryable=False,
            resumable=True,
            recommended_action="查看任务详情",
        )
        assert exc.status_code == 409
        d = exc.detail
        assert isinstance(d, dict)
        assert d["detail"] == "冲突"
        assert d["message"] == "冲突"
        assert d["stable_error_code"] == "after_close_conflict"
        assert d["error_code"] == "DUPLICATE_RUN"
        assert d["reason"] == "DUPLICATE_RUN"
        assert d["severity"] == "warning"
        assert d["retryable"] is False
        assert d["resumable"] is True
        assert d["recommended_action"] == "查看任务详情"

    def test_admin_error_legacy_defaults_to_error_code(self) -> None:
        """未传 legacy_error_code 时，error_code/reason 回退到 stable_error_code。"""
        exc = admin_error(400, "bad_request_x", "bad")
        d = exc.detail
        assert d["stable_error_code"] == "bad_request_x"
        assert d["error_code"] == "bad_request_x"
        assert d["reason"] == "bad_request_x"

    def test_admin_error_extra_fields_passthrough(self) -> None:
        """业务上下文字段（如 after_close_run_id / trade_date）必须透传。"""
        exc = admin_error(
            409,
            "after_close_conflict",
            "dup",
            legacy_error_code="DUPLICATE_RUN",
            after_close_run_id="run-123",
            trade_date="2026-08-04",
        )
        d = exc.detail
        assert d["after_close_run_id"] == "run-123"
        assert d["trade_date"] == "2026-08-04"

    def test_admin_error_request_id(self) -> None:
        """request_id 必须透传（审计对齐）。"""
        exc = admin_error(500, "x", "y", request_id="req-1")
        assert exc.detail["request_id"] == "req-1"


class TestAdminErrorConvenienceAliases:
    def test_admin_conflict_status_409(self) -> None:
        exc = admin_conflict("after_close_conflict", "冲突", legacy_error_code="DUPLICATE_RUN")
        assert exc.status_code == status.HTTP_409_CONFLICT
        assert exc.detail["stable_error_code"] == "after_close_conflict"
        assert exc.detail["error_code"] == "DUPLICATE_RUN"

    def test_admin_not_found_status_404(self) -> None:
        exc = admin_not_found("resource_not_found", "不存在")
        assert exc.status_code == status.HTTP_404_NOT_FOUND

    def test_admin_bad_request_status_400(self) -> None:
        exc = admin_bad_request("bad_request", "参数非法")
        assert exc.status_code == status.HTTP_400_BAD_REQUEST


class TestAdminErrorSourceGuard:
    """源码守卫：管理端点必须统一经 admin_error 构造错误，禁止手工拼 detail 字典。

    这是 R14 闭环的回归防线——一旦端点又改回手工 ``HTTPException(detail={...})``
    或直接 ``raise HTTPException(404, str(...))``，测试立即失败。
    """

    _API_DIR = "app/api"

    def test_after_close_endpoint_uses_admin_error(self) -> None:
        """admin_after_close.py 不得再直接 raise HTTPException（手工 detail 字典）。"""
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / self._API_DIR / "admin_after_close.py"
        src = path.read_text(encoding="utf-8")
        # 统一构造器必须被使用
        assert "admin_error(" in src, "必须使用 admin_error"
        assert "admin_conflict(" in src, "必须使用 admin_conflict"
        assert "admin_not_found(" in src, "必须使用 admin_not_found"
        assert "admin_bad_request(" in src, "必须使用 admin_bad_request"
        # 不得再手工构造 HTTPException（唯一允许的是 import 处不再出现 HTTPException）
        assert "raise HTTPException(" not in src, "端点不得再手工 raise HTTPException"
        assert "HTTPException," not in src.replace("from fastapi import APIRouter, Depends, Query, Request, status", ""), (
            "不得再 import HTTPException（应从 admin_errors 走统一构造器）"
        )
