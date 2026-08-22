"""Phase 4A-2 — FrozenMDAS（实验级，仅放 experiments/duplicate_compute_audit/）。

设计原则（来自 4A-2 授权）：
- 不新增 production FrozenMDAS 类。
- 不重写 qfq / hash / finalize 逻辑；直接复用 production：
    _build_daily_aggregation (统一 daily aggregation owner)
    AdjustmentFactorService.apply_qfq
    compute_source_bar_hash
- Frozen provider 只负责：parquet → 按 instrument_id 切 daily_df / factor_df →
  转成 production repository 预期的 index/columns/dtypes → 交给 _build_daily_aggregation。
- 离线 replay policy：allow_backfill=False, expected=target_trade_date。
  （production caller 默认 allow_backfill=True；离线 replay 强制 DB/frozen-only
   以防 network/external-source contamination。）

数据源唯一：backend/.perfdata/afterclose/afterclose-20260817-v1/
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
DATA_DIR = (
    BACKEND_ROOT
    / ".perfdata"
    / "afterclose"
    / "afterclose-20260817-v1"
)

# production repository 的 daily 列顺序（经 get_daily_bars_batch 同构）
_BAR_COLUMNS = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]

# 离线 replay 固定 now（运行时时钟，不纳入 business contract 比较）
REPLAY_NOW = datetime(2026, 8, 17, 15, 0, 0, tzinfo=timezone.utc)


class FrozenMDAS:
    """实验级 Frozen MDAS：从冻结 parquet 重建 production 输入，复用 production 聚合。"""

    def __init__(self, data_dir: Path = DATA_DIR):
        self._data_dir = data_dir
        self._bars = pd.read_parquet(data_dir / "bars_daily_raw.parquet")
        self._factors = pd.read_parquet(data_dir / "adj_factors.parquet")
        # 预分区：instrument_id(str) -> DataFrame（保持 trade_date 排序）
        # [4A-3L] 正式主链的 instrument_id 是 UUID；统一 str() 以支持 UUID 或 str 两种 key，
        # 否则 UUID 在 5293 全量真实链路查不到 parquet 分区。
        self._bars_by_inst: dict[str, pd.DataFrame] = {}
        self._factors_by_inst: dict[str, pd.DataFrame] = {}
        for inst_id, grp in self._bars.groupby("instrument_id"):
            g = grp.sort_values("trade_date").copy()
            self._bars_by_inst[str(inst_id)] = g
        for inst_id, grp in self._factors.groupby("instrument_id"):
            g = grp.sort_values("trade_date").copy()
            self._factors_by_inst[str(inst_id)] = g

    # --- 内部：将冻结 raw 转成 production repository 同构 df ---

    def _make_daily_df(self, inst_id: str) -> pd.DataFrame:
        """production get_daily_bars_batch 的同构输出：
        index=trade_date(DatetimeIndex), columns=open/high/low/close/volume/amount/adj_factor
        """
        g = self._bars_by_inst.get(str(inst_id))
        if g is None or len(g) == 0:
            # 空数据也必须保持 DatetimeIndex + float64 列（与 production to_numeric 同构）
            empty = pd.DataFrame(columns=_BAR_COLUMNS, dtype="float64")
            empty.index = pd.DatetimeIndex([], name="trade_date")
            return empty
        df = g.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date")
        # 仅保留 + 顺序排列 production 列
        df = df.reindex(columns=_BAR_COLUMNS)
        for c in _BAR_COLUMNS:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def _make_factor_df(self, inst_id: str) -> pd.DataFrame:
        """production get_adj_factor_series_batch 的同构输出：
        columns=[trade_date, adj_factor]，trade_date 为 datetime。
        """
        g = self._factors_by_inst.get(str(inst_id))
        if g is None or len(g) == 0:
            return pd.DataFrame(columns=["trade_date", "adj_factor"])
        df = g.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
        df = df[["trade_date", "adj_factor"]]
        return df

    # --- 公共接口（与 compute_review_core_with_run_items 实际调用同构） ---

    async def get_bars_batch(
        self,
        session,
        instrument_ids: list,
        timeframe: str = "1d",
        adj: str = "qfq",
        include_realtime: bool = False,
        completed_only: bool = True,
        end_date: date = date(2026, 8, 17),
        adjustment_as_of: date = date(2026, 8, 17),
    ) -> dict:
        """返回 {instrument_id: BarAggregationResult}。

        离线 replay policy：allow_backfill=False, expected=end_date。
        """
        from app.services.market_data_aggregation_service import (
            _build_daily_aggregation,
        )
        from app.core.time import SHANGHAI_TZ

        now = datetime(2026, 8, 17, 15, 0, 0, tzinfo=SHANGHAI_TZ)
        results: dict = {}
        for inst_id in instrument_ids:
            daily_df = self._make_daily_df(inst_id)
            factor_df = self._make_factor_df(inst_id)
            # production 统一 daily aggregation owner；离线强制 allow_backfill=False
            result = await _build_daily_aggregation(
                session,
                inst_id,
                daily_df,
                factor_df,
                end_date,  # expected
                now,
                timeframe=timeframe,
                adj=adj,
                include_realtime=include_realtime,
                completed_only=completed_only,
                start=None,
                end=end_date,
                limit=None,
                warmup_bars=0,
                adjustment_as_of=adjustment_as_of,
                allow_backfill=False,
            )
            results[inst_id] = result
        return results


def load_instruments(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    return pd.read_parquet(data_dir / "instruments.parquet")
