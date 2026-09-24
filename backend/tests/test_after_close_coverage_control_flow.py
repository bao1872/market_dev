"""[BOARD-LOCAL-OWNERSHIP-CORRECTION-01/02 / RC2 + RC4] 覆盖率步骤控制流合同（AST 结构级）。

背景（真实 bug 1，RC2）：`checking_coverage` 曾嵌在 `if not skip_refresh:` 内部，
于是 `mainchain_stage="computing_features"`（daily_ready / legacy syncing_boards 起点）
会**同时**跳过 refreshing_daily 与 checking_coverage。

背景（真实 bug 2，RC4）：覆盖率成功后旧代码仍写
`_update_heartbeat_and_step(..., AfterCloseRunStatus.REFRESHING_DAILY.value, ...)`，
在 resume 时把已有的 computing_features checkpoint **倒退**为 refreshing_daily。

冻结语义：
- 覆盖率步骤位于 refreshing_daily 与 rebuilding_market_dashboard 之间，
  **不在** `if not skip_refresh:` 子树内（normal / resume / restart 共享）；
- 覆盖率非 durable、绝不动 last_completed_step；
- dashboard 仍 optional / 非 checkpoint；
- `refreshing_daily` checkpoint 仅由刷新步骤自身写一次。

注：行为级断言在 `tests/test_after_close_coverage_behavior.py`（直接驱动 production 函数）。
"""

from __future__ import annotations

import ast
import inspect

from app.services import after_close_orchestrator as orch

_SHARED_FN = "_run_shared_checking_coverage_step"


def _module_ast() -> ast.Module:
    return ast.parse(inspect.getsource(orch))


def _top_level_functions() -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in _module_ast().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _func_ast() -> ast.AST:
    src = inspect.getsource(orch.execute_after_close_run)
    module = ast.parse(src)
    for node in ast.walk(module):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "execute_after_close_run"
        ):
            return node
    raise AssertionError("未找到 execute_after_close_run 的 AST 定义")  # pragma: no cover


def _skip_refresh_if(func: ast.AST) -> ast.If:
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
                and test.operand.id == "skip_refresh"
            ):
                return node
    raise AssertionError("未找到 `if not skip_refresh:` 分支")  # pragma: no cover


def _step_calls(node: ast.AST, step_name: str) -> list[ast.Call]:
    found: list[ast.Call] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not (isinstance(func, ast.Name) and func.id == "execute_orchestrator_step"):
            continue
        if child.args and isinstance(child.args[0], ast.Constant):
            if child.args[0].value == step_name:
                found.append(child)
    return found


def _calls_to(node: ast.AST, fn_name: str) -> list[ast.Call]:
    found: list[ast.Call] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            if child.func.id == fn_name:
                found.append(child)
    return found


def _descendants(node: ast.AST) -> list[ast.AST]:
    return [c for c in ast.walk(node) if c is not node]


def _attr_chain(node: ast.AST) -> str | None:
    """把 `A.B.C` 形式的属性链渲染为字符串；非纯属性链返回 None。"""
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _is_refreshing_daily_checkpoint_write(node: ast.Call) -> bool:
    """是否为 `_update_heartbeat_and_step(..., AfterCloseRunStatus.REFRESHING_DAILY.value, ...)`。"""
    if not (isinstance(node.func, ast.Name) and node.func.id == "_update_heartbeat_and_step"):
        return False
    return any(
        _attr_chain(a) == "AfterCloseRunStatus.REFRESHING_DAILY.value"
        for a in node.args
    )


# =============================================================================
# 1. 覆盖率步骤必须是模块级共享步骤（不可被 skip_refresh 掩盖）
# =============================================================================


def test_coverage_step_is_a_module_level_shared_function() -> None:
    """[RC2] 覆盖率步骤必须抽为**模块级**函数，而不是嵌在 skip_refresh 分支内。"""
    tops = _top_level_functions()
    assert _SHARED_FN in tops, (
        "覆盖率步骤必须抽为模块级共享函数（否则无法保证 normal/resume 都执行）"
    )
    # 覆盖率执行器调用位于共享函数内部，而非 execute_after_close_run 内
    shared_calls = _step_calls(tops[_SHARED_FN], "checking_coverage")
    assert len(shared_calls) == 1, "共享覆盖率步骤必须恰好调用一次执行器"

    caller = _func_ast()
    assert not _step_calls(caller, "checking_coverage"), (
        "execute_after_close_run 不应再内联 checking_coverage 执行器调用"
    )


def test_shared_coverage_call_site_is_not_inside_skip_refresh_branch() -> None:
    """[RC2 核心] 对共享覆盖步骤的调用绝不能被 `if not skip_refresh:` 掩盖。"""
    caller = _func_ast()
    skip_if = _skip_refresh_if(caller)
    inside = {id(n) for n in _descendants(skip_if)}

    calls = _calls_to(caller, _SHARED_FN)
    assert len(calls) == 1, f"应恰好调用一次共享覆盖率步骤；实际 {len(calls)}"
    assert id(calls[0]) not in inside, (
        "共享覆盖率步骤调用仍嵌在 `if not skip_refresh:` 内："
        "daily_ready / legacy syncing_boards 起点会静默绕过覆盖率门禁"
    )


def test_coverage_runs_after_skip_branch_and_before_dashboard() -> None:
    """覆盖率调用必须位于 if/else 之后、rebuilding_market_dashboard 之前。"""
    caller = _func_ast()
    skip_if = _skip_refresh_if(caller)
    coverage_call = _calls_to(caller, _SHARED_FN)[0]
    dashboard_calls = _step_calls(caller, "rebuilding_market_dashboard")
    assert dashboard_calls, "必须存在 rebuilding_market_dashboard 步骤调用"
    dashboard_call = dashboard_calls[0]

    assert coverage_call.lineno > skip_if.end_lineno, (
        "coverage 步骤必须位于 normal/resume 汇合点之后"
    )
    assert coverage_call.lineno < dashboard_call.lineno, (
        "coverage 必须在 rebuilding_market_dashboard 之前（否则低质量投影可能先发布）"
    )


def test_caller_returns_when_coverage_does_not_pass() -> None:
    """覆盖率未通过时调用方必须 return（阻塞强制主链）。"""
    src = inspect.getsource(orch.execute_after_close_run)
    assert "coverage_passed = await " + _SHARED_FN + "(" in src
    assert "if not coverage_passed:" in src


# =============================================================================
# 2. RC4：覆盖率非 durable，绝不动 checkpoint
# =============================================================================


def test_shared_coverage_never_writes_a_checkpoint() -> None:
    """[RC4 核心] 共享覆盖率步骤不得写入任何 last_completed_step 值。"""
    src = inspect.getsource(orch._run_shared_checking_coverage_step)
    assert "REFRESHING_DAILY.value" not in src, (
        "不得回写 refreshing_daily（resume 会把 computing_features 倒退）"
    )
    assert "CHECKING_COVERAGE.value" not in src, (
        "checking_coverage 本身也不是 durable checkpoint"
    )
    assert "None, worker_id" in src, "heartbeat 刷新必须显式传 None"


def test_resume_path_reevaluates_coverage_from_persisted_facts() -> None:
    """resume/restart（无本次刷新结果）必须从持久化事实重算覆盖率，且复用 owner。"""
    src = inspect.getsource(orch._run_shared_checking_coverage_step)
    assert "if refresh_daily_coverage is None:" in src, (
        "缺少 resume/restart 覆盖率重算守卫：'已刷新日线' 不得自动等价于 'coverage 通过'"
    )
    assert "compute_daily_coverage(" in src, (
        "resume 路径必须复用 production 覆盖率 owner（compute_daily_coverage → "
        "BarsCoverageService），禁止复制覆盖率数学"
    )
    caller = inspect.getsource(orch.execute_after_close_run)
    assert "= batch_result.daily_coverage" in caller, (
        "normal 路径必须把本次刷新得到的覆盖率交给共享 coverage 步骤"
    )


def test_coverage_gate_and_failure_semantics_unchanged() -> None:
    """门禁阈值 0.9 与失败语义（DAILY_COVERAGE_BLOCKED + return）保持不变。"""
    src = inspect.getsource(orch._run_shared_checking_coverage_step)
    assert ">= 0.9" in src, "覆盖率门禁阈值必须仍为 0.9"
    assert '"DAILY_COVERAGE_BLOCKED"' in src, "覆盖率失败必须仍标记 DAILY_COVERAGE_BLOCKED"
    assert "AfterCloseRunStatus.CHECKING_COVERAGE" in src, (
        "must still write orchestrator_status=checking_coverage"
    )


def test_refreshing_daily_checkpoint_written_only_by_refresh_step() -> None:
    """[RC4 单调性] 编排函数内 refreshing_daily checkpoint 写入恰好一次（刷新成功后）。"""
    caller = _func_ast()
    writes = [
        node
        for node in ast.walk(caller)
        if isinstance(node, ast.Call) and _is_refreshing_daily_checkpoint_write(node)
    ]
    assert len(writes) == 1, (
        f"refreshing_daily checkpoint 必须恰好写一次（刷新步骤）；实际 {len(writes)} 次"
    )
    # 该唯一写入点必须在 skip_refresh 分支**之内**（即只有真的刷新过才写）
    skip_if = _skip_refresh_if(caller)
    inside = {id(n) for n in _descendants(skip_if)}
    assert id(writes[0]) in inside, (
        "refreshing_daily checkpoint 只应在真实刷新路径写入，不得在 resume 路径伪造"
    )


def test_checking_coverage_and_dashboard_are_not_durable_checkpoints() -> None:
    """覆盖率与 dashboard 都不是 durable checkpoint。"""
    for step in ("checking_coverage", "rebuilding_market_dashboard"):
        assert step not in orch._CHECKPOINT_ORDER, f"{step} 不得进入 _CHECKPOINT_ORDER"
        assert step not in orch._COMPLETED_STEPS, f"{step} 不得进入 _COMPLETED_STEPS"
