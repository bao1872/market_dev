"""Unit contract for BJ (北交所) market routing in pytdx adapter (§ Phase 3D).

Root cause of BJ DATA_INGESTION_GAP: `market_from_code` 把 920xxx 路由到 SZ（market=0），
而 pytdx/通达信协议中 BJ 在 market=2（BSE）。修复后 92x/43x/83x/87x/88x → 2。
"""
import pytest

from app.core.pytdx_adapter import market_from_code


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600519", 1),  # SH 主板
        ("688635", 1),  # SH 科创板
        ("000858", 0),  # SZ 主板
        ("300550", 0),  # SZ 创业板
        ("001399", 0),  # SZ 中小板/主板
        # BJ/BSE → market=2
        ("920002", 2),  # 北交所
        ("920009", 2),
        ("920010", 2),
        ("430012", 2),  # 新三板/北交所旧码
        ("830001", 2),
        ("870001", 2),
        ("880001", 2),
    ],
)
def test_market_from_code_routes_bj_to_2(code: str, expected: int) -> None:
    assert market_from_code(code) == expected


def test_market_from_code_sz_unchanged() -> None:
    """非 6 前缀、非 BJ 前缀仍为 SZ（0），行为不回归。"""
    assert market_from_code("000858") == 0
    assert market_from_code("001399") == 0
    assert market_from_code("002594") == 0
    assert market_from_code("399001") == 0  # 指数仍 SZ


def test_market_from_code_sh_unchanged() -> None:
    assert market_from_code("600000") == 1
    assert market_from_code("688111") == 1


def test_market_from_code_bj_beats_sz_prefix() -> None:
    """92x 必须是 BJ，不能误判为 SZ。"""
    assert market_from_code("920002") == 2
    # 88x 在 BJ 分支（与 stock_symbol_sql_filter 一致）
    assert market_from_code("880001") == 2
