"""管理员 API 统一错误模型（PRD §8.4.9）。

背景：管理后台各模块此前用 HTTPException(detail=str) 或 detail=dict 混合返回错误，
前端难以统一识别"是否可重试 / 是否可恢复 / 建议动作"。本模块提供统一错误结构，
保证稳定字段（error_code/severity/retryable/resumable/recommended_action）直出。

约定（前端依赖稳定字段，勿随意改键名）：
- detail.detail: 人类可读错误消息（兼容旧前端 detail=str 解析）
- detail.error_code: 稳定机器码（如 after_close_conflict / coverage_insufficient）
- detail.severity: error / warning / info
- detail.retryable: 是否可重跑（如重跑盘后编排）
- detail.resumable: 是否可恢复（如断点继续）
- detail.recommended_action: 建议动作（人类可读）

错误码命名规范：<domain>_<reason>，如 after_close_conflict、after_close_coverage_insufficient。

用法：
    from app.api.admin_errors import admin_error
    raise admin_error(409, "after_close_conflict",
                      "同一交易日已存在进行中的盘后编排",
                      retryable=False, resumable=False,
                      recommended_action="查看任务详情或等待其进入终态")
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi import status as http_status


def admin_error(
    status_code: int,
    error_code: str,
    detail: str,
    *,
    severity: str = "error",
    retryable: bool = False,
    resumable: bool = False,
    recommended_action: str = "",
) -> HTTPException:
    """构造携带统一稳定字段的 HTTPException。

    Args:
        status_code: HTTP 状态码
        error_code: 稳定机器码（前端用于分类展示）
        detail: 人类可读错误消息
        severity: error / warning / info
        retryable: 是否可重跑
        resumable: 是否可恢复
        recommended_action: 建议动作

    Returns:
        可直接 raise 的 HTTPException（detail 为统一结构字典）
    """
    return HTTPException(
        status_code=status_code,
        detail={
            "detail": detail,
            "error_code": error_code,
            "severity": severity,
            "retryable": retryable,
            "resumable": resumable,
            "recommended_action": recommended_action,
        },
    )


# 便捷别名：常用场景
def admin_conflict(error_code: str, detail: str, **kw) -> HTTPException:
    """409 冲突错误（如编排冲突/覆盖率不足）。"""
    return admin_error(http_status.HTTP_409_CONFLICT, error_code, detail, **kw)


def admin_not_found(error_code: str, detail: str, **kw) -> HTTPException:
    """404 资源不存在错误。"""
    return admin_error(http_status.HTTP_404_NOT_FOUND, error_code, detail, **kw)


def admin_bad_request(error_code: str, detail: str, **kw) -> HTTPException:
    """400 参数/状态非法错误。"""
    return admin_error(http_status.HTTP_400_BAD_REQUEST, error_code, detail, **kw)
