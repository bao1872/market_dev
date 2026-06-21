"""Prometheus 指标模块，提供可观察性。

提供 GET /metrics 端点，返回 Prometheus exposition 格式文本，供 Prometheus
scraper 直接抓取（无需认证）。

指标定义：
- http_requests_total（Counter）：HTTP 请求总数，labels: method, path, status
- http_request_duration_seconds（Histogram）：HTTP 请求延迟，labels: method, path
- job_queue_depth（Gauge）：Job 队列深度，labels: queue_type
- outbox_pending（Gauge）：Outbox 待处理消息数
- active_users（Gauge）：活跃用户数

用法：
- 在 main.py 中通过 prometheus_middleware 中间件自动记录 HTTP 请求指标；
  中间件调用 http_requests_total.labels(...).inc() 与
  http_request_duration_seconds.labels(...).observe(duration)。
- 业务侧（Job 调度器、Outbox 处理器、用户会话管理）按需调用对应 Gauge 的
  .set() 更新队列深度、积压量与活跃用户数。
- 依赖 prometheus_client 库；若运行环境未安装该库，则启用轻量回退实现，
  保证 /metrics 端点仍可返回有效 Prometheus 文本（指标仅驻留进程内存）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

router = APIRouter(tags=["metrics"])

# ---------------------------------------------------------------------------
# 指标后端选择：优先使用 prometheus_client；缺失时启用轻量回退实现。
# 回退实现保证模块可导入、端点可用，指标值仅驻留当前进程内存。
# ---------------------------------------------------------------------------
try:
    from prometheus_client import (  # type: ignore[import-not-found]
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - 仅在 prometheus_client 缺失时触发
    _PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _BoundMetric:
        """回退指标句柄，绑定一组 label 值后提供 inc/set/observe 操作。"""

        def __init__(self, parent: _FallbackMetric, key: tuple[str, ...]) -> None:
            self._parent = parent
            self._key = key

        def inc(self, amount: float = 1.0) -> None:
            self._parent._inc(self._key, amount)

        def set(self, value: float) -> None:  # noqa: A003 - 对齐 prometheus API
            self._parent._set(self._key, value)

        def observe(self, value: float) -> None:
            self._parent._observe(self._key, value)

    class _FallbackMetric:
        """prometheus_client 缺失时的指标回退基类，维护内存样本。

        Counter/Gauge 共用 _values（标量）；Histogram 额外维护 _sum/_count
        与默认桶累计计数，以输出合规的 Prometheus exposition 文本。
        """

        # Histogram 默认桶上限（与 prometheus_client 默认桶一致）
        _DEFAULT_BUCKETS = (
            0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5,
            0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
        )

        def __init__(
            self,
            name: str,
            documentation: str,
            labelnames: tuple[str, ...] = (),
            metric_type: str = "counter",
        ) -> None:
            self.name = name
            self.help = documentation
            self.labelnames = labelnames
            self.metric_type = metric_type
            self._values: dict[tuple[str, ...], float] = {}
            # Histogram 专用
            self._sums: dict[tuple[str, ...], float] = {}
            self._counts: dict[tuple[str, ...], int] = {}
            self._buckets: dict[tuple[str, ...], list[float]] = {}

        def labels(self, *args: Any, **kwargs: Any) -> _BoundMetric:
            if args:
                if len(args) != len(self.labelnames):
                    raise ValueError(
                        f"指标 {self.name} 期望 {len(self.labelnames)} 个 label，"
                        f"实际收到 {len(args)} 个"
                    )
                key = tuple(str(a) for a in args)
            else:
                key = tuple(str(kwargs.get(label_name, "")) for label_name in self.labelnames)
            return _BoundMetric(self, key)

        def _inc(self, key: tuple[str, ...], amount: float) -> None:
            self._values[key] = self._values.get(key, 0.0) + amount

        def _set(self, key: tuple[str, ...], value: float) -> None:
            self._values[key] = value

        def _observe(self, key: tuple[str, ...], value: float) -> None:
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._counts[key] = self._counts.get(key, 0) + 1
            buckets = self._buckets.setdefault(
                key, [0.0] * len(self._DEFAULT_BUCKETS)
            )
            for i, upper in enumerate(self._DEFAULT_BUCKETS):
                if value <= upper:
                    buckets[i] += 1.0

    class Counter(_FallbackMetric):  # type: ignore[no-redef]
        def __init__(self, name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> None:
            super().__init__(name, documentation, labelnames, "counter")

    class Gauge(_FallbackMetric):  # type: ignore[no-redef]
        def __init__(self, name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> None:
            super().__init__(name, documentation, labelnames, "gauge")

    class Histogram(_FallbackMetric):  # type: ignore[no-redef]
        def __init__(self, name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> None:
            super().__init__(name, documentation, labelnames, "histogram")

    _REGISTRY: list[_FallbackMetric] = []

    def generate_latest() -> bytes:  # type: ignore[no-redef]
        """将所有回退指标序列化为 Prometheus exposition 文本。"""
        lines: list[str] = []
        for metric in _REGISTRY:
            lines.append(f"# HELP {metric.name} {metric.help}")
            lines.append(f"# TYPE {metric.name} {metric.metric_type}")
            if metric.metric_type == "histogram":
                for key in metric._counts:  # noqa: SLF001 - 回退实现内部访问
                    base_labels = list(zip(metric.labelnames, key, strict=True))
                    buckets = metric._buckets[key]  # noqa: SLF001
                    cumulative = 0.0
                    for upper, count in zip(metric._DEFAULT_BUCKETS, buckets, strict=True):
                        cumulative += count
                        labels = base_labels + [("le", str(upper))]
                        lines.append(
                            f"{metric.name}_bucket{_format_label_pairs(labels)} {cumulative}"
                        )
                    labels = base_labels + [("le", "+Inf")]
                    lines.append(
                        f"{metric.name}_bucket{_format_label_pairs(labels)} {metric._counts[key]}"  # noqa: SLF001
                    )
                    lines.append(
                        f"{metric.name}_sum{_format_label_pairs(base_labels)} {metric._sums[key]}"  # noqa: SLF001
                    )
                    lines.append(
                        f"{metric.name}_count{_format_label_pairs(base_labels)} {metric._counts[key]}"  # noqa: SLF001
                    )
            else:
                for key, value in metric._values.items():  # noqa: SLF001
                    labels = list(zip(metric.labelnames, key, strict=True))
                    lines.append(f"{metric.name}{_format_label_pairs(labels)} {value}")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _format_label_pairs(pairs: list[tuple[str, str]]) -> str:
        """将 (name, value) 对列表格式化为 Prometheus label 文本。"""
        if not pairs:
            return ""
        return "{" + ",".join(f'{n}="{v}"' for n, v in pairs) + "}"


# ---------------------------------------------------------------------------
# 指标定义
# ---------------------------------------------------------------------------
http_requests_total = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    ("method", "path", "status"),
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求延迟",
    ("method", "path"),
)

job_queue_depth = Gauge(
    "job_queue_depth",
    "Job 队列深度",
    ("queue_type",),
)

outbox_pending = Gauge(
    "outbox_pending",
    "Outbox 待处理消息数",
)

active_users = Gauge(
    "active_users",
    "活跃用户数",
)

# 回退实现需要显式注册以便 generate_latest 遍历
if not _PROMETHEUS_AVAILABLE:
    _REGISTRY.extend(
        [
            http_requests_total,  # type: ignore[arg-type]
            http_request_duration_seconds,  # type: ignore[arg-type]
            job_queue_depth,  # type: ignore[arg-type]
            outbox_pending,  # type: ignore[arg-type]
            active_users,  # type: ignore[arg-type]
        ]
    )


@router.get("/metrics")
async def metrics() -> Response:
    """返回 Prometheus 格式指标文本。

    无需认证，供 Prometheus scraper 直接抓取。
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    # 自测入口：验证指标定义与端点输出
    http_requests_total.labels("GET", "/health", "200").inc()
    http_request_duration_seconds.labels("GET", "/health").observe(0.01)
    job_queue_depth.labels("default").set(3)
    outbox_pending.set(5)
    active_users.set(12)
    print(f"prometheus_client available: {_PROMETHEUS_AVAILABLE}")
    print(generate_latest().decode("utf-8"))
