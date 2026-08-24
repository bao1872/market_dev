"""[Slice 4A8] Admin 增量发布状态面板切到 Unified Review pointer。

Pure-unit tests（PURE_UNIT_TEST=1，不连库）覆盖：

1. admin 状态端点不再查询/展示 legacy market_aggregation 指针作为当前
   板块/复盘依赖 gate，改为 market_review pointer。
2. 状态响应结构：暴露 review 节点，不再暴露 aggregation 节点。
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

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
# 1. 源码检查：不再把 legacy market_aggregation 作为当前板块/复盘依赖 gate
# --------------------------------------------------------------------------- #
def test_status_source_no_legacy_market_aggregation_gate() -> None:
    """admin status 端点源码不得再出现 market_aggregation 指针依赖。"""
    src = _src()
    assert "PUBLICATION_KIND_MARKET_AGGREGATION" not in src
    assert "market_aggregation" not in src
    # 改用已发布 Unified Review 的 market_review pointer
    assert "PUBLICATION_KIND_MARKET_REVIEW" in src


def test_status_function_reads_market_review_pointer() -> None:
    """get_incremental_publish_status 必须查询 market_review pointer。"""
    fn_src = inspect.getsource(aip.get_incremental_publish_status)
    assert "PUBLICATION_KIND_MARKET_REVIEW" in fn_src


# --------------------------------------------------------------------------- #
# 2. 响应结构：review 节点存在，aggregation 节点移除
# --------------------------------------------------------------------------- #
def test_status_response_has_review_no_aggregation() -> None:
    """返回结构暴露 review 节点，不再暴露 legacy aggregation 节点。"""
    fn_src = inspect.getsource(aip.get_incremental_publish_status)
    assert '"review"' in fn_src
    assert '"aggregation"' not in fn_src
