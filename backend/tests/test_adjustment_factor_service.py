"""G1B-3B1: XDXR 未来事件日程（CorporateActionScheduleState）存储 / 读取 + detect 集成。

纯单元测试：用 fake Redis 替换 get_sync_redis，不连真实 Redis / DB。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from app.services.adjustment_factor_calculator import corporate_action_fingerprint
from app.services.adjustment_factor_service import (
    AdjustmentFactorService,
    CorporateActionScheduleState,
)

IID = uuid.uuid4()


def _xdxr_df(events: list[dict]) -> pd.DataFrame:
    rows = []
    for e in events:
        rows.append({
            "date": pd.Timestamp(e["date"]),
            "category": e.get("category", 1),
            "fenhong": e.get("fenhong", 0.0),
            "songzhuangu": e.get("songzhuangu", 0.0),
            "peigu": e.get("peigu", 0.0),
            "peigujia": e.get("peigujia", 0.0),
        })
    return pd.DataFrame(rows, columns=[
        "date", "category", "fenhong", "songzhuangu", "peigu", "peigujia",
    ])


class _FakeRedis:
    """内存版 Redis（仅实现计划用到的 get/set）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def get(self, key: str) -> str | None:
        return self.data.get(key)


class _ErrorRedis:
    """Redis 全程报错（模拟连接/协议故障）。"""

    def set(self, key: str, value: str) -> None:
        raise RuntimeError("redis down")

    def get(self, key: str) -> str | None:
        raise RuntimeError("redis down")


# =============================================================================
# 7-14: schedule metadata 存储 / 读取（fail-closed）
# =============================================================================


def test_store_read_roundtrip() -> None:
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    state = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=date(2026, 9, 10),
    )
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        svc._store_schedule_state(IID, state)
        got = svc.get_corporate_action_schedule_state(IID)
    assert got == state


def test_store_read_next_event_none() -> None:
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    state = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=None,
    )
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        svc._store_schedule_state(IID, state)
        got = svc.get_corporate_action_schedule_state(IID)
    assert got == state


def test_malformed_json_none() -> None:
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    fake.data[f"adj_factor_xdxr_schedule:{IID}"] = "{not-json"
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) is None


def test_missing_field_none() -> None:
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    fake.data[f"adj_factor_xdxr_schedule:{IID}"] = json.dumps({"scanned_as_of": "2026-09-01"})
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) is None


def test_invalid_scanned_as_of_none() -> None:
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    fake.data[f"adj_factor_xdxr_schedule:{IID}"] = json.dumps(
        {"scanned_as_of": "x", "next_event_date": None}
    )
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) is None


def test_next_event_le_scanned_none() -> None:
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    fake.data[f"adj_factor_xdxr_schedule:{IID}"] = json.dumps(
        {"scanned_as_of": "2026-09-01", "next_event_date": "2026-08-31"}
    )
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) is None


def test_redis_miss_none() -> None:
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) is None


def test_redis_error_none() -> None:
    svc = AdjustmentFactorService()
    with patch("app.core.redis_client.get_sync_redis", return_value=_ErrorRedis()):
        assert svc.get_corporate_action_schedule_state(IID) is None


# =============================================================================
# 15: future event 出现但 fingerprint 未变 → detect 返回 None，schedule 仍更新
# =============================================================================


async def test_detect_updates_schedule_when_fingerprint_unchanged() -> None:
    """G1B-3B1 核心：fingerprint 未变（无已生效事件变化），但未来事件刚出现，

    detect 必须仍返回 None（不改变现有返回语义），同时 schedule.next_event_date
    被更新为本轮发现的未来事件日。
    """
    cutoff = date(2026, 9, 1)
    xdxr = _xdxr_df([
        {"date": "2026-08-01", "category": 1, "fenhong": 1.0},  # past → 进 fingerprint
        {"date": "2026-09-10", "category": 1, "fenhong": 2.0},  # future → 仅日程
    ])
    fp, _ = corporate_action_fingerprint(xdxr, effective_as_of=cutoff)
    fake = _FakeRedis()
    fake.set(f"adj_factor_fp:{IID}", fp)  # stored == current → detect 返回 None

    adapter = MagicMock()
    adapter.get_xdxr_info = MagicMock(return_value=xdxr)

    svc = AdjustmentFactorService()
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        result = await svc.detect_company_action_change(
            session=MagicMock(),
            instrument_id=IID,
            symbol="X",
            adapter=adapter,
            effective_as_of=cutoff,
        )

    assert result is None  # fingerprint 未变，det 返回语义不变
    sched_raw = fake.get(f"adj_factor_xdxr_schedule:{IID}")
    assert sched_raw is not None
    payload = json.loads(sched_raw)
    assert payload["scanned_as_of"] == "2026-09-01"
    assert payload["next_event_date"] == "2026-09-10"  # 未来事件被记录
