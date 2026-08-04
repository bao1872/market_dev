"""长任务统一资源治理工具（DS-107）。

盘迹 Feature Snapshot / stock core / Review 等长任务主链统一复用本模块做内存预算治理：
分片、并发默认 1、RSS 采样与峰值累计、预算超限判定、心跳与进度、安全停止原因、
checkpoint 序列化与恢复。

纯工具模块，不依赖业务模型 / 数据库，便于单测与复用。任何长任务不得各自复制实现，
避免三份漂移（详见 ``rules/80-deployment-data-safety.md`` DS-107）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class LongTaskStopReason(str, Enum):
    """长任务安全停止原因。"""

    COMPLETED = "completed"
    MEMORY_BUDGET_EXCEEDED = "memory_budget_exceeded"
    CANCELLED = "cancelled"
    ERROR = "error"


def current_rss_mb() -> float | None:
    """读取当前进程 RSS（MB）。不可用时返回 None，绝不因监控失败中断业务。"""
    try:
        with open("/proc/self/statm", encoding="utf-8") as fh:
            pages = int(fh.read().split()[1])
        return pages * 4096 / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None


@dataclass
class LongTaskBudgetState:
    """单个长任务运行的资源治理状态。

    调用方在每个分片结束后调用 :meth:`record_chunk_done`，
    批次内部按需调用 :meth:`should_sample` / :meth:`sample` 做内存采样。
    """

    # 可配置项
    chunk_size: int
    concurrency: int = 1  # 并发固定为 1，禁止用并行放大峰值内存
    memory_budget_mb: int = 1024  # 必须低于所在容器 mem_limit（DS-101）
    sample_every: int = 50  # 批次内部按步长采样内存的间隔

    # 运行状态
    total: int = 0
    processed: int = 0
    peak_rss_mb: float | None = None
    heartbeat_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stop_reason: LongTaskStopReason | None = None

    # checkpoint（用于断点续跑 / 部分完成状态）
    resume_token: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # [CHANGE-20260804-005 / DS-107] 真实业务断点：由调用方在每个分片/批次边界更新，
    # 并随 checkpoint 序列化。续跑入口必须读取它（或等价 DB 游标）决定从何处继续。
    # 至少含：last_cursor（最后完成的业务单位，如最后 instrument index / 最后 trade_date）、
    #          run_id、input_hash、schema_version、chunk_index。
    business_cursor: dict[str, Any] = field(default_factory=dict)

    def _update_heartbeat(self) -> None:
        self.heartbeat_at = datetime.now(timezone.utc)

    def should_sample(self, processed_delta: int = 0) -> bool:
        """批次内部是否应当采样（按步长）。``processed`` 相对上一次采样的增量。"""
        if self.sample_every <= 0:
            return True
        return (self.processed % self.sample_every) < max(1, processed_delta) or (
            processed_delta > 0 and (self.processed - processed_delta) % self.sample_every
            > self.processed % self.sample_every
        )

    def sample(self) -> float | None:
        """采样一次当前 RSS 并累计峰值。返回当前 RSS（MB）。"""
        self._update_heartbeat()
        rss = current_rss_mb()
        if rss is not None:
            self.peak_rss_mb = (
                rss if self.peak_rss_mb is None else max(self.peak_rss_mb, rss)
            )
        return rss

    def record_chunk_done(self, chunk_processed: int) -> float | None:
        """分片结束后调用：累加处理数、采样峰值、刷新心跳。

        返回当前 RSS（MB）；调用方据此做预算判定（见 :meth:`should_stop`）。
        """
        self.processed += chunk_processed
        self._update_heartbeat()
        return self.sample()

    def should_stop(self, rss: float | None = None) -> bool:
        """当前是否超出内存预算需要安全停止。"""
        if rss is None:
            rss = self.peak_rss_mb
        if rss is None:
            return False
        return rss > self.memory_budget_mb

    @property
    def progress(self) -> float:
        """进度 0.0~1.0（total 为 0 时返回 0）。"""
        if self.total <= 0:
            return 0.0
        return round(min(1.0, self.processed / self.total), 4)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.processed)

    def mark_stopped(self, reason: LongTaskStopReason, **extra: Any) -> None:
        """设置停止原因并写入附加 metadata。"""
        self.stop_reason = reason
        self.metadata.update(extra)
        self._update_heartbeat()

    def to_status(self) -> dict[str, Any]:
        """转为结构化状态字典（供状态表 / API / 日志 / checkpoint 使用）。"""
        self._update_heartbeat()
        return {
            "chunk_size": self.chunk_size,
            "concurrency": self.concurrency,
            "memory_budget_mb": self.memory_budget_mb,
            "sample_every": self.sample_every,
            "total": self.total,
            "processed": self.processed,
            "remaining": self.remaining,
            "progress": self.progress,
            "peak_rss_mb": (
                round(self.peak_rss_mb, 1) if self.peak_rss_mb is not None else None
            ),
            "heartbeat_at": self.heartbeat_at.isoformat(),
            "stop_reason": (
                self.stop_reason.value if self.stop_reason is not None else None
            ),
            "resume_token": self.resume_token,
            "metadata": self.metadata,
            "business_cursor": self.business_cursor,
        }

    def make_checkpoint(self) -> str:
        """把当前状态序列化为可恢复的 checkpoint（resume_token 的载体）。

        注意：本工具本身不保存任何业务进度——调用方必须把 checkpoint 串
        （或 :meth:`to_status` 中的 resume_token）持久化到自己的状态字段，
        恢复时通过 :meth:`restore_from_checkpoint` 重建运行上下文。
        """
        return json.dumps(self.to_status(), ensure_ascii=False)

    @classmethod
    def restore_from_checkpoint(cls, checkpoint: str) -> "LongTaskBudgetState":
        """从 checkpoint 串重建运行状态，用于断点续跑。

        只恢复资源治理字段与 metadata；业务进度（processed 等）由调用方
        在续跑时按已落库的完成分片重新构造。
        """
        data = json.loads(checkpoint)
        state = cls(
            chunk_size=data.get("chunk_size", 5),
            concurrency=data.get("concurrency", 1),
            memory_budget_mb=data.get("memory_budget_mb", 1024),
            sample_every=data.get("sample_every", 50),
            total=data.get("total", 0),
            processed=data.get("processed", 0),
            resume_token=data.get("resume_token"),
            metadata=dict(data.get("metadata", {})),
        )
        peak = data.get("peak_rss_mb")
        state.peak_rss_mb = float(peak) if peak is not None else None
        reason = data.get("stop_reason")
        if reason is not None:
            try:
                state.stop_reason = LongTaskStopReason(reason)
            except ValueError:
                state.stop_reason = None
        cursor = data.get("business_cursor")
        if isinstance(cursor, dict):
            state.business_cursor = cursor
        return state

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LongTaskBudgetState":
        """从字典构造（等价于 checkpoint 恢复但更直接，供测试与迁移使用）。"""
        return cls.restore_from_checkpoint(json.dumps(data, ensure_ascii=False))
