"""行情数据 Prometheus 指标。

定义行情系统专用指标，供 bars_scheduler_service / bar_repository / bars_cache /
freshness_sla / bars_retention 等模块记录业务指标。

指标定义：
- bars_fetch_total（Counter）：行情拉取次数，labels: period, status
- bars_fetch_duration_seconds（Histogram）：行情拉取耗时，labels: period
- bars_upsert_total（Counter）：行情写入次数，labels: period, status
- bars_upsert_records（Counter）：行情写入记录数，labels: period
- bars_query_total（Counter）：行情查询次数，labels: period, adj
- bars_query_duration_seconds（Histogram）：行情查询耗时，labels: period
- bars_cache_hits_total（Counter）：缓存命中次数，labels: cache_type
- bars_cache_misses_total（Counter）：缓存未命中次数，labels: cache_type
- bars_freshness_age_seconds（Gauge）：数据新鲜度（秒），labels: period
- bars_retention_deleted_total（Counter）：保留策略清理记录数，labels: table_name

用法：
    from app.services.bars_metrics import bars_fetch_total, bars_upsert_total

    # 记录拉取成功
    bars_fetch_total.labels(period="d", status="success").inc()
    # 记录拉取耗时
    bars_fetch_duration_seconds.labels(period="d").observe(1.5)
    # 记录写入记录数
    bars_upsert_records.labels(period="d").inc(800)

依赖 prometheus_client 库；若运行环境未安装该库，则启用轻量回退实现
（与 app/api/metrics.py 一致的 fallback 模式），保证指标可记录。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 指标后端选择：优先使用 prometheus_client；缺失时启用轻量回退实现。
# 回退实现与 app/api/metrics.py 一致，指标值仅驻留当前进程内存。
# ---------------------------------------------------------------------------
try:
    from prometheus_client import (  # type: ignore[import-not-found]
        Counter,
        Gauge,
        Histogram,
    )

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - 仅在 prometheus_client 缺失时触发
    _PROMETHEUS_AVAILABLE = False

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
        """prometheus_client 缺失时的指标回退基类，维护内存样本。"""

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
                key = tuple(str(kwargs.get(ln, "")) for ln in self.labelnames)
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


# ---------------------------------------------------------------------------
# 行情指标定义
# ---------------------------------------------------------------------------

# 行情拉取（从 pytdx 获取数据）
bars_fetch_total = Counter(
    "bars_fetch_total",
    "行情拉取次数（从数据源拉取）",
    ("period", "status"),
)

bars_fetch_duration_seconds = Histogram(
    "bars_fetch_duration_seconds",
    "行情拉取耗时（秒）",
    ("period",),
)

# 行情写入（upsert 到 DB）
bars_upsert_total = Counter(
    "bars_upsert_total",
    "行情写入次数（upsert 到 DB）",
    ("period", "status"),
)

bars_upsert_records = Counter(
    "bars_upsert_records",
    "行情写入记录数（upsert 的 bar 条数）",
    ("period",),
)

# 行情查询（API 查询）
bars_query_total = Counter(
    "bars_query_total",
    "行情查询次数（API 查询）",
    ("period", "adj"),
)

bars_query_duration_seconds = Histogram(
    "bars_query_duration_seconds",
    "行情查询耗时（秒）",
    ("period",),
)

# 缓存命中/未命中
bars_cache_hits_total = Counter(
    "bars_cache_hits_total",
    "缓存命中次数",
    ("cache_type",),  # cache_type: bars / xdxr / instruments
)

bars_cache_misses_total = Counter(
    "bars_cache_misses_total",
    "缓存未命中次数",
    ("cache_type",),
)

# 数据新鲜度（Gauge，按周期记录最新数据的年龄）
bars_freshness_age_seconds = Gauge(
    "bars_freshness_age_seconds",
    "数据新鲜度（最新数据距今秒数）",
    ("period",),
)

# 保留策略清理记录数
bars_retention_deleted_total = Counter(
    "bars_retention_deleted_total",
    "保留策略清理记录数",
    ("table_name",),
)


if __name__ == "__main__":
    # 自测入口：验证指标定义与记录（无副作用，不连接 Prometheus）
    print(f"prometheus_client available: {_PROMETHEUS_AVAILABLE}")

    # 1. 验证指标类型
    assert isinstance(bars_fetch_total, Counter), "bars_fetch_total 应为 Counter"
    assert isinstance(bars_fetch_duration_seconds, Histogram), "bars_fetch_duration_seconds 应为 Histogram"
    assert isinstance(bars_upsert_total, Counter), "bars_upsert_total 应为 Counter"
    assert isinstance(bars_upsert_records, Counter), "bars_upsert_records 应为 Counter"
    assert isinstance(bars_query_total, Counter), "bars_query_total 应为 Counter"
    assert isinstance(bars_query_duration_seconds, Histogram), "bars_query_duration_seconds 应为 Histogram"
    assert isinstance(bars_cache_hits_total, Counter), "bars_cache_hits_total 应为 Counter"
    assert isinstance(bars_cache_misses_total, Counter), "bars_cache_misses_total 应为 Counter"
    assert isinstance(bars_freshness_age_seconds, Gauge), "bars_freshness_age_seconds 应为 Gauge"
    assert isinstance(bars_retention_deleted_total, Counter), "bars_retention_deleted_total 应为 Counter"
    print("所有 10 个指标类型验证 ✓")

    # 2. 验证 labelnames（prometheus_client 用 _labelnames，fallback 用 labelnames）
    def _get_labelnames(metric: Any) -> tuple[str, ...]:
        """兼容获取 labelnames（prometheus_client 与 fallback 模式）。"""
        return getattr(metric, "labelnames", None) or getattr(metric, "_labelnames", ())

    assert _get_labelnames(bars_fetch_total) == ("period", "status"), \
        f"bars_fetch_total labelnames 不匹配: {_get_labelnames(bars_fetch_total)}"
    assert _get_labelnames(bars_fetch_duration_seconds) == ("period",), \
        "bars_fetch_duration_seconds labelnames 不匹配"
    assert _get_labelnames(bars_upsert_records) == ("period",), \
        "bars_upsert_records labelnames 不匹配"
    assert _get_labelnames(bars_cache_hits_total) == ("cache_type",), \
        "bars_cache_hits_total labelnames 不匹配"
    assert _get_labelnames(bars_freshness_age_seconds) == ("period",), \
        "bars_freshness_age_seconds labelnames 不匹配"
    assert _get_labelnames(bars_retention_deleted_total) == ("table_name",), \
        "bars_retention_deleted_total labelnames 不匹配"
    print("所有指标 labelnames 验证 ✓")

    # 3. 验证指标记录功能（Counter.inc / Histogram.observe / Gauge.set）
    bars_fetch_total.labels(period="d", status="success").inc()
    bars_fetch_total.labels(period="d", status="success").inc(2)
    bars_fetch_total.labels(period="15m", status="failed").inc()

    bars_fetch_duration_seconds.labels(period="d").observe(1.5)
    bars_fetch_duration_seconds.labels(period="15m").observe(0.3)

    bars_upsert_records.labels(period="d").inc(800)
    bars_upsert_records.labels(period="15m").inc(500)

    bars_query_total.labels(period="1d", adj="qfq").inc()
    bars_query_duration_seconds.labels(period="1d").observe(0.05)

    bars_cache_hits_total.labels(cache_type="bars").inc()
    bars_cache_misses_total.labels(cache_type="bars").inc()
    bars_cache_hits_total.labels(cache_type="xdxr").inc(3)

    bars_freshness_age_seconds.labels(period="d").set(600.0)
    bars_freshness_age_seconds.labels(period="15m").set(30.0)

    bars_retention_deleted_total.labels(table_name="bars_minute").inc(1000)
    print("指标记录功能验证 ✓（Counter.inc / Histogram.observe / Gauge.set）")

    # 4. 验证 fallback 模式下的值读取（仅 fallback 模式可读）
    if not _PROMETHEUS_AVAILABLE:
        # fallback 模式下可直接读取内部值
        key_d_success = ("d", "success")
        assert bars_fetch_total._values.get(key_d_success) == 3.0, \
            f"bars_fetch_total(d,success) 应为 3.0，实际 {bars_fetch_total._values.get(key_d_success)}"
        print(f"fallback 模式值验证 ✓: bars_fetch_total(d,success)={bars_fetch_total._values[key_d_success]}")

        key_d = ("d",)
        assert bars_fetch_duration_seconds._counts.get(key_d) == 1, \
            "bars_fetch_duration_seconds(d) count 应为 1"
        assert bars_fetch_duration_seconds._sums.get(key_d) == 1.5, \
            "bars_fetch_duration_seconds(d) sum 应为 1.5"
        print(f"fallback 模式 Histogram 验证 ✓: count={bars_fetch_duration_seconds._counts[key_d]}, sum={bars_fetch_duration_seconds._sums[key_d]}")

        assert bars_freshness_age_seconds._values.get(key_d) == 600.0, \
            "bars_freshness_age_seconds(d) 应为 600.0"
        print(f"fallback 模式 Gauge 验证 ✓: bars_freshness_age_seconds(d)={bars_freshness_age_seconds._values[key_d]}")
    else:
        print("prometheus_client 可用，跳过 fallback 值验证")

    print("\n所有自测通过 ✓")
