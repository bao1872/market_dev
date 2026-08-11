"""REVIEW-SIGNAL-RECOMPUTE-NOT-REPLACE-SET 修复单元测试（P1）。

验证 generate_signals_for_scope 的 replace-set 语义：

- 当前命中先 upsert（保留存活 ID）；
- 计算 stale = 现存 − 当前命中；
- stale 被 MarketReviewTracking 引用 → fail-closed 抛 SignalGenerationError；
- 否则仅删除 stale（FK 级联删除 attribution/instrument）。

不连接真实 PG（fake session + 受控 filter/payload 管线）。
覆盖 TEST1..TEST8：stale removal / zero hit / scope isolation / run isolation
/ tracking guard / cascade contract (ORM) / idempotency / 8-11 形态。
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList

from app.models.market_review import MarketReviewSignal, MarketReviewTracking
from app.services.review_signal_service import (
    SignalGenerationError,
    generate_signals_for_scope,
)

# ---------------------------------------------------------------------------
# 受控 filter / payload / status 管线
# ---------------------------------------------------------------------------

def _make_hit(family: str, signal_type: str) -> Any:
    class _Fam:
        value = family

    class _Hit:
        pass

    h = _Hit()
    h.family = _Fam()
    h.signal_type = signal_type
    h.confirmation_rule = {"min_consecutive_days": 1}
    h.invalidation_rule = {}
    return h


def _fake_build_payloads(filt, context, *, duration_days=0, scope_type="", scope_name=""):
    return {
        "trigger_payload": {"family": filt.family.value, "type": filt.signal_type},
        "baseline_payload": {},
        "evidence_payload": {},
        "confirmation_rule": filt.confirmation_rule,
        "invalidation_rule": filt.invalidation_rule,
        "rank_key": {"family": filt.family.value, "type": filt.signal_type},
    }


def _fake_status(**kwargs):
    return "new"


# ---------------------------------------------------------------------------
# SQL-aware fake session
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self._rows

    def scalar_one(self):
        return self._rows[0]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """内存版 MarketReviewSignal / MarketReviewTracking 表（SQL-aware 路由）。

    - pg_insert(MarketReviewSignal)：upsert（按 run+family+type+scope 身份键），
      冲突保留原 id。
    - select MarketReviewSignal（含 run/scope 过滤）：返回本 run/scope 现存信号。
    - select MarketReviewTracking.id（source_signal_id IN ...）：命中引用返回 1 行。
    - session.delete(obj)：从 store 移除。
    """

    def __init__(self):
        self.signals: list[MarketReviewSignal] = []
        self.trackings: list[MarketReviewTracking] = []
        self.deleted: list[Any] = []

    def _extract_where(self, stmt):
        """从 Select.whereclause 提取列名→值（支持 == 与 .in_()）。"""
        eq: dict[str, Any] = {}
        in_sets: dict[str, list[Any]] = {}

        def walk(node):
            if isinstance(node, BooleanClauseList):
                for c in node.clauses:
                    walk(c)
            elif isinstance(node, BinaryExpression):
                op = getattr(node.operator, "__name__", str(node.operator))
                left = node.left
                right = node.right
                col_name = getattr(getattr(left, "expression", left), "name", None)
                if col_name is None and hasattr(left, "name"):
                    col_name = left.name
                if col_name is None:
                    return
                if op in ("eq", "=", "__eq__"):
                    val = right.value if hasattr(right, "value") else right
                    eq[col_name] = val
                elif op in ("in_op", "in"):
                    vals = right.value if hasattr(right, "value") else right
                    if isinstance(vals, (list, tuple, set)):
                        in_sets[col_name] = list(vals)

        walk(getattr(stmt, "whereclause", None))
        return eq, in_sets

    async def execute(self, stmt):
        text = str(stmt)
        if text.strip().startswith("INSERT INTO market_review_signals"):
            raw = dict(getattr(stmt, "_values", {}) or {})
            values = {
                (k.name if hasattr(k, "name") else k): (v.value if hasattr(v, "value") else v)
                for k, v in raw.items()
            }
            rid = values["review_run_id"]
            fam = values["filter_family"]
            stype = values["signal_type"]
            scope_type = values["scope_type"]
            scope_key = values["scope_key"]
            existing = next(
                (s for s in self.signals
                 if s.review_run_id == rid and s.filter_family == fam
                 and s.signal_type == stype and s.scope_type == scope_type
                 and s.scope_key == scope_key),
                None,
            )
            if existing is not None:
                for k in ("status", "trigger_payload", "baseline_payload",
                          "evidence_payload", "rank_key", "coverage_ratio"):
                    if k in values:
                        setattr(existing, k, values[k])
                return _Result([])
            obj = MarketReviewSignal(
                id=uuid.uuid4(),
                review_run_id=rid,
                trade_date=values["trade_date"],
                filter_family=fam,
                signal_type=stype,
                scope_type=scope_type,
                scope_key=scope_key,
                scope_name=values["scope_name"],
                status=values["status"],
                first_seen_date=values["first_seen_date"],
                previous_signal_id=values["previous_signal_id"],
                transformed_to_signal_id=None,
                trigger_payload=values["trigger_payload"],
                baseline_payload=values["baseline_payload"],
                evidence_payload=values["evidence_payload"],
                confirmation_rule=values["confirmation_rule"],
                invalidation_rule=values["invalidation_rule"],
                coverage_ratio=values["coverage_ratio"],
                rank_key=values["rank_key"],
            )
            self.signals.append(obj)
            return _Result([])

        if "FROM market_review_signals" in text:
            eq, _ = self._extract_where(stmt)
            rid = eq.get("review_run_id")
            scope_type = eq.get("scope_type")
            scope_key = eq.get("scope_key")
            rows = [
                s for s in self.signals
                if (rid is None or s.review_run_id == rid)
                and (scope_type is None or s.scope_type == scope_type)
                and (scope_key is None or s.scope_key == scope_key)
            ]
            if ".limit(1)" in text:
                return _Result(rows[:1])
            return _Result(rows)

        if "FROM market_review_trackings" in text:
            _, in_sets = self._extract_where(stmt)
            referenced = in_sets.get("source_signal_id", [])
            for t in self.trackings:
                if t.source_signal_id is not None and t.source_signal_id in referenced:
                    return _Result([t.id])
            return _Result([])

        return _Result([])

    async def delete(self, obj):
        if obj in self.signals:
            self.signals.remove(obj)
        self.deleted.append(obj)

    async def flush(self):
        return None

    async def get(self, cls, ident):
        if cls is MarketReviewSignal:
            return next((s for s in self.signals if s.id == ident), None)
        return None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class _Run:
    def __init__(self, run_id=None):
        self.id = run_id or uuid.uuid4()
        self.trade_date = date(2026, 8, 11)
        self.algorithm_version = "review-2.0.0"
        self.baseline_window = 120
        self.source_core_run_id = uuid.uuid4()


class _Snapshot:
    def __init__(self, scope_type, scope_key, scope_name=None, coverage_ratio=1.0,
                 ready_count=10):
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.scope_name = scope_name or scope_key
        self.coverage_ratio = coverage_ratio
        self.ready_count = ready_count
        self.p_payload = {}
        self.q_payload = {}
        self.u_payload = {}
        self.c_payload = {}
        self.v_payload = {}
        self.data_quality_json = {}


def _seed_signal(session, run, snap, family, signal_type, sig_id=None):
    sig = MarketReviewSignal(
        id=sig_id or uuid.uuid4(),
        review_run_id=run.id,
        trade_date=run.trade_date,
        filter_family=family,
        signal_type=signal_type,
        scope_type=snap.scope_type,
        scope_key=snap.scope_key,
        scope_name=snap.scope_name,
        status="new",
        first_seen_date=run.trade_date,
        previous_signal_id=None,
        transformed_to_signal_id=None,
        trigger_payload={},
        baseline_payload={},
        evidence_payload={},
        confirmation_rule={},
        invalidation_rule={},
        coverage_ratio=Decimal("1.0"),
        rank_key={"family": family, "type": signal_type},
    )
    session.signals.append(sig)
    return sig


async def _run_scope(session, run, snap, hits):
    import app.services.review_signal_service as rs

    def _eval(context, *, filters=None):
        return [_make_hit(f, t) for (f, t) in hits]

    with patch.object(rs, "evaluate_filters", _eval), \
         patch.object(rs, "build_signal_payloads", _fake_build_payloads), \
         patch.object(rs, "determine_signal_status", _fake_status), \
         patch.object(rs, "evaluate_confirmation", lambda *a, **k: False), \
         patch.object(rs, "evaluate_invalidation", lambda *a, **k: False), \
         patch.object(rs, "find_previous_signals", AsyncMock(return_value=[])):
        return await generate_signals_for_scope(session, run, snap, history_extras={})


# ---------------------------------------------------------------------------
# TEST 1 — stale removal
# ---------------------------------------------------------------------------

class TestReplaceSet:
    async def test_stale_removed_surviving_ids_preserved(self):
        run = _Run()
        snap = _Snapshot("market", "ALL_A_SHARE")
        session = FakeSession()
        id_a = uuid.uuid4()
        id_b = uuid.uuid4()
        id_c = uuid.uuid4()
        _seed_signal(session, run, snap, "A", "sig_a", id_a)
        _seed_signal(session, run, snap, "A", "sig_b", id_b)
        _seed_signal(session, run, snap, "A", "sig_c", id_c)

        await _run_scope(session, run, snap, [("A", "sig_a"), ("A", "sig_c"), ("A", "sig_d")])

        types = {s.signal_type for s in session.signals}
        assert types == {"sig_a", "sig_c", "sig_d"}, types
        by_type = {s.signal_type: s for s in session.signals}
        assert by_type["sig_a"].id == id_a
        assert by_type["sig_c"].id == id_c
        assert by_type["sig_d"].id != id_b
        assert all(s.review_run_id == run.id for s in session.signals)

    async def test_zero_hit_reconciles_to_empty(self):
        run = _Run()
        snap = _Snapshot("market", "ALL_A_SHARE")
        session = FakeSession()
        _seed_signal(session, run, snap, "A", "sig_a")
        _seed_signal(session, run, snap, "A", "sig_b")

        await _run_scope(session, run, snap, [])

        remaining = [s for s in session.signals
                     if s.review_run_id == run.id and s.scope_type == "market"]
        assert remaining == []

    async def test_scope_isolation(self):
        run = _Run()
        snap_x = _Snapshot("market", "X")
        snap_y = _Snapshot("market", "Y")
        session = FakeSession()
        _seed_signal(session, run, snap_x, "A", "sig_a")
        _seed_signal(session, run, snap_x, "A", "sig_b")
        _seed_signal(session, run, snap_y, "A", "sig_c")

        await _run_scope(session, run, snap_x, [("A", "sig_a")])

        types_x = {s.signal_type for s in session.signals
                   if s.scope_type == "market" and s.scope_key == "X"}
        types_y = {s.signal_type for s in session.signals
                   if s.scope_type == "market" and s.scope_key == "Y"}
        assert types_x == {"sig_a"}
        assert types_y == {"sig_c"}

    async def test_run_isolation(self):
        run1 = _Run()
        run2 = _Run()
        snap = _Snapshot("market", "ALL_A_SHARE")
        session = FakeSession()
        _seed_signal(session, run1, snap, "A", "sig_a")
        _seed_signal(session, run1, snap, "A", "sig_b")
        _seed_signal(session, run2, snap, "A", "sig_a")

        await _run_scope(session, run1, snap, [("A", "sig_a")])

        types_r1 = {s.signal_type for s in session.signals
                    if s.review_run_id == run1.id}
        types_r2 = {s.signal_type for s in session.signals
                    if s.review_run_id == run2.id}
        assert types_r1 == {"sig_a"}
        assert types_r2 == {"sig_a"}

    async def test_tracking_guard_raises_no_silent_null(self):
        run = _Run()
        snap = _Snapshot("market", "ALL_A_SHARE")
        session = FakeSession()
        id_a = uuid.uuid4()
        id_b = uuid.uuid4()
        _seed_signal(session, run, snap, "A", "sig_a", id_a)
        _seed_signal(session, run, snap, "A", "sig_b", id_b)
        t = MarketReviewTracking(
            id=uuid.uuid4(), user_id=uuid.uuid4(),
            source_signal_id=id_b, tracking_type="signal", status="active",
        )
        session.trackings.append(t)

        with pytest.raises(SignalGenerationError) as exc:
            await _run_scope(session, run, snap, [("A", "sig_a")])

        assert "STALE_SIGNAL_REFERENCED_BY_TRACKING" in str(exc.value)
        types = {s.signal_type for s in session.signals
                 if s.review_run_id == run.id}
        assert "sig_b" in types
        assert t.source_signal_id == id_b

    async def test_cascade_contract_orm(self):
        run = _Run()
        snap = _Snapshot("market", "ALL_A_SHARE")
        session = FakeSession()
        _seed_signal(session, run, snap, "A", "sig_a")
        _seed_signal(session, run, snap, "A", "sig_b")
        before = len(session.signals)
        await _run_scope(session, run, snap, [("A", "sig_a")])
        after = len(session.signals)
        assert before == 2 and after == 1
        assert {s.signal_type for s in session.signals} == {"sig_a"}

    async def test_idempotency(self):
        run = _Run()
        snap = _Snapshot("market", "ALL_A_SHARE")
        session = FakeSession()
        await _run_scope(session, run, snap, [("A", "sig_a"), ("A", "sig_b")])
        ids_first = {s.id for s in session.signals}
        await _run_scope(session, run, snap, [("A", "sig_a"), ("A", "sig_b")])
        ids_second = {s.id for s in session.signals}
        assert ids_first == ids_second
        assert len(session.signals) == 2

    async def test_eight_eleven_shape(self):
        run = _Run()
        snap = _Snapshot("market", "ALL_A_SHARE")
        session = FakeSession()
        _seed_signal(session, run, snap, "A", "old_a")
        _seed_signal(session, run, snap, "A", "old_b")

        await _run_scope(session, run, snap, [("A", "new_c"), ("A", "new_d")])

        types = {s.signal_type for s in session.signals
                 if s.review_run_id == run.id}
        assert types == {"new_c", "new_d"}
        assert "old_a" not in types and "old_b" not in types
