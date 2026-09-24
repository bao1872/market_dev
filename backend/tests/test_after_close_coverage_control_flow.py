"""[BOARD-LOCAL-OWNERSHIP-CORRECTION-01 / RC2] 覆盖率步骤控制流合同（AST 结构级，纯单元）。

背景（真实 bug）：`checking_coverage` 曾嵌在 `if not skip_refresh:` 内部，
于是 `mainchain_stage="computing_features"`（daily_ready / legacy syncing_boards 起点）
会**同时**跳过 refreshing_daily 和 checking_coverage，直接进入
rebuilding_market_dashboard —— 与冻结合同「仅 refreshing_daily 预完成，current pipeline 继续」
矛盾。

冻结语义：
- normal run       : refresh → coverage → dashboard → features
- daily_ready      : NO refresh / YES coverage / YES dashboard / YES features
- legacy syncing   : NO refresh / NO board sync / YES coverage / YES dashboard / YES features
- coverage 失败     : 仍然阻塞强制主链（DAILY_COVERAGE_BLOCKED）

本测试用 AST 直接约束 production 源码结构，使该回归不可能再次悄悄发生：
1. `execute_orchestrator_step("checking_coverage", ...)` **不得**位于
   `if not skip_refresh:` 的子树内；
2. 它必须位于该 if/else **之后**（行号更大），与 rebuilding_market_dashboard 同级；
3. resume/restart 路径必须存在 `if refresh_daily_coverage is None:` 守卫，
   并从持久化事实重算（调用 production owner `compute_daily_coverage`）；
4. 覆盖率门禁阈值 0.9 与失败语义 `DAILY_COVERAGE_BLOCKED` 保持不变。
"""

from __future__ import annotations

import ast
import inspect

from app.services import after_close_orchestrator as orch


def _func_ast() -> ast.AsyncFunctionDef:
    src = inspect.getsource(orch.execute_after_close_run)
    module = ast.parse(src)
    for node in ast.walk(module):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "execute_after_close_run"
        ):
            return node  # type: ignore[return-value]
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
    """收集 execute_orchestrator_step("<step_name>", ...) 调用节点。"""
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


def _descendants(node: ast.AST) -> list[ast.AST]:
    return [c for c in ast.walk(node) if c is not node]


def test_coverage_step_is_not_nested_inside_skip_refresh_branch() -> None:
    """[RC2 核心] checking_coverage 绝不能被 `if not skip_refresh:` 掩盖。"""
    func = _func_ast()
    skip_if = _skip_refresh_if(func)
    inside = {id(n) for n in _descendants(skip_if)}

    coverage_calls = _step_calls(func, "checking_coverage")
    assert coverage_calls, "必须存在 execute_orchestrator_step('checking_coverage', ...) 调用"
    assert len(coverage_calls) == 1, "覆盖率步骤必须只有一处（禁止重复实现）"

    coverage_call = coverage_calls[0]
    assert id(coverage_call) not in inside, (
        "checking_coverage 仍嵌在 `if not skip_refresh:` 内："
        "daily_ready / legacy syncing_boards 起点会静默绕过覆盖率门禁"
    )


def test_coverage_step_runs_after_skip_refresh_branch_and_before_dashboard() -> None:
    """覆盖率步骤必须位于 if/else 之后、rebuilding_market_dashboard 之前。"""
    func = _func_ast()
    skip_if = _skip_refresh_if(func)
    coverage_call = _step_calls(func, "checking_coverage")[0]
    dashboard_calls = _step_calls(func, "rebuilding_market_dashboard")
    assert dashboard_calls, "必须存在 rebuilding_market_dashboard 步骤调用"
    dashboard_call = dashboard_calls[0]

    assert coverage_call.lineno > skip_if.end_lineno, (
        "coverage 步骤必须位于 normal/resume 汇合点之后"
    )
    assert coverage_call.lineno < dashboard_call.lineno, (
        "coverage 必须在 rebuilding_market_dashboard 之前（否则低质量投影可能先发布）"
    )


def test_resume_path_reevaluates_coverage_from_persisted_facts() -> None:
    """resume/restart（无本次刷新结果）必须从持久化事实重算覆盖率，且复用 owner。"""
    src = inspect.getsource(orch.execute_after_close_run)
    assert "if refresh_daily_coverage is None:" in src, (
        "缺少 resume/restart 覆盖率重算守卫：'已刷新日线' 不得自动等价于 'coverage 通过'"
    )
    assert "compute_daily_coverage(" in src, (
        "resume 路径必须复用 production 覆盖率 owner（compute_daily_coverage → "
        "BarsCoverageService），禁止复制覆盖率数学"
    )
    assert "= batch_result.daily_coverage" in src, (
        "normal 路径必须把本次刷新得到的覆盖率交给共享 coverage 步骤"
    )


def test_coverage_gate_and_failure_semantics_unchanged() -> None:
    """门禁阈值 0.9 与失败语义（DAILY_COVERAGE_BLOCKED + return）保持不变。"""
    src = inspect.getsource(orch.execute_after_close_run)
    assert ">= 0.9" in src, "覆盖率门禁阈值必须仍为 0.9"
    assert '"DAILY_COVERAGE_BLOCKED"' in src, "覆盖率失败必须仍标记 DAILY_COVERAGE_BLOCKED"
    assert "AfterCloseRunStatus.CHECKING_COVERAGE" in src, (
        "must still write orchestrator_status=checking_coverage"
    )


def test_checking_coverage_is_not_a_durable_checkpoint() -> None:
    """覆盖率步骤不得成为 durable checkpoint（不得推进 last_completed_step=checking_coverage）。"""
    assert "checking_coverage" not in orch._CHECKPOINT_ORDER
    assert "checking_coverage" not in orch._COMPLETED_STEPS
    src = inspect.getsource(orch.execute_after_close_run)
    assert 'AfterCloseRunStatus.CHECKING_COVERAGE.value' not in src, (
        "不得把 checking_coverage 写进 last_completed_step（伪造 checkpoint）"
    )


def test_dashboard_step_remains_optional_and_non_checkpoint() -> None:
    """rebuilding_market_dashboard 仍为 optional 且非 checkpoint。"""
    assert "rebuilding_market_dashboard" not in orch._CHECKPOINT_ORDER
    assert "rebuilding_market_dashboard" not in orch._COMPLETED_STEPS
    src = inspect.getsource(orch._execute_rebuilding_market_dashboard)
    assert src  # 存在真实实现
