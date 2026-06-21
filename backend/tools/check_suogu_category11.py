"""Task 2: 扩缩股处理一致性检验 - 查询 category==11 事件。

检验目标：
1. 查询 DB 中 instruments 表，抽样 10 只股票
2. 对每只股票调用 PytdxAdapter.get_xdxr_info 获取 xdxr 数据
3. 统计 category==11（扩缩股）事件
4. 评估当前 _calculate_adj_factor（仅处理 category==1）是否受影响

用法：
    cd /root/web_dev/backend
    /root/web_dev/backend/.venv/bin/python tools/check_suogu_category11.py

副作用：连接 pytdx 行情服务器（只读），查询 PostgreSQL（只读）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保能导入 app 模块
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import pandas as pd
from sqlalchemy import create_engine, text

from app.core.pytdx_adapter import PytdxAdapter
from app.config import get_settings


def get_sample_symbols_from_db(limit: int = 10) -> list[tuple[str, str]]:
    """从 DB instruments 表抽样实际股票（排除指数/ETF/债券）。

    Returns:
        [(symbol, name), ...] 列表
    """
    settings = get_settings()
    db_url = settings.database_url
    engine = create_engine(db_url)
    with engine.connect() as conn:
        # 查询实际股票（SH: 6开头主板；SZ: 00/30/002 开头）
        # 排除指数（SH 000/880/999，SZ 39）、ETF（SH 51/58，SZ 15/16）、债券
        result = conn.execute(text("""
            SELECT symbol, name
            FROM instruments
            WHERE (
                (market = 'SH' AND symbol ~ '^6[0-9]{5}$')
                OR (market = 'SZ' AND symbol ~ '^(00[0-9]{4}|30[0-9]{4}|002[0-9]{3})$' AND symbol NOT LIKE '39%')
            )
            ORDER BY symbol
            LIMIT :limit
        """), {"limit": limit})
        rows = result.fetchall()
    return [(r[0], r[1]) for r in rows]


def check_category11_events(symbols: list[tuple[str, str]]) -> pd.DataFrame:
    """检查每只股票的 xdxr 数据中是否存在 category==11 事件。

    Returns:
        DataFrame: columns=[symbol, name, total_events, cat1_count, cat11_count,
                            cat11_dates, cat11_suogu_values]
    """
    adapter = PytdxAdapter(max_retries=2)
    adapter.connect()

    results: list[dict] = []
    try:
        for symbol, name in symbols:
            try:
                xdxr_df = adapter.get_xdxr_info(symbol)
            except Exception as exc:
                print(f"  [WARN] 获取 xdxr 失败 symbol={symbol}: {exc}")
                results.append({
                    "symbol": symbol,
                    "name": name,
                    "total_events": 0,
                    "cat1_count": 0,
                    "cat11_count": 0,
                    "cat11_dates": "",
                    "cat11_suogu_values": "",
                    "error": str(exc),
                })
                continue

            if xdxr_df.empty:
                results.append({
                    "symbol": symbol,
                    "name": name,
                    "total_events": 0,
                    "cat1_count": 0,
                    "cat11_count": 0,
                    "cat11_dates": "",
                    "cat11_suogu_values": "",
                    "error": "",
                })
                continue

            total = len(xdxr_df)
            cat1 = len(xdxr_df[xdxr_df["category"] == 1]) if "category" in xdxr_df.columns else 0
            cat11_mask = xdxr_df["category"] == 11 if "category" in xdxr_df.columns else pd.Series([False] * total)
            cat11_df = xdxr_df[cat11_mask]

            # 提取 category==11 事件的日期和 suogu 值
            cat11_dates = ""
            cat11_suogu = ""
            if len(cat11_df) > 0:
                dates_list = cat11_df["date"].dt.strftime("%Y-%m-%d").tolist() if "date" in cat11_df.columns else []
                cat11_dates = "; ".join(dates_list)
                if "suogu" in cat11_df.columns:
                    suogu_list = cat11_df["suogu"].tolist()
                    cat11_suogu = "; ".join([str(s) for s in suogu_list])
                else:
                    cat11_suogu = "（无 suogu 列）"

            results.append({
                "symbol": symbol,
                "name": name,
                "total_events": total,
                "cat1_count": cat1,
                "cat11_count": len(cat11_df),
                "cat11_dates": cat11_dates,
                "cat11_suogu_values": cat11_suogu,
                "error": "",
            })
            print(f"  {symbol} {name}: total={total}, cat1={cat1}, cat11={len(cat11_df)}")
    finally:
        adapter.disconnect()

    return pd.DataFrame(results)


def main() -> None:
    print("=" * 70)
    print("Task 2: 扩缩股处理一致性检验")
    print("=" * 70)

    # 1. 从 DB 抽样 10 只实际股票
    print("\n[1] 从 DB instruments 表抽样 10 只实际股票...")
    try:
        symbols = get_sample_symbols_from_db(limit=10)
    except Exception as exc:
        print(f"  [ERROR] 查询 DB 失败: {exc}")
        print("  降级使用预定义股票列表（10 只知名股票）")
        symbols = [
            ("600519", "贵州茅台"),
            ("000001", "平安银行"),
            ("000002", "万科A"),
            ("600036", "招商银行"),
            ("601318", "中国平安"),
            ("000651", "格力电器"),
            ("600276", "恒瑞医药"),
            ("002415", "海康威视"),
            ("300750", "宁德时代"),
            ("601012", "隆基绿能"),
        ]

    print(f"  抽样股票 {len(symbols)} 只:")
    for s, n in symbols:
        print(f"    {s} {n}")

    # 2. 检查 category==11 事件
    print("\n[2] 调用 PytdxAdapter.get_xdxr_info 检查 category==11 事件...")
    result_df = check_category11_events(symbols)

    # 3. 输出结果汇总
    print("\n" + "=" * 70)
    print("[3] 检验结果汇总（抽样 10 只）")
    print("=" * 70)
    print(result_df[["symbol", "name", "total_events", "cat1_count", "cat11_count"]].to_string(index=False))

    # 4. 输出 category==11 事件详情
    cat11_stocks = result_df[result_df["cat11_count"] > 0]
    if len(cat11_stocks) > 0:
        print(f"\n[4] 发现 {len(cat11_stocks)} 只股票存在 category==11（扩缩股）事件:")
        print("-" * 70)
        for _, row in cat11_stocks.iterrows():
            print(f"  股票: {row['symbol']} {row['name']}")
            print(f"  category==11 事件数: {row['cat11_count']}")
            print(f"  事件日期: {row['cat11_dates']}")
            print(f"  suogu 值: {row['cat11_suogu_values']}")
            print("-" * 70)
    else:
        print("\n[4] 抽样的 10 只股票中未发现 category==11（扩缩股）事件")

    # 5. 输出所有 category 分布
    print("\n[5] 所有 xdxr 事件的 category 分布统计（抽样 10 只）:")
    print("-" * 70)
    adapter = PytdxAdapter(max_retries=2)
    adapter.connect()
    all_categories: dict[int, int] = {}
    try:
        for symbol, _ in symbols:
            try:
                xdxr_df = adapter.get_xdxr_info(symbol)
                if xdxr_df.empty or "category" not in xdxr_df.columns:
                    continue
                cat_counts = xdxr_df["category"].value_counts()
                for cat, cnt in cat_counts.items():
                    all_categories[int(cat)] = all_categories.get(int(cat), 0) + int(cnt)
            except Exception:
                continue
    finally:
        adapter.disconnect()

    if all_categories:
        print(f"  {'category':<12} {'事件数':<10} {'含义'}")
        cat_names = {
            1: "除权除息",
            11: "扩缩股",
            2: "配股",
            3: "转配股",
            4: "增发新股",
            5: "股改",
            6: "增发新股发行",
            7: "增发新股上市",
            8: "转配股上市",
            9: "转配",
            10: "未知",
        }
        for cat in sorted(all_categories.keys()):
            name = cat_names.get(cat, "未知")
            print(f"  {cat:<12} {all_categories[cat]:<10} {name}")
    else:
        print("  无 xdxr 事件数据")

    # 6. 大范围扫描：检查更多股票（100 只）以确认 category==11 是否存在
    print("\n" + "=" * 70)
    print("[6] 大范围扫描：检查 100 只股票确认 category==11 是否存在")
    print("=" * 70)
    try:
        broad_symbols = get_sample_symbols_from_db(limit=100)
        print(f"  扫描 {len(broad_symbols)} 只股票...")
        broad_result = check_category11_events(broad_symbols)
        broad_cat11 = broad_result[broad_result["cat11_count"] > 0]
        if len(broad_cat11) > 0:
            print(f"\n  ✗ 大范围扫描发现 {len(broad_cat11)} 只股票存在 category==11 事件:")
            for _, row in broad_cat11.iterrows():
                print(f"    {row['symbol']} {row['name']}: cat11={row['cat11_count']}, 日期={row['cat11_dates']}")
        else:
            print(f"  ✓ 大范围扫描 {len(broad_symbols)} 只股票，未发现 category==11 事件")

        # 大范围扫描的 category 分布
        broad_categories: dict[int, int] = {}
        adapter = PytdxAdapter(max_retries=2)
        adapter.connect()
        try:
            for symbol, _ in broad_symbols:
                try:
                    xdxr_df = adapter.get_xdxr_info(symbol)
                    if xdxr_df.empty or "category" not in xdxr_df.columns:
                        continue
                    cat_counts = xdxr_df["category"].value_counts()
                    for cat, cnt in cat_counts.items():
                        broad_categories[int(cat)] = broad_categories.get(int(cat), 0) + int(cnt)
                except Exception:
                    continue
        finally:
            adapter.disconnect()

        print(f"\n  大范围扫描 category 分布（{len(broad_symbols)} 只股票）:")
        cat_names = {
            1: "除权除息", 11: "扩缩股", 2: "配股", 3: "转配股",
            4: "增发新股", 5: "股改", 6: "增发新股发行", 7: "增发新股上市",
            8: "转配股上市", 9: "转配", 10: "未知",
        }
        for cat in sorted(broad_categories.keys()):
            name = cat_names.get(cat, "未知")
            print(f"    category={cat:<4} {broad_categories[cat]:<8} {name}")
    except Exception as exc:
        print(f"  [WARN] 大范围扫描失败: {exc}")

    # 7. 结论
    print("\n" + "=" * 70)
    print("[7] 结论")
    print("=" * 70)
    total_cat11 = int(result_df["cat11_count"].sum())
    broad_cat11_count = int(broad_result["cat11_count"].sum()) if 'broad_result' in dir() else 0
    if total_cat11 > 0 or broad_cat11_count > 0:
        print(f"  ✗ 存在 category==11（扩缩股）事件")
        print(f"    抽样 10 只: {total_cat11} 个事件")
        print(f"    大范围 100 只: {broad_cat11_count} 个事件")
        print(f"  ✗ 当前 _calculate_adj_factor 仅处理 category==1，不处理 category==11")
        print(f"  → 建议：需要添加扩缩股处理逻辑")
    else:
        print(f"  ✓ 抽样 10 只 + 大范围 100 只股票均未发现 category==11 事件")
        print(f"  → 当前实现不受影响（category==11 事件在 A 股中极为罕见或不存在）")

    # 保存结果到 CSV
    output_path = BACKEND_DIR / "tools" / "check_suogu_category11_result.csv"
    result_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\n结果已保存至: {output_path}")


if __name__ == "__main__":
    main()
