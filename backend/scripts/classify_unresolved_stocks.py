"""临时调查脚本：分类 249 只未解析股票（CHANGE-20260716-007）。

运行方式（容器内）：
    python -m scripts.classify_unresolved_stocks

输出：
    - 按 SH/SZ/BJ 后缀分类统计
    - 按"数据库不存在/非active/代码异常"分类统计
    - 前 50 个未解析样本（脱敏，仅 symbol + 后缀 + 原因）

不修改任何数据；不写全量样本到日志。
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.wencai_board_provider import fetch_board_snapshot


async def _classify_unresolved() -> None:
    """主流程：拉取问财快照 → 解析 instrument_id → 分类未解析样本。"""
    print("=" * 70, flush=True)
    print("[1/4] 拉取问财板块快照...", flush=True)
    snapshot = await fetch_board_snapshot()
    print(
        f"    raw_rows={snapshot.raw_rows}, boards={len(snapshot.boards)}, "
        f"memberships={len(snapshot.memberships)}",
        flush=True,
    )

    # 收集所有 wencai 返回的原始股票代码（含后缀）
    raw_symbol_set: set[str] = set()
    symbol_set: set[str] = set()
    for symbols in snapshot.memberships.values():
        for sym in symbols:
            raw_symbol_set.add(sym)
            symbol_set.add(sym)
    total = len(symbol_set)
    print(f"[2/4] 收集到唯一股票代码 {total} 个", flush=True)

    # 解析为 instrument_id（与生产 resolver 一致）
    print("[3/4] 解析 instrument_id...", flush=True)
    from app.services.after_close_orchestrator import (
        _resolve_instruments_for_board_sync,
    )

    symbol_to_id = await _resolve_instruments_for_board_sync(list(symbol_set))
    resolved = len(symbol_to_id)
    unresolved_symbols = sorted(symbol_set - set(symbol_to_id.keys()))
    print(
        f"    resolved={resolved}, unresolved={len(unresolved_symbols)}, "
        f"parse_rate={resolved / total * 100:.2f}%",
        flush=True,
    )

    if not unresolved_symbols:
        print("[4/4] 无未解析样本", flush=True)
        return

    # 查询数据库：检查每个未解析 symbol 的存在性 + status
    print(
        f"[4/4] 分类 {len(unresolved_symbols)} 只未解析样本...",
        flush=True,
    )
    async with AsyncSessionLocal() as db:
        # 一次性查询所有未解析 symbol 的 Instrument 记录
        stmt = select(
            Instrument.symbol, Instrument.status, Instrument.market
        ).where(Instrument.symbol.in_(unresolved_symbols))
        result = await db.execute(stmt)
        db_records: dict[str, tuple[str | None, str | None]] = {}
        for row in result:
            db_records[row.symbol] = (row.status, row.market)

    # 分类统计
    by_suffix: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []

    # memberships 中存储的是规范化后的 6 位代码（无后缀），
    # 通过代码前缀推断交易所归属（与上交所/深交所/北交所代码段对齐）：
    # - SH: 60xxxx (主板) / 688xxx (科创板) / 11xxxx / 13xxxx (转债)
    # - SZ: 00xxxx (主板/中小板) / 30xxxx (创业板) / 20xxxx (转债/ETF)
    # - BJ: 43xxxx / 83xxxx / 87xxxx / 920xxx (北交所新代码段)
    def _infer_exchange(code: str) -> str:
        if not code or len(code) < 2 or not code.isdigit():
            return "UNKNOWN"
        if code.startswith(("60", "68", "11", "13")):
            return "SH"
        if code.startswith(("00", "30", "20")):
            return "SZ"
        if code.startswith(("43", "83", "87", "92")):
            return "BJ"
        return "UNKNOWN"

    for sym in unresolved_symbols:
        suffix = _infer_exchange(sym)
        by_suffix[suffix] += 1

        if sym not in db_records:
            reason = "DB_NOT_EXIST"
        else:
            status, market = db_records[sym]
            if status != "active":
                reason = f"NOT_ACTIVE(status={status})"
            elif market and market.upper() != suffix and suffix != "UNKNOWN":
                reason = f"MARKET_MISMATCH(db={market}, wc={suffix})"
            else:
                reason = "OTHER_ACTIVE_BUT_UNRESOLVED"

        by_reason[reason] += 1
        if len(samples) < 50:
            samples.append(
                {"symbol": sym, "suffix": suffix, "reason": reason}
            )

    # 输出报告
    print("\n" + "=" * 70, flush=True)
    print("== 未解析股票分类报告 ==", flush=True)
    print("=" * 70, flush=True)

    print("\n[按交易所后缀]", flush=True)
    for suffix, count in sorted(by_suffix.items()):
        print(f"  .{suffix}: {count}", flush=True)

    print("\n[按未解析原因]", flush=True)
    for reason, count in sorted(by_reason.items()):
        print(f"  {reason}: {count}", flush=True)

    print(f"\n[前 {len(samples)} 个样本]", flush=True)
    for i, s in enumerate(samples, 1):
        print(
            f"  {i:3d}. {s['symbol']}.{s['suffix']}  ->  {s['reason']}",
            flush=True,
        )

    print("\n" + "=" * 70, flush=True)
    print(
        f"汇总: total={total}, resolved={resolved}, unresolved={len(unresolved_symbols)}, "
        f"parse_rate={resolved / total * 100:.2f}%",
        flush=True,
    )
    print("=" * 70, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(_classify_unresolved())
    except Exception as exc:
        print(f"[FATAL] 分类失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
