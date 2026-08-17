"""竞价历史回补抓取（问财源）—— 替代 pytdx 逐股 socket 的慢路径。

背景：
- pytdx 真实源极慢（~94h/120天），且逐股 socket 易触发重试/重连。
- 问财按天问句 `YYYYMMDD竞价涨幅` 一次返回全市场竞价快照（~5554 行），
  远快于逐股拉取。已验证字段含 竞价匹配价/竞价量/竞价金额/竞价涨幅/评级 等。
- 竞价涨幅即相对昨收涨幅%，无需再单独取 prev_close（用户确认）。

限流（用户硬性规则 2026-08-17）：
- 问财对非登录/高频访问有限流。回补 120 天逐日问句，每天之间必须
  随机间隔 30–60 秒（random.uniform(30, 60)），否则会被限流阻断。
- 本模块在「每个交易日问句完成后」sleep 该随机间隔。

本脚本只做抓取 + 字段规范化 + 落盘 jsonl（FILE EVIDENCE）。
正式落库（member-fact DB / migration / API）是后续独立任务，不在本脚本范围。

用法（本地，不连 PG）：
  python3 experiments/pytdx_auction_history/auction_wencai_backfill.py \
      --days 20260814 20260813   # 试跑少量日期
  python3 experiments/pytdx_auction_history/auction_wencai_backfill.py \
      --trading-dates-file dates.txt   # 120 天
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import date
from pathlib import Path

# 复用项目统一问财客户端（底层 HTTP，绕过失效的 pywencai 库）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend"))
from app.services.wencai_client import (  # noqa: E402
    fetch_query_table,
    load_cookie,
    QUERY_INTERVAL_RANGE,
    PAGE_DELAY_RANGE,
)

OUT_DIR = Path(__file__).resolve().parent
# 问句之间：30–60s 随机间隔（用户硬性限流规则）
QUERY_INTERVAL_RANGE = QUERY_INTERVAL_RANGE
# 同一问句内翻页之间：短间隔（默认 1–2s），非 30–60s 规则
PAGE_DELAY_RANGE = PAGE_DELAY_RANGE


def _norm_day(day: str) -> str:
    """接受 20260814 / 2026-08-14，统一成 YYYYMMDD。"""
    day = day.strip().replace("-", "")
    if len(day) != 8 or not day.isdigit():
        raise ValueError(f"非法交易日: {day}")
    return day


def _build_query(day: str) -> str:
    return f"{day}竞价涨幅"


def _extract_row(row: dict, day: str) -> dict:
    """从问财一行提取盘迹回补字段（带 [YYYYMMDD] 后缀的字段按日取）。"""
    def col(name: str) -> Any:
        return row.get(name) or row.get(f"{name}[{day}]")

    return {
        "day": day,
        "code": row.get("股票代码") or row.get("code"),
        "name": row.get("股票简称") or row.get("name"),
        # 竞价匹配价 = final_price
        "final_price": col("竞价匹配价"),
        # 竞价量（股）
        "auction_volume": col("竞价量"),
        # 竞价金额（元）
        "auction_amount": col("竞价金额"),
        # 竞价涨幅%（相对昨收）
        "auction_change_pct": col("竞价涨幅"),
        # 未匹配量/额（稀缺性信号）
        "unmatched_volume": col("竞价未匹配量"),
        "unmatched_amount": col("竞价未匹配金额"),
        # 标签
        "rating": col("集合竞价评级"),
        "anomaly_type": col("竞价异动类型"),
        "anomaly_desc": col("竞价异动说明"),
    }


def backfill_days(days: list[str], out_path: Path) -> dict:
    """抓取多个交易日的竞价快照，落盘 jsonl。

    每天问句间随机间隔 30–60s（限流规则）。
    """
    cookie = load_cookie()
    if not cookie:
        raise SystemExit("未配置 WENCAI_COOKIE：请先运行解析/写入（见 wencai_client）")

    stats = {"days": len(days), "rows": 0, "per_day": {}}
    with out_path.open("w", encoding="utf-8") as f:
        for i, day in enumerate(days):
            day = _norm_day(day)
            t0 = time.time()
            rows = fetch_query_table(
                _build_query(day),
                cookie=cookie,
                perpage=100,
                page_delay_range=PAGE_DELAY_RANGE,  # 同一问句内翻页短间隔（1–2s）
            )
            n = 0
            for r in rows:
                rec = _extract_row(r, day)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
            stats["rows"] += n
            stats["per_day"][day] = n
            print(f"[{day}] rows={n} elapsed={time.time()-t0:.1f}s")
            # 限流（用户硬性规则）：每次「问句之间」随机间隔 30–60s（最后一天不睡）
            if i < len(days) - 1:
                delay = random.uniform(*QUERY_INTERVAL_RANGE)
                print(f"  rate-limit sleep {delay:.1f}s before next query")
                time.sleep(delay)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", help="交易日列表 20260814 20260813 ...")
    ap.add_argument("--trading-dates-file", help="每行一个 YYYYMMDD 的文件")
    ap.add_argument("--out", default=str(OUT_DIR / "auction_wencai_backfill.jsonl"))
    args = ap.parse_args()

    days: list[str] = list(args.days or [])
    if args.trading_dates_file:
        days += [
            ln.strip() for ln in Path(args.trading_dates_file).read_text().splitlines()
            if ln.strip()
        ]
    if not days:
        # 默认试跑最近一个交易日
        days = ["20260814"]
        print("未指定 --days，默认试跑 20260814")

    out_path = Path(args.out)
    stats = backfill_days(days, out_path)
    print("\n=== backfill stats ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"输出: {out_path}")


if __name__ == "__main__":
    main()
