"""Realtime SMC Transition Semantics 单元测试（DESIGN PASS @6100ecf8）。

覆盖：fail-closed 门槛、close-only crossing、动态 lane bias、一次性 structure、
identity 不含 event_type、internal gate（含 unformed counterpart）、OB episode、
session/gap 行为、deterministic replay。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.services.smc_monitor_target_service import build_smc_monitor_target_set
from app.services.smc_realtime_transition_service import (
    QfqMinuteBar,
    RealtimeSmcEvaluationContext,
    deserialize_transition_state,
    evaluate_smc_realtime_transitions,
    initial_transition_state,
    logical_ob_key,
    serialize_transition_state,
    state_fingerprint,
    structure_transition_key,
)

PARAMS = {"internal_filter_confluence": True}
PARAMS_NO_CONFLUENCE = {"internal_filter_confluence": False}
INSTR = "INST-1"
SESSION = "2026-09-14"
SESSION2 = "2026-09-15"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _slot(level=None, anchor_index=None, anchor_time=None, crossed=False):
    return {"level": level, "anchor_index": anchor_index, "anchor_time": anchor_time, "crossed": crossed}


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
        confirmed_index=2, confirmed_time="2026-01-03T00:00:00"):
    return {
        "internal": internal, "bias": bias, "bar_low": bar_low, "bar_high": bar_high,
        "anchor_index": anchor_index, "anchor_time": anchor_time,
        "confirmed_index": confirmed_index, "confirmed_time": confirmed_time,
        "mitigated_index": None, "entered": False,
    }


def _target_set(structure, obs=None, params=None):
    bars = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5]},
        index=pd.to_datetime(["2026-01-01"]),
    )
    return build_smc_monitor_target_set(
        bars,
        {
            "structure_target_state": structure,
            "order_blocks": obs or [],
            "params": dict(params if params is not None else PARAMS),
        },
    )


def _bar(t, o, h, low, c, session=SESSION):
    return QfqMinuteBar(bar_time=t, session_key=session, open=o, high=h, low=low, close=c)


def _ctx(params=None, proven=True, reason="", sequence_proven=True, sequence_reason=""):
    return RealtimeSmcEvaluationContext(
        effective_params=dict(params if params is not None else PARAMS),
        qfq_coordinate_proven=proven,
        qfq_coordinate_reason=reason,
        completed_bar_sequence_proven=sequence_proven,
        completed_bar_sequence_reason=sequence_reason,
    )


def _run(ts, bars, state=None, params=None, ctx=None):
    if state is None:
        state = initial_transition_state(ts, instrument_id=INSTR, daily_epoch=SESSION)
    return evaluate_smc_realtime_transitions(
        instrument_id=INSTR,
        target_set=ts,
        completed_bars=bars,
        context=ctx if ctx is not None else _ctx(params),
        state=state,
    )


def _sh(level=105.0):
    return _slot(level=level, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)


# ---------------------------------------------------------------------------
# gates / fail closed
# ---------------------------------------------------------------------------


class TestFailClosedGates:
    def test_qfq_not_proven_zero_events(self):
        ts = _target_set(_structure(swing_high=_sh()))
        res = _run(ts, [_bar("09:31", 105, 106, 104, 106)], ctx=_ctx(proven=False, reason="stale"))
        assert res.ok is False
        assert res.structure_transitions == ()
        assert res.ob_episode_transitions == ()
        assert "qfq" in res.fail_closed_reason

    def test_params_hash_mismatch_zero_events(self):
        ts = _target_set(_structure(swing_high=_sh()))
        res = _run(ts, [_bar("09:31", 105, 106, 104, 106)], params={"internal_filter_confluence": True, "x": 1})
        assert res.ok is False
        assert res.structure_transitions == ()
        assert "params_hash" in res.fail_closed_reason

    def test_missing_confluence_param_zero_events(self):
        ts = _target_set(_structure(swing_high=_sh()), params={"other": 1})
        res = _run(ts, [_bar("09:31", 105, 106, 104, 106)], params={"other": 1})
        assert res.ok is False
        assert "internal_filter_confluence" in res.fail_closed_reason


# ---------------------------------------------------------------------------
# structure crossing（close only）
# ---------------------------------------------------------------------------


class TestStructureCrossing:
    def test_wick_over_level_does_not_fire(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)))
        res = _run(
            ts,
            [
                _bar("09:31", 104.0, 104.5, 103.5, 104.0),
                _bar("09:32", 104.0, 106.0, 103.8, 104.5),  # high 刺破但 close 未过
            ],
        )
        assert res.structure_transitions == ()

    def test_close_crossing_fires_once(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)))
        res = _run(
            ts,
            [
                _bar("09:31", 104.0, 104.5, 103.5, 104.0),
                _bar("09:32", 104.5, 106.0, 104.0, 105.5),  # close 过 105
            ],
        )
        assert len(res.structure_transitions) == 1
        t = res.structure_transitions[0]
        assert (t.lane, t.kind) == ("swing", "high")
        assert t.confirmed_bar_time == "09:32"
        assert t.event_type == "BOS"
        assert t.bias_before == 0 and t.bias_after == 1

    def test_same_structure_never_fires_twice(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)))
        res = _run(
            ts,
            [
                _bar("09:31", 104.0, 104.5, 103.5, 104.0),
                _bar("09:32", 104.5, 106.0, 104.0, 105.5),  # fire
                _bar("09:33", 105.5, 106.0, 105.0, 105.8),  # 同结构，仍在 level 上方
            ],
        )
        assert len(res.structure_transitions) == 1

    def test_transition_key_excludes_event_type(self):
        # 同一 (instrument, lane, kind, anchor_time) 的 key 与 bias / event_type 无关
        k = structure_transition_key(INSTR, "swing", "high", "2026-01-04T00:00:00")
        assert k == structure_transition_key(INSTR, "swing", "high", "2026-01-04T00:00:00")
        assert k != structure_transition_key(INSTR, "swing", "low", "2026-01-04T00:00:00")

    def test_bias_minus_one_high_break_is_choch(self):
        ts = _target_set(_structure(swing_high=_sh(105.0), swing_bias=-1))
        res = _run(
            ts,
            [
                _bar("09:31", 104.0, 104.5, 103.5, 104.0),
                _bar("09:32", 104.5, 106.0, 104.0, 105.5),
            ],
        )
        assert res.structure_transitions[0].event_type == "CHoCH"

    def test_gap_crossing_across_sessions(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)))
        res = _run(
            ts,
            [
                _bar("14:59", 104.0, 104.5, 103.5, 104.0, session=SESSION),
                _bar("09:31", 108.0, 109.0, 107.5, 108.5, session=SESSION2),  # 高开越过
            ],
        )
        assert len(res.structure_transitions) == 1


# ---------------------------------------------------------------------------
# dynamic lane bias
# ---------------------------------------------------------------------------


class TestDynamicLaneBias:
    def test_bias_evolves_within_same_day(self):
        ts = _target_set(
            _structure(internal_high=_sh(105.0), internal_low=_slot(95.0, 3, "2026-01-04T00:00:00", False)),
            params=PARAMS_NO_CONFLUENCE,
        )
        res = _run(
            ts,
            [
                _bar("09:31", 100.0, 100.5, 99.5, 100.0),
                _bar("09:32", 100.0, 106.0, 99.8, 105.5),  # high crossing → BOS → bias +1
                _bar("09:33", 105.0, 105.5, 94.0, 94.2),   # low crossing → 此时 bias=+1 → CHoCH
            ],
            params=PARAMS_NO_CONFLUENCE,
        )
        types = [(t.kind, t.event_type, t.bias_before, t.bias_after) for t in res.structure_transitions]
        assert types == [
            ("high", "BOS", 0, 1),
            ("low", "CHoCH", 1, -1),
        ]
        assert res.state.internal.bias == -1

    def test_restart_replay_is_deterministic(self):
        ts = _target_set(
            _structure(internal_high=_sh(105.0), internal_low=_slot(95.0, 3, "2026-01-04T00:00:00", False)),
            params=PARAMS_NO_CONFLUENCE,
        )
        bars = [
            _bar("09:31", 100.0, 100.5, 99.5, 100.0),
            _bar("09:32", 100.0, 106.0, 99.8, 105.5),
            _bar("09:33", 105.0, 105.5, 94.0, 94.2),
        ]
        one_shot = _run(ts, bars, params=PARAMS_NO_CONFLUENCE)

        state = initial_transition_state(ts, instrument_id=INSTR, daily_epoch=SESSION)
        collected = []
        for b in bars:
            step = evaluate_smc_realtime_transitions(
                instrument_id=INSTR, target_set=ts, completed_bars=[b],
                context=_ctx(PARAMS_NO_CONFLUENCE), state=state,
            )
            state = step.state
            collected.extend(step.structure_transitions)

        assert state_fingerprint(state) == state_fingerprint(one_shot.state)
        assert [t.transition_key for t in collected] == [
            t.transition_key for t in one_shot.structure_transitions
        ]


# ---------------------------------------------------------------------------
# internal gate
# ---------------------------------------------------------------------------


class TestInternalGate:
    def _ts(self, internal_high_level=105.0, swing_high_level=105.0, params=PARAMS):
        # swing_high 用 crossed=True：其 level 仍保留在 structure_context（供 internal gate 比较），
        # 但该 slot 不是 active target，从而隔离 internal lane（否则 swing 会先自行触发）。
        if swing_high_level is None:
            sh = _slot()
        else:
            sh = _slot(swing_high_level, 3, "2026-01-04T00:00:00", True)
        return _target_set(
            _structure(swing_high=sh, internal_high=_sh(internal_high_level)), params=params
        )

    def test_equal_level_blocks_internal(self):
        ts = self._ts(internal_high_level=105.0, swing_high_level=105.0)
        res = _run(ts, [_bar("09:31", 104.0, 104.5, 103.5, 104.0),
                        _bar("09:32", 104.5, 107.0, 104.0, 106.0)])
        assert res.structure_transitions == ()

    def test_different_level_with_bullish_bar_fires(self):
        # swing_high 未形成（None）→ 不等式 gate 通过；confluence=True 需 bullish_bar
        ts = self._ts(internal_high_level=105.0, swing_high_level=None)
        res = _run(ts, [_bar("09:31", 105.0, 105.2, 104.9, 105.0),
                        _bar("09:32", 105.0, 107.0, 105.5, 106.0)])
        assert len(res.structure_transitions) == 1
        assert res.structure_transitions[0].lane == "internal"

    def test_confluence_false_ignores_bar_gate(self):
        # 同一组 bar：confluence=True 时 bullish_bar=False 不触发；False 时触发
        ts_true = self._ts(internal_high_level=105.0, swing_high_level=None, params=PARAMS)
        ts_false = self._ts(internal_high_level=105.0, swing_high_level=None, params=PARAMS_NO_CONFLUENCE)
        bars = [_bar("09:31", 105.0, 105.5, 104.5, 105.0),
                _bar("09:32", 105.5, 106.0, 104.0, 106.0)]
        r_true = _run(ts_true, bars, params=PARAMS)
        r_false = _run(ts_false, bars, params=PARAMS_NO_CONFLUENCE)
        assert r_true.structure_transitions == ()
        assert len(r_false.structure_transitions) == 1


# ---------------------------------------------------------------------------
# OB episodes
# ---------------------------------------------------------------------------


class TestObEpisodes:
    def _ts(self, obs=None):
        return _target_set(_structure(), obs=obs if obs is not None else [_ob(bar_low=10.0, bar_high=11.0)])

    def test_entry_notifies_once_then_reentry_is_new_episode(self):
        ts = self._ts()
        bars = [
            _bar("09:31", 12.0, 12.5, 11.5, 12.0),   # outside
            _bar("09:32", 12.0, 12.2, 10.5, 10.8),   # touch（high>=10 且 low<=11）
            _bar("09:33", 10.8, 10.9, 10.4, 10.6),   # 仍在 zone → 不重复
            _bar("09:34", 10.6, 12.5, 11.5, 12.0),   # outside（low 高于 zone）
            _bar("09:35", 12.0, 12.1, 10.9, 11.0),   # 再次进入 → 新 episode
        ]
        res = _run(ts, bars)
        eps = res.ob_episode_transitions
        assert len(eps) == 2
        assert eps[0].entry_bar_time == "09:32"
        assert eps[1].entry_bar_time == "09:35"
        assert eps[0].episode_key != eps[1].episode_key
        assert eps[0].logical_ob_key == eps[1].logical_ob_key

    def test_same_entry_bar_replay_same_episode_key(self):
        ts = self._ts()
        bars = [
            _bar("09:31", 12.0, 12.5, 11.5, 12.0),
            _bar("09:32", 12.0, 12.2, 10.5, 10.8),
        ]
        r1 = _run(ts, bars)
        r2 = _run(ts, bars)
        assert r1.ob_episode_transitions[0].episode_key == r2.ob_episode_transitions[0].episode_key

    def test_session_rollover_does_not_close_episode(self):
        ts = self._ts()
        bars = [
            _bar("14:59", 12.0, 12.2, 10.5, 10.8, session=SESSION),   # 进入
            _bar("09:31", 10.8, 10.9, 10.4, 10.6, session=SESSION2),  # 次日仍在 zone
        ]
        res = _run(ts, bars)
        assert len(res.ob_episode_transitions) == 1  # 不产生假 retest

    def test_gap_without_intersection_is_not_touch(self):
        ts = self._ts()
        bars = [
            _bar("14:59", 12.0, 12.5, 11.5, 12.0, session=SESSION),
            _bar("09:31", 9.0, 9.5, 8.5, 9.0, session=SESSION2),  # 直接跳到 zone 下方
        ]
        res = _run(ts, bars)
        assert res.ob_episode_transitions == ()

    def test_absent_ob_target_becomes_terminal_and_does_not_refire(self):
        ts_with = self._ts()
        res = _run(ts_with, [_bar("09:31", 12.0, 12.2, 10.5, 10.8)])
        assert len(res.ob_episode_transitions) == 1
        assert len(res.state.ob_states) == 1
        assert res.state.ob_states[0].status == "INSIDE_EPISODE"
        live_key = res.state.ob_states[0].logical_ob_key

        # 下一版 TargetSet 中该 OB 消失 → TERMINAL，state 必须保留
        ts_without = _target_set(_structure(), obs=[])
        res2 = evaluate_smc_realtime_transitions(
            instrument_id=INSTR, target_set=ts_without,
            completed_bars=[_bar("09:32", 10.8, 10.9, 10.7, 10.8)],
            context=_ctx(), state=res.state,
        )
        assert len(res2.state.ob_states) == 1
        assert res2.state.ob_states[0].status == "TERMINAL"
        assert res2.state.ob_states[0].logical_ob_key == live_key
        assert res2.ob_episode_transitions == ()

        # 同一 epoch 内 rebuild 后同一 logical OB 重新出现 → 仍 TERMINAL，0 新 episode
        reappeared = _target_set(_structure(), obs=[_ob(bar_low=10.0, bar_high=11.0)])
        assert (
            logical_ob_key(INSTR, False, 1, "2026-01-02T00:00:00", "2026-01-03T00:00:00") == live_key
        )
        res3 = evaluate_smc_realtime_transitions(
            instrument_id=INSTR, target_set=reappeared,
            completed_bars=[_bar("09:33", 12.0, 12.1, 10.4, 10.6)],
            context=_ctx(), state=res2.state,
        )
        assert res3.ob_episode_transitions == ()
        assert res3.state.ob_states[0].status == "TERMINAL"

    def test_new_epoch_reinitializes_ob_baseline(self):
        ts = self._ts()
        res = _run(ts, [_bar("09:31", 12.0, 12.2, 10.5, 10.8)])
        closed = evaluate_smc_realtime_transitions(
            instrument_id=INSTR, target_set=_target_set(_structure(), obs=[]),
            completed_bars=[], context=_ctx(), state=res.state,
        )
        assert closed.state.ob_states[0].status == "TERMINAL"
        # 新 authoritative daily-state epoch → 由新 C3 TargetSet 重建 baseline
        fresh = initial_transition_state(ts, instrument_id=INSTR, daily_epoch="2026-09-15")
        assert fresh.ob_states[0].status == "OUTSIDE"

    def test_ob_zone_must_be_crossed_by_minute_high_low(self):
        ts = self._ts()
        # close 在 zone 内但 minute high/low 完全高于 zone → 不是 touch
        res = _run(ts, [_bar("09:31", 12.0, 12.5, 11.5, 12.0)])
        assert res.ob_episode_transitions == ()
        # 用 close-only 的旧实现会误判；此处 minute_low(11.5) > bar_high(11.0)


# ---------------------------------------------------------------------------
# A0.2 输入 proof / 格式 fail closed
# ---------------------------------------------------------------------------


class TestInputProofGates:
    def test_sequence_not_proven_zero_events_and_state_unchanged(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)))
        state0 = initial_transition_state(ts, instrument_id=INSTR, daily_epoch=SESSION)
        res = _run(
            ts,
            [_bar("09:31", 104.0, 104.5, 103.5, 104.0), _bar("09:32", 104.5, 106.0, 104.0, 105.5)],
            state=state0,
            ctx=_ctx(sequence_proven=False, sequence_reason="missing 09:31"),
        )
        assert res.ok is False
        assert res.structure_transitions == ()
        assert res.ob_episode_transitions == ()
        assert state_fingerprint(res.state) == state_fingerprint(state0)
        assert "sequence" in res.fail_closed_reason

    def test_nan_ohlc_zero_events_and_state_unchanged(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)))
        state0 = initial_transition_state(ts, instrument_id=INSTR, daily_epoch=SESSION)
        res = _run(ts, [_bar("09:31", float("nan"), 104.5, 103.5, 104.0)], state=state0)
        assert res.ok is False
        assert res.structure_transitions == ()
        assert state_fingerprint(res.state) == state_fingerprint(state0)
        assert "malformed" in res.fail_closed_reason

    def test_empty_session_key_fail_closed(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)))
        state0 = initial_transition_state(ts, instrument_id=INSTR, daily_epoch=SESSION)
        res = _run(ts, [_bar("09:31", 104.0, 104.5, 103.5, 104.0, session="")], state=state0)
        assert res.ok is False
        assert state_fingerprint(res.state) == state_fingerprint(state0)


# ---------------------------------------------------------------------------
# A7.4 前缀：state 持久化（JSON-safe，禁止 repr）
# ---------------------------------------------------------------------------


class TestPersistenceRoundTrip:
    def test_serialize_deserialize_round_trip_continues_identically(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)), obs=[_ob()])
        res = _run(
            ts,
            [_bar("09:31", 104.0, 104.5, 103.5, 104.0), _bar("09:32", 104.5, 106.0, 104.0, 105.5)],
        )
        payload = serialize_transition_state(res.state)
        json.dumps(payload)  # 必须 JSON-safe（非 repr）
        restored = deserialize_transition_state(payload)
        assert state_fingerprint(restored) == state_fingerprint(res.state)

        nxt = [_bar("09:33", 105.5, 106.0, 105.0, 105.8)]
        c1 = _run(ts, nxt, state=res.state)
        c2 = _run(ts, nxt, state=restored)
        assert state_fingerprint(c1.state) == state_fingerprint(c2.state)

    def test_deserialize_rejects_unknown_ob_status(self):
        ts = _target_set(_structure(swing_high=_sh(105.0)), obs=[_ob()])
        res = _run(ts, [_bar("09:31", 104.0, 104.5, 103.5, 104.0)])
        payload = serialize_transition_state(res.state)
        payload["ob_states"][0]["status"] = "BOGUS"
        with pytest.raises(ValueError):
            deserialize_transition_state(payload)
