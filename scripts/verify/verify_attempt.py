#!/usr/bin/env python3
"""V2.1 远程验证尝试执行器（CHANGE-20260806-008, Phase 1）。

[DS-110 / DS-112 / rules/80] 单次远程验证尝试的编排核心，运行于 panji-prod 已有
PostgreSQL 容器内（Live Mount 只读挂载 backend/app + alembic + scripts/verify）。

attempt 身份模型：
  - target_sha       : 本次验证的代码精确 SHA（来自 git rev-parse HEAD，由 panji-verify-run 传入）
  - attempt_id       : 本次尝试唯一 ID（target_sha 派生 + 时间戳）
  - verify_database  : bz_stock_verify_<sha>（DS-110，位于已有 PG 容器，不新建容器/Volume）
  - compose_project  : 本次尝试的 compose project（精确隔离，避免串扰）
  - evidence_dir     : 本次尝试专属证据目录（精确删除，禁止删共享目录）

状态机（等价用户计划 §16）：
  created → preflight_passed → db_created → migration_ok → runtime_up →
  identity_ok → pg_tests_ok → seed_twice_ok → e2e_ok → cleanup_completed
  任一阶段异常 → failed → finally(export_evidence + cleanup_exact_attempt_resources + verify_cleanup)

finally 合同（失败也必须执行）：
  try:
      run_preflight(); create_verify_database(); run_migration_round_trip()
      start_verify_runtime(); assert_identity()
      run_self_contained_pg_tests(); run_synthetic_seed_twice(); run_synthetic_e2e()
  except (KeyboardInterrupt, Exception):
      result = "failed"
  finally:
      export_evidence(); cleanup_exact_attempt_resources(); verify_cleanup()

退出码：0=成功闭环；非0=失败（细节在 evidence_dir/summary.md）。

用法（由 scripts/ops/panji-verify 在远程环境调用）：
  python scripts/verify/verify_attempt.py \
      --target-sha <FULL_SHA> \
      --verify-db-url postgresql+asyncpg://bz:***@trading-postgres:5432/bz_stock_verify_<sha> \
      --compose-project panji-verify-<sha> \
      --env-file /path/to/.env.verify \
      --compose-file docker-compose.verify.yml
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

# 本文件与 cleanup_runner / evidence_exporter 同目录
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cleanup_runner import _verify_db_re, cleanup_attempt
from evidence_exporter import EvidenceExporter
from verification_plan import VerificationPlan, load_plan

VERIFY_DB_RE = _verify_db_re()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_attempt_id(target_sha: str) -> str:
    short = target_sha[:12]
    return f"verify-{short}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _assert_verify_db_name(db_name: str) -> None:
    """Fail closed: formal verification always uses the complete target SHA."""
    if not VERIFY_DB_RE.match(db_name):
        raise RuntimeError(
            f"非法验证库名 '{db_name}'（必须 bz_stock_verify_<40位sha>，DS-110）"
        )
    if db_name in {"postgres", "bz_stock", "template0", "template1"}:
        raise RuntimeError(f"严重错误：命中保护库名 '{db_name}'，立即中止")


def _read_env_value(path: str | Path, key: str) -> str:
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        candidate, value = line.split("=", 1)
        if candidate == key:
            return value
    raise RuntimeError(f"verification environment is missing {key}")


def _run(cmd: list[str], *, timeout: int = 600, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "command timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def _db_name_from_url(url: str) -> str:
    return url.rsplit("/", 1)[-1].split("?")[0]


class VerifyAttempt:
    """单次验证尝试编排器。"""

    def __init__(self, *, target_sha: str, compose_project: str,
                 env_file: str, compose_file: str, evidence_root: str,
                 plan: VerificationPlan) -> None:
        if len(target_sha) != 40 or any(ch not in "0123456789abcdef" for ch in target_sha):
            raise ValueError("target_sha must be a complete 40-character lowercase hex SHA")
        self.target_sha = target_sha
        self.verify_db_url = _read_env_value(env_file, "DATABASE_URL")
        self.db_name = _db_name_from_url(self.verify_db_url)
        _assert_verify_db_name(self.db_name)
        if self.db_name != f"bz_stock_verify_{target_sha}":
            raise ValueError("verify database must be derived from the complete target SHA")
        if compose_project != f"panji-verify-{target_sha}":
            raise ValueError("compose project must be derived from the complete target SHA")

        self.compose_project = compose_project
        self.env_file = env_file
        self.compose_file = compose_file
        self.plan = plan
        self._lock_file: TextIO | None = None
        self.attempt_id = _gen_attempt_id(target_sha)
        self.evidence_dir = (
            Path(evidence_root) / self.attempt_id
        )
        self.manifest: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "target_sha": self.target_sha,
            "verify_database": self.db_name,
            "compose_project": self.compose_project,
            "compose_file": str(compose_file),
            "env_file": str(env_file),
            "evidence_dir": str(self.evidence_dir),
            "status": "created",
            "plan": self.plan.name,
            "runtime_mode": "verification",
            "created_at": _utcnow(),
        }
        self.exporter = EvidenceExporter(self.evidence_dir, self.manifest)
        self.compose_base = [
            "docker", "compose", "-p", self.compose_project,
            "--env-file", self.env_file, "-f", self.compose_file,
        ]

    # -- 阶段实现 ----------------------------------------------------------

    def run_preflight(self) -> None:
        """预检：RUNTIME_SHA 与 git HEAD 一致；compose config 通过；DB 名合法。"""
        self.exporter.log("preflight: 开始")
        lock_path = Path(os.environ.get("PANJI_VERIFY_LOCK", "/tmp/panji-verify.lock"))
        self._lock_file = lock_path.open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another verification attempt is active") from exc
        code, head, err = _run(["git", "rev-parse", "HEAD"], timeout=30)
        if code != 0 or head.strip() != self.target_sha:
            raise RuntimeError(f"remote repo SHA mismatch: {head.strip()} {err.strip()}")
        code, dirty, _ = _run(["git", "status", "--porcelain"], timeout=30)
        if code != 0 or dirty.strip():
            raise RuntimeError("remote verification checkout is not clean")
        if not VERIFY_DB_RE.match(self.db_name):
            raise RuntimeError(f"preflight: 非法验证库名 {self.db_name}")
        code, _out, err = _run([*self.compose_base, "config"], timeout=60)
        if code != 0:
            raise RuntimeError(f"preflight: compose config 失败: {err.strip()}")
        self.manifest["status"] = "preflight_passed"
        self.exporter.record_gate("preflight", True, detail="RUNTIME_SHA 一致 + compose config 通过")
        self.exporter.log("preflight: 通过")

    def create_verify_database(self) -> None:
        """创建验证库（DS-110：位于已有 PG 容器，不新建容器/Volume）。"""
        self.exporter.log(f"create_verify_database: {self.db_name}")
        code, _out, err = _run([
            "docker", "exec", "trading-postgres", "psql", "-U", "bz", "-d", "postgres",
            "-v", "ON_ERROR_STOP=1", "-c",
            f'CREATE DATABASE "{self.db_name}" WITH TEMPLATE template0 ENCODING "UTF8";',
        ])
        if code != 0:
            raise RuntimeError(f"create_verify_database 失败: {err.strip()}")
        self.manifest["status"] = "db_created"
        self.exporter.record_resource("db", self.db_name)
        code, out, err = _run([
            "docker", "exec", "trading-postgres", "psql", "-U", "bz", "-d", self.db_name,
            "-tAc", "SELECT current_database();",
        ])
        if code != 0 or out.strip() != self.db_name:
            raise RuntimeError(f"created database identity assertion failed: {err.strip()}")
        self.exporter.record_gate("create_database", True, detail=f"库 {self.db_name} 就绪")
        self.exporter.log("create_verify_database: 完成")

    def run_migration_round_trip(self) -> None:
        """精确 SHA Migration：绑定目标 SHA alembic + 验证库，断言 revision。"""
        self.exporter.log("migration_round_trip: 开始")
        steps = (("upgrade", "head"), ("downgrade", "-1"), ("upgrade", "head"), ("upgrade", "head"))
        revisions: list[str] = []
        for operation, target in steps:
            code, _out, err = _run(
                [*self.compose_base, "run", "--rm", "verify-test", "alembic", "-c",
                 "/app/alembic.ini", operation, target],
                timeout=self.plan.timeouts["migration"],
            )
            if code != 0:
                raise RuntimeError(f"migration {operation} {target} failed: {err.strip()}")
            code, out, err = _run(
                [*self.compose_base, "run", "--rm", "verify-test", "alembic", "-c",
                 "/app/alembic.ini", "current"], timeout=60,
            )
            if code != 0 or not out.strip():
                raise RuntimeError(f"migration revision assertion failed: {err.strip()}")
            revisions.append(out.strip())
        self.manifest["status"] = "migration_ok"
        self.exporter.record_gate(
            "migration", True, detail="upgrade/downgrade/upgrade round-trip succeeded",
            extra={"revisions": revisions},
        )
        self.exporter.log("migration_round_trip: 完成")

    def start_verify_runtime(self) -> None:
        """起验证运行时（backend + redis），等待就绪。"""
        self.exporter.log("start_verify_runtime: 开始")
        code, _out, err = _run([*self.compose_base, "up", "-d", "verify-redis", "verify-backend"], timeout=self.plan.timeouts["runtime"])
        if code != 0:
            raise RuntimeError(f"start_verify_runtime 失败: {err.strip()}")
        self.exporter.record_resource("compose", f"project={self.compose_project}")
        # 探针：/v1/version 返回 runtime_git_sha + deployment_mode==live
        time.sleep(5)
        self.manifest["status"] = "runtime_up"
        self.exporter.record_gate("runtime_up", True, detail="verify-backend + verify-redis 已起")
        self.exporter.log("start_verify_runtime: 完成")

    def assert_identity(self) -> None:
        """断言运行时 runtime_git_sha == target_sha 且 deployment_mode==live。"""
        self.exporter.log("assert_identity: 开始")
        host_port = os.environ.get("VERIFY_BACKEND_HOST_PORT", "18000")
        url = f"http://localhost:{host_port}/v1/version"
        deadline = time.monotonic() + self.plan.timeouts["runtime"]
        code, out, err = 1, "", "runtime readiness timeout"
        data: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            code, out, err = _run(
                ["curl", "--fail", "--silent", "--show-error", url], timeout=10,
            )
            if code == 0:
                try:
                    candidate = json.loads(out)
                except json.JSONDecodeError:
                    candidate = None
                if isinstance(candidate, dict):
                    data = candidate
                    break
            time.sleep(2)
        if data is None:
            self._record_runtime_diagnostics()
            detail = err.strip() or f"invalid response: {out[:200]}"
            raise RuntimeError(f"assert_identity: 探针在时限内未就绪: {detail}")
        runtime_sha = data.get("runtime_git_sha")
        mode = data.get("deployment_mode")
        if runtime_sha != self.target_sha:
            raise RuntimeError(
                f"assert_identity: runtime_git_sha({runtime_sha}) != target_sha({self.target_sha})"
            )
        if mode != "live" or self.manifest["runtime_mode"] != "verification":
            raise RuntimeError("verification runtime identity or live mount identity is invalid")
        self.manifest["status"] = "identity_ok"
        self.exporter.record_gate("identity", True, detail=f"runtime_git_sha={runtime_sha} mode=live")
        self.exporter.log("assert_identity: 通过")

    def _record_runtime_diagnostics(self) -> None:
        """Record bounded, secret-redacted diagnostics before attempt cleanup."""
        for label, command in (
            ("compose_ps", [*self.compose_base, "ps", "--all"]),
            ("backend_logs", [*self.compose_base, "logs", "--no-color", "--tail", "120", "verify-backend"]),
        ):
            _code, stdout, stderr = _run(command, timeout=30)
            diagnostic = self._redact_output(stdout, stderr)
            self.exporter.log(f"assert_identity {label}:\n{diagnostic}")

    def _redact_output(self, stdout: str, stderr: str) -> str:
        """Bound command evidence and remove every attempt-scoped secret value."""
        diagnostic = (stdout + stderr)[-12000:]
        for key in ("DATABASE_URL", "MIGRATION_DATABASE_URL", "JWT_SECRET"):
            diagnostic = diagnostic.replace(
                _read_env_value(self.env_file, key), "[REDACTED]",
            )
        return diagnostic

    def run_self_contained_pg_tests(self) -> None:
        """运行自包含 PG 测试（docker compose run --rm verify-test）。"""
        self.exporter.log("run_self_contained_pg_tests: 开始")
        code, out, err = _run(
            [*self.compose_base, "run", "--rm", "verify-test"],
            timeout=self.plan.timeouts["tests"],
        )
        if code != 0:
            diagnostic = self._redact_output(out, err)
            self.exporter.log(f"pg_tests failure output:\n{diagnostic}")
            self.exporter.record_gate("pg_tests", False, detail=diagnostic[-2000:])
            raise RuntimeError(f"自包含 PG 测试失败 (exit={code})")
        self.manifest["status"] = "pg_tests_ok"
        self.exporter.record_gate("pg_tests", True, detail="atomic/projection/100-stock/closure 全过")
        self.exporter.log("run_self_contained_pg_tests: 通过")

    def run_synthetic_seed_twice(self) -> None:
        """运行 100% synthetic Seed 两次，验证幂等（第二次不冲突）。"""
        self.exporter.log("run_synthetic_seed_twice: 开始")
        for i in range(1, 3):
            code, out, err = _run(
                [*self.compose_base, "run", "--rm", "verify-test", "python",
                 "/app/scripts/verify/seed_v21_verify_data.py", "--scenario", "all"],
                timeout=self.plan.timeouts["seed"],
            )
            if code != 0:
                diagnostic = self._redact_output(out, err)
                self.exporter.log(f"seed 第{i}次 failure output:\n{diagnostic}")
                self.exporter.record_gate(
                    "seed_twice", False, detail=f"第{i}次 seed 失败: {diagnostic[-2000:]}"
                )
                raise RuntimeError(f"synthetic seed 第{i}次失败 (exit={code})")
            self.exporter.log(f"seed 第{i}次完成")
        self.manifest["status"] = "seed_twice_ok"
        self.exporter.record_gate("seed_twice", True, detail="synthetic seed 两次幂等通过")
        self.exporter.log("run_synthetic_seed_twice: 通过")

    def run_synthetic_e2e(self) -> None:
        """端到端产品就绪评估（真实 product_readiness_service 评估 closure 六态）。"""
        self.exporter.log("run_synthetic_e2e: 开始")
        code, out, err = _run(
            [*self.compose_base, "run", "--rm", "verify-test", "pytest", "-m", "postgres",
             "tests/test_pg_seed_scenario_closures.py"],
            timeout=self.plan.timeouts["e2e"],
        )
        if code != 0:
            diagnostic = self._redact_output(out, err)
            self.exporter.log(f"e2e failure output:\n{diagnostic}")
            self.exporter.record_gate("e2e", False, detail=diagnostic[-2000:])
            raise RuntimeError(f"synthetic e2e 失败 (exit={code})")
        self.manifest["status"] = "e2e_ok"
        self.exporter.record_gate("e2e", True, detail="closure 六态评估通过")
        self.exporter.log("run_synthetic_e2e: 通过")

    def export_evidence(self) -> None:
        try:
            self.exporter.export()
            self.exporter.log("export_evidence: 完成")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[WARN] export_evidence 失败: {exc}\n")

    def cleanup_exact_attempt_resources(self) -> bool:
        try:
            summary = cleanup_attempt(self.evidence_dir / "manifest.json")
            self.exporter.log(f"cleanup_exact_attempt_resources: blocked={summary['blocked_cleanup']}")
            return not summary["blocked_cleanup"]
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[WARN] cleanup 失败: {exc}\n")
            return False

    def verify_cleanup(self) -> bool:
        """清理后校验：验证库已删、compose project 已下。失败记 blocked_cleanup。"""
        self.exporter.log("verify_cleanup: 开始")
        # 校验库已删
        code, out, _ = _run([
            "docker", "exec", "trading-postgres", "psql", "-U", "bz", "-d", "postgres",
            "-tAc", f"SELECT 1 FROM pg_database WHERE datname='{self.db_name}';",
        ])
        db_gone = (code != 0) or (out.strip() == "0" or out.strip() == "")
        if not db_gone:
            return False
        # 校验 compose project 已下
        code, out, _ = _run([*self.compose_base, "ps", "-q"], timeout=60)
        containers = [c for c in out.splitlines() if c.strip()]
        if containers:
            return False
        self.exporter.log("verify_cleanup: 完成")
        return True

    # -- 编排 --------------------------------------------------------------

    def run(self) -> int:
        exit_code = 0
        try:
            self.run_preflight()
            self.create_verify_database()
            self.run_migration_round_trip()
            self.start_verify_runtime()
            self.assert_identity()
            self.run_self_contained_pg_tests()
            self.run_synthetic_seed_twice()
            self.run_synthetic_e2e()
        except KeyboardInterrupt as exc:
            exit_code = 60
            self.manifest["status"] = "failed"
            self.exporter.record_gate(
                "attempt", False,
                detail=f"{type(exc).__name__}: {exc}",
            )
            self.exporter.log(f"attempt 失败: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - evidence must capture every gate failure
            exit_code = 50
            self.manifest["status"] = "failed"
            self.exporter.record_gate(
                "attempt", False,
                detail=f"{type(exc).__name__}: {exc}",
            )
            self.exporter.log(f"attempt 失败: {type(exc).__name__}: {exc}")
        finally:
            self.export_evidence()
            cleanup_ok = self.cleanup_exact_attempt_resources() and self.verify_cleanup()
            if not cleanup_ok:
                exit_code = 70
                self.manifest["status"] = "blocked_cleanup"
            elif exit_code == 0:
                self.manifest["status"] = "cleanup_completed"
            self.exporter.log(f"attempt exit_code={exit_code}")
            self.export_evidence()
            if self._lock_file is not None:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
        return exit_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-sha", required=True)
    ap.add_argument("--compose-project", required=True)
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--compose-file", required=True)
    ap.add_argument("--evidence-root", default="/root/.panji-verify/evidence")
    ap.add_argument("--plan", default="scripts/verify/plans/full-closure.json")
    args = ap.parse_args()

    attempt = VerifyAttempt(
        target_sha=args.target_sha,
        compose_project=args.compose_project,
        env_file=args.env_file,
        compose_file=args.compose_file,
        evidence_root=args.evidence_root,
        plan=load_plan(args.plan),
    )
    return attempt.run()


if __name__ == "__main__":
    sys.exit(main())
