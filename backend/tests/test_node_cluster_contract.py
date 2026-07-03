"""Node Cluster 250×16 契约测试。

验证：
- NODE_CLUSTER_LOW_BARS = DAILY_HISTORY_BARS * 16 = 4000
- NODE_CLUSTER_15M_BARS_PER_DAY = 16
- INDICATOR_BARS["15m"] 引用 NODE_CLUSTER_LOW_BARS（不再硬编码 3600）
- CHART_DISPLAY_BARS 与 NODE_CLUSTER_LOW_BARS 分离
- 生产代码中不存在将 3600 作为 Node Cluster 参数的硬编码
"""
import pytest

from app.constants.indicator_contract import (
    CHART_BARS_COUNT,
    DAILY_HISTORY_BARS,
    INDICATOR_BARS,
    NODE_CLUSTER_15M_BARS_PER_DAY,
    NODE_CLUSTER_LOW_BARS,
    NODE_CLUSTER_MINUTE_BARS,
    NODE_CLUSTER_PRIMARY_BARS,
)


class TestNodeClusterContract:
    """Node Cluster 输入契约。"""

    def test_daily_history_bars_is_250(self):
        """日线根数为 250。"""
        assert DAILY_HISTORY_BARS == 250

    def test_15m_bars_per_day_is_16(self):
        """每个交易日 16 根 15m Bar。"""
        assert NODE_CLUSTER_15M_BARS_PER_DAY == 16

    def test_node_cluster_low_bars_is_4000(self):
        """Node Cluster 15m 输入为 250*16=4000 根。"""
        assert NODE_CLUSTER_LOW_BARS == 4000
        assert NODE_CLUSTER_LOW_BARS == DAILY_HISTORY_BARS * NODE_CLUSTER_15M_BARS_PER_DAY

    def test_node_cluster_primary_bars_equals_daily(self):
        """Node Cluster 主周期（日线）= DAILY_HISTORY_BARS。"""
        assert NODE_CLUSTER_PRIMARY_BARS == DAILY_HISTORY_BARS

    def test_node_cluster_minute_bars_is_2(self):
        """Node Cluster 1m 输入为 2 根。"""
        assert NODE_CLUSTER_MINUTE_BARS == 2

    def test_indicator_bars_15m_references_node_cluster(self):
        """INDICATOR_BARS['15m'] 引用 NODE_CLUSTER_LOW_BARS，不再硬编码 3600。"""
        assert INDICATOR_BARS["15m"] == NODE_CLUSTER_LOW_BARS

    def test_chart_display_bars_separate_from_node_input(self):
        """CHART_BARS_COUNT（页面显示）与 NODE_CLUSTER_LOW_BARS（Node 输入）分离。"""
        assert CHART_BARS_COUNT == DAILY_HISTORY_BARS  # 250
        assert CHART_BARS_COUNT != NODE_CLUSTER_LOW_BARS  # 250 != 4000

    def test_no_3600_as_node_cluster_param(self):
        """生产代码中不存在将 3600 作为 Node Cluster 参数的硬编码。"""
        # NODE_CLUSTER_LOW_BARS 不等于 3600
        assert NODE_CLUSTER_LOW_BARS != 3600
        # INDICATOR_BARS["15m"] 不等于 3600
        assert INDICATOR_BARS["15m"] != 3600


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
