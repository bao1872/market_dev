"""验证基础设施静态安全测试（[CHANGE-20260806-008]，PURE_UNIT_TEST=1 可跑）。

仅测试 cleanup_runner / evidence_exporter 的**纯函数安全逻辑**与导出结构，不连库、不联网：
- 验证库名正则（仅 bz_stock_verify_<7-40位sha> 合法）
- 永久保护清单（bz_stock / postgres / trading-* / web_dev* / 基础镜像 拒绝）
- _safe_drop_database 永久保护与容器内精确删除
- cleanup_attempt 对受保护资源不删除、manifest 缺失标记 blocked_cleanup
- EvidenceExporter 导出 manifest.json / gates.json / summary.md

通过 = failed=0 且相关 skipped=0；属于本地门禁的“验证工具静态测试”项。
"""
from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

# 将 scripts/verify 加入 path（纯 stdlib 模块，无 app 依赖）
# backend/tests/test_verify_infra_safety.py → parents[2] = 仓库根
_VERIFY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "verify"
if str(_VERIFY_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFY_DIR))

import cleanup_runner as cr  # noqa: E402
from evidence_exporter import EvidenceExporter  # noqa: E402
from prepare_verify_environment import prepare  # noqa: E402
from verification_plan import load_plan  # noqa: E402

FULL_SHA = "a3caf4b86bdc126fd110b1f1a148f4f2c508652b"


def _executable_python(source: str) -> str:
    """剥离注释与 docstring，只留可执行代码。

    治理断言需要区分「代码里真的还在用某个已删除概念」与「docstring 里说明它已被删除」。
    用 tokenize 去掉 COMMENT，再用 AST 去掉所有 docstring 常量。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    # ast.unparse 输出不含注释，docstring 已置空
    return ast.unparse(tree)


def test_verify_db_name_regex() -> None:
    assert cr._verify_db_re().match(f"bz_stock_verify_{FULL_SHA}") is not None
    # 非法
    assert cr._verify_db_re().match("bz_stock") is None
    assert cr._verify_db_re().match("postgres") is None
    assert cr._verify_db_re().match("bz_stock_verify_26544") is None  # <7 位
    assert cr._verify_db_re().match("bz_stock_verify_GH" * 10) is None  # 非 hex


def test_permanent_protection_list() -> None:
    # 受保护库名
    assert cr._is_protected("bz_stock", "database")
    assert cr._is_protected("postgres", "database")
    assert cr._is_protected("template0", "database")
    # 受保护容器前缀
    assert cr._is_protected("trading-postgres", "container")
    assert cr._is_protected("web_dev", "container")
    assert cr._is_protected("panji-prod-foo", "container")
    # 非受保护（合法验证库）
    assert not cr._is_protected(f"bz_stock_verify_{FULL_SHA}", "database")
    assert not cr._is_protected("verify-test-26544de", "container")


def test_safe_drop_database_rejects_illegal_name() -> None:
    # 非法名 → 拒绝（不连库）
    res = cr._safe_drop_database("bz_stock")
    assert res["dropped"] is False
    assert res["error"] is not None


def test_safe_drop_database_uses_exact_container_command(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cr, "_run", lambda cmd, **_kwargs: (commands.append(cmd) or (0, "", "")))
    result = cr._safe_drop_database(f"bz_stock_verify_{FULL_SHA}")
    assert result["dropped"] is True
    assert commands[0][:8] == [
        "docker", "exec", "trading-postgres", "psql", "-U", "bz", "-d", "postgres",
    ]
    assert f'bz_stock_verify_{FULL_SHA}' in commands[0][-1]


def test_cleanup_attempt_missing_manifest_blocks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "missing_manifest.json"
        summary = cr.cleanup_attempt(manifest)
        assert summary["blocked_cleanup"] is True
        assert any("manifest" in r for r in summary["blocked_reasons"])


def test_cleanup_attempt_protected_compose_project() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # 构造一个指向受保护 compose project 的 manifest
        manifest = {
            "attempt_id": "verify-test-abc",
            "verify_database": f"bz_stock_verify_{FULL_SHA}",
            "compose_project": "trading-prod",  # 受保护前缀
            "evidence_dir": str(Path(tmp) / "evidence"),
        }
        mp = Path(tmp) / "manifest.json"
        mp.write_text(json.dumps(manifest))
        summary = cr.cleanup_attempt(mp)
        assert summary["blocked_cleanup"] is True
        assert any("compose" in r for r in summary["blocked_reasons"])


def test_evidence_exporter_writes_manifest_and_gates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ev_dir = Path(tmp) / "evidence" / "verify-abc"
        manifest = {"attempt_id": "verify-abc", "target_sha": "26544de", "status": "created"}
        exporter = EvidenceExporter(ev_dir, manifest)
        exporter.record_gate("preflight", True, detail="ok")
        exporter.log("hello")
        exporter.record_resource("db", f"bz_stock_verify_{FULL_SHA}")
        out = exporter.export()
        assert (out / "manifest.json").exists()
        assert (out / "gates.json").exists()
        assert (out / "summary.md").exists()
        assert (out / "logs.txt").exists()
        assert (out / "resources-db.json").exists()
        gates = json.loads((out / "gates.json").read_text())
        assert gates[0]["gate"] == "preflight" and gates[0]["passed"] is True


def test_evidence_summary_reports_gate_counts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ev_dir = Path(tmp) / "evidence" / "verify-def"
        manifest = {"attempt_id": "verify-def", "target_sha": "26544de", "status": "created"}
        exporter = EvidenceExporter(ev_dir, manifest)
        exporter.record_gate("a", True)
        exporter.record_gate("b", False)
        exporter.export()
        summary = (ev_dir / "summary.md").read_text()
        assert "通过 1" in summary
        assert "失败 1" in summary


def test_plan_is_closed_and_registered(tmp_path: Path) -> None:
    plan_path = _VERIFY_DIR / "plans" / "full-closure.json"
    plan = load_plan(plan_path)
    assert plan.name == "full-closure"
    assert plan.test_profile == "pg_contract"
    injected = tmp_path / "bad.json"
    injected.write_text(
        json.dumps({
            "schema_version": 1,
            "name": "bad",
            "runtime_profile": "after_close",
            "test_profile": "pg_contract",
            "seed_profile": "v21_synthetic",
            "e2e_profile": "closure_v21",
            "timeout_profile": "standard",
            "command": "rm -rf /",
        })
    )
    with pytest.raises(ValueError, match="unsupported plan keys"):
        load_plan(injected)


def test_prepare_environment_keeps_secret_in_mode_600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = {
        "trading-postgres": {
            "Config": {"Env": ["POSTGRES_USER=bz", "POSTGRES_PASSWORD=secret"], "Image": "pg"},
            "NetworkSettings": {"Networks": {"trading_default": {}}},
        },
        "trading-backend": {"Config": {"Env": [], "Image": "backend:sha"}},
        "trading-frontend": {"Config": {"Env": [], "Image": "frontend:sha"}},
    }
    objects["trading-backend"]["NetworkSettings"] = {"Networks": {"trading_default": {}}}
    monkeypatch.setattr("prepare_verify_environment._inspect", lambda name: objects[name])
    output = prepare(FULL_SHA, tmp_path / "attempt" / "market.verify.env")
    assert output.stat().st_mode & 0o777 == 0o600
    content = output.read_text()
    assert f"bz_stock_verify_{FULL_SHA}" in content
    assert "MIGRATION_DATABASE_URL=postgresql+psycopg://" in content
    assert "POSTGRES_PASSWORD" not in content
    # [CHANGE-20260806-012] 单可复用运行时：不再下发 per-SHA topology 变量与 host port
    for removed in ("VERIFY_TEST_IMAGE", "VERIFY_BACKEND_IMAGE", "VERIFY_FRONTEND_IMAGE"):
        assert removed not in content
    # 本轮 verification 只连 PG，不连 Redis（CHANGE-012 一次性审计结论）
    assert "REDIS_URL" not in content
    # attempt-scoped 必要变量齐备，供 verify_exec.py 注入 fresh process
    # [CHANGE-20260806-012] ATTEMPT_ID 已移除（不再下发，由 VerifyAttempt 内部自管）
    for required in ("DATABASE_URL=", f"TARGET_SHA={FULL_SHA}", "JWT_SECRET="):
        assert required in content
    # RUNTIME_SHA 与 attempt.env 同处固定 runtime 目录（容器只读挂载 /run/panji-verify/）
    assert (output.parent / "RUNTIME_SHA").read_text() == FULL_SHA


def test_alembic_prefers_dedicated_sync_migration_url() -> None:
    source = (_VERIFY_DIR.parents[1] / "backend" / "alembic" / "env.py").read_text()
    assert 'os.environ.get("MIGRATION_DATABASE_URL")' in source


def test_compose_declares_single_reusable_runtime() -> None:
    """[CHANGE-20260806-012] Compose 只描述一个常驻空闲验证容器。"""
    compose = (_VERIFY_DIR.parents[1] / "docker-compose.verify.yml").read_text()
    # 单镜像固定 tag + 常驻服务 + 空闲命令
    assert "panji-verify-runtime:current" in compose
    assert "verify-python:" in compose
    assert "container_name: panji-verify-python" in compose
    assert "command: sleep infinity" in compose
    # attempt 变量经只读 runtime mount 注入；复用已有 trading-postgres 外部网络
    assert "/run/panji-verify/" in compose
    assert "external: true" in compose
    # 旧 topology 不得回潮（仅检查有效 YAML，剥离说明性注释行）
    code = "\n".join(
        line for line in compose.splitlines() if not line.lstrip().startswith("#")
    )
    for removed in (
        "verify-test:", "VERIFY_TEST_IMAGE", "verify-backend",
        "verify-frontend", "verify-redis",
    ):
        assert removed not in code
    # 不发布 host port（验证栈不对外暴露）
    assert "127.0.0.1" not in code
    assert "ports:" not in code


def test_verification_image_installs_test_dependencies_at_build_time() -> None:
    dockerfile = (_VERIFY_DIR.parents[1] / "backend" / "Dockerfile").read_text()
    assert "FROM runtime AS verification" in dockerfile
    assert dockerfile.rstrip().endswith("FROM runtime AS production")
    assert 'pip install ".[dev]"' in dockerfile
    # 依赖合同 hash 以 build-arg 注入并写进 image label，供入口两方比较
    assert "ARG DEP_HASH" in dockerfile
    assert "LABEL panji.verify.dependency-hash=${DEP_HASH}" in dockerfile


def test_remote_runner_is_single_flight_and_reuses_runtime() -> None:
    """最外层 flock 独占 + 依赖 hash 两方比较，不再 per-SHA build/tag。"""
    runner = (_VERIFY_DIR / "run_remote_verification.sh").read_text()
    code = "\n".join(
        line for line in runner.splitlines() if not line.lstrip().startswith("#")
    )
    # single-flight：最外层锁，并发第二 attempt exit 75
    assert "flock -n 9" in code
    assert "exit 75" in code
    # 单可复用运行时 + 固定 project/容器
    assert 'VERIFY_IMAGE="panji-verify-runtime:current"' in code
    assert 'VERIFY_CONTAINER="panji-verify-python"' in code
    assert 'COMPOSE_PROJECT="panji-verify"' in code
    # 依赖 hash 两方比较后才 build
    assert "panji.verify.dependency-hash" in code
    assert "--target verification" in code
    assert "--build-arg" in code
    # attempt.env 必须真正生成（否则 verify_attempt.py 无 DATABASE_URL 可读）
    assert "prepare_verify_environment.py" in code
    assert "verify_attempt.py" in code
    # 精确 SHA checkout 与干净工作区断言
    assert "git checkout --detach" in code
    assert "git status --porcelain" in code
    # per-SHA 镜像路线已删除
    for removed in ("panji-verify-test:", "VERIFY_TEST_IMAGE", "docker image rm"):
        assert removed not in code


def test_verify_attempt_uses_fresh_process_env_injection() -> None:
    """gate 经 docker exec + verify_exec.py 注入 env；不再有第二层锁/进程注册表。"""
    source = (_VERIFY_DIR / "verify_attempt.py").read_text()
    # 固定 project/容器 + verify_exec 动态 env 注入
    assert 'COMPOSE_PROJECT = "panji-verify"' in source
    assert 'VERIFY_CONTAINER = "panji-verify-python"' in source
    assert "verify_exec.py" in source
    assert "/run/panji-verify/attempt.env" in source
    # 异常恢复用 restart，不删 infra
    assert "docker" in source and "restart" in source
    # 本轮不连 Redis
    assert '"uses_redis": False' in source
    # 已删除概念不得回潮：per-SHA project / 第二层锁 / 一次性 verify-test / 进程注册表。
    # 只扫描可执行代码：docstring/注释里"不再 X / 不 X"属于合规说明，不算回潮。
    code = _executable_python(source)
    for removed in (
        "panji-verify-{", "panji-verify-${", "LOCK_NB", "fcntl",
        "verify-test", "VERIFY_TEST_IMAGE", "compose run",
        "processes.json", "setsid", "PGID", "process registry",
        "FLUSHALL",
    ):
        assert removed not in code
    # 已删除入口 scripts/ops/panji-verify-run（注意 panji-verify-runtime 是合法前缀重叠）
    assert not re.search(r"panji-verify-run(?!time)", code)
    # 不得直接在 host 上跑 psql/alembic（必须容器内执行）
    assert '_run(["psql"' not in source
    assert '_run(["alembic"' not in source


def test_cleanup_source_never_uses_volume_delete() -> None:
    source = (_VERIFY_DIR / "cleanup_runner.py").read_text()
    assert '"down", "-v"' not in source
    assert "docker volume prune" not in source


def test_cleanup_never_destroys_reusable_runtime() -> None:
    """常驻容器（固定 project panji-verify + panji-verify-python）不得被 cleanup 删除。"""
    source = (_VERIFY_DIR / "cleanup_runner.py").read_text()
    # 禁止 compose down / --remove-orphans（会摧毁单可复用运行时）
    assert "compose down" not in source
    assert "--remove-orphans" not in source
    assert '"down"' not in source
    # PROTECTED_CONTAINERS 必须包含常驻验证容器（纵深防御）
    assert "panji-verify-python" in source
