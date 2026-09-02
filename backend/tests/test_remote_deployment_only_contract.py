"""远程部署唯一性合同测试。

本测试防止后续把本地开发、本地验证或本地控制端误演化为本地部署实现。
部署实现只能存在于远程服务器脚本 scripts/deploy/panji-deploy.sh。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _active_shell_lines(source: str) -> list[str]:
    """返回非空、非纯注释的 shell 行，用于区分说明文字和实际本地命令。"""

    return [
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_remote_deployment_rule_is_indexed_and_explicit() -> None:
    rules_index = _read("rules/README.md")
    rule = _read("rules/80-deployment-migration.md")

    assert "80-deployment-migration.md" in rules_index
    assert "盘迹实际运行部署只发生在远程运行服务器 `panji-prod`" in rule
    assert "Live Refresh" in rule
    assert "origin/dev exact SHA" in rule


def test_local_controller_contains_no_deployment_implementation() -> None:
    controller = _read("scripts/ops/panji-test-deploy")
    active_lines = _active_shell_lines(controller)

    assert 'SSH_WRAPPER="${SCRIPT_DIR}/panji-prod-ssh"' in controller
    assert '"${SSH_WRAPPER}"' in controller
    assert "panji-prod-preflight" in controller
    assert "scripts/deploy/panji-deploy.sh" in controller

    forbidden_local_commands = (
        "docker compose",
        "docker build",
        "npm ci",
        "npm run build",
        "alembic upgrade",
        "rsync ",
    )
    for command in forbidden_local_commands:
        assert not any(command in line for line in active_lines), (
            f"本地控制端出现部署实现命令: {command}"
        )


def test_server_script_owns_deployment_implementation() -> None:
    server_script = _read("scripts/deploy/panji-deploy.sh")

    assert "panji-prod" in server_script
    assert "COMPOSE_CMD=" in server_script
    assert "/opt/panji-live" in server_script
    assert "RUNTIME_SHA" in server_script
    assert "alembic" in server_script
    assert "前端构建" in server_script


def test_runtime_prd_already_identifies_remote_as_only_runtime_target() -> None:
    prd = _read("docs/prd/80-system-runtime.md")

    assert "本地开发" in prd
    assert "远程稳定运行" in prd
    assert "当前 `panji-prod` 腾讯云物理机是盘迹唯一的远程运行环境" in prd
    assert "本地不创建或启动盘迹 PostgreSQL、Redis 和应用容器" in prd


# =============================================================================
# DEPLOYMENT FAIL-CLOSED ACTIVE-JOB GATE（ref/guide.md REOPEN NARROW RUNTIME-SAFETY FIX）
# =============================================================================
# 目标：backend runtime 变更（会重启 worker-after-close）时，任何 live runtime mutation
# 之前必须检查活跃长任务；有活跃任务 → fail-closed 停止部署。
# 以下为静态 source-contract 测试，验证顺序 / 副作用，而非仅 grep helper 存在。

# worker-after-close 进程内执行的全部业务 job_name（FIX A 审计结果）。
_EXPECTED_ACTIVE_JOB_NAMES = (
    "after_close_orchestrator",
    "after_close_chip_consensus",
    "review_bootstrap",
    "auction_final",
    "auction_open_confirmation",
)


def _function_body(source: str, name: str) -> str:
    """提取 shell 函数体（含首尾），用于顺序/副作用静态校验。"""
    lines = source.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip().startswith(f"{name}()")),
        None,
    )
    assert start is not None, f"未找到函数 {name}"
    depth = 0
    for j in range(start, len(lines)):
        depth += lines[j].count("{") - lines[j].count("}")
        if j > start and depth <= 0:
            return "\n".join(lines[start:j + 1])
    raise AssertionError(f"无法定位函数 {name} 结束")


def _deploy_body() -> str:
    script = _read("scripts/deploy/panji-deploy.sh")
    return _function_body(script, "deploy")


def _guard_body() -> str:
    script = _read("scripts/deploy/panji-deploy.sh")
    return _function_body(script, "guard_active_after_close_jobs")


def _line(text: str, needle: str) -> int:
    """定位可执行行（跳过纯注释行），避免匹配到注释里的同名文字。"""
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if needle in line:
            return i
    raise AssertionError(f"体内未找到（非注释行）: {needle}")


def test_active_job_gate_is_called_before_any_backend_runtime_mutation() -> None:
    """CASE 1: 活跃任务存在时，部署在 backend Live Mount 同步之前停止。"""
    body = _deploy_body()
    gate = _line(body, "guard_active_after_close_jobs")
    sync = _line(body, "sync_backend_runtime")
    assert gate < sync, "活跃任务门禁必须早于 backend Live Mount 同步"
    # 门禁也是整个 deploy() 的第一处 FAILURE_STAGE，早于首个 mutation update_env_file。
    env = _line(body, "update_env_file")
    assert gate < env, "活跃任务门禁必须早于 update_env_file"


def test_active_job_gate_runs_before_runtime_sha_write() -> None:
    """CASE 2: 活跃任务存在时，RUNTIME_SHA 不被写入。"""
    body = _deploy_body()
    gate = _line(body, "guard_active_after_close_jobs")
    sha = _line(body, "write_runtime_sha")
    assert gate < sha


def test_active_job_gate_runs_before_migration() -> None:
    """CASE 3: 活跃任务存在时，migration 不被执行。"""
    body = _deploy_body()
    gate = _line(body, "guard_active_after_close_jobs")
    mig = _line(body, "run_migration")
    assert gate < mig


def test_active_job_gate_runs_before_worker_after_close_recreate() -> None:
    """CASE 4: 活跃任务存在时，worker-after-close 不被 force-recreate。"""
    body = _deploy_body()
    gate = _line(body, "guard_active_after_close_jobs")
    restart = _line(body, "restart_services")
    assert gate < restart


def test_gate_reports_conflicts_for_supervisor_drain() -> None:
    """冲突任务先可见化，再由 supervisor-drain 自然排空，不在只读检查中伪造完成。"""
    body = _guard_body()
    assert "status = 'running'" in body, "活跃过滤必须以 SchedulerJobRun.status='running' 为真值"
    assert "DEPLOYMENT_PENDING_AFTER_CLOSE_VISIBLE=TRUE" in body
    assert "supervisor-drain fence" in body
    assert "无强制阻塞盘后长任务，继续部署" in body
    # 查询为只读 SELECT，不允许业务写入。
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
        assert forbidden not in body, f"门禁查询不得包含写操作: {forbidden}"
    active_idx = _line(body, '[[ -n "${blocking_out}" ]]')
    visibility_idx = _line(body, 'log "DEPLOYMENT_PENDING_AFTER_CLOSE_VISIBLE=TRUE"')
    assert active_idx < visibility_idx


# =============================================================================
# [REVIEW-V2 / DEPLOY-GATE-REINTRODUCES-CHIP-PRIORITY-INVERSION] 优先级分治门禁
# =============================================================================
# Chip(after_close_chip_consensus) = enhancement，运行不阻塞部署；
# mandatory after_close_orchestrator 运行 = 强制阻塞（fail-closed）。

def _script() -> str:
    return _read("scripts/deploy/panji-deploy.sh")


def test_gate_splits_blocking_and_preemptible_job_sets() -> None:
    """FIX A/B：Chip 归入 PREEMPTIBLE；强制任务归入 BLOCKING；两者均定义。"""
    script = _script()
    assert "BLOCKING_AFTER_CLOSE_JOB_NAMES=" in script
    assert "PREEMPTIBLE_AFTER_CLOSE_JOB_NAMES=" in script
    # 强制阻塞集合必须含 mandatory orchestrator，且不含 Chip。
    blocking_arr = script.split("BLOCKING_AFTER_CLOSE_JOB_NAMES=(")[1].split(")")[0]
    preempt_arr = script.split("PREEMPTIBLE_AFTER_CLOSE_JOB_NAMES=(")[1].split(")")[0]
    assert "after_close_orchestrator" in blocking_arr
    assert "after_close_chip_consensus" not in blocking_arr
    assert "after_close_chip_consensus" in preempt_arr
    # 门禁函数体只引用集合名；强制任务交给 supervisor-drain，Chip 只记录增强任务证据。
    body = _guard_body()
    assert "PREEMPTIBLE_ENHANCEMENT_ACTIVE" in body
    assert "[[ -n \"${blocking_out}\" ]]" in body
    assert "DEPLOYMENT_PENDING_AFTER_CLOSE_VISIBLE=TRUE" in body
    # PREEMPTIBLE 分支不得包含任何 fail 调用。
    preempt_section = body.split("PREEMPTIBLE_ENHANCEMENT_ACTIVE")[1]
    assert "fail " not in preempt_section, "PREEMPTIBLE 分支不得触发 fail"


def test_gate_allows_when_only_chip_running() -> None:
    """CASE 1: 仅 after_close_chip_consensus = running → 部署门禁不 fail。"""
    body = _guard_body()
    # Chip 仅出现在可抢占分支（PREEMPTIBLE 集合），不在 blocking 失败触发条件内。
    assert "PREEMPTIBLE_AFTER_CLOSE_JOB_NAMES" in body
    assert "BLOCKING_AFTER_CLOSE_JOB_NAMES" in body
    # blocking 失败路径只依赖 blocking_out；preempt 分支只 log 不 fail。
    assert "[[ -n \"${blocking_out}\" ]]" in body
    assert "PREEMPTIBLE_ENHANCEMENT_ACTIVE" in body


def test_gate_routes_orchestrator_running_to_drain_visibility() -> None:
    """orchestrator running 必须可见，并由后续 supervisor-drain 安全排空。"""
    script = _script()
    # orchestrator 属于 blocking 集合，其状态必须进入 drain visibility。
    blocking_arr = script.split("BLOCKING_AFTER_CLOSE_JOB_NAMES=(")[1].split(")")[0]
    assert "after_close_orchestrator" in blocking_arr
    body = _guard_body()
    assert "BLOCKING_AFTER_CLOSE_JOB_NAMES" in body
    assert "DEPLOYMENT_PENDING_AFTER_CLOSE_VISIBLE=TRUE" in body
    assert "supervisor-drain fence" in body


def test_gate_allows_when_chip_running_and_orchestrator_queued() -> None:
    """CASE 3: Chip running + orchestrator queued（当前真实 runtime 情形）→ 允许部署。"""
    body = _guard_body()
    # queued 不是活跃执行（status='running' 过滤），故 blocking_out 为空 → 不 fail；
    # Chip running 命中 preempt 分支 → 仅记录 PREEMPTIBLE_ENHANCEMENT_ACTIVE 并继续。
    assert "status = 'running'" in body
    assert "PREEMPTIBLE_ENHANCEMENT_ACTIVE" in body
    # 失败分支仅由 blocking_out 驱动；Chip 运行不会使 blocking_out 非空。
    assert "[[ -n \"${blocking_out}\" ]]" in body


def test_gate_allows_when_no_active_jobs() -> None:
    """CASE 4: 无活跃任务 → 部署允许。"""
    body = _guard_body()
    assert "无强制阻塞盘后长任务，继续部署" in body


def test_gate_frontend_only_unchanged() -> None:
    """CASE 5: frontend-only 部署不受 worker-after-close 活跃任务阻塞（不变）。"""
    body = _guard_body()
    assert "_after_close_process_will_refresh" in body, "门禁必须由 after-close process impact 判定守卫"
    guard_idx = _line(body, "_after_close_process_will_refresh")
    query_idx = _line(body, "psql")
    assert guard_idx < query_idx, "必须在执行查询前先判定本次是否变更 backend runtime"
    # 门禁覆盖全部 worker-after-close 业务任务（FIX A）：job_name 集合定义在脚本内。
    script = _script()
    for name in _EXPECTED_ACTIVE_JOB_NAMES:
        assert name in script, f"门禁缺少活跃 job_name: {name}"
