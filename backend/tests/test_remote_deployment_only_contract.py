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
