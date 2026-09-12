"""C3B — Versioned SMC Monitor Target Contract 测试。

覆盖：
- input identity（daily_bars_hash 稳定性 / 不 hash volume / 不吞微小 float）
- structure extraction（formed AND crossed==False；crossed/未形成不生成 active）
- structure / OB target identity（不 scoped by target_set_version）
- structure_context / target_set_version（bias、crossed context level 改变都进 version）
- OB（mitigated_index 判据；entered/OB_ENTERED 不影响；顺序无关）
- fail-closed（malformed contract 报错，不 silent fallback）
- serialization（确定性、allow_nan=False、SHA256 hex、无 wall-clock）
- isolation（不 import smc_monitor / node_cluster；不重算 SMC）

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_smc_monitor_target_service.py -v -p no:cacheprovider
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.services.smc_monitor_target_service import (
    SmcTargetContractError,
    build_smc_monitor_target_set,
    compute_daily_bars_hash,
)

DEFAULT_PARAMS = {"min_dir_bars": 5}


# =============================================================================
# Fixture helpers
# =============================================================================


def _bars(rows, with_volume=False):
    """rows: list[(time_str, o, h, l, c)]。"""
    idx = pd.to_datetime([r[0] for r in rows])
    data = {
        "open": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[4] for r in rows],
    }
    if with_volume:
        data["volume"] = [1000.0 for _ in rows]
    return pd.DataFrame(data, index=idx)


def _slot(level=None, anchor_index=None, anchor_time=None, crossed=False):
    return {
        "level": level,
        "anchor_index": anchor_index,
        "anchor_time": anchor_time,
        "crossed": crossed,
    }


def _structure(swing_high=None, swing_low=None, internal_high=None, internal_low=None,
               swing_bias=0, internal_bias=0):
    return {
        "swing_bias": swing_bias,
        "internal_bias": internal_bias,
        "slots": {
            "swing_high": swing_high or _slot(),
            "swing_low": swing_low or _slot(),
            "internal_high": internal_high or _slot(),
            "internal_low": internal_low or _slot(),
        },
    }


def _ob(internal=False, bias=1, bar_low=10.0, bar_high=11.0,
        anchor_index=1, anchor_time="2026-01-02T00:00:00",
        confirmed_index=2, confirmed_time="2026-01-03T00:00:00",
        mitigated_index=None, entered=False):
    return {
        "internal": internal,
        "bias": bias,
        "bar_low": bar_low,
        "bar_high": bar_high,
        "anchor_index": anchor_index,
        "anchor_time": anchor_time,
        "confirmed_index": confirmed_index,
        "confirmed_time": confirmed_time,
        "mitigated_index": mitigated_index,
        "entered": entered,
    }


def _smc(structure, obs=None, params=None):
    return {
        "structure_target_state": structure,
        "order_blocks": obs or [],
        "params": params if params is not None else dict(DEFAULT_PARAMS),
    }


def _find_struct(targets, lane, kind):
    for t in targets:
        if t.lane == lane and t.kind == kind:
            return t
    return None


# =============================================================================
# Input identity
# =============================================================================


class TestInputIdentity:
    def test_same_bars_same_hash(self):
        b = _bars([("2026-01-01", 10, 11, 9, 10.5), ("2026-01-02", 10.5, 12, 10, 11.5)])
        assert compute_daily_bars_hash(b) == compute_daily_bars_hash(b)

    def test_one_ohlc_change_changes_hash(self):
        b1 = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        b2 = _bars([("2026-01-01", 10, 11, 9, 10.6)])  # close 改
        assert compute_daily_bars_hash(b1) != compute_daily_bars_hash(b2)

    def test_timestamp_change_changes_hash(self):
        b1 = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        b2 = _bars([("2026-01-02", 10, 11, 9, 10.5)])
        assert compute_daily_bars_hash(b1) != compute_daily_bars_hash(b2)

    def test_volume_change_unchanged(self):
        b_no_vol = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        b_with_vol = _bars([("2026-01-01", 10, 11, 9, 10.5)], with_volume=True)
        assert compute_daily_bars_hash(b_no_vol) == compute_daily_bars_hash(b_with_vol)

    def test_tiny_float_change_not_swallowed(self):
        b1 = _bars([("2026-01-01", 10, 11, 9, 100.00001)])
        b2 = _bars([("2026-01-01", 10, 11, 9, 100.00002)])
        assert compute_daily_bars_hash(b1) != compute_daily_bars_hash(b2)

    def test_deterministic(self):
        b = _bars([("2026-01-01", 10, 11, 9, 10.5), ("2026-01-02", 10.5, 12, 10, 11.5)])
        h1 = compute_daily_bars_hash(b)
        h2 = compute_daily_bars_hash(b)
        assert h1 == h2 and len(h1) == 64


# =============================================================================
# Structure extraction（formed AND crossed==False）
# =============================================================================


class TestStructureExtraction:
    def test_unformed_crossed_false_no_target(self):
        st = _structure(swing_high=_slot(level=None, anchor_index=None, anchor_time=None, crossed=False))
        res = build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), _smc(st))
        assert res.active_structure_targets == []

    def test_formed_crossed_false_target(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        st = _structure(swing_high=sh)
        res = build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), _smc(st))
        assert len(res.active_structure_targets) == 1
        t = res.active_structure_targets[0]
        assert t.lane == "swing" and t.kind == "high"
        assert t.level == 105.0

    def test_formed_crossed_true_no_active_but_context_preserved(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=True)
        st = _structure(swing_high=sh)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res = build_smc_monitor_target_set(bars, _smc(st))
        assert res.active_structure_targets == []
        # crossed slot 仍保留在 structure_context
        ctx = res.structure_context["slots"]["swing_high"]
        assert ctx["level"] == 105.0 and ctx["crossed"] is True

    def test_partial_formed_fail_closed(self):
        sh = _slot(level=105.0, anchor_index=None, anchor_time=None, crossed=False)
        st = _structure(swing_high=sh)
        with pytest.raises(SmcTargetContractError):
            build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), _smc(st))

    def test_equal_not_in_contract(self):
        st = _structure(
            swing_high=_slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        )
        res = build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), _smc(st))
        # 只有 4 个固定槽位进入 contract；equal_high/equal_low 永远不会出现
        assert set(res.structure_context["slots"].keys()) == {
            "swing_high", "swing_low", "internal_high", "internal_low"
        }
        assert "equal_high" not in res.structure_context["slots"]


# =============================================================================
# Structure target identity（不 scoped by target_set_version）
# =============================================================================


class TestStructureIdentity:
    def test_unrelated_ob_added_structure_id_unchanged(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        st = _structure(swing_high=sh)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res_a = build_smc_monitor_target_set(bars, _smc(st, obs=[]))
        res_b = build_smc_monitor_target_set(bars, _smc(st, obs=[_ob()]))
        id_a = _find_struct(res_a.active_structure_targets, "swing", "high").target_id
        id_b = _find_struct(res_b.active_structure_targets, "swing", "high").target_id
        assert id_a == id_b

    def test_unrelated_structure_change_other_id_unchanged(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        ih = _slot(level=102.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        st1 = _structure(swing_high=sh, internal_high=ih)
        st2 = _structure(swing_high=_slot(level=999.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False),
                         internal_high=ih)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(st1))
        res2 = build_smc_monitor_target_set(bars, _smc(st2))
        ih1 = _find_struct(res1.active_structure_targets, "internal", "high").target_id
        ih2 = _find_struct(res2.active_structure_targets, "internal", "high").target_id
        assert ih1 == ih2  # swing_high 改变不影响 internal_high 的 target_id

    def test_level_change_own_id_changes(self):
        sh1 = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        sh2 = _slot(level=105.5, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh1)))
        res2 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh2)))
        id1 = _find_struct(res1.active_structure_targets, "swing", "high").target_id
        id2 = _find_struct(res2.active_structure_targets, "swing", "high").target_id
        assert id1 != id2

    def test_anchor_change_own_id_changes(self):
        sh1 = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        sh2 = _slot(level=105.0, anchor_index=9, anchor_time="2026-01-04T00:00:00", crossed=False)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh1)))
        res2 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh2)))
        id1 = _find_struct(res1.active_structure_targets, "swing", "high").target_id
        id2 = _find_struct(res2.active_structure_targets, "swing", "high").target_id
        assert id1 != id2

    def test_params_change_id_changes(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh), params={"min_dir_bars": 5}))
        res2 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh), params={"min_dir_bars": 7}))
        id1 = _find_struct(res1.active_structure_targets, "swing", "high").target_id
        id2 = _find_struct(res2.active_structure_targets, "swing", "high").target_id
        assert id1 != id2


# =============================================================================
# Structure context / target_set_version
# =============================================================================


class TestStructureContextVersion:
    def test_bias_change_version_changes(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        st1 = _structure(swing_high=sh, swing_bias=1, internal_bias=1)
        st2 = _structure(swing_high=sh, swing_bias=-1, internal_bias=1)  # 仅 bias 变
        res1 = build_smc_monitor_target_set(bars, _smc(st1))
        res2 = build_smc_monitor_target_set(bars, _smc(st2))
        assert res1.target_set_version != res2.target_set_version
        # active target 内容应完全相同（level/anchor 没变）
        assert (
            res1.active_structure_targets[0].target_id
            == res2.active_structure_targets[0].target_id
        )

    def test_crossed_context_level_change_version_changes(self):
        # swing_high 已 crossed，但 internal_high 是 active；
        # 改 crossed context 的 swing_high level → version 必须变
        sh = _slot(level=108.0, anchor_index=2, anchor_time="2026-01-03T00:00:00", crossed=True)
        ih = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        st1 = _structure(swing_high=sh, internal_high=ih)
        st2 = _structure(
            swing_high=_slot(level=109.0, anchor_index=2, anchor_time="2026-01-03T00:00:00", crossed=True),
            internal_high=ih,
        )
        res1 = build_smc_monitor_target_set(bars, _smc(st1))
        res2 = build_smc_monitor_target_set(bars, _smc(st2))
        assert res1.target_set_version != res2.target_set_version

    def test_structure_target_order_no_version_change(self):
        # 固定 slot 顺序，内容相同则 version 相同（dict 顺序无关由 sort_keys 保证）
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        ih = _slot(level=102.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh, internal_high=ih)))
        res2 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh, internal_high=ih)))
        assert res1.target_set_version == res2.target_set_version


# =============================================================================
# Order block targets
# =============================================================================


class TestOrderBlockTargets:
    def test_mitigated_index_none_enters(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob(mitigated_index=None)]))
        assert len(res.active_order_block_targets) == 1

    def test_mitigated_index_set_skipped(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob(mitigated_index=10)]))
        assert res.active_order_block_targets == []

    def test_entered_ignored(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob(entered=True, mitigated_index=None)]))
        assert len(res.active_order_block_targets) == 1  # entered 不影响目标集合

    def test_mitigated_index_missing_fail_closed(self):
        # mitigated_index 字段缺失 → malformed（不得 silent 当 active）
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        ob_bad = {
            "internal": False, "bias": 1, "bar_low": 10.0, "bar_high": 11.0,
            "anchor_index": 1, "anchor_time": "2026-01-02T00:00:00",
            "confirmed_index": 2, "confirmed_time": "2026-01-03T00:00:00",
            "entered": False,
            # mitigated_index 故意缺失
        }
        with pytest.raises(SmcTargetContractError):
            build_smc_monitor_target_set(bars, _smc(_structure(), obs=[ob_bad]))

    def test_two_same_zone_diff_anchor_diff_id(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        obs = [
            _ob(bar_low=10.0, bar_high=11.0, anchor_index=1, anchor_time="2026-01-02T00:00:00"),
            _ob(bar_low=10.0, bar_high=11.0, anchor_index=5, anchor_time="2026-01-06T00:00:00"),
        ]
        res = build_smc_monitor_target_set(bars, _smc(_structure(), obs=obs))
        assert len(res.active_order_block_targets) == 2
        ids = {t.target_id for t in res.active_order_block_targets}
        assert len(ids) == 2

    def test_add_unrelated_ob_old_id_unchanged(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        ob1 = _ob(anchor_index=1, anchor_time="2026-01-02T00:00:00")
        res_a = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[ob1]))
        res_b = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[ob1, _ob(anchor_index=5, anchor_time="2026-01-06T00:00:00")]))
        id_a = res_a.active_order_block_targets[0].target_id
        id_b = [t.target_id for t in res_b.active_order_block_targets if t.anchor_index == 1][0]
        assert id_a == id_b

    def test_ob_zone_change_old_id_changes(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob(bar_low=10.0, bar_high=11.0)]))
        res2 = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob(bar_low=10.5, bar_high=11.5)]))
        id1 = res1.active_order_block_targets[0].target_id
        id2 = res2.active_order_block_targets[0].target_id
        assert id1 != id2

    def test_ob_input_order_no_version_change(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        ob1 = _ob(anchor_index=1, anchor_time="2026-01-02T00:00:00")
        ob2 = _ob(anchor_index=5, anchor_time="2026-01-06T00:00:00", bar_low=20.0, bar_high=21.0)
        res1 = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[ob1, ob2]))
        res2 = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[ob2, ob1]))
        assert res1.target_set_version == res2.target_set_version


# =============================================================================
# TargetSet version 总开关
# =============================================================================


class TestTargetSetVersion:
    def test_bars_hash_change_version_changes(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        st = _structure(swing_high=sh)
        b1 = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        b2 = _bars([("2026-01-01", 10, 11, 9, 10.6)])
        res1 = build_smc_monitor_target_set(b1, _smc(st))
        res2 = build_smc_monitor_target_set(b2, _smc(st))
        assert res1.target_set_version != res2.target_set_version

    def test_params_change_version_changes(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        st = _structure(swing_high=sh)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(st, params={"min_dir_bars": 5}))
        res2 = build_smc_monitor_target_set(bars, _smc(st, params={"min_dir_bars": 7}))
        assert res1.target_set_version != res2.target_set_version

    def test_active_target_set_change_version_changes(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        st_empty = _structure()
        st_with = _structure(swing_high=_slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False))
        res1 = build_smc_monitor_target_set(bars, _smc(st_empty))
        res2 = build_smc_monitor_target_set(bars, _smc(st_with))
        assert res1.target_set_version != res2.target_set_version

    def test_volume_not_version_source(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        st = _structure(swing_high=sh)
        b_no_vol = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        b_with_vol = _bars([("2026-01-01", 10, 11, 9, 10.5)], with_volume=True)
        res1 = build_smc_monitor_target_set(b_no_vol, _smc(st))
        res2 = build_smc_monitor_target_set(b_with_vol, _smc(st))
        assert res1.target_set_version == res2.target_set_version
        assert res1.input_identity["daily_bars_hash"] == res2.input_identity["daily_bars_hash"]


# =============================================================================
# Serialization
# =============================================================================


class TestSerialization:
    def test_deterministic_serialization(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        st = _structure(swing_high=sh)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(st, obs=[_ob()]))
        res2 = build_smc_monitor_target_set(bars, _smc(st, obs=[_ob()]))
        assert json.dumps(res1.to_dict(), sort_keys=True) == json.dumps(res2.to_dict(), sort_keys=True)

    def test_json_allow_nan_false_ok(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        res = build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), _smc(_structure(swing_high=sh), obs=[_ob()]))
        # 所有值有限 → allow_nan=False 成功
        json.dumps(res.to_dict(), allow_nan=False)

    def test_ids_full_sha256_hex(self):
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        res = build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), _smc(_structure(swing_high=sh), obs=[_ob()]))
        assert len(res.target_set_version) == 64
        int(res.target_set_version, 16)  # 合法 hex
        for t in res.active_structure_targets + res.active_order_block_targets:
            assert len(t.target_id) == 64
            int(t.target_id, 16)

    def test_no_wallclock_or_uuid(self):
        # 两次构建结果完全一致（无随机/时间成分）
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res1 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh)))
        res2 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh)))
        assert res1.target_set_version == res2.target_set_version


# =============================================================================
# Fail-closed
# =============================================================================


class TestFailClosed:
    def test_missing_structure_state(self):
        with pytest.raises(SmcTargetContractError):
            build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), {"order_blocks": [], "params": dict(DEFAULT_PARAMS)})

    def test_missing_params(self):
        st = _structure()
        with pytest.raises(SmcTargetContractError):
            build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), {"structure_target_state": st, "order_blocks": []})

    def test_empty_bars_fail_closed(self):
        st = _structure()
        empty = pd.DataFrame(columns=["open", "high", "low", "close"], index=pd.to_datetime([]))
        with pytest.raises(SmcTargetContractError):
            build_smc_monitor_target_set(empty, _smc(st))

    def test_ob_missing_field(self):
        bad_ob = _ob()
        del bad_ob["bar_high"]
        with pytest.raises(SmcTargetContractError):
            build_smc_monitor_target_set(_bars([("2026-01-01", 10, 11, 9, 10.5)]), _smc(_structure(), obs=[bad_ob]))

    def test_ob_bar_low_gt_bar_high(self):
        with pytest.raises(SmcTargetContractError):
            build_smc_monitor_target_set(
                _bars([("2026-01-01", 10, 11, 9, 10.5)]),
                _smc(_structure(), obs=[_ob(bar_low=12.0, bar_high=11.0)]),
            )

    def test_non_finite_ohlc_fail_closed(self):
        b = _bars([("2026-01-01", float("nan"), 11, 9, 10.5)])
        with pytest.raises(SmcTargetContractError):
            compute_daily_bars_hash(b)


# =============================================================================
# Isolation（不依赖 Monitor / Node / 不重算 SMC）
# =============================================================================


class TestIsolation:
    def test_module_does_not_import_smc_monitor_or_node(self):
        import inspect

        import app.services.smc_monitor_target_service as mod

        src = inspect.getsource(mod)
        # 检查实际 import，而非模块自身函数名中的 "smc_monitor" 子串
        assert "app.services.smc_monitor" not in src, "C3B 不应 import smc_monitor（realtime monitor 服务）"
        assert "node_cluster" not in src, "C3B 不应 import Node target service"
        # 不 import SMC core 内核（docstring 中提及期望输入来源是合法文档，不算耦合）
        assert "smc_pine_core" not in src, "C3B 不应 import SMC core 内核重算"

    def test_build_uses_provided_smc_result_not_core(self):
        # 完全合成 smc_result，无需调用 compute_smc_pine 即可构建
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        res = build_smc_monitor_target_set(
            _bars([("2026-01-01", 10, 11, 9, 10.5)]), _smc(_structure(swing_high=sh), obs=[_ob()])
        )
        assert len(res.active_structure_targets) == 1
        assert len(res.active_order_block_targets) == 1


# =============================================================================
# C3B hardening (1)：时间 canonicalization 必须与 core 一致（ts.isoformat()）
# =============================================================================


class TestTimeCanonicalization:
    def test_time_uses_isoformat_not_str(self):
        # tz-aware index：str(ts) 含空格，isoformat 含 'T' 分隔符；二者不同
        idx = pd.to_datetime(["2026-09-12 09:30:00-04:00"])
        bars = pd.DataFrame(
            {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5]}, index=idx
        )
        res = build_smc_monitor_target_set(bars, _smc(_structure()))
        upd = res.input_identity["updated_through"]
        # 必须与 core 实际时间输入路径 idx.isoformat() 一致，不能是 str(ts)
        assert upd == "2026-09-12T09:30:00-04:00"
        assert upd == idx[0].isoformat()
        assert " " not in upd  # str(ts) 会含空格，作为回归防线

    def test_timezone_aware_deterministic(self):
        idx = pd.to_datetime(["2026-09-12 09:30:00-04:00", "2026-09-13 09:30:00-04:00"])
        bars = pd.DataFrame(
            {"open": [10.0, 10.5], "high": [11.0, 12.0], "low": [9.0, 10.0], "close": [10.5, 11.5]},
            index=idx,
        )
        h1 = compute_daily_bars_hash(bars)
        h2 = compute_daily_bars_hash(bars)
        assert h1 == h2 and len(h1) == 64

    def test_bars_hash_matches_isoformat_payload(self):
        # 直接断言 daily_bars_hash 与“以 isoformat 时间 + 原始 float”构造的 payload 一致
        # （即时间走 isoformat，float 走统一 canonical serializer）
        import app.services.smc_monitor_target_service as m

        bars = _bars([("2026-01-01", 10, 11, 9, 10.5), ("2026-01-02", 10.5, 12, 10, 11.5)])
        times, opens, highs, lows, closes = m._extract_bars(bars)
        assert times == [ts.isoformat() for ts in bars.index]
        expected = m._sha256_json(
            {"bars": [[t, o, h, low, c] for t, o, h, low, c in zip(times, opens, highs, lows, closes, strict=True)]}
        )
        assert compute_daily_bars_hash(bars) == expected


# =============================================================================
# C3B hardening (3)：target_id 绑定 monitor target contract schema version
# =============================================================================


class TestTargetContractSchemaVersionBinding:
    @staticmethod
    def _set_schema(new_value):
        import app.services.smc_monitor_target_service as m

        original = m.SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION
        m.SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION = new_value
        return m, original

    def test_monitor_schema_version_change_structure_target_id(self):
        import app.services.smc_monitor_target_service as m

        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res_v1 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh)))
        id_v1 = _find_struct(res_v1.active_structure_targets, "swing", "high").target_id
        mod, original = self._set_schema(m.SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION + 1)
        try:
            res_v2 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh)))
            id_v2 = _find_struct(res_v2.active_structure_targets, "swing", "high").target_id
        finally:
            mod.SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION = original
        assert id_v1 != id_v2

    def test_monitor_schema_version_change_ob_target_id(self):
        import app.services.smc_monitor_target_service as m

        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        res_v1 = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob()]))
        id_v1 = res_v1.active_order_block_targets[0].target_id
        mod, original = self._set_schema(m.SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION + 1)
        try:
            res_v2 = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob()]))
            id_v2 = res_v2.active_order_block_targets[0].target_id
        finally:
            mod.SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION = original
        assert id_v1 != id_v2


# =============================================================================
# C3B hardening (4/5)：统一 canonical serializer（float → hex，non-finite fail）
# =============================================================================


class TestUnifiedCanonicalFloat:
    def test_structure_level_micro_float_version_changes(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        st1 = _structure(swing_high=_slot(level=100.00001, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False))
        st2 = _structure(swing_high=_slot(level=100.00002, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False))
        r1 = build_smc_monitor_target_set(bars, _smc(st1))
        r2 = build_smc_monitor_target_set(bars, _smc(st2))
        assert r1.target_set_version != r2.target_set_version

    def test_ob_boundary_micro_float_version_changes(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        r1 = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob(bar_low=10.0, bar_high=11.0)]))
        r2 = build_smc_monitor_target_set(bars, _smc(_structure(), obs=[_ob(bar_low=10.00001, bar_high=11.0)]))
        assert r1.target_set_version != r2.target_set_version

    def test_params_micro_float_hash_and_version_changes(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        sh = _slot(level=105.0, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)
        r1 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh), params={"k": 1.5}))
        r2 = build_smc_monitor_target_set(bars, _smc(_structure(swing_high=sh), params={"k": 1.50001}))
        assert r1.contract_identity["params_hash"] != r2.contract_identity["params_hash"]
        assert r1.target_set_version != r2.target_set_version

    def test_params_dict_key_order_irrelevant(self):
        import app.services.smc_monitor_target_service as m

        h1 = m._sha256_json({"params": {"a": 1, "b": 2}})
        h2 = m._sha256_json({"params": {"b": 2, "a": 1}})
        assert h1 == h2

    def test_non_finite_params_fail_closed(self):
        bars = _bars([("2026-01-01", 10, 11, 9, 10.5)])
        bad = _smc(_structure(), params={"x": float("nan")})
        with pytest.raises(SmcTargetContractError):
            build_smc_monitor_target_set(bars, bad)

    def test_bool_int_not_confused(self):
        import json as _json

        import app.services.smc_monitor_target_service as m

        # canonicalize 保持各自类型，且 JSON 序列化后 bool 与 int 可区分（true vs 1）
        assert m._canonicalize(True) is True
        assert m._canonicalize(1) == 1
        assert _json.dumps(m._canonicalize(True)) != _json.dumps(m._canonicalize(1))

    def test_unknown_type_fail_closed(self):
        import app.services.smc_monitor_target_service as m

        with pytest.raises(SmcTargetContractError):
            m._canonicalize(object())
