"""选股筛选字段契约测试 - 验证趋势列筛选使用 dsa_dir_bars 而非 current_trend。

用法：
    python tests/test_screener_filter_uses_dsa_dir_bars.py   # 纯逻辑测试
    APP_ENV=test pytest tests/test_screener_filter_uses_dsa_dir_bars.py

测试用例：
1. dsa_selector.yaml manifest 中 dsa_dir_bars 是 filterable 输出字段
2. dsa_selector.yaml manifest 中不存在 current_trend 输出字段
3. _validate_metric_filters 接受 dsa_dir_bars 筛选条件
4. _validate_metric_filters 拒绝 current_trend 筛选条件（422）
5. 前端共享基础列不重新暴露旧 DSA-only 列

业务背景：
- 趋势列原始字段为 dsa_dir_bars（正值为多头持续天数，负值为空头持续天数）
- 前端禁止发送 current_trend 作为筛选字段，必须发送 dsa_dir_bars
- 后端 _validate_metric_filters 按 manifest filterable 白名单校验，current_trend 不在白名单
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# dsa_selector.yaml manifest 路径
_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "app"
    / "strategy_assets"
    / "manifests"
    / "dsa_selector.yaml"
)

# ScreenerPage.tsx 路径（用于静态扫描前端列定义）
_SCREENER_PAGE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "frontend"
    / "src"
    / "pages"
    / "ScreenerPage.tsx"
)

# features/trend-selection/columns.tsx 路径（advice.md v8 Task 7 重构后列定义唯一实现位置）
_TREND_SELECTION_COLUMNS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "frontend"
    / "src"
    / "features"
    / "trend-selection"
    / "columns.tsx"
)


def _load_dsa_selector_manifest() -> dict:
    """加载 dsa_selector.yaml manifest。"""
    with open(_MANIFEST_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_dsa_dir_bars_is_filterable_output():
    """验证 dsa_dir_bars 是 dsa_selector manifest 的 filterable 输出字段。"""
    manifest = _load_dsa_selector_manifest()
    outputs = manifest.get("outputs", [])
    filterable_keys = {
        o["key"] for o in outputs if o.get("filterable") is True
    }
    assert "dsa_dir_bars" in filterable_keys, (
        f"dsa_dir_bars 应在 filterable 白名单中，实际 filterable_keys={filterable_keys}"
    )
    print("  dsa_dir_bars 是 filterable 输出字段 ✓")


def test_current_trend_not_in_manifest_outputs():
    """验证 current_trend 不在 dsa_selector manifest 的任何输出字段中。

    前端禁止发送 current_trend 作为筛选字段，后端 manifest 也不应包含此 key。
    """
    manifest = _load_dsa_selector_manifest()
    outputs = manifest.get("outputs", [])
    all_keys = {o["key"] for o in outputs}
    assert "current_trend" not in all_keys, (
        f"current_trend 不应出现在 manifest outputs 中，实际 all_keys={all_keys}"
    )
    print("  current_trend 不在 manifest outputs 中 ✓")


class _FakeVersion:
    """模拟 StrategyVersion，仅含 manifest 属性供 _validate_metric_filters 使用。"""

    def __init__(self, manifest: dict):
        self.manifest = manifest


def test_validate_metric_filters_accepts_dsa_dir_bars():
    """验证 _validate_metric_filters 接受 dsa_dir_bars 筛选条件。"""
    from app.api.strategy_runs import _validate_metric_filters

    manifest = _load_dsa_selector_manifest()
    version = _FakeVersion(manifest)
    # dsa_dir_bars > 0（多头）应被接受，不抛异常
    filters = [{"metric_key": "dsa_dir_bars", "operator": "gt", "value": 0}]
    _validate_metric_filters(filters, version)  # 不抛异常即通过
    print("  _validate_metric_filters 接受 dsa_dir_bars ✓")


def test_validate_metric_filters_rejects_current_trend():
    """验证 _validate_metric_filters 拒绝 current_trend 筛选条件（422）。"""
    from fastapi import HTTPException

    from app.api.strategy_runs import _validate_metric_filters

    manifest = _load_dsa_selector_manifest()
    version = _FakeVersion(manifest)
    filters = [{"metric_key": "current_trend", "operator": "eq", "value": "多头"}]
    with pytest.raises(HTTPException) as exc_info:
        _validate_metric_filters(filters, version)
    assert exc_info.value.status_code == 422
    assert "current_trend" in str(exc_info.value.detail)
    print("  _validate_metric_filters 拒绝 current_trend (422) ✓")


def test_screener_page_does_not_restore_legacy_dsa_only_column():
    """验证趋势选股共享基础列不恢复旧 DSA-only 趋势列。

    DSA 仍由 selector manifest 提供可筛选字段，但行情列表的共享基础列已按
    CHANGE-20260731-REMOVE-DSA 移除旧 DSA-only 展示列。第一金字塔字段由
    MarketWorkspacePage 的 canonical 列集合追加，不应把旧列重新塞回共享模块。
    """
    # 优先扫描共享列定义（Task 7 后唯一实现位置）
    with open(_TREND_SELECTION_COLUMNS_PATH, encoding="utf-8") as f:
        shared_columns_content = f.read()
    # current_trend 不应作为列 key 出现
    assert "key: 'current_trend'" not in shared_columns_content, (
        "features/trend-selection/columns.tsx 中趋势列 key 不应为 current_trend"
    )
    assert "key: 'dsa_dir_bars'" not in shared_columns_content, (
        "features/trend-selection/columns.tsx 不应恢复旧 dsa_dir_bars 展示列"
    )

    # ScreenerPage.tsx 不应再独立定义 current_trend 列 key（防止回退）
    with open(_SCREENER_PAGE_PATH, encoding="utf-8") as f:
        screener_content = f.read()
    assert "key: 'current_trend'" not in screener_content, (
        "ScreenerPage.tsx 中不应独立定义 current_trend 列 key"
    )
    print("  趋势选股共享基础列未恢复旧 DSA-only 列 ✓")


if __name__ == "__main__":
    print("=== 选股筛选字段契约测试 ===")
    test_dsa_dir_bars_is_filterable_output()
    test_current_trend_not_in_manifest_outputs()
    test_screener_page_does_not_restore_legacy_dsa_only_column()

    # 以下两个测试需要导入 app 模块，standalone 运行（无 PYTHONPATH）时跳过并提示
    # 通过 PYTHONPATH=/root/web_dev/backend 或 pytest 运行可执行全部测试
    try:
        test_validate_metric_filters_accepts_dsa_dir_bars()
        test_validate_metric_filters_rejects_current_trend()
    except ModuleNotFoundError as e:
        print(f"\n提示: {e}")
        print("  _validate_metric_filters 测试需设置 PYTHONPATH=/root/web_dev/backend 或通过 pytest 运行")

    print("\nOK")
    sys.exit(0)
