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
        self.data: dict[str, object] = {}

    def set(self, key: str, value: object) -> None:
        self.data[key] = value

    def get(self, key: str) -> object | None:
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
# A-D: Redis 读取真正 fail-closed（bytes 解码 / 非 str 类型）
# =============================================================================


def test_malformed_utf8_bytes_none() -> None:
    # A: 非 UTF-8 bytes → 不抛 UnicodeDecodeError，返回 None → schedule_unknown
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    fake.set(f"adj_factor_xdxr_schedule:{IID}", b"\xff\xfe")
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) is None


def test_non_str_int_type_none() -> None:
    # B: raw=123（非 str/bytes）→ None
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    fake.set(f"adj_factor_xdxr_schedule:{IID}", 123)
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) is None


def test_non_str_list_type_none() -> None:
    # C: raw=[]（非 str/bytes）→ None
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    fake.set(f"adj_factor_xdxr_schedule:{IID}", [])
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) is None


def test_valid_utf8_bytes_parsed() -> None:
    # D: 合法 UTF-8 bytes JSON → 正常解析
    svc = AdjustmentFactorService()
    fake = _FakeRedis()
    state = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=date(2026, 9, 10),
    )
    payload = json.dumps(
        {"scanned_as_of": "2026-09-01", "next_event_date": "2026-09-10"},
        separators=(",", ":"), sort_keys=True,
    )
    fake.set(f"adj_factor_xdxr_schedule:{IID}", payload.encode("utf-8"))
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        assert svc.get_corporate_action_schedule_state(IID) == state


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


async def test_detect_empty_xdxr_with_explicit_effective_as_of_stores_schedule() -> None:
    """G1B-3B1.1 合同锁：空 XDXR + 显式 effective_as_of → 仍写 schedule。

    下一轮 bootstrap 时大量股票根本没有 XDXR。调用方若明确传 ``effective_as_of=
    trade_date``，这些股票必须从 ``schedule_unknown`` 收敛为「已证明截至 T 日无已知
    未来事件」，否则优化永远降不下来。本轮只锁这个行为，不改 production caller。
    """
    trade_date = date(2026, 9, 11)
    xdxr = _xdxr_df([])  # 空 XDXR
    fake = _FakeRedis()
    adapter = MagicMock()
    adapter.get_xdxr_info = MagicMock(return_value=xdxr)

    svc = AdjustmentFactorService()
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        result = await svc.detect_company_action_change(
            session=MagicMock(),
            instrument_id=IID,
            symbol="X",
            adapter=adapter,
            effective_as_of=trade_date,
        )

    assert result is None  # 空 XDXR → 无事件 → None
    sched_raw = fake.get(f"adj_factor_xdxr_schedule:{IID}")
    assert sched_raw is not None
    payload = json.loads(sched_raw)
    assert payload["scanned_as_of"] == "2026-09-11"  # 显式 effective_as_of 被记录
    assert payload["next_event_date"] is None  # 空 XDXR → 无已知未来事件


# =============================================================================
# G1B-3B2: 批量 schedule state（MGET）合同 A-F
# =============================================================================


class _CountingRedis:
    """记录 get / mget 调用次数的 fake Redis。"""

    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data: dict[str, object] = dict(data or {})
        self.get_calls = 0
        self.mget_calls = 0

    def set(self, key: str, value: object) -> None:
        self.data[key] = value

    def get(self, key: str) -> object | None:
        self.get_calls += 1
        return self.data.get(key)

    def mget(self, keys: list[str]) -> list[object | None]:
        self.mget_calls += 1
        return [self.data.get(k) for k in keys]


class _BoomRedis:
    """MGET 整体抛错的 fake Redis。"""

    def mget(self, keys: list[str]) -> list[object | None]:
        raise RuntimeError("redis down")


class _ShortRedis:
    """MGET 返回长度不匹配的 fake Redis。"""

    def mget(self, keys: list[str]) -> list[object | None]:
        return ["x"]  # 长度错误


def _schedule_payload(scanned: str, next_event: str | None) -> str:
    return json.dumps({
        "scanned_as_of": scanned,
        "next_event_date": next_event,
    })


def test_batch_schedule_5000_ids_single_mget() -> None:
    # A: 5000 IDs → 1 次 MGET，0 次 GET
    svc = AdjustmentFactorService()
    ids = [uuid.uuid4() for _ in range(5000)]
    fake = _CountingRedis()
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        result = svc.get_corporate_action_schedule_states(ids)
    assert fake.mget_calls == 1
    assert fake.get_calls == 0
    assert len(result) == 5000
    assert all(v is None for v in result.values())


def test_batch_schedule_dedup_keys() -> None:
    # B: 重复 ID → MGET key 去重，返回 unique ID map
    svc = AdjustmentFactorService()
    i1, i2 = uuid.uuid4(), uuid.uuid4()
    fake = _CountingRedis({
        f"adj_factor_xdxr_schedule:{i1}": _schedule_payload("2026-09-01", None),
        f"adj_factor_xdxr_schedule:{i2}": _schedule_payload("2026-09-01", "2026-09-10"),
    })
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        result = svc.get_corporate_action_schedule_states([i1, i2, i1, i2])
    assert len(result) == 2
    assert result[i1] == CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=None,
    )
    assert result[i2] == CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=date(2026, 9, 10),
    )


def test_batch_schedule_malformed_only_that_id() -> None:
    # C: 单个 malformed payload → 仅该 ID=None，其他正常解析
    svc = AdjustmentFactorService()
    i1, i2 = uuid.uuid4(), uuid.uuid4()
    fake = _CountingRedis({
        f"adj_factor_xdxr_schedule:{i1}": "{not-json",
        f"adj_factor_xdxr_schedule:{i2}": _schedule_payload("2026-09-01", None),
    })
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        result = svc.get_corporate_action_schedule_states([i1, i2])
    assert result[i1] is None
    assert result[i2] == CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 1), next_event_date=None,
    )


def test_batch_schedule_mget_raise_all_none() -> None:
    # D: MGET raise → 所有 requested ID=None（安全退化）
    svc = AdjustmentFactorService()
    ids = [uuid.uuid4() for _ in range(3)]
    with patch("app.core.redis_client.get_sync_redis", return_value=_BoomRedis()):
        result = svc.get_corporate_action_schedule_states(ids)
    assert all(v is None for v in result.values())


def test_batch_schedule_mget_length_mismatch_all_none() -> None:
    # E: MGET 返回长度不匹配 → 所有 requested ID=None
    svc = AdjustmentFactorService()
    ids = [uuid.uuid4() for _ in range(3)]
    with patch("app.core.redis_client.get_sync_redis", return_value=_ShortRedis()):
        result = svc.get_corporate_action_schedule_states(ids)
    assert all(v is None for v in result.values())


def test_batch_schedule_empty_ids_no_calls() -> None:
    # F: 空 IDs → Redis 0 calls，返回空 dict
    svc = AdjustmentFactorService()
    fake = _CountingRedis()
    with patch("app.core.redis_client.get_sync_redis", return_value=fake):
        result = svc.get_corporate_action_schedule_states([])
    assert result == {}
    assert fake.mget_calls == 0
    assert fake.get_calls == 0
