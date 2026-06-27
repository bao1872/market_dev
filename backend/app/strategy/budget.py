"""资源预算控制 - 策略运行时的超时与内存限制。

提供：
- BudgetExceededError: 预算超限异常
- BudgetGuard: 资源预算守卫，封装超时/内存控制逻辑
- 预算常量: SELECTOR_BUDGET_MS / MONITOR_BUDGET_MS

设计说明：
- DSA selector 默认预算: 100ms/股（来自 dsa_selector.yaml resource_budget.target_ms_per_instrument）
- Volume Node monitor 默认预算: 500ms/股（来自 watchlist_monitor.yaml resource_budget.target_ms_per_instrument）
- 超时通过 asyncio.wait_for + asyncio.to_thread 实现（同步计算通过 to_thread 桥接）
- 内存限制通过 tracemalloc 监控（可选，默认不启用以减少开销）
- 超时抛出 BudgetExceededError，禁止吞没

参考文档：05_STRATEGY_EXTENSION_SPEC.md 第 7 节"发布门"
"""

from __future__ import annotations

import asyncio
import logging
import tracemalloc
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger("strategy.budget")

T = TypeVar("T")

# 预算常量（对照各策略 manifest 的 resource_budget.target_ms_per_instrument）
SELECTOR_BUDGET_MS = 100  # DSA selector: 100ms/股
MONITOR_BUDGET_MS = 500  # Volume Node monitor: 500ms/股（1m bar 频率）


class BudgetExceededError(Exception):
    """资源预算超限异常。

    当策略执行超过预设的超时时间或内存限制时抛出。
    """

    def __init__(self, message: str, *, timeout_ms: int | None = None,
                 memory_mb: float | None = None) -> None:
        super().__init__(message)
        self.timeout_ms = timeout_ms
        self.memory_mb = memory_mb


class BudgetGuard:
    """资源预算守卫 - 控制策略执行的超时与内存。

    用法：
        guard = BudgetGuard(timeout_ms=100)
        result = await guard.run_with_budget(sync_compute_func, arg1, arg2)

    说明：
    - timeout_ms: 最大执行时间（毫秒），超时抛出 BudgetExceededError
    - memory_mb: 最大内存增量（MB），超限抛出 BudgetExceededError（可选）
    - 同步计算函数通过 asyncio.to_thread 桥接到线程池执行，不阻塞事件循环
    """

    def __init__(
        self,
        timeout_ms: int = 100,
        memory_mb: float | None = None,
    ) -> None:
        """初始化预算守卫。

        Args:
            timeout_ms: 超时时间（毫秒），默认 100ms（DSA 默认预算）
            memory_mb: 内存上限（MB），None 表示不限制
        """
        self.timeout_ms = timeout_ms
        self.memory_mb = memory_mb

    async def run_with_budget(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """在预算限制内执行同步函数。

        将同步计算函数放到线程池中执行，通过 asyncio.wait_for 控制超时。
        如果启用内存限制，使用 tracemalloc 监控内存增量。

        Args:
            func: 同步计算函数（通常是 pandas/numpy 计算）
            *args: 函数位置参数
            **kwargs: 函数关键字参数

        Returns:
            函数执行结果

        Raises:
            BudgetExceededError: 超时或内存超限
        """
        track_memory = self.memory_mb is not None
        if track_memory and not tracemalloc.is_tracing():
            tracemalloc.start()

        try:
            if track_memory:
                snapshot_before = tracemalloc.take_snapshot()

            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(func, *args, **kwargs),
                    timeout=self.timeout_ms / 1000.0,
                )
            except TimeoutError as exc:
                raise BudgetExceededError(
                    f"策略执行超时: timeout_ms={self.timeout_ms}",
                    timeout_ms=self.timeout_ms,
                ) from exc

            if track_memory:
                snapshot_after = tracemalloc.take_snapshot()
                stats = snapshot_after.compare_to(snapshot_before, "lineno")
                total_diff = sum(s.size_diff for s in stats)
                total_mb = total_diff / (1024 * 1024)
                if total_mb > self.memory_mb:  # type: ignore[operator]
                    raise BudgetExceededError(
                        f"策略执行内存超限: used={total_mb:.2f}MB, "
                        f"limit={self.memory_mb}MB",
                        memory_mb=total_mb,
                    )

            return result
        except BudgetExceededError:
            raise
        except Exception as exc:
            # 补充上下文后 re-raise（禁止吞没）
            logger.warning(
                "策略执行异常 timeout_ms=%s memory_mb=%s: %s",
                self.timeout_ms, self.memory_mb, exc,
            )
            raise


if __name__ == "__main__":
    # 自测入口：验证 BudgetGuard 基础逻辑（无副作用）
    import time

    async def _test_normal() -> None:
        """测试正常执行（不超时）。"""
        guard = BudgetGuard(timeout_ms=500)
        result = await guard.run_with_budget(lambda x: x * 2, 21)
        assert result == 42, f"预期 42, 实际 {result}"
        print(f"正常执行: {result} ✓")

    async def _test_timeout() -> None:
        """测试超时抛出 BudgetExceededError。"""
        guard = BudgetGuard(timeout_ms=50)
        try:
            await guard.run_with_budget(time.sleep, 0.2)
            raise AssertionError("应抛出 BudgetExceededError")
        except BudgetExceededError as e:
            assert e.timeout_ms == 50
            print(f"超时抛出 BudgetExceededError: {e} ✓")

    async def _run() -> None:
        await _test_normal()
        await _test_timeout()

    asyncio.run(_run())
    print("OK")
