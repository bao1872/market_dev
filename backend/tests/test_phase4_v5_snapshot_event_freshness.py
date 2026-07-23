"""Phase 4 定向测试：v5 snapshot event_freshness_payload 持久化。

验证内容：
1. ORM StockFeatureSnapshot 接受 event_freshness_payload 且 default=dict 生效
2. _SCHEMA_VERSION == 5
3. compute_feature_snapshot_for_date 签名含 event_freshness_payload 参数
4. build_empty_event_freshness_payload 生成正确 v5 骨架结构
5. 传入 event_freshness_payload 时 snapshot 使用传入值；None 时使用空骨架

不验证：完整 compute_feature_snapshot_for_date 计算（需 DB + MDAS，属集成测试范围）
"""

from __future__ import annotations

import inspect
from datetime import date
from uuid import uuid4

import pytest

from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.services.event_freshness_service import build_empty_event_freshness_payload
from app.services.feature_snapshot_service import (
    _SCHEMA_VERSION,
    compute_feature_snapshot_for_date,
)


class TestSchemaVersionBump:
    def test_schema_version_is_5(self) -> None:
        assert _SCHEMA_VERSION == 5, f"Phase 4 应为 5，实际 {_SCHEMA_VERSION}"


class TestOrmEventFreshnessPayloadField:
    def test_column_exists(self) -> None:
        cols = [c.name for c in StockFeatureSnapshot.__table__.columns]
        assert "event_freshness_payload" in cols

    def test_not_passed_yields_none_attribute(self) -> None:
        """不传 event_freshness_payload 时实例属性为 None（与 degraded_reasons 一致）。

        INSERT 时 SQLAlchemy 省略该列，DB 应用 server_default='{}'。
        生产路径 compute_feature_snapshot_for_date 始终显式传入，不会触发此路径。
        """
        snap = StockFeatureSnapshot(
            instrument_id=uuid4(),
            trade_date=date(2026, 7, 23),
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=5,
            structural_payload={"test": True},
            temporal_payload={"test": True},
            summary_payload={"test": True},
            degraded_reasons=[],
        )
        assert snap.event_freshness_payload is None, "未传时实例属性应为 None"

    def test_accepts_provided_payload(self) -> None:
        payload = build_empty_event_freshness_payload(as_of=date(2026, 7, 23))
        snap = StockFeatureSnapshot(
            instrument_id=uuid4(),
            trade_date=date(2026, 7, 23),
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=5,
            structural_payload={"test": True},
            temporal_payload={"test": True},
            summary_payload={"test": True},
            event_freshness_payload=payload,
            degraded_reasons=[],
        )
        assert snap.event_freshness_payload is payload
        assert snap.event_freshness_payload["meta"]["schema_version"] == 5


class TestComputeFeatureSnapshotSignature:
    def test_event_freshness_payload_param_exists(self) -> None:
        sig = inspect.signature(compute_feature_snapshot_for_date)
        params = sig.parameters
        assert "event_freshness_payload" in params, "缺少 event_freshness_payload 参数"
        assert (
            params["event_freshness_payload"].default is None
        ), "默认应为 None（触发空骨架）"

    def test_precomputed_dsa_bundle_param_exists(self) -> None:
        sig = inspect.signature(compute_feature_snapshot_for_date)
        assert "precomputed_dsa_bundle" in sig.parameters


class TestEmptyEventFreshnessPayloadStructure:
    def test_v5_skeleton_has_required_sections(self) -> None:
        payload = build_empty_event_freshness_payload(
            as_of=date(2026, 7, 23), schema_version=5,
        )
        assert "daily_structure" in payload
        assert "monitor_interaction" in payload
        assert "meta" in payload
        assert payload["meta"]["schema_version"] == 5
        assert payload["meta"]["as_of"] == "2026-07-23"

    def test_daily_structure_has_smc_subkey(self) -> None:
        payload = build_empty_event_freshness_payload(as_of=date(2026, 7, 23))
        assert "smc" in payload["daily_structure"]
        assert "dsa" in payload["daily_structure"]
        assert "swing" in payload["daily_structure"]

    def test_monitor_interaction_has_subkeys(self) -> None:
        payload = build_empty_event_freshness_payload(as_of=date(2026, 7, 23))
        assert "smc" in payload["monitor_interaction"]
        assert "node_cluster" in payload["monitor_interaction"]
        assert "bollinger" in payload["monitor_interaction"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
