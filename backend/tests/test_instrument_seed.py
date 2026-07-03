"""Instrument 种子服务测试。

覆盖 transform_instruments_df 的核心过滤逻辑：
1. 基础 6 位数字过滤
2. A 股过滤：剔除指数/ETF/基金，保留深桑达A 等股票
3. symbol 冲突时：上海指数 000032 应被过滤，深圳股票 000032 应保留
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.instrument_seed import transform_instruments_df


def test_transform_instruments_df_filters_non_six_digit_codes():
    """非 6 位数字代码应被基础过滤剔除。"""
    raw_df = pd.DataFrame(
        {
            "code": ["600519", "A股指数", "可转债", "300750"],
            "name": ["贵州茅台", "A股指数", "某某转债", "宁德时代"],
            "market": ["SH", "SH", "SZ", "SZ"],
        }
    )
    df = transform_instruments_df(raw_df)
    symbols = set(df["symbol"])
    assert symbols == {"600519", "300750"}


def test_transform_instruments_df_filters_index_and_etf():
    """指数/ETF/基金应被 A 股过滤剔除。"""
    raw_df = pd.DataFrame(
        {
            "code": ["000001", "000032", "399001", "510300", "159919"],
            "name": ["上证指数", "上证能源", "深证成指", "沪深300ETF", "嘉实沪深300ETF"],
            "market": ["SH", "SH", "SZ", "SH", "SZ"],
        }
    )
    df = transform_instruments_df(raw_df)
    assert df.empty


def test_transform_instruments_df_keeps_sz_stock_when_sh_index_collides():
    """上海指数与深圳股票 symbol 冲突时，应保留深圳股票（深桑达A）。

    复现 bug：000032 同时对应 SH 指数'上证能源'和 SZ 股票'深桑达A'。
    种子脚本应过滤掉指数，仅保留深桑达A。
    """
    raw_df = pd.DataFrame(
        {
            "code": ["000032", "000032"],
            "name": ["上证能源", "深桑达A"],
            "market": ["SH", "SZ"],
        }
    )
    df = transform_instruments_df(raw_df)
    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "000032"
    assert df.iloc[0]["name"] == "深桑达A"
    assert df.iloc[0]["market"] == "SZ"


def test_transform_instruments_df_keeps_normal_stocks():
    """普通 A 股股票应完整保留并生成拼音首字母。"""
    raw_df = pd.DataFrame(
        {
            "code": ["600519", "000001", "300750"],
            "name": ["贵州茅台", "平安银行", "宁德时代"],
            "market": ["SH", "SZ", "SZ"],
        }
    )
    df = transform_instruments_df(raw_df)
    assert len(df) == 3
    symbols = set(df["symbol"])
    assert symbols == {"600519", "000001", "300750"}
    # 拼音首字母已生成
    initials = {row["symbol"]: row["pinyin_initials"] for _, row in df.iterrows()}
    assert initials["600519"] == "gzmt"
    assert initials["000001"] == "payh"
    assert initials["300750"] == "ndsd"


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
