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
    rule = _read("rules/81-remote-deployment-only.md")

    assert "81-remote-deployment-only.md" in rules_index
    assert "盘迹所有部署只能发生在远程运行服务器 `panji-prod`" in rule
    assert "本地执行 `scripts/ops/panji-test-deploy` 只是发起远程部署控制流程" in rule
    assert "实际部署使用的 `frontend/dist` 必须由远程服务器" in rule


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


def test_gate_fail_closed_blocks_and_allows_when_no_active_jobs() -> None:
    """CASE 5: 无活跃任务 → 门禁继续部署；有活跃任务 → fail-closed 带证据停止。"""
    body = _guard_body()
    assert "status = 'running'" in body, "活跃过滤必须以 SchedulerJobRun.status='running' 为真值"
    assert "ACTIVE_AFTER_CLOSE_JOB_BLOCKS_DEPLOY" in body
    # 无活跃任务时有明确的继续路径（非 fail），而不是任何非空都拒绝。
    assert "无活跃盘后长任务，继续部署" in body
    # 查询为只读 SELECT，不允许业务写入。
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
        assert forbidden not in body, f"门禁查询不得包含写操作: {forbidden}"
    # 失败路径仅在检测到活跃任务时触发。
    fail_idx = _line(body, 'fail "ACTIVE_AFTER_CLOSE_JOB_BLOCKS_DEPLOY')
    active_idx = _line(body, '[[ -n "${psql_out}" ]]')
    assert active_idx < fail_idx, "fail 必须位于活跃检测分支之后"


def test_gate_guards_only_backend_runtime_mutation_not_frontend_only() -> None:
    """CASE 6: frontend-only 部署不受无关 worker-after-close 活跃任务阻塞。"""
    body = _guard_body()
    assert "_backend_runtime_will_mutate" in body, "门禁必须由 backend mutation 判定守卫"
    guard_idx = _line(body, "_backend_runtime_will_mutate")
    query_idx = _line(body, "psql")
    assert guard_idx < query_idx, "必须在执行查询前先判定本次是否变更 backend runtime"
    # 门禁覆盖全部 worker-after-close 业务任务（FIX A）：job_name 集合定义在脚本内。
    script = _read("scripts/deploy/panji-deploy.sh")
    for name in _EXPECTED_ACTIVE_JOB_NAMES:
        assert name in script, f"门禁缺少活跃 job_name: {name}"
