"""[CORRECTION-02] AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-02 合同测试。

本模块锁定 P0 回归修复后的生产路由/owner 逻辑，不依赖已退役的
board_analysis_service，全部为纯单元（mock / 源码守卫），无需真实 PG。

核心合同（来自独立审计 + 架构合同）：

- 正常路径不再 dispatch PUBLISHING 阶段、不再调用 publish_stock_core_atomically。
- state_events / chip 的 readiness owner 是 CoreRun 显式绑定
  （snapshot_run_id X），而非 stock_core publication / published_at。
- Core X succeeded（无 stock_core publication）→ Review 照常执行 →
  History(T) 推进 → state_events / chip 照常执行（non-blocking）。
- state_events / chip 失败 → Review 不受影响 → 父任务 partial_success（诚实）。
- auction anchor 已 RETIRED，永远跳过。
- DSA 兼容性投影为 ACTIVE_OPTIONAL，_dsa_projection_ok 据真实执行产物置位，
  不再无条件置 True（防 false-green）。

本模块覆盖审计要求的 Case A-G。
"""

import inspect
import re

import pytest

from app.services import after_close_orchestrator as orch


def _main_src() -> str:
    return inspect.getsource(orch.execute_after_close_run)


# ---------------------------------------------------------------------------
# Case A: Core succeeded, publication absent → Review called
# ---------------------------------------------------------------------------


def test_review_called_without_stock_core_publication():
    """Case A: 正常路径无 stock_core publication，Review 仍被调用。

    锁定：
    1) 主编排必须通过执行器提交 computing_review（已有 AC-02 守卫，
       此处补充确认其不受 _stock_core_published 影响）。
    2) computing_review 的提交不得被 _stock_core_published 守卫包住。
    """
    src = _main_src()
    # Review 步骤提交存在
    assert 'execute_orchestrator_step(\n            "computing_review"' in src
    # 提交 computing_review 的语句附近不得出现 _stock_core_published 守卫
    review_block = src[src.find('"computing_review"'): src.find('"computing_review"') + 400]
    assert "_stock_core_published" not in review_block, (
        "Review 步骤的提交不得被 _stock_core_published 守卫包住"
    )


# ---------------------------------------------------------------------------
# Case F: publish_stock_core_atomically must NOT be called in normal path
# ---------------------------------------------------------------------------


def test_publish_stock_core_not_called_in_normal_path():
    """Case F: 正常路径 publish_stock_core_atomically 调用次数 == 0。

    锁定：
    1) 正常分支（not skip_publish）不得出现 publish_stock_core_atomically 调用。
    2) 若强行 stub 该函数为 raise，正常路径不会触碰它（源码层确认）。
    """
    src = _main_src()
    # 整个函数体中不得再有任何对 publish_stock_core_atomically 的调用
    assert "publish_stock_core_atomically(" not in src, (
        "execute_after_close_run 不得再调用 publish_stock_core_atomically"
    )


# ---------------------------------------------------------------------------
# Case B: state_events gated on snapshot_run_id (Core X), not publication
# ---------------------------------------------------------------------------


def test_state_events_gate_on_core_readiness():
    """Case B: state_events 以 snapshot_run_id(Core X) 为 readiness owner。

    锁定：state_events 守卫为 `if snapshot_run_id is not None:`，
    且不得依赖 _stock_core_published / published_at。
    """
    src = _main_src()
    # 定位 state events 注释块后的守卫
    idx = src.find("state events（non-blocking post-core）")
    assert idx != -1, "未找到 state events 注释块"
    block = src[idx: idx + 400]
    m = re.search(r'\n\s*if (.*?):\n', block)
    assert m is not None, "未找到 state_events 守卫"
    cond = m.group(1).strip()
    # [CORRECTION-03 升级] 门控进一步升级为 canonical CORE_READY
    # （由 _validate_core_ready 校验真实 CoreRun 行后置位）
    assert cond == "core_ready", (
        f"state_events 守卫必须基于 canonical CORE_READY（core_ready），"
        f"实际为: {cond}"
    )
    assert "_stock_core_published" not in cond, (
        "state_events 不得再依赖 _stock_core_published"
    )
    assert "publication" in block.lower(), (
        "state_events 块应声明 publication 是否与其无关"
    )


# ---------------------------------------------------------------------------
# Case C: chip enqueue gated on snapshot_run_id (Core X), not publication
# ---------------------------------------------------------------------------


def test_chip_gate_on_core_readiness():
    """Case C: chip 以 snapshot_run_id(Core X) 为 readiness owner。

    锁定：chip 守卫为 `if snapshot_run_id is not None:`，
    且不得依赖 _stock_core_published / published_at。
    """
    src = _main_src()
    idx = src.find("chip 入队（non-blocking post-core）")
    assert idx != -1, "未找到 chip 注释块"
    block = src[idx: idx + 400]
    m = re.search(r'\n\s*if (.*?):\n', block)
    assert m is not None, "未找到 chip 守卫"
    cond = m.group(1).strip()
    # [CORRECTION-03 升级] 门控进一步升级为 canonical CORE_READY
    # （由 _validate_core_ready 校验真实 CoreRun 行后置位）
    assert cond == "core_ready", (
        f"chip 守卫必须基于 canonical CORE_READY（core_ready），实际为: {cond}"
    )
    assert "_stock_core_published" not in cond, (
        "chip 不得再依赖 _stock_core_published"
    )


@pytest.mark.asyncio
async def test_chip_enqueue_called_with_core_run_id():
    """Case C (行为): chip 入队以 snapshot_run_id 作为 core_run_id 幂等依据。

    直接调用 _enqueue_chip_job_step 并 stub create_after_close_chip_consensus_job，
    验证 core_run_id == snapshot_run_id。
    """
    from datetime import date
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    snap = object()  # 用对象 identity 验证透传
    captured = {}

    class _FakeJob:
        id = "job-xyz"

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeJob(), True

    with (
        patch.object(
            orch, "create_after_close_chip_consensus_job", new=fake_create,
        ),
        patch.object(orch, "AsyncSessionLocal"),
        patch.object(orch, "_persist_step_summary", new=AsyncMock()),
    ):
        status, _jid = await orch._enqueue_chip_job_step(
            job_run_id=object(),
            worker_id="w1",
            lease_epoch=1,
            trade_date=date(2026, 7, 31),
            snapshot_run_id=snap,
            expected_count=100,
        )

    assert status == "succeeded"
    # core_run_id 必须透传 snapshot_run_id（幂等依据）
    assert captured.get("core_run_id") is snap


# ---------------------------------------------------------------------------
# Case D / E: state_events / chip failure → Review unaffected, truthful
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chip_failure_does_not_revoke_review():
    """Case E: chip 入队抛异常 → 返回 failed，供 partial_success 判定。

    Review 在 chip 之前已完成（post-core 顺序），chip 失败不影响 Review 有效性。
    """
    from datetime import date
    from unittest.mock import AsyncMock, patch

    with (
        patch.object(
            orch, "create_after_close_chip_consensus_job",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch.object(orch, "AsyncSessionLocal"),
        patch.object(orch, "_persist_step_summary", new=AsyncMock()),
    ):
        status, _jid = await orch._enqueue_chip_job_step(
            job_run_id=object(),
            worker_id="w1",
            lease_epoch=1,
            trade_date=date(2026, 7, 31),
            snapshot_run_id=object(),
            expected_count=100,
        )

    assert status == "failed"


def test_optional_failure_drives_partial_success():
    """Case D/E: state_events/chip 失败进入 optional_failures → partial_success。

    锁定：final status 装配必须把 step_summary 中 failed 的可选步骤
    （enqueue_chip_job / state_events）纳入 optional_failures，
    进而 _optional_failed → partial_success。_stock_core_published=False
    不得被用于判定失败。
    """
    src = _main_src()

    # optional_failures 推导必须消费 step_summary 中的 failed 步骤
    assert "step_summary" in src
    assert "optional_failures" in src
    # 不得把 _stock_core_published 用于判定任务失败
    # （仅在 diagnostics payload 出现是允许的；这里锁定 final 判定块不含它）
    final_block = src[src.rfind("optional_failures"):]
    assert "_stock_core_published" not in final_block[: final_block.find("_stock_core_superseded")], (
        "final 失败判定不得把 _stock_core_published=False 误读为 Core 不可用"
    )


# ---------------------------------------------------------------------------
# Case G: History(T) producer ordering — Review before History
# ---------------------------------------------------------------------------


def test_review_invocation_before_history_producer():
    """Case G: Review 调用出现在 History(T) producer 之前。

    锁定正常 DAG 顺序：computing_review → computing_history(History T) →
    state_events → chip。
    """
    src = _main_src()

    def _exec_pos(step: str) -> int:
        m = re.search(r'execute_orchestrator_step\(\s*\n?\s*"' + step + '"', src)
        assert m is not None, f"未找到 {step} 执行器提交"
        return m.start()

    review_pos = _exec_pos("computing_review")
    history_pos = _exec_pos("computing_history")
    # auction_anchor 执行器提交存在但被 if False 守卫跳过（RETIRED）
    auction_pos = _exec_pos("auction_anchor")

    assert review_pos < history_pos, (
        "Review 步骤必须早于 History(T) producer 执行"
    )
    assert history_pos < auction_pos, (
        "History(T) 必须早于 auction_anchor 提交位置（auction 已 RETIRED 跳过）"
    )


# ---------------------------------------------------------------------------
# Auction anchor: RETIRED, never gated on publication
# ---------------------------------------------------------------------------


def test_auction_anchor_retired_not_gated_on_publication():
    """auction anchor 已 RETIRED（PRD75 §23），永远跳过，不得读 publication。

    锁定：auction anchor 守卫为 `if False:`（显式 RETIRED），
    不再因 _stock_core_published=False 而"悄悄永不执行"。
    """
    src = _main_src()
    idx = src.find("因此 auction_anchor 永远是显式 skipped")
    assert idx != -1, "未找到 auction anchor RETIRED 注释"
    block = src[idx: idx + 200]
    m = re.search(r'\n\s*if (.*?):\s*# RETIRED', block)
    assert m is not None, "未找到 auction anchor RETIRED 守卫（if False: # RETIRED）"
    cond = m.group(1).strip()
    assert cond == "False", (
        f"auction anchor 必须显式 RETIRED（if False:），实际为: {cond}"
    )


# ---------------------------------------------------------------------------
# DSA compatibility projection: ACTIVE_OPTIONAL, truthful _dsa_projection_ok
# ---------------------------------------------------------------------------


def test_dsa_projection_status_truthful_not_from_run_id():
    """DSA 兼容性状态：不得由 run id 推断成功，必须来自真实执行结果。

    [CORRECTION-03 升级] _dsa_projection_ok 已被整体移除；正常分支使用
    _dsa_compatibility_status="not_run" 初始化，真实值仅能来自
    _run_dsa_compatibility_projection 的实际执行返回。
    """
    src = _main_src()
    # 旧的 run-id 推断布尔必须不存在
    assert "_dsa_projection_ok" not in src, (
        "_dsa_projection_ok 已废止：run id 仅证明身份创建，不证明完成/就绪"
    )
    # 正常分支以 not_run 初始化（未执行前不得伪造成功）
    assert '_dsa_compatibility_status: str = "not_run"' in src, (
        "DSA 兼容性状态必须以 not_run 初始化，等待真实执行结果"
    )
    assert "_dsa_projection_ok = True" not in src
