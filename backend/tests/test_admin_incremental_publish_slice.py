"""[REVIEW-V2-R1 runtime closure] Admin 增量发布状态面板的 review 节点来源。

旧 Review 产品退役后，admin status 的 ``review`` 节点不再读已删除的
``market_review`` publication pointer；改为汇报当前复盘产品
（Market Dashboard 复盘 projection）的最新交易日，不伪造 Review pointer。

Pure-unit tests（PURE_UNIT_TEST=1，不连库）覆盖：

1. admin 状态端点源码不再查询/展示 legacy market_aggregation / market_review pointer。
2. 状态函数从 Market Dashboard projection 推导 review 状态。
3. 状态响应结构：暴露 review 节点，不再暴露 aggregation 节点。
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

# Pure-unit guard: these tests must never touch a real DB/network.
os.environ.setdefault("PURE_UNIT_TEST", "1")

from app.api import admin_incremental_publish as aip  # noqa: E402

_SRC_PATH = (
    Path(__file__).resolve().parent.parent
    / "app/api/admin_incremental_publish.py"
)


def _src() -> str:
    return _SRC_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. 源码检查：legacy 板块聚合 / 旧 Review pointer 全部退役
# --------------------------------------------------------------------------- #
def test_status_source_has_no_legacy_review_or_aggregation_pointer() -> None:
    """admin status 端点源码不得再出现 legacy market_aggregation / market_review pointer。"""
    src = _src()
    assert "PUBLICATION_KIND_MARKET_AGGREGATION" not in src
    assert "market_aggregation" not in src
    # [REVIEW-V2-R1] 旧 market_review publication 已退役：不得再作为 review 依赖。
    assert "PUBLICATION_KIND_MARKET_REVIEW" not in src
    assert "review_publication_service" not in src


def test_status_function_reads_market_dashboard_projection() -> None:
    """get_incremental_publish_status 的 review 节点来自 Market Dashboard 复盘 projection。"""
    fn_src = inspect.getsource(aip.get_incremental_publish_status)
    assert "PUBLICATION_KIND_MARKET_REVIEW" not in fn_src, "不得再读已退役 Review pointer"
    assert "review_publication_service" not in fn_src
    assert "MarketDashboardMarketDaily" in fn_src, (
        "review 状态须由 Market Dashboard projection 提供"
    )
    assert "market_dashboard_projection" in fn_src


# --------------------------------------------------------------------------- #
# 2. 响应结构：review 节点存在，aggregation 节点移除
# --------------------------------------------------------------------------- #
def test_status_response_has_review_no_aggregation() -> None:
    """返回结构暴露 review 节点，不再暴露 legacy aggregation 节点。"""
    fn_src = inspect.getsource(aip.get_incremental_publish_status)
    assert '"review"' in fn_src
    assert '"aggregation"' not in fn_src
