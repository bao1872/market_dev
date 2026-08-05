"""管理员 API 统一错误模型（PRD §8.4.9）。

背景：管理后台各模块此前用 HTTPException(detail=str) 或 detail=dict 混合返回错误，
前端难以统一识别"是否可重试 / 是否可恢复 / 建议动作"。本模块提供统一错误结构，
保证稳定字段（stable_error_code/severity/retryable/resumable/recommended_action）直出。

约定（前端依赖稳定字段，勿随意改键名）：
- detail.detail: 人类可读错误消息（兼容旧前端 detail=str 解析）
- detail.stable_error_code: 统一机器码（PRD 规范 <domain>_<reason>，如 after_close_conflict）
- detail.error_code: 兼容字段（旧前端依赖的历史错误码，如 DUPLICATE_RUN / NON_TRADING_DAY）
- detail.reason: 兼容字段（旧前端依赖的 reason 别名）
- detail.severity: error / warning / info
- detail.retryable: 是否可重跑（如重跑盘后编排）
- detail.resumable: 是否可恢复（如断点继续）
- detail.recommended_action: 建议动作（人类可读）

双字段兼容（新旧错误码）：
- stable_error_code 是唯一权威统一码，前端新逻辑应消费它。
- error_code / reason 保留历史值，保证旧前端（如 AfterClosePipelineCard 判断
  DUPLICATE_RUN / DATA_COVERAGE_INSUFFICIENT）不回归。
- 调用方通过 legacy_error_code 透传历史码；不传则 error_code=stable_error_code。

错误码命名规范：<domain>_<reason>，如 after_close_conflict、after_close_coverage_insufficient。

用法：
    from app.api.admin_errors import admin_error
    raise admin_error(409, "after_close_conflict",
                      "同一交易日已存在进行中的盘后编排",
                      legacy_error_code="DUPLICATE_RUN",
                      retryable=False, resumable=False,
                      recommended_action="查看任务详情或等待其进入终态")
"""

from __future__ import annotations

from typing import Any

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
    request_id: str | None = None,
    legacy_error_code: str | None = None,
    **extra: Any,
) -> HTTPException:
    """构造携带统一稳定字段的 HTTPException（管理 API 唯一错误构造器）。

    这是管理后台所有端点构造错误响应的**唯一事实源**：禁止端点手工拼多套
    detail 字典。业务上下文字段（如 after_close_run_id / trade_date / coverage）
    一律通过 ``extra`` 透传，保证既有前端消费的扩展字段不丢失。

    Args:
        status_code: HTTP 状态码
        error_code: 稳定机器码（前端新逻辑分类展示，PRD 规范 <domain>_<reason>）
        detail: 人类可读错误消息
        severity: error / warning / info
        retryable: 是否可重跑
        resumable: 是否可恢复
        recommended_action: 建议动作
        request_id: 可选请求追踪 ID（从 x-request-id 透传，用于审计对齐）
        legacy_error_code: 兼容字段，保留旧前端依赖的历史错误码（如 DUPLICATE_RUN）；
            同时透传给 error_code 与 reason，保证旧前端（如 AfterClosePipelineCard）
            判断不回归。
        **extra: 额外业务上下文字段，合并进 detail 字典（保留旧前端依赖的字段）

    Returns:
        可直接 raise 的 HTTPException（detail 为统一结构字典）
    """
    legacy = legacy_error_code or error_code
    body: dict[str, Any] = {
        "detail": detail,
        # 兼容旧前端 detail.message 解析（旧端点手工字典普遍带 message）
        "message": detail,
        # 权威统一错误码（新前端消费）
        "stable_error_code": error_code,
        # 兼容旧前端：error_code / reason 保留历史值（旧前端判断 DUPLICATE_RUN 等）
        "error_code": legacy,
        "reason": legacy,
        "severity": severity,
        "retryable": retryable,
        "resumable": resumable,
        "recommended_action": recommended_action,
    }
    if request_id is not None:
        body["request_id"] = request_id
    body.update(extra)
    return HTTPException(status_code=status_code, detail=body)


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


def admin_not_implemented(error_code: str, detail: str, **kw) -> HTTPException:
    """501 未实现错误（已纳入 PRD 合同但后端隔离重算函数尚未实现）。"""
    return admin_error(http_status.HTTP_501_NOT_IMPLEMENTED, error_code, detail, **kw)
