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

import json
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
    assert f"VERIFY_TEST_IMAGE=panji-verify-test:{FULL_SHA}" in content
    assert "POSTGRES_PASSWORD" not in content


def test_alembic_prefers_dedicated_sync_migration_url() -> None:
    source = (_VERIFY_DIR.parents[1] / "backend" / "alembic" / "env.py").read_text()
    assert 'os.environ.get("MIGRATION_DATABASE_URL")' in source


def test_verify_test_receives_sync_migration_url() -> None:
    compose = (_VERIFY_DIR.parents[1] / "docker-compose.verify.yml").read_text()
    verify_test = compose.split("  verify-test:", 1)[1]
    assert "MIGRATION_DATABASE_URL:" in verify_test
    assert "image: ${VERIFY_TEST_IMAGE:" in verify_test


def test_verification_image_installs_test_dependencies_at_build_time() -> None:
    dockerfile = (_VERIFY_DIR.parents[1] / "backend" / "Dockerfile").read_text()
    assert "FROM runtime AS verification" in dockerfile
    assert dockerfile.rstrip().endswith("FROM runtime AS production")
    assert 'pip install ".[dev]"' in dockerfile
    runner = (_VERIFY_DIR / "run_remote_verification.sh").read_text()
    assert "--target verification" in runner
    assert 'docker image rm "panji-verify-test:${SHA}"' in runner


def test_cleanup_source_never_uses_volume_delete() -> None:
    source = (_VERIFY_DIR / "cleanup_runner.py").read_text()
    assert '"down", "-v"' not in source
    assert "docker volume prune" not in source
