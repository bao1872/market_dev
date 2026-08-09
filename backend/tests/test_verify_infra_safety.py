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
    # [P0] PANJI_REMOTE_VERIFY_DB_TEST=1 必须下发，否则 conftest fail-closed 拒绝跑 PG 集成
    for required in ("DATABASE_URL=", f"TARGET_SHA={FULL_SHA}", "JWT_SECRET=", "PANJI_REMOTE_VERIFY_DB_TEST=1"):
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


def test_seed_canonical_fixture_id_deterministic() -> None:
    """[R1.4b-P2/P7] 验证 canonical fixture ID 是 deterministic uuid5（seed_twice 幂等前提）。

    seed_v21_verify_data._cfixture 用 `uuid.uuid5(_NS, f"canonical/{scope}/{name}")` 生成
    deterministic ID；同一 (scope, name) 两次必须一致（第二次 seed 不新增数量），不同
    name 必须不同。此处按同一实现内联验证（PURE_UNIT 下 seed 模块因需 DATABASE_URL 无法导入）。
    """
    import uuid

    ns = uuid.uuid5(uuid.NAMESPACE_DNS, "panji.verify.synthetic")
    a1 = uuid.uuid5(ns, "canonical/core_run/2026-08-04")
    a2 = uuid.uuid5(ns, "canonical/core_run/2026-08-04")
    b = uuid.uuid5(ns, "canonical/core_run/2026-08-05")
    assert a1 == a2  # 同一 scope+name → 同一 ID（幂等）
    assert a1 != b  # 不同 name → 不同 ID（独立 lineage）


def test_cleanup_never_destroys_reusable_runtime() -> None:
    """常驻容器（固定 project panji-verify + panji-verify-python）不得被 cleanup 删除。"""
    source = (_VERIFY_DIR / "cleanup_runner.py").read_text()
    # 禁止 compose down / --remove-orphans（会摧毁单可复用运行时）
    assert "compose down" not in source
    assert "--remove-orphans" not in source
    assert '"down"' not in source
    # PROTECTED_CONTAINERS 必须包含常驻验证容器（纵深防御）
    assert "panji-verify-python" in source


def test_run_timeout_normalizes_bytes_output_to_str(monkeypatch) -> None:
    """[R1.1c-C1] TimeoutExpired 携带 bytes stdout/stderr 时，_run 必须归一为 str。

    TimeoutExpired.stdout/stderr 即使在 text=True 下也可能是 bytes；_redact_output
    按 str 拼接/replace，未归一会 TypeError 丢掉整条 timeout evidence。
    断言：exit=124、stdout/stderr 均为 str、partial evidence 被保留。
    """
    import subprocess

    import verify_attempt as va

    def _fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["python", "-u", "-m", "scripts.verify.seed_v21_verify_data"],
            timeout=1800,
            output=b"[seed] base_bars start\n[seed] base_bars end\n",
            stderr=b"partial stderr\n",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    code, out, err = va._run(["python", "-u", "-m", "x"], timeout=1800)

    assert code == 124
    assert isinstance(out, str)
    assert isinstance(err, str)
    # partial evidence 必须保留（超时 hotspot 定位依赖它）
    assert "[seed] base_bars start" in out
    assert "partial stderr" in err
    # 归一后可安全进入 _redact_output（原实现在 bytes 上会 TypeError）
    assert isinstance((out + err), str)


def test_run_timeout_without_output_keeps_str_contract(monkeypatch) -> None:
    """[R1.1c-C1] TimeoutExpired 无 output 时仍必须返回 tuple[int, str, str]。"""
    import subprocess

    import verify_attempt as va

    def _fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["x"], timeout=5)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    code, out, err = va._run(["x"], timeout=5)

    assert code == 124
    assert out == ""
    assert err == "command timeout"


def test_parse_pytest_summary_all_passed() -> None:
    """[R1.4b-P0-A] "3 passed in 42.5s"（真实全 PASS 无 skipped/failed 子句）→ accept。

    旧 parser 要求 summary 必须同时含 skipped/failed/error 才匹配，导致真实全 PASS
    "3 passed in 42.5s" 被误判为不可解析而 false-red。此处验证修复后接受。
    """
    import verify_attempt as va

    out = (
        "tests/test_pg_atomic_publication.py::test_a PASSED\n"
        "tests/test_pg_projection_lifecycle.py::test_b PASSED\n"
        "tests/test_pg_100_stock_call_counts.py::test_100 PASSED\n"
        "===== 3 passed in 42.5s =====\n"
    )
    counts = va._parse_pytest_summary(out)
    assert counts == {"passed": 3, "skipped": 0, "failed": 0, "errors": 0}


def test_parse_pytest_summary_accepts_warning() -> None:
    """[R1.4b-P0-B] "3 passed, 1 warning in 42.5s" → accept（warning 不影响 PASS）。"""
    import verify_attempt as va

    out = "===== 3 passed, 1 warning in 42.5s =====\n"
    counts = va._parse_pytest_summary(out)
    assert counts["passed"] == 3
    assert counts["skipped"] == 0
    assert counts["failed"] == 0
    assert counts["errors"] == 0


def test_parse_pytest_summary_rejects_skip() -> None:
    """[R1.4b-P0-C] "2 passed, 1 skipped in 1.5s" → skipped>0，调用方必须 fail-closed 拒绝。"""
    import verify_attempt as va

    out = (
        "tests/test_pg_atomic_publication.py::test_a PASSED\n"
        "tests/test_pg_100_stock_call_counts.py::test_100 SKIPPED\n"
        "===== 2 passed, 1 skipped in 1.5s =====\n"
    )
    counts = va._parse_pytest_summary(out)
    assert counts == {"passed": 2, "skipped": 1, "failed": 0, "errors": 0}
    assert counts["skipped"] > 0  # fail-closed 触发条件


def test_parse_pytest_summary_rejects_missing_summary() -> None:
    """[R1.4b-P0-E] 无可解析 summary → 返回 None（fail-closed）。"""
    import verify_attempt as va

    assert va._parse_pytest_summary("") is None
    assert va._parse_pytest_summary("tests/test_a.py::test_a PASSED\n") is None


def test_parse_pytest_summary_counts_failed_and_error() -> None:
    """[R1.4b-P0-D] failed/errors>0 亦必须被识别（顺序可变）。"""
    import verify_attempt as va

    out = "===== 1 passed, 2 failed in 5.0s =====\n"
    counts = va._parse_pytest_summary(out)
    assert counts["passed"] == 1
    assert counts["failed"] == 2
    assert counts["errors"] == 0

    out2 = "===== 1 passed, 1 error in 5.0s =====\n"
    counts2 = va._parse_pytest_summary(out2)
    assert counts2["passed"] == 1
    assert counts2["errors"] == 1


class _FakeExporter:
    def __init__(self):
        self.gates = []
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def record_gate(self, name, ok, detail=None):
        self.gates.append((name, ok, detail))


def _make_pg_tests_attempt(monkeypatch, code, out):
    """构造一个仅含 run_self_contained_pg_tests 所需属性的 VerifyAttempt 测试替身。

    [R1.4b-P0] gate-level targeted test：不建完整 VerifyAttempt（避免依赖真实
    runtime/evidence/plan），用 object.__new__ 绕过 __init__，注入最小属性，并
    monkeypatch 模块级 _run 返回固定 pytest stdout。
    """
    import verify_attempt as va

    att = object.__new__(va.VerifyAttempt)
    exporter = _FakeExporter()
    att.exporter = exporter
    att.gate_base = ["docker", "exec", "panji-verify-python", "python3", "verify_exec.py"]
    att.attempt_env_file = "/tmp/fake-attempt.env"
    att.manifest = {"status": "running"}

    class _Plan:
        timeouts = {"tests": 1800}

    att.plan = _Plan()

    def _fake_run(_cmd, *, timeout=600, env=None):
        return code, out, ""

    monkeypatch.setattr(va, "_run", _fake_run)
    return att, exporter


def test_pg_gate_all_passed_records_true(monkeypatch) -> None:
    """[R1.4b-P0] mock _run 返回真实 "3 passed in 42.5s" → record_gate(pg_tests, True)。"""
    out = (
        "tests/test_pg_atomic_publication.py::test_a PASSED\n"
        "tests/test_pg_projection_lifecycle.py::test_b PASSED\n"
        "tests/test_pg_100_stock_call_counts.py::test_100 PASSED\n"
        "===== 3 passed in 42.5s =====\n"
    )
    att, exporter = _make_pg_tests_attempt(monkeypatch, 0, out)
    att.run_self_contained_pg_tests()
    assert exporter.gates[-1][0] == "pg_tests"
    assert exporter.gates[-1][1] is True  # 全真实 passed → PASS


def test_pg_gate_with_skip_records_false_and_raises(monkeypatch) -> None:
    """[R1.4b-P0] mock _run 返回 "2 passed, 1 skipped in 1.5s" → record_gate(False) + raise。"""
    out = (
        "tests/test_pg_atomic_publication.py::test_a PASSED\n"
        "tests/test_pg_100_stock_call_counts.py::test_100 SKIPPED\n"
        "===== 2 passed, 1 skipped in 1.5s =====\n"
    )
    att, exporter = _make_pg_tests_attempt(monkeypatch, 0, out)
    try:
        att.run_self_contained_pg_tests()
        raise AssertionError("应 fail-closed 抛 RuntimeError")
    except RuntimeError:
        pass
    assert exporter.gates[-1][0] == "pg_tests"
    assert exporter.gates[-1][1] is False  # SKIP 假绿被拒绝
    assert "skipped" in exporter.gates[-1][2]


def test_pg_contract_list_includes_closure_suite() -> None:
    """[VERIFY-COVERAGE-01] 静态断言 targeted-pg 的 pg_contract curated 列表包含已授权 closure suite。

    仅做 AST 静态检查：解析 verify_attempt.py，定位 run_self_contained_pg_tests 方法内
    `pytest -m postgres` 的参数列表，确认 test_pg_review_runtime_blocker_closure.py 存在。
    不执行真实 pytest、不连库。防止 closure suite 被意外移出 curated 列表（false-green 回归）。
    """
    import verify_attempt as va

    src = Path(va.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    closure_file = "tests/test_pg_review_runtime_blocker_closure.py"
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_self_contained_pg_tests":
            # 收集该方法内所有字符串常量，检查 closure 文件是否被 pytest 参数引用
            strings = [
                n.value
                for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            found = closure_file in strings
            break

    assert found, (
        f"pg_contract curated 列表缺少已授权 closure suite: {closure_file}。"
        "VERIFY-COVERAGE-01 要求该文件必须被 targeted-pg 的 pg_tests gate 注册。"
    )
    # 同时确认原 3 个基线文件仍保留（最小追加，不替换）
    baseline = {
        "tests/test_pg_atomic_publication.py",
        "tests/test_pg_projection_lifecycle.py",
        "tests/test_pg_100_stock_call_counts.py",
    }
    src_all = src
    for b in baseline:
        assert b in src_all, f"pg_contract 不应删除基线文件: {b}"
