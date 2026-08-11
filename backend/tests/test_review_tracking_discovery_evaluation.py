"""[REVIEW-V2-BATCH3-R2] Discovery Tracking evaluation 单元测试。

验证 FIX A/B/C：
- Discovery target（source_signal_id=None）仍进入正式 Discovery evaluation path
- 当前 run 有同 scope Discovery → evaluation payload 携带 current discovery/evidence
- 当前 run 无同 scope Discovery → 形成 deterministic absence context（非异常/查询失败）
- Signal / Scope tracking 原行为不变
- migration 089 contract 不变（discovery_id 列 + tracking_type check）

只 mock 正式 service boundary（build_discoveries_for_run / _get_previous_evaluation /
_upsert_evaluation / _find_signal_in_run），不 mock 不存在的 contract。

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_review_tracking_discovery_evaluation.py -v -p no:cacheprovider
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import review_tracking_service as svc


def _make_tracking(
    *,
    tracking_type: str = "discovery",
    discovery_id: str | None = "orig-disc-111",
    scope_type: str | None = "industry_l1",
    scope_key: str | None = "l1-tech",
    source_signal_id=None,
    status: str = "active",
):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.tracking_type = tracking_type
    t.discovery_id = discovery_id
    t.scope_type = scope_type
    t.scope_key = scope_key
    t.source_signal_id = source_signal_id
    t.status = status
    t.confirmation_conditions = None
    t.invalidation_conditions = None
    t.created_at = MagicMock()
    t.created_at.date.return_value = date(2026, 8, 1)
    return t


def _make_run(trade_date: str = "2026-08-11"):
    r = MagicMock()
    r.id = uuid.uuid4()
    r.trade_date = date.fromisoformat(trade_date)
    return r


def _make_discovery(
    *,
    scope_type: str = "industry_l1",
    scope_key: str = "l1-tech",
    status: str = "confirmed",
    discovery_id: str = "cur-disc-999",
    supporting_signal_ids=None,
):
    d = MagicMock()
    d.scope_type = scope_type
    d.scope_key = scope_key
    d.status = status
    d.discovery_id = discovery_id
    d.supporting_signal_ids = supporting_signal_ids or ["sig-1", "sig-2"]
    return d


def _make_eval_record():
    e = MagicMock()
    e.id = uuid.uuid4()
    return e


async def _run_evaluation(
    tracking,
    run,
    *,
    discoveries,
    previous_eval=None,
    find_signal=None,
):
    """执行 evaluate_tracking_for_run，mock 全部 DB/service boundary。"""
    with patch.object(svc, "_get_previous_evaluation", AsyncMock(return_value=previous_eval)), patch.object(
        svc, "build_discoveries_for_run", AsyncMock(return_value=discoveries)
    ) as mock_build, patch.object(
        svc, "_find_signal_in_run", AsyncMock(return_value=find_signal)
    ) as mock_find_signal, patch.object(
        svc, "_find_same_scope_signal_in_run", AsyncMock(return_value=None)
    ), patch.object(
        svc, "_upsert_evaluation", AsyncMock(return_value=_make_eval_record())
    ) as mock_upsert:
        session = MagicMock()
        record = await svc.evaluate_tracking_for_run(session, tracking, run)
    return record, mock_upsert, mock_find_signal, mock_build


def _upserted_payload(mock_upsert) -> dict:
    _, kwargs = mock_upsert.call_args
    return kwargs["evaluation_payload"]


# ============================================================
# 1. Discovery tracking（source_signal_id=None）进入 Discovery path
# ============================================================

async def test_discovery_tracking_enters_evaluation_path_despite_null_signal():
    tracking = _make_tracking(tracking_type="discovery", source_signal_id=None)
    run = _make_run()
    _, mock_upsert, _, mock_build = await _run_evaluation(
        tracking, run,
        discoveries=[_make_discovery()],
    )
    # 即使 source_signal_id=None，也必须走 Discovery evaluation（build_discoveries_for_run 被调用）
    assert mock_build.await_count == 1
    payload = _upserted_payload(mock_upsert)
    assert payload["discovery"]["target_type"] == "discovery"
    assert payload["discovery"]["source_discovery_id"] == "orig-disc-111"


# ============================================================
# 2. 当前 run 有同 scope Discovery → payload 含 current discovery/evidence
# ============================================================

async def test_present_discovery_payload_has_current_discovery_and_evidence():
    tracking = _make_tracking(
        tracking_type="discovery",
        discovery_id="orig-disc-111",
        scope_type="industry_l1",
        scope_key="l1-tech",
    )
    run = _make_run()
    discovery = _make_discovery(
        scope_type="industry_l1", scope_key="l1-tech",
        status="confirmed", discovery_id="cur-disc-999",
        supporting_signal_ids=["sig-1", "sig-2"],
    )
    record, mock_upsert, _, _ = await _run_evaluation(
        tracking, run, discoveries=[discovery],
    )
    payload = _upserted_payload(mock_upsert)
    disc = payload["discovery"]
    assert disc["current_discovery_present"] is True
    assert disc["current_discovery_id"] == "cur-disc-999"
    assert disc["current_discovery_status"] == "confirmed"
    assert disc["supporting_signal_ids"] == ["sig-1", "sig-2"]
    # supporting evidence 通过 pointer 记录（未复制完整 Discovery payload）
    assert "state" not in disc and "change" not in disc
    # Discovery 生命周期状态映射到现有 state machine：confirmed → tracking confirmed
    assert payload["current_signal_status"] == "confirmed"
    assert payload["discovery"]["target_type"] == "discovery"
    assert tracking.status == "confirmed"


# ============================================================
# 3. 当前 run 无同 scope Discovery → deterministic absence context
# ============================================================

async def test_absent_discovery_is_deterministic_absence_not_error():
    tracking = _make_tracking(
        tracking_type="discovery",
        scope_type="industry_l1", scope_key="l1-tech",
    )
    run = _make_run()
    # 无任何 Discovery，或只有其它 scope 的 Discovery
    record, mock_upsert, _, _ = await _run_evaluation(
        tracking, run, discoveries=[],
    )
    payload = _upserted_payload(mock_upsert)
    disc = payload["discovery"]
    assert disc["current_discovery_present"] is False
    assert disc["current_discovery_id"] is None
    assert disc["absence_reason"] == "no_current_discovery"
    assert payload["current_signal_status"] is None
    # 不抛异常、不是查询失败；由 state machine 保持原状态
    assert tracking.status == "active"


async def test_absent_discovery_scope_mismatch_detected():
    tracking = _make_tracking(
        tracking_type="discovery",
        scope_type="industry_l1", scope_key="l1-tech",
    )
    run = _make_run()
    # 当前 run 只有其它 scope 的 Discovery → 对追踪的 scope 而言仍是 absence
    other = _make_discovery(scope_type="style", scope_key="growth")
    _, mock_upsert, _, _ = await _run_evaluation(
        tracking, run, discoveries=[other],
    )
    disc = _upserted_payload(mock_upsert)["discovery"]
    assert disc["current_discovery_present"] is False
    assert disc["absence_reason"] == "no_current_discovery"


# ============================================================
# 4. Signal tracking 原行为不变
# ============================================================

async def test_signal_tracking_unchanged():
    sig_id = uuid.uuid4()
    tracking = _make_tracking(
        tracking_type="signal",
        source_signal_id=sig_id,
        discovery_id=None, scope_type=None, scope_key=None,
    )
    run = _make_run()
    signal = MagicMock()
    signal.status = "confirmed"
    record, mock_upsert, mock_find_signal, _ = await _run_evaluation(
        tracking, run, discoveries=[], find_signal=signal,
    )
    # Signal path：进入 _find_signal_in_run，不进入 Discovery read model
    assert mock_find_signal.await_count == 1
    payload = _upserted_payload(mock_upsert)
    assert payload["current_signal_status"] == "confirmed"
    assert "discovery" not in payload, "signal tracking payload 不得带 discovery context"
    assert tracking.status == "confirmed"


# ============================================================
# 5. Scope tracking 原行为不变
# ============================================================

async def test_scope_tracking_unchanged():
    tracking = _make_tracking(
        tracking_type="scope",
        source_signal_id=None,
        discovery_id=None,
        scope_type="style", scope_key="growth",
    )
    run = _make_run()
    record, mock_upsert, mock_find_signal, mock_build = await _run_evaluation(
        tracking, run, discoveries=[_make_discovery()],
    )
    payload = _upserted_payload(mock_upsert)
    # Scope path：不进入 Discovery evaluation，也不进入 signal path
    assert mock_build.await_count == 0, "scope tracking 不得进入 Discovery read model"
    assert "discovery" not in payload, "scope tracking payload 不得带 discovery context"
    assert payload["current_signal_status"] is None
    assert tracking.status == "active"  # 无关联信号，state machine 保持原状态


# ============================================================
# 6. migration 089 contract 不变
# ============================================================

def test_migration_089_contract_unchanged():
    mig = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "089_review_discovery_tracking.py"
    text = mig.read_text()
    # discovery_id 列
    assert '"discovery_id"' in text or 'sa.Column("discovery_id"' in text
    # tracking_type check 含 discovery
    assert "tracking_type IN ('signal','scope','instrument','discovery')" in text
