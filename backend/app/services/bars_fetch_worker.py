"""F1B-1 — spawn-safe provider fetch boundary（行情供应商 I/O 唯一边界）。

[PHASE F1B-1] 本模块是 **provider 网络 I/O 的唯一边界**，使 serial path 与
未来的 multiprocessing path 能共享**完全相同**的 provider 语义。

设计约束：
- **CHILD_DB_WRITER = NO**：本模块**不得** import AsyncSession / SQLAlchemy /
  Redis / SchedulerJobRun。child process 只做 provider I/O。
- **无 import-time 副作用**：不在模块导入时创建 DB engine、Redis 连接或
  Pytdx socket。PytdxAdapter 按 PID lazy 创建（PROCESS_LOCAL_PYTDX）。
- **top-level picklable**：`fetch_period_bars_task` 是模块级函数，可被
  `multiprocessing.get_context("spawn")` 序列化并在 child 中执行。

provider I/O 归属（daily 共三类，全部在本层）：
    A. raw bars      get_daily_bars(symbol, start_date, end_date)
    B. xdxr          get_xdxr_info(symbol)
    C. supplement    get_daily_bars(symbol, min_event-10d, max_event)
                     —— 仅当存在 category=1 事件且事件日 close 缺失时才拉取

算法 owner **不在**本模块：adj_factor 计算仍由
`calculate_adjustment_factor_series` 独占（见 bar_repository）。
本模块只负责把 xdxr / supplement 的**原始 provider 结果**带回 parent。
"""
from __future__ import annotations

import importlib
import logging
import os
import resource
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# xdxr 结果状态：empty 与 provider error 虽然都可能最终得到 factor=1.0，
# 但可观测性不同，并行化后**不得**揉成同一状态（F1B-1 §6）。
# ---------------------------------------------------------------------------
XDXR_SUCCESS_WITH_ROWS = "success_with_rows"
XDXR_SUCCESS_EMPTY = "success_empty"
XDXR_PROVIDER_ERROR = "provider_error"

DAILY_PERIOD = "d"
MINUTE_PERIODS = ("15m", "60m")
SUPPORTED_PERIODS = (DAILY_PERIOD, *MINUTE_PERIODS)

# 事件日 close 缺失时，向前扩展的天数（与 bar_repository 既有行为一致）
_SUPPLEMENT_LOOKBACK_DAYS = 10


# ---------------------------------------------------------------------------
# process-local provider adapter
# ---------------------------------------------------------------------------

_ADAPTER: Any = None
_ADAPTER_SPEC: dict[str, Any] | None = None


def init_worker(adapter_spec: dict[str, Any] | None = None) -> None:
    """child process initializer（每个 PID 调用一次）。

    :param adapter_spec: picklable 的 provider 构造说明。None 表示使用真实
        PytdxAdapter（生产默认）。测试可传
        ``{"module": ..., "attr": ..., "kwargs": {...}}`` 指定 deterministic
        fake provider —— 必须指向**可 import 的模块级对象**（spawn 会重新
        import 模块，lambda / 局部函数不可 pickle）。
    """
    global _ADAPTER, _ADAPTER_SPEC
    _ADAPTER_SPEC = adapter_spec
    _ADAPTER = None


def _build_adapter() -> Any:
    spec = _ADAPTER_SPEC
    if spec is None:
        from app.core.pytdx_adapter import get_pytdx_adapter

        return get_pytdx_adapter()
    module = importlib.import_module(spec["module"])
    factory = getattr(module, spec["attr"])
    return factory(**spec.get("kwargs", {}))


def get_process_local_adapter() -> Any:
    """每 PID 一个 adapter：不跨 PID 共享 socket / connection。"""
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = _build_adapter()
    return _ADAPTER


def current_adapter_pid() -> int:
    """返回持有当前 process-local adapter 的 PID（测试用于证明 PROCESS_LOCAL）。"""
    return os.getpid()


# ---------------------------------------------------------------------------
# payloads
# ---------------------------------------------------------------------------


@dataclass
class DailyProviderPayload:
    """period=d 的 provider 原始结果（全部可序列化）。"""

    instrument_id: str
    symbol: str
    pid: int
    raw_df: pd.DataFrame
    xdxr_df: pd.DataFrame | None
    xdxr_status: str
    xdxr_error: str | None = None
    supplement_df: pd.DataFrame | None = None
    supplement_error: str | None = None
    provider_elapsed_seconds: float = 0.0
    provider_calls: list[str] = field(default_factory=list)

    @property
    def raw_empty(self) -> bool:
        return self.raw_df is None or self.raw_df.empty


@dataclass
class MinuteProviderPayload:
    """period=15m / 60m 的 provider 原始结果（只含 raw bars，child 不访问 DB）。"""

    instrument_id: str
    symbol: str
    period: str
    pid: int
    raw_df: pd.DataFrame
    provider_elapsed_seconds: float = 0.0
    provider_calls: list[str] = field(default_factory=list)

    @property
    def raw_empty(self) -> bool:
        return self.raw_df is None or self.raw_df.empty


# ---------------------------------------------------------------------------
# provider I/O
# ---------------------------------------------------------------------------


def _event_dates_missing_close(
    raw_df: pd.DataFrame, xdxr_df: pd.DataFrame,
) -> tuple[list[date], list[date]]:
    """纯计算：返回 (category=1 事件日, 其中 close 缺失的事件日)。

    与 bar_repository 既有筛选逻辑一致（category == 1）。
    """
    exc_events = xdxr_df[xdxr_df["category"] == 1]
    all_event_dates = [
        pd.Timestamp(e["date"]).date() for _, e in exc_events.iterrows()
    ]
    if not all_event_dates:
        return [], []

    close_map: dict[date, float] = {}
    for _, row in raw_df.iterrows():
        close_map[pd.Timestamp(row["datetime"]).date()] = float(row["close"])
    missing = [d for d in all_event_dates if d not in close_map]
    return all_event_dates, missing


def fetch_daily_provider_inputs(
    instrument_id: str,
    symbol: str,
    start_date: date,
    end_date: date,
    adapter: Any | None = None,
) -> DailyProviderPayload:
    """采集 period=d 的**全部** provider I/O（raw + xdxr + 必要时的 supplement）。

    完全不含 DB 访问，可在 child process 执行。
    """
    started = time.monotonic()
    pytdx = adapter if adapter is not None else get_process_local_adapter()
    calls: list[str] = []

    raw_df = pytdx.get_daily_bars(symbol, start_date, end_date)
    calls.append("get_daily_bars")

    if raw_df is None or raw_df.empty:
        return DailyProviderPayload(
            instrument_id=instrument_id,
            symbol=symbol,
            pid=os.getpid(),
            raw_df=raw_df if raw_df is not None else pd.DataFrame(),
            xdxr_df=None,
            xdxr_status=XDXR_SUCCESS_EMPTY,
            provider_elapsed_seconds=time.monotonic() - started,
            provider_calls=calls,
        )

    # --- xdxr ---
    xdxr_df: pd.DataFrame | None = None
    xdxr_error: str | None = None
    try:
        xdxr_df = pytdx.get_xdxr_info(symbol)
        calls.append("get_xdxr_info")
    except Exception as exc:  # noqa: BLE001 - provider 异常按既有 contract 降级
        xdxr_error = str(exc)
        logger.warning("获取除权除息数据失败 symbol=%s: %s", symbol, exc)
        xdxr_status = XDXR_PROVIDER_ERROR
    else:
        xdxr_status = (
            XDXR_SUCCESS_EMPTY
            if xdxr_df is None or xdxr_df.empty
            else XDXR_SUCCESS_WITH_ROWS
        )

    # --- supplement：仅当存在 category=1 事件且事件日 close 缺失 ---
    supplement_df: pd.DataFrame | None = None
    supplement_error: str | None = None
    if xdxr_status == XDXR_SUCCESS_WITH_ROWS and xdxr_df is not None:
        all_event_dates, missing = _event_dates_missing_close(raw_df, xdxr_df)
        if missing:
            fetch_start = min(all_event_dates) - timedelta(
                days=_SUPPLEMENT_LOOKBACK_DAYS
            )
            try:
                supplement_df = pytdx.get_daily_bars(
                    symbol, fetch_start, max(all_event_dates)
                )
                calls.append("get_daily_bars:supplement")
            except Exception as exc:  # noqa: BLE001 - 补齐失败沿用当前可用数据
                supplement_error = str(exc)
                logger.warning(
                    "补充拉取事件日收盘价失败 symbol=%s dates=%s~%s: %s",
                    symbol, fetch_start, max(all_event_dates), exc,
                )

    return DailyProviderPayload(
        instrument_id=instrument_id,
        symbol=symbol,
        pid=os.getpid(),
        raw_df=raw_df,
        xdxr_df=xdxr_df,
        xdxr_status=xdxr_status,
        xdxr_error=xdxr_error,
        supplement_df=supplement_df,
        supplement_error=supplement_error,
        provider_elapsed_seconds=time.monotonic() - started,
        provider_calls=calls,
    )


_PERIOD_METHOD = {"15m": "get_15min_bars", "60m": "get_60min_bars"}


def fetch_minute_provider_inputs(
    instrument_id: str,
    symbol: str,
    period: str,
    count: int,
    adapter: Any | None = None,
) -> MinuteProviderPayload:
    """采集 period=15m / 60m 的 provider raw bars（child 不访问 DB）。"""
    if period not in _PERIOD_METHOD:
        raise ValueError(f"unsupported minute period: {period}")

    started = time.monotonic()
    pytdx = adapter if adapter is not None else get_process_local_adapter()
    method = getattr(pytdx, _PERIOD_METHOD[period])
    raw_df = method(symbol, count)

    return MinuteProviderPayload(
        instrument_id=instrument_id,
        symbol=symbol,
        period=period,
        pid=os.getpid(),
        raw_df=raw_df if raw_df is not None else pd.DataFrame(),
        provider_elapsed_seconds=time.monotonic() - started,
        provider_calls=[_PERIOD_METHOD[period]],
    )


def fetch_period_provider_inputs(
    period: str,
    instrument_id: str,
    symbol: str,
    *,
    count: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    adapter: Any | None = None,
) -> DailyProviderPayload | MinuteProviderPayload:
    """canonical dispatcher：一个入口按 period 选择 provider 方法。

    [F1B-1 §14] 统一 request/response，避免
    fetch_daily_* / fetch_15m_* / fetch_60m_* 三套重复 retry/serialization 逻辑。
    """
    if period == DAILY_PERIOD:
        if start_date is None or end_date is None:
            raise ValueError("period=d 需要 start_date 与 end_date")
        return fetch_daily_provider_inputs(
            instrument_id, symbol, start_date, end_date, adapter,
        )
    if period in MINUTE_PERIODS:
        if count is None:
            raise ValueError(f"period={period} 需要 count")
        return fetch_minute_provider_inputs(
            instrument_id, symbol, period, count, adapter,
        )
    raise ValueError(f"unsupported period: {period}")


# ---------------------------------------------------------------------------
# child entrypoint（top-level，spawn 可 pickle）
# ---------------------------------------------------------------------------


def fetch_period_bars_task(request: dict[str, Any]) -> dict[str, Any]:
    """child process 执行入口：primitive in → serializable out。

    返回的 dict 只含可序列化内容（DataFrame + primitive），
    便于跨进程传输；parent 侧再还原为 payload 对象。
    """
    period = request["period"]
    payload = fetch_period_provider_inputs(
        period=period,
        instrument_id=request["instrument_id"],
        symbol=request["symbol"],
        count=request.get("count"),
        start_date=request.get("start_date"),
        end_date=request.get("end_date"),
    )
    out: dict[str, Any] = {
        "period": period,
        "instrument_id": payload.instrument_id,
        "symbol": payload.symbol,
        "pid": payload.pid,
        "raw_df": payload.raw_df,
        "provider_elapsed_seconds": payload.provider_elapsed_seconds,
        "provider_calls": payload.provider_calls,
        "child_max_rss_kb": (
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            if sys.platform == "darwin"
            else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        ),
    }
    if isinstance(payload, DailyProviderPayload):
        out.update(
            {
                "xdxr_df": payload.xdxr_df,
                "xdxr_status": payload.xdxr_status,
                "xdxr_error": payload.xdxr_error,
                "supplement_df": payload.supplement_df,
                "supplement_error": payload.supplement_error,
            }
        )
    return out


def decode_period_bars_result(
    result: dict[str, Any],
) -> DailyProviderPayload | MinuteProviderPayload:
    """Parent-side decode for a child result; contains no provider or DB I/O."""
    common = {
        "instrument_id": result["instrument_id"],
        "symbol": result["symbol"],
        "pid": result["pid"],
        "raw_df": result["raw_df"],
        "provider_elapsed_seconds": result.get("provider_elapsed_seconds", 0.0),
        "provider_calls": result.get("provider_calls", []),
    }
    if result["period"] == DAILY_PERIOD:
        return DailyProviderPayload(
            **common,
            xdxr_df=result.get("xdxr_df"),
            xdxr_status=result["xdxr_status"],
            xdxr_error=result.get("xdxr_error"),
            supplement_df=result.get("supplement_df"),
            supplement_error=result.get("supplement_error"),
        )
    return MinuteProviderPayload(period=result["period"], **common)
