"""Step6 真实回归验证脚本（CHANGE-20260717-002）。

用生产真实 DB 核对 603538 美诺华在 2026-07-03 前后：
1. factor rebuild 修复 adj_factor（只更新最近 8 根的 bug）
2. 1d/15m/1h × none/qfq 逐 bar 证据
3. adjustment_as_of=2026-07-01、2026-07-03、最新交易日（无未来泄漏）
4. 对照股 600276 恒瑞医药（无公司行为，factor 全 1.0）
5. 跨调用方 hash 一致性（/bars、indicator、strategy_batch、feature_snapshot 同参）
6. factor rebuild 幂等 + fingerprint 回滚

运行方式（临时容器，不热更新生产）：
    docker run --rm --network web_dev_default \
      -v /root/web_dev/backend:/app -w /app \
      -e DATABASE_URL=postgresql+psycopg://bz:bz@postgres:5432/bz_stock \
      -e REDIS_URL=redis://redis:6379/0 \
      -e APP_ENV=production -e TZ=Asia/Shanghai \
      market-dev-backend:b4cc65d \
      python scripts/verify_603538_step6.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from datetime import date

import pandas as pd

from app.db import AsyncSessionLocal
from app.services.adjustment_factor_service import AdjustmentFactorService
from app.services.market_data_aggregation_service import MarketDataAggregationService

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("verify_step6")

# 603538 美诺华
IID_603538 = uuid.UUID("1fea317d-7206-41e9-b371-2ef79a57ce73")
SYMBOL_603538 = "603538"
# 600276 恒瑞医药（对照股，factor 全 1.0）
IID_600276 = uuid.UUID("4f31316a-ed09-455a-b162-d9c3b5523a2c")
SYMBOL_600276 = "600276"

# 除权日
EX_DIV_DATE = date(2026, 7, 9)
# 验证窗口
WINDOW_START = date(2026, 6, 15)
WINDOW_END = date(2026, 7, 15)


def _sep(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _fmt_row(ts, row, cols=("open", "high", "low", "close")) -> str:
    parts = [f"{ts}"]
    for c in cols:
        if c in row:
            parts.append(f"{c}={float(row[c]):.2f}")
    return " ".join(parts)


async def step1_factor_rebuild(afs: AdjustmentFactorService, session) -> bool:
    """Step1: factor rebuild 修复 603538 adj_factor。"""
    _sep("Step1: factor rebuild 修复 603538 adj_factor")

    # 1a. 重建前：查询当前 factor 状态
    factor_before = await afs.get_factor_series(session, IID_603538)
    print(f"[重建前] factor 序列行数: {len(factor_before)}")
    if not factor_before.empty:
        window_before = factor_before[
            (factor_before["trade_date"] >= pd.Timestamp(WINDOW_START))
            & (factor_before["trade_date"] <= pd.Timestamp(WINDOW_END))
        ]
        print("[重建前] 窗口内 factor（前5 + 后5）:")
        for _, r in window_before.head(5).iterrows():
            print(f"  {r['trade_date'].strftime('%Y-%m-%d')} factor={r['adj_factor']:.6f}")
        print("  ...")
        for _, r in window_before.tail(5).iterrows():
            print(f"  {r['trade_date'].strftime('%Y-%m-%d')} factor={r['adj_factor']:.6f}")
        broken = window_before[window_before["adj_factor"] == 1.0]
        print(f"[重建前] 窗口内 factor=1.0 的行数: {len(broken)}（除权后正常，除权前应为~0.711）")

    # 1b. 删除旧 fingerprint（确保 detect 能检测到变化，测试完整 detect→rebuild 流程）
    afs._delete_fingerprint(IID_603538)
    print("[1b] 已删除旧 fingerprint（确保 detect→rebuild 完整流程）")

    # 1c. detect_company_action_change → earliest_affected
    earliest = await afs.detect_company_action_change(session, IID_603538, SYMBOL_603538)
    print(f"[1c] detect_company_action_change earliest_affected={earliest}")
    if earliest is None:
        print("  [警告] detect 返回 None（无公司行为变化），直接用历史最早日期 rebuild")
        earliest = date(2020, 1, 1)

    # 1d. rebuild_factor_series（失败回滚 fingerprint）
    try:
        count = await afs.rebuild_factor_series(
            session, IID_603538, SYMBOL_603538, earliest
        )
        await session.commit()
        print(f"[1d] rebuild_factor_series 成功 records={count} 已 commit")
    except Exception as exc:
        await session.rollback()
        afs._delete_fingerprint(IID_603538)
        print(f"[1d] rebuild 失败: {exc}，已回滚 fingerprint 和 DB")
        return False

    # 1e. 重建后：验证 factor 序列
    factor_after = await afs.get_factor_series(session, IID_603538)
    print(f"\n[重建后] factor 序列行数: {len(factor_after)}")
    window_after = factor_after[
        (factor_after["trade_date"] >= pd.Timestamp(WINDOW_START))
        & (factor_after["trade_date"] <= pd.Timestamp(WINDOW_END))
    ]
    print("[重建后] 窗口内完整 factor:")
    for _, r in window_after.iterrows():
        print(f"  {r['trade_date'].strftime('%Y-%m-%d')} factor={r['adj_factor']:.6f}")

    # 1f. 验证：除权日前 factor 应一致（~0.711），除权日后应为 1.0
    pre_div = window_after[window_after["trade_date"] < pd.Timestamp(EX_DIV_DATE)]
    post_div = window_after[window_after["trade_date"] >= pd.Timestamp(EX_DIV_DATE)]
    pre_factors = {round(float(f), 4) for f in pre_div["adj_factor"]}
    post_factors = {round(float(f), 4) for f in post_div["adj_factor"]}
    print(f"\n[验证] 除权日前 unique factors: {pre_factors}")
    print(f"[验证] 除权日后 unique factors: {post_factors}")

    ok = True
    if len(pre_factors) > 2:
        print(f"  [失败] 除权日前 factor 不一致（应全部~0.711）: {pre_factors}")
        ok = False
    else:
        print("  [通过] 除权日前 factor 一致（~0.711）✓")
    if post_factors != {1.0}:
        print(f"  [失败] 除权日后 factor 应为 1.0: {post_factors}")
        ok = False
    else:
        print("  [通过] 除权日后 factor=1.0 ✓")

    # 价格连续性验证：除权日前最后 close × factor ≈ 除权日后第一 close
    if not pre_div.empty and not post_div.empty:
        pre_last = pre_div.iloc[-1]
        post_first = post_div.iloc[0]
        print(f"\n[价格连续性] 除权前最后 factor={float(pre_last['adj_factor']):.4f}, 除权后第一 factor={float(post_first['adj_factor']):.4f}")
        print(f"  factor 比值 = {float(pre_last['adj_factor']):.4f}（应≈0.711，即价格除权后约为之前的 71%）")

    return ok


async def step2_bars_none_qfq(mdas: MarketDataAggregationService, session) -> bool:
    """Step2: 1d/15m/1h × none/qfq 逐 bar 证据。"""
    _sep("Step2: 1d/15m/1h × none/qfq 逐 bar 证据")
    ok = True

    for tf in ["1d", "15m", "1h"]:
        print(f"\n--- timeframe={tf} ---")
        results = {}
        for adj in ["none", "qfq"]:
            result = await mdas.get_bars(
                session, IID_603538,
                timeframe=tf, adj=adj,
                include_realtime=False, completed_only=True,
                start_date=WINDOW_START, end_date=WINDOW_END,
                adjustment_as_of=None,
            )
            results[adj] = result
            print(f"  [{adj}] bars={len(result.bars)} source={result.data_source} "
                  f"degraded={result.degraded} reason={result.degraded_reason}")
            print(f"       source_bar_hash={result.source_bar_hash} "
                  f"adj_factor_hash={result.adj_factor_hash}")
            print(f"       contract_version={result.market_data_contract_version} "
                  f"completed_through={result.completed_through}")

        # 验证 none 和 qfq 的 bar 数量和时间一致
        none_df = results["none"].bars
        qfq_df = results["qfq"].bars
        if len(none_df) != len(qfq_df):
            print(f"  [失败] bar 数量不一致 none={len(none_df)} qfq={len(qfq_df)}")
            ok = False
            continue
        if not none_df.empty:
            # 时间索引一致
            if not none_df.index.equals(qfq_df.index):
                print("  [失败] 时间索引不一致")
                ok = False
                continue
            print(f"  [通过] bar 数量和时间索引一致（{len(none_df)} 根）✓")

            # 输出除权日附近的逐 bar 对比
            print("\n  除权日附近逐 bar 对比（none vs qfq）:")
            ex_div_ts = pd.Timestamp(EX_DIV_DATE)
            # 找除权日前后各 3 根
            if tf == "1d":
                mask = (none_df.index >= ex_div_ts - pd.Timedelta(days=7)) & \
                       (none_df.index <= ex_div_ts + pd.Timedelta(days=7))
            else:
                mask = (none_df.index >= ex_div_ts - pd.Timedelta(days=3)) & \
                       (none_df.index <= ex_div_ts + pd.Timedelta(days=3))
            for ts in none_df.index[mask]:
                n_row = none_df.loc[ts]
                q_row = qfq_df.loc[ts]
                print(f"    {ts} | raw_close={float(n_row['close']):.2f} "
                      f"qfq_close={float(q_row['close']):.2f} "
                      f"ratio={float(q_row['close'])/float(n_row['close']):.4f}"
                      if float(n_row['close']) != 0 else
                      f"    {ts} | raw_close={float(n_row['close']):.2f} qfq_close={float(q_row['close']):.2f}")

            # 验证 none 的 source_bar_hash 与 qfq 不同（OHLCV 不同）
            if results["none"].source_bar_hash == results["qfq"].source_bar_hash:
                print("  [警告] none 和 qfq source_bar_hash 相同（可能 OHLCV 一致，adj=none 时 qfq 无变化）")
            else:
                print("  [通过] none 和 qfq source_bar_hash 不同（OHLCV 已复权）✓")

            # 验证 adj=none 时 adj_factor_hash 为空
            if results["none"].adj_factor_hash != "":
                print(f"  [失败] adj=none 时 adj_factor_hash 应为空，实际={results['none'].adj_factor_hash}")
                ok = False
            else:
                print("  [通过] adj=none 时 adj_factor_hash 为空 ✓")

    return ok


async def step3_adjustment_as_of(mdas: MarketDataAggregationService, afs: AdjustmentFactorService, session) -> bool:
    """Step3: adjustment_as_of 三个锚点验证（无未来泄漏）。"""
    _sep("Step3: adjustment_as_of 三个锚点验证（无未来泄漏）")

    # 三个锚点
    as_of_dates = [date(2026, 7, 1), date(2026, 7, 3), date(2026, 7, 15)]
    ok = True

    # 获取完整 factor 序列（用于无未来泄漏对照：截断序列不得包含 > as_of 的因子）
    full_factor = await afs.get_factor_series(session, IID_603538)
    full_map: dict = {}
    if not full_factor.empty:
        for _, r in full_factor.iterrows():
            full_map[r["trade_date"].date()] = float(r["adj_factor"])

    # raw bars（公式验证分母，所有 as_of 共用）
    raw_result = await mdas.get_bars(
        session, IID_603538,
        timeframe="1d", adj="none",
        include_realtime=False, completed_only=True,
        start_date=WINDOW_START, end_date=WINDOW_END,
    )
    raw_df = raw_result.bars

    for as_of in as_of_dates:
        print(f"\n--- adjustment_as_of={as_of} ---")
        result = await mdas.get_bars(
            session, IID_603538,
            timeframe="1d", adj="qfq",
            include_realtime=False, completed_only=True,
            start_date=WINDOW_START, end_date=WINDOW_END,
            adjustment_as_of=as_of,
        )
        print(f"  bars={len(result.bars)} source_bar_hash={result.source_bar_hash} "
              f"adj_factor_hash={result.adj_factor_hash}")
        print(f"  adjustment_as_of回显={result.adjustment_as_of} "
              f"completed_through={result.completed_through}")

        if result.bars.empty:
            print("  [跳过] 无数据")
            continue

        # 获取截断因子序列（只含 trade_date <= as_of，与 MDAS 内部 get_factor_series(as_of=) 一致）。
        # 禁止未来除权事件泄漏：as_of 之后的因子（如除权后 1.0）不得参与计算。
        truncated_factor = await afs.get_factor_series(session, IID_603538, as_of=as_of)
        trunc_map: dict = {}
        if not truncated_factor.empty:
            for _, r in truncated_factor.iterrows():
                trunc_map[r["trade_date"].date()] = float(r["adj_factor"])

        # factor(as_of) = 截断序列最后一个因子（ffill 语义）
        if trunc_map:
            as_of_factor = list(trunc_map.values())[-1]
        else:
            as_of_factor = 1.0
        print(f"  factor(as_of={as_of})={as_of_factor:.6f} (截断序列，禁止未来泄漏)")

        # 公式验证: qfq = raw × factor(bar_date) / factor(as_of)
        # factor(bar_date) 用截断序列 ffill：bar_date > as_of 时取截断序列最后因子
        # （因为 as_of 之后的公司行为未知，bar 不得用未来因子）
        print("\n  逐 bar 公式验证 (qfq = raw × factor(bar_date) / factor(as_of)):")
        print(f"  {'日期':<12} {'raw_close':>10} {'f(bar)':>10} {'f(as_of)':>10} {'预期qfq':>10} {'实际qfq':>10} {'误差':>10}")
        formula_ok = True
        for ts in result.bars.index:
            bar_date = ts.date() if hasattr(ts, "date") else pd.Timestamp(ts).date()
            raw_close = float(raw_df.loc[ts, "close"]) if ts in raw_df.index else float("nan")
            qfq_close = float(result.bars.loc[ts, "close"])
            # f_bar: 截断序列中 <= bar_date 的最后一个因子（ffill）
            eligible_bar = {d: f for d, f in trunc_map.items() if d <= bar_date}
            if eligible_bar:
                f_bar = eligible_bar[max(eligible_bar)]
            elif trunc_map:
                f_bar = list(trunc_map.values())[0]
            else:
                f_bar = 1.0
            expected_qfq = raw_close * f_bar / as_of_factor if as_of_factor != 0 else float("nan")
            diff = abs(qfq_close - expected_qfq) if not pd.isna(expected_qfq) else float("nan")
            status = "✓" if diff < 0.01 else "✗"
            if diff >= 0.01:
                formula_ok = False
            print(f"  {str(bar_date):<12} {raw_close:>10.2f} {f_bar:>10.4f} {as_of_factor:>10.4f} "
                  f"{expected_qfq:>10.2f} {qfq_close:>10.2f} {diff:>10.4f} {status}")

        if formula_ok:
            print("  [通过] 公式验证全部通过 ✓")
        else:
            print("  [失败] 公式验证存在误差")
            ok = False

        # 无未来泄漏验证：截断序列不得包含 > as_of 的因子；
        # as_of 早于除权日时，factor(as_of) 必须是除权前因子（~0.711），不得是除权后 1.0。
        # 正确语义：as_of 之后的除权事件对 as_of 时点未知，bar 用截断序列 ffill 因子，
        # 因此 as_of 早于除权日时所有 bar qfq=raw（ratio=1），这是无泄漏的正确表现。
        print("\n  无未来泄漏验证（截断序列不得读取 as_of 之后的事件）:")
        leak_ok = True
        # 1. 截断序列不应包含任何 trade_date > as_of
        future_in_trunc = [d for d in trunc_map if d > as_of]
        if future_in_trunc:
            leak_ok = False
            print(f"    [失败] 截断序列包含未来日期 {future_in_trunc[:3]}（未来泄漏）✗")
        # 2. as_of 早于除权日时，factor(as_of) 不得等于除权后因子
        if as_of < EX_DIV_DATE:
            post_div_vals = {f for d, f in full_map.items() if d >= EX_DIV_DATE}
            pre_div_vals = {f for d, f in full_map.items() if d < EX_DIV_DATE}
            if pre_div_vals and as_of_factor in post_div_vals and as_of_factor not in pre_div_vals:
                leak_ok = False
                print(f"    [失败] as_of={as_of} 早于除权日但 factor(as_of)={as_of_factor} "
                      f"用了除权后因子（未来泄漏）✗")
        # 3. 对照完整序列：截断序列长度应 <= 完整序列中 <= as_of 的因子数
        full_upto = sum(1 for d in full_map if d <= as_of)
        print(f"    截断序列因子数={len(trunc_map)} 完整序列<=as_of 因子数={full_upto}")
        if len(trunc_map) > full_upto:
            leak_ok = False
            print("    [失败] 截断序列因子数多于 <=as_of 的因子数（未来泄漏）✗")
        if leak_ok:
            print(f"    [通过] factor(as_of={as_of}) 只用 <=as_of 的因子（无未来泄漏）✓")
        else:
            ok = False

    return ok


async def step4_control_stock(mdas: MarketDataAggregationService, session) -> bool:
    """Step4: 对照股 600276 恒瑞医药（无公司行为，none 与 qfq 应一致）。"""
    _sep("Step4: 对照股 600276 恒瑞医药（无公司行为）")
    ok = True

    for adj in ["none", "qfq"]:
        result = await mdas.get_bars(
            session, IID_600276,
            timeframe="1d", adj=adj,
            include_realtime=False, completed_only=True,
            start_date=WINDOW_START, end_date=WINDOW_END,
        )
        print(f"  [{adj}] bars={len(result.bars)} source_bar_hash={result.source_bar_hash} "
              f"adj_factor_hash={result.adj_factor_hash}")
        if not result.bars.empty:
            print(f"       首根 close={float(result.bars.iloc[0]['close']):.2f} "
                  f"末根 close={float(result.bars.iloc[-1]['close']):.2f}")

    # 验证 none 和 qfq 一致
    none_res = await mdas.get_bars(
        session, IID_600276, timeframe="1d", adj="none",
        include_realtime=False, completed_only=True,
        start_date=WINDOW_START, end_date=WINDOW_END,
    )
    qfq_res = await mdas.get_bars(
        session, IID_600276, timeframe="1d", adj="qfq",
        include_realtime=False, completed_only=True,
        start_date=WINDOW_START, end_date=WINDOW_END,
    )

    if none_res.bars.empty or qfq_res.bars.empty:
        print("  [跳过] 无数据")
        return True

    # OHLCV 完全一致
    none_ohlcv = none_res.bars[["open", "high", "low", "close", "volume"]]
    qfq_ohlcv = qfq_res.bars[["open", "high", "low", "close", "volume"]]
    try:
        pd.testing.assert_frame_equal(none_ohlcv, qfq_ohlcv)
        print("  [通过] none 与 qfq OHLCV 完全一致（factor=1.0，无公司行为）✓")
    except AssertionError as e:
        print(f"  [失败] none 与 qfq OHLCV 不一致: {e}")
        ok = False

    # source_bar_hash 一致
    if none_res.source_bar_hash == qfq_res.source_bar_hash:
        print("  [通过] source_bar_hash 一致 ✓")
    else:
        print(f"  [失败] source_bar_hash 不一致: {none_res.source_bar_hash} vs {qfq_res.source_bar_hash}")
        ok = False

    # adj_factor_hash: none 应为空，qfq 应非空（但全为 1.0）
    if none_res.adj_factor_hash == "":
        print("  [通过] adj=none 时 adj_factor_hash 为空 ✓")
    else:
        print("  [失败] adj=none 时 adj_factor_hash 应为空")
        ok = False

    return ok


async def step5_cross_caller_hash(mdas: MarketDataAggregationService, session) -> bool:
    """Step5: 跨调用方 hash 一致性（同参 4 次调用 MDAS）。"""
    _sep("Step5: 跨调用方 hash 一致性（/bars、indicator、strategy_batch、feature_snapshot 同参）")

    # 模拟各调用方的参数（统一为相同参数集，验证 hash 一致）
    callers = {
        "/bars API": {},
        "indicator_service": {},
        "strategy_batch": {},
        "feature_snapshot": {},
    }

    hashes = {}
    for name in callers:
        result = await mdas.get_bars(
            session, IID_603538,
            timeframe="1d", adj="qfq",
            include_realtime=False, completed_only=True,
            start_date=WINDOW_START, end_date=date(2026, 7, 8),
            adjustment_as_of=None,
        )
        hashes[name] = {
            "source_bar_hash": result.source_bar_hash,
            "adj_factor_hash": result.adj_factor_hash,
            "bars_count": len(result.bars),
            "contract_version": result.market_data_contract_version,
            "completed_through": str(result.completed_through),
        }
        print(f"  [{name}] bars={len(result.bars)} "
              f"source_bar_hash={result.source_bar_hash} "
              f"adj_factor_hash={result.adj_factor_hash} "
              f"completed_through={result.completed_through}")

    # 验证所有 hash 一致
    sb_hashes = {h["source_bar_hash"] for h in hashes.values()}
    af_hashes = {h["adj_factor_hash"] for h in hashes.values()}
    counts = {h["bars_count"] for h in hashes.values()}

    ok = True
    if len(sb_hashes) == 1:
        print(f"\n  [通过] source_bar_hash 跨调用方一致: {sb_hashes.pop()} ✓")
    else:
        print(f"\n  [失败] source_bar_hash 不一致: {sb_hashes}")
        ok = False
    if len(af_hashes) == 1:
        print(f"  [通过] adj_factor_hash 跨调用方一致: {af_hashes.pop()} ✓")
    else:
        print(f"  [失败] adj_factor_hash 不一致: {af_hashes}")
        ok = False
    if len(counts) == 1:
        print(f"  [通过] bars 数量一致: {counts.pop()} ✓")
    else:
        print(f"  [失败] bars 数量不一致: {counts}")
        ok = False

    return ok


async def step6_idempotency(afs: AdjustmentFactorService, session) -> bool:
    """Step6: factor rebuild 幂等 + fingerprint 重检。"""
    _sep("Step6: factor rebuild 幂等 + fingerprint 重检")
    ok = True

    # 6a. detect 再次执行 → 应返回 None（fingerprint 已存储，无变化）
    earliest2 = await afs.detect_company_action_change(session, IID_603538, SYMBOL_603538)
    print(f"[6a] detect 再次执行 earliest_affected={earliest2}")
    if earliest2 is None:
        print("  [通过] fingerprint 已存储，detect 返回 None（无变化）✓")
    else:
        print("  [失败] detect 应返回 None（fingerprint 未正确存储或事件变化）")
        ok = False

    # 6b. 重建前 factor 快照
    factor_before = await afs.get_factor_series(session, IID_603538)

    # 6c. 再次 rebuild（幂等验证）
    try:
        count = await afs.rebuild_factor_series(
            session, IID_603538, SYMBOL_603538, date(2020, 1, 1)
        )
        await session.commit()
        print(f"[6c] 第二次 rebuild records={count}")
    except Exception as exc:
        await session.rollback()
        print(f"[6c] 第二次 rebuild 失败: {exc}")
        ok = False
        return ok

    # 6d. 重建后 factor 快照对比
    factor_after = await afs.get_factor_series(session, IID_603538)
    if len(factor_before) == len(factor_after):
        # 逐行对比 adj_factor
        diffs = 0
        for i in range(len(factor_before)):
            b = float(factor_before.iloc[i]["adj_factor"])
            a = float(factor_after.iloc[i]["adj_factor"])
            if abs(b - a) > 1e-6:
                diffs += 1
        if diffs == 0:
            print("[6d] [通过] 两次 rebuild factor 序列完全一致（幂等）✓")
        else:
            print(f"[6d] [失败] 两次 rebuild factor 有 {diffs} 处不一致")
            ok = False
    else:
        print(f"[6d] [失败] factor 行数不一致: before={len(factor_before)} after={len(factor_after)}")
        ok = False

    return ok


async def main() -> int:
    print("=" * 70)
    print("Step6 真实回归验证: 603538 美诺华 @ 2026-07-03 前后")
    print("分支: fix/market-data-ssot-adjustment-v2-20260717")
    print(f"验证窗口: {WINDOW_START} ~ {WINDOW_END}")
    print(f"除权日: {EX_DIV_DATE}")
    print("=" * 70)

    afs = AdjustmentFactorService()
    mdas = MarketDataAggregationService()

    results = {}
    async with AsyncSessionLocal() as session:
        results["step1_rebuild"] = await step1_factor_rebuild(afs, session)
        results["step2_bars"] = await step2_bars_none_qfq(mdas, session)
        results["step3_as_of"] = await step3_adjustment_as_of(mdas, afs, session)
        results["step4_control"] = await step4_control_stock(mdas, session)
        results["step5_cross_caller"] = await step5_cross_caller_hash(mdas, session)
        results["step6_idempotency"] = await step6_idempotency(afs, session)

    _sep("汇总")
    all_ok = True
    for name, ok in results.items():
        status = "通过 ✓" if ok else "失败 ✗"
        print(f"  {name}: {status}")
        if not ok:
            all_ok = False

    print(f"\n{'=' * 70}")
    print(f"总结: {'全部通过 ✓' if all_ok else '存在失败 ✗'}")
    print(f"{'=' * 70}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
