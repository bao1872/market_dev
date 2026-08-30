"""F1B-1 test support：deterministic fake bars provider。

[§12] spawn 会**重新 import 模块**，因此 fake provider 必须：
- 位于**可 import 的模块**顶层（禁止 lambda / 嵌套函数 / pytest 局部闭包）
- 可 pickle 或可由 child 依据 picklable spec 构造
- 不访问真实 internet / Pytdx
- 不 import AsyncSession / SQLAlchemy（保证 CHILD_DB_DEPENDENCY=NO 可被证明）

child 侧通过 ``app.services.bars_fetch_worker.init_worker(adapter_spec)`` 传入
``{"module": "tests.support.bars_fake_provider", "attr": "build_fake_provider",
   "kwargs": {...}}`` 构造本类实例。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd

DAILY_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "amount"]
XDXR_COLUMNS = ["date", "category", "fenhong", "songzhuangu", "peigu", "peigujia"]

# xdxr 模式
XDXR_NONE = "none"        # 返回空 DataFrame（XDXR_SUCCESS_EMPTY）
XDXR_ROWS = "rows"        # 返回 category=1 事件（XDXR_SUCCESS_WITH_ROWS）
XDXR_ERROR = "error"      # 抛异常（XDXR_PROVIDER_ERROR）


def _daily_frame(start_date, end_date, *, base_close: float = 10.0) -> pd.DataFrame:
    """deterministic 日线：每个工作日一条，close 逐日递增（便于区分 adj 计算）。"""
    days = pd.bdate_range(start_date, end_date)
    n = len(days)
    if n == 0:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    closes = [round(base_close + i * 0.5, 4) for i in range(n)]
    return pd.DataFrame(
        {
            "datetime": days,
            "open": closes,
            "high": [round(c * 1.01, 4) for c in closes],
            "low": [round(c * 0.99, 4) for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
            "amount": [round(c * 1000.0, 4) for c in closes],
        }
    )


def _minute_frame(count: int, *, base_close: float = 10.0) -> pd.DataFrame:
    n = max(0, min(count, 64))  # 保持 fixture 轻量
    idx = pd.date_range("2026-08-28 09:30", periods=n, freq="15min")
    closes = [round(base_close + i * 0.1, 4) for i in range(n)]
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": closes,
            "high": [round(c * 1.01, 4) for c in closes],
            "low": [round(c * 0.99, 4) for c in closes],
            "close": closes,
            "volume": [100.0] * n,
            "amount": [round(c * 100.0, 4) for c in closes],
        }
    )


def _xdxr_frame(event_dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(event_dates),
            "category": [1] * len(event_dates),
            "fenhong": [0.5] * len(event_dates),
            "songzhuangu": [0.0] * len(event_dates),
            "peigu": [0.0] * len(event_dates),
            "peigujia": [0.0] * len(event_dates),
        }
    )


class FakeBarsProvider:
    """deterministic provider：行为完全由构造参数决定。"""

    def __init__(
        self,
        *,
        xdxr_mode: str = XDXR_NONE,
        latency_seconds: float = 0.0,
        trace_dir: str | None = None,
        xdxr_event_dates: list[str] | None = None,
        base_close: float = 10.0,
        daily_empty: bool = False,
        abrupt_exit_symbol: str | None = None,
        # [F1B-2 P1-A] per-symbol deterministic failure policy
        fail_symbols: list[str] | None = None,
        transient_fail_symbols: list[str] | None = None,
        empty_symbols: list[str] | None = None,
        attempt_dir: str | None = None,
    ) -> None:
        self.xdxr_mode = xdxr_mode
        self.latency_seconds = latency_seconds
        self.trace_dir = trace_dir
        self.xdxr_event_dates = xdxr_event_dates or []
        self.base_close = base_close
        self.daily_empty = daily_empty
        self.abrupt_exit_symbol = abrupt_exit_symbol
        self.fail_symbols = list(fail_symbols or [])
        self.transient_fail_symbols = list(transient_fail_symbols or [])
        self.empty_symbols = list(empty_symbols or [])
        self.attempt_dir = attempt_dir
        self.calls: list[str] = []
        self._local_attempts: dict[str, int] = {}

    # --- [F1B-2 P1-A] deterministic per-symbol failure policy ---
    # 计数器走共享文件（attempt_dir），因此 parallel 重试落到**另一个 child
    # provider 实例**时仍保持"首次失败、之后成功"的确定性语义。
    def _bump_attempt(self, key: str) -> int:
        if not self.attempt_dir:
            self._local_attempts[key] = self._local_attempts.get(key, 0) + 1
            return self._local_attempts[key]
        path = Path(self.attempt_dir) / f"attempt_{key}.count"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("x")
        return len(path.read_text(encoding="utf-8"))

    def _gate(self, symbol: str, method: str) -> None:
        """按策略决定是否抛 provider 异常（等价于真实 I/O 失败）。"""
        attempt = self._bump_attempt(f"{symbol}_{method}")
        if symbol in self.fail_symbols:
            raise RuntimeError(f"fake provider permanent failure: {method} {symbol}")
        if symbol in self.transient_fail_symbols and attempt == 1:
            raise RuntimeError(f"fake provider transient failure: {method} {symbol} attempt={attempt}")

    # --- tracing（用于证明真实并发与 process-local adapter）---
    def _trace(self, event: str, **extra: object) -> None:
        if not self.trace_dir:
            return
        Path(self.trace_dir).mkdir(parents=True, exist_ok=True)
        rec = {"pid": os.getpid(), "event": event, **extra}
        # 每 (event, pid) 一个文件，避免并发 append 互相覆盖
        path = Path(self.trace_dir) / f"{event}_{os.getpid()}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _sleep(self) -> None:
        if self.latency_seconds:
            time.sleep(self.latency_seconds)

    # --- provider API（与 PytdxAdapter 对应方法同名）---
    def get_daily_bars(self, symbol: str, start_date, end_date) -> pd.DataFrame:
        if symbol == self.abrupt_exit_symbol:
            os._exit(91)
        self.calls.append(f"get_daily_bars:{symbol}:{start_date}~{end_date}")
        self._gate(symbol, "get_daily_bars")
        t0 = time.time()  # wall clock：跨进程可比，用于证明执行区间 overlap
        self._sleep()
        t1 = time.time()
        self._trace("daily", symbol=symbol, start=str(start_date), end=str(end_date), t0=t0, t1=t1)
        if self.daily_empty or symbol in self.empty_symbols:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        return _daily_frame(start_date, end_date, base_close=self.base_close)

    def get_xdxr_info(self, symbol: str) -> pd.DataFrame:
        self.calls.append(f"get_xdxr_info:{symbol}")
        self._trace("xdxr", symbol=symbol, mode=self.xdxr_mode)
        self._sleep()
        if self.xdxr_mode == XDXR_ERROR:
            raise RuntimeError("fake xdxr provider failure")
        if self.xdxr_mode == XDXR_NONE or not self.xdxr_event_dates:
            return pd.DataFrame(columns=XDXR_COLUMNS)
        return _xdxr_frame(self.xdxr_event_dates)

    def get_15min_bars(self, symbol: str, count: int) -> pd.DataFrame:
        self.calls.append(f"get_15min_bars:{symbol}:{count}")
        self._gate(symbol, "get_15min_bars")
        t0 = time.time()
        self._sleep()
        t1 = time.time()
        self._trace("15m", symbol=symbol, count=count, t0=t0, t1=t1)
        if symbol in self.empty_symbols:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        return _minute_frame(count, base_close=self.base_close)

    def get_60min_bars(self, symbol: str, count: int) -> pd.DataFrame:
        self.calls.append(f"get_60min_bars:{symbol}:{count}")
        self._gate(symbol, "get_60min_bars")
        t0 = time.time()
        self._sleep()
        t1 = time.time()
        self._trace("60m", symbol=symbol, count=count, t0=t0, t1=t1)
        if symbol in self.empty_symbols:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        return _minute_frame(count, base_close=self.base_close)


def build_fake_provider(**kwargs: object) -> FakeBarsProvider:
    """top-level factory：供 child 依据 picklable spec 构造。"""
    return FakeBarsProvider(**kwargs)
