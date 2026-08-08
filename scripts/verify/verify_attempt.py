#!/usr/bin/env python3
"""单可复用验证运行时 attempt 执行器（CHANGE-20260806-012, 减法版）。

[DS-110 / DS-112 / rules/80] 单次远程验证尝试的编排核心，运行于 panji-prod 已有
PostgreSQL 容器内（Live Mount 只读挂载 backend/app + alembic + scripts/verify）。

架构（2026-08-06 减法重构）：
  - 单可复用验证镜像 panji-verify-runtime:current
  - 单一长期容器 panji-verify-python（常驻空闲，禁 Scheduler/Worker/Uvicorn/pytest/seed）
  - 复用 trading-postgres（验证库 bz_stock_verify_<SHA>）
  - 本轮 verification 不连接 Redis（一次性审计：full-closure 仅连 PG）
  - attempt 仅隔离执行状态（SHA/DB/process/env/evidence）
  - 最外层 single-flight flock 由 run_remote_verification.sh 持有，本文件不再加锁
  - 每个 gate 用 fresh process：`docker exec panji-verify-python verify_exec.py <cmd>`
    verify_exec.py 从 /run/panji-verify/attempt.env 动态注入 attempt env
  - 异常/timeout/interrupted 恢复：`docker restart panji-verify-python`（杀容器所有验证进程、
    不删 container/image、不影响 PG/Redis/稳定栈、保留 bind mount）

状态机：
  created → preflight_passed → db_created → migration_ok → identity_ok →
  pg_tests_ok → seed_twice_ok → e2e_ok → cleanup_completed
  任一阶段异常 → failed → finally(export_evidence + cleanup_exact_attempt_resources + verify_cleanup)

finally 合同（失败也必须执行）：
  try:
      run_preflight(); create_verify_database(); run_migration_round_trip()
      assert_identity()
      run_self_contained_pg_tests(); run_synthetic_seed_twice(); run_synthetic_e2e()
  except (KeyboardInterrupt, Exception):
      result = "failed"
  finally:
      export_evidence(); cleanup_exact_attempt_resources(); verify_cleanup()

退出码：0=成功闭环；60=KeyboardInterrupt；50=gate 失败；70=blocked_cleanup；
       75=verification_busy（仅最外层锁持有）；异常可加 restart 恢复标记。

用法（由 scripts/verify/run_remote_verification.sh 在远程环境调用）：
  python scripts/verify/verify_attempt.py \
      --sha <FULL_SHA> \
      --plan full-closure \
      --runtime-dir /root/.panji-verify/runtime \
      --evidence-root /root/.panji-verify/evidence \
      --compose-project panji-verify \
      --verify-container panji-verify-python
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 本文件与 cleanup_runner / evidence_exporter 同目录
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from cleanup_runner import _verify_db_re, cleanup_attempt
from evidence_exporter import EvidenceExporter
from verification_plan import VerificationPlan, load_plan

VERIFY_DB_RE = _verify_db_re()

# 固定常量（与 run_remote_verification.sh 一致，不随 SHA 变化）
COMPOSE_PROJECT = "panji-verify"
VERIFY_CONTAINER = "panji-verify-python"
VERIFY_EXEC = "/app/scripts/verify/verify_exec.py"  # Live Mount 内路径
ATTEMPT_ENV_IN_CONTAINER = "/run/panji-verify/attempt.env"  # 只读 mount 进容器


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_attempt_id(target_sha: str) -> str:
    short = target_sha[:12]
    return f"verify-{short}-{int(time.time())}-{os.urandom(4).hex()}"


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


def _as_text(value: object) -> str:
    """[R1.1c-C1] 把 subprocess 输出统一成 str。

    TimeoutExpired.stdout/stderr 即使在 text=True 下也可能是 bytes（CPython 在超时
    路径返回原始缓冲），而 _redact_output 按 str 拼接/replace，会 TypeError 丢掉
    整条 timeout evidence。此处做局部归一：None → ""，bytes → utf-8 replace 解码，
    str → 原样。
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _extract_pytest_summary(stdout: str) -> str:
    """[R1.4] 从 pytest stdout 提取 bounded 的真实测试 summary。

    pytest 的逐测试进度行形如 "tests/xxx.py::test_y ... SKIPPED / PASSED / FAILED"，
    末尾汇总行形如 "3 passed, 2 skipped, 1 failed in 12.34s"。gate 成功路径需要
    如实记录 passed/skipped/failed 数量，不能把 SKIP 表述为 PASS。

    返回 bounded 摘要文本：取末尾汇总行 + 末尾 "===...===" 短摘要块 + 逐测试 SKIPPED
    行（若有）。缺汇总行时返回 ""（调用方降级为通用文案）。
    """
    lines = [l for l in (stdout or "").splitlines() if l.strip()]
    summary_lines: list[str] = []
    # 从后往前收集末尾的 ==== 摘要块与汇总行（pytest 短摘要块在最后）。
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("===") and "passed" in stripped:
            summary_lines.append(stripped)
            continue
        if "passed" in stripped and ("skipped" in stripped or "failed" in stripped or "warning" in stripped):
            summary_lines.append(stripped)
            continue
        if stripped in ("PASSED",) or "SKIPPED" in stripped or "ERROR" in stripped:
            summary_lines.append(stripped)
            continue
        if stripped.startswith(("==", "FAILED", "ERROR")):
            summary_lines.append(stripped)
            continue
        # 遇到非摘要内容即停止（摘要块在末尾连续区）
        break
    if not summary_lines:
        return ""
    summary_lines.reverse()
    return "\n".join(summary_lines)[-2000:]


def _run(cmd: list[str], *, timeout: int = 600, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)
        return proc.returncode, _as_text(proc.stdout), _as_text(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        # [R1.1b-E] TimeoutExpired 丢弃 partial stdout/stderr；保留已捕获的部分证据，
        # 避免超时后 hotspot 阶段信息丢失（seed 超时定位必需）。
        # [R1.1c-C1] 归一化为 str，保证返回类型恒为 tuple[int, str, str]。
        partial_out = _as_text(exc.stdout)
        partial_err = _as_text(exc.stderr)
        return 124, partial_out, partial_err or "command timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def _db_name_from_url(url: str) -> str:
    return url.rsplit("/", 1)[-1].split("?")[0]


def _redact_output(stdout: str, stderr: str, env_file: str) -> str:
    """Bound command evidence and remove every attempt-scoped secret value."""
    diagnostic = (stdout + stderr)[-12000:]
    for key in ("DATABASE_URL", "MIGRATION_DATABASE_URL", "JWT_SECRET"):
        try:
            diagnostic = diagnostic.replace(_read_env_value(env_file, key), "[REDACTED]")
        except RuntimeError:
            pass
    return diagnostic


class VerifyAttempt:
    """单次验证尝试编排器（单可复用运行时）。"""

    def __init__(self, *, target_sha: str, runtime_dir: str, evidence_root: str,
                 compose_project: str, verify_container: str, plan: VerificationPlan) -> None:
        if len(target_sha) != 40 or any(ch not in "0123456789abcdef" for ch in target_sha):
            raise ValueError("target_sha must be a complete 40-character lowercase hex SHA")

        self.target_sha = target_sha
        self.runtime_dir = Path(runtime_dir)
        self.attempt_env_file = self.runtime_dir / "attempt.env"
        if not self.attempt_env_file.is_file():
            raise RuntimeError(f"attempt.env not found at {self.attempt_env_file}")
        self.verify_db_url = _read_env_value(self.attempt_env_file, "DATABASE_URL")
        self.db_name = _db_name_from_url(self.verify_db_url)
        _assert_verify_db_name(self.db_name)
        if self.db_name != f"bz_stock_verify_{target_sha}":
            raise ValueError("verify database must be derived from the complete target SHA")

        # 固定 project（不再 per-SHA）
        if compose_project != COMPOSE_PROJECT:
            raise ValueError(f"compose project must be fixed '{COMPOSE_PROJECT}'")
        self.compose_project = compose_project
        self.verify_container = verify_container
        self.plan = plan
        self.attempt_id = _gen_attempt_id(target_sha)
        self.evidence_dir = Path(evidence_root) / self.attempt_id
        self.manifest: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "target_sha": self.target_sha,
            "verify_database": self.db_name,
            "compose_project": self.compose_project,
            "verify_container": self.verify_container,
            "attempt_env_file": str(self.attempt_env_file),
            "evidence_dir": str(self.evidence_dir),
            "status": "created",
            "plan": self.plan.name,
            "runtime_mode": "verification",
            "uses_redis": False,
            "created_at": _utcnow(),
        }
        self.exporter = EvidenceExporter(self.evidence_dir, self.manifest)
        # fresh process 执行封装：docker exec <container> verify_exec.py <cmd>
        # verify_exec.py 从 /run/panji-verify/attempt.env 动态注入 attempt env
        self.gate_base = [
            "docker", "exec", self.verify_container, "python3", VERIFY_EXEC,
        ]

    # -- 阶段实现 ----------------------------------------------------------

    def run_preflight(self) -> None:
        """预检：RUNTIME_SHA 与 git HEAD 一致；DB 名合法；容器存活。"""
        self.exporter.log("preflight: 开始")
        # single-flight 由最外层 flock 保证，本处不再加锁
        code, head, err = _run(["git", "rev-parse", "HEAD"], timeout=30)
        if code != 0 or head.strip() != self.target_sha:
            raise RuntimeError(f"remote repo SHA mismatch: {head.strip()} {err.strip()}")
        code, dirty, _ = _run(["git", "status", "--porcelain"], timeout=30)
        if code != 0 or dirty.strip():
            raise RuntimeError("remote verification checkout is not clean")
        if not VERIFY_DB_RE.match(self.db_name):
            raise RuntimeError(f"preflight: 非法验证库名 {self.db_name}")
        # 容器存活检查（常驻 panji-verify-python）
        code, out, _ = _run(["docker", "ps", "--format", "{{.Names}}"], timeout=30)
        if self.verify_container not in out.split():
            raise RuntimeError(f"preflight: 容器 {self.verify_container} 未运行")
        self.manifest["status"] = "preflight_passed"
        self.exporter.record_gate("preflight", True, detail="RUNTIME_SHA 一致 + 容器存活")
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
                [*self.gate_base, "alembic", "-c", "/app/alembic.ini", operation, target],
                timeout=self.plan.timeouts["migration"],
            )
            if code != 0:
                raise RuntimeError(f"migration {operation} {target} failed: {err.strip()}")
            code, out, err = _run(
                [*self.gate_base, "alembic", "-c", "/app/alembic.ini", "current"], timeout=60,
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

    def assert_identity(self) -> None:
        """容器内自检（不依赖 HTTP 探针 / 不启动 verify-backend）。

        host git HEAD / git status 已由 run_preflight 在 host 上检查，容器内不重复
        （Compose 不 mount .git）；此处只校验容器内可观测的身份与挂载。
        """
        self.exporter.log("assert_identity: 开始（容器内自检）")
        checks = [
            # 1. /run/panji-verify/RUNTIME_SHA（容器只读 mount，非 /app/RUNTIME_SHA）
            (["cat", "/run/panji-verify/RUNTIME_SHA"], self.target_sha, "/run/panji-verify/RUNTIME_SHA == target"),
            # 5. APP_ENV
            (["sh", "-c", "echo $APP_ENV"], "verification", "APP_ENV == verification"),
            # 6. DATABASE_URL 指向精确 verify DB
            (["sh", "-c", "echo $DATABASE_URL"], self.verify_db_url, "DATABASE_URL == verify DB"),
        ]
        for cmd, expected, label in checks:
            code, out, err = _run([*self.gate_base, *cmd], timeout=60)
            if code != 0 or out.strip() != expected:
                raise RuntimeError(
                    f"assert_identity 失败 [{label}]: got='{out.strip()}' err='{err.strip()}'"
                )

        # 7+8. current_database() 比对（psycopg 直连验证库）
        # fail-closed：当前连接必须是精确 verify DB，且不能是 bz_stock。
        # [P0] psycopg.connect 需原生 DSN（postgresql://...），不能直接吃 SQLAlchemy driver URL
        # （postgresql+asyncpg:// / postgresql+psycopg://）；用 MIGRATION_DATABASE_URL 去掉 +psycopg 同步前缀。
        pg_check = (
            "import psycopg,os,sys;"
            "dsn=os.environ['MIGRATION_DATABASE_URL'].replace('postgresql+psycopg://','postgresql://',1);"
            "conn=psycopg.connect(dsn);"
            "cur=conn.cursor();cur.execute('SELECT current_database()');"
            "db=cur.fetchone()[0];"
            "conn.close();"
            f"sys.exit(0 if (db=='{self.db_name}' and db!='bz_stock') else 1)"
        )
        code, out, err = _run(
            [*self.gate_base, "python3", "-c", pg_check], timeout=60,
        )
        if code != 0:
            raise RuntimeError(
                f"assert_identity 失败 [current_database 比对]: "
                f"db!={self.db_name} 或误连 bz_stock; out='{out.strip()}' err='{err.strip()}'"
            )
        # 3+4. import path + mount probe（/app 可读 + RUNTIME_SHA 一致）
        probe = (
            "import os,sys;"
            f"assert os.path.isdir('/app'), '/app not mounted';"
            f"sha=open('/run/panji-verify/RUNTIME_SHA').read().strip();"
            f"sys.exit(0 if sha=='{self.target_sha}' else 1)"
        )
        code, out, err = _run([*self.gate_base, "python3", "-c", probe], timeout=60)
        if code != 0:
            raise RuntimeError(f"assert_identity 失败 [Live Mount probe]: err='{err.strip()}'")

        self.manifest["status"] = "identity_ok"
        self.exporter.record_gate("identity", True, detail="容器内 identity 自检通过（含 current_database 比对）")
        self.exporter.log("assert_identity: 通过")

    def _record_runtime_diagnostics(self) -> None:
        """Record bounded, secret-redacted diagnostics before attempt cleanup."""
        for label, command in (
            ("container_ps", ["docker", "ps", "--format", "{{.Names}}"]),
            ("container_logs", ["docker", "logs", "--no-color", "--tail", "120", self.verify_container]),
        ):
            _code, stdout, stderr = _run(command, timeout=30)
            diagnostic = _redact_output(stdout, stderr, str(self.attempt_env_file))
            self.exporter.log(f"assert_identity {label}:\n{diagnostic}")

    def run_self_contained_pg_tests(self) -> None:
        """运行自包含基础 PG 测试（fresh process in panji-verify-python）。

        基础 PG Gate 职责：atomic publication / projection lifecycle / 100-stock call counts。
        closure 场景测试由 E2E Gate 负责，避免两道 Gate 重复同一测试（覆盖回退）。
        """
        self.exporter.log("run_self_contained_pg_tests: 开始")
        code, out, err = _run(
            [*self.gate_base, "pytest", "-m", "postgres",
             "tests/test_pg_atomic_publication.py",
             "tests/test_pg_projection_lifecycle.py",
             "tests/test_pg_100_stock_call_counts.py"],
            timeout=self.plan.timeouts["tests"],
        )
        if code != 0:
            diagnostic = _redact_output(out, err, str(self.attempt_env_file))
            self.exporter.log(f"pg_tests failure output:\n{diagnostic}")
            self.exporter.record_gate("pg_tests", False, detail=diagnostic[-2000:])
            raise RuntimeError(f"自包含 PG 测试失败 (exit={code})")
        self.manifest["status"] = "pg_tests_ok"
        # [R1.4] 成功路径保留真实 pytest summary（bounded），不再硬编码"全过"。
        # pytest 的 summary 形如 "3 passed, 2 skipped, 0 failed in 12.34s"，
        # 落在输出末尾的 "===...===" 摘要块内。若某个自包含测试被 SKIP（如 100-stock
        # 在空库下 skip），此处必须如实反映，避免把 SKIP 表述为 PASS。
        summary = _extract_pytest_summary(out)
        detail = summary if summary else "pg_tests 退出码 0（未提取到 pytest summary，见 logs）"
        self.exporter.log(f"run_self_contained_pg_tests: 通过，pytest summary:\n{detail}")
        self.exporter.record_gate("pg_tests", True, detail=detail)

    def run_synthetic_seed_twice(self) -> None:
        """运行 100% synthetic Seed 两次，验证幂等（第二次不冲突）。"""
        self.exporter.log("run_synthetic_seed_twice: 开始")
        for i in range(1, 3):
            code, out, err = _run(
                # [R1.1b-E] python -u 解除子进程 stdout 缓冲，使粗粒度 checkpoint
                # 在超时发生时已落盘（配合 _run 保留 partial stdout）。
                [*self.gate_base, "python", "-u", "-m", "scripts.verify.seed_v21_verify_data",
                 "--scenario", "all"],
                timeout=self.plan.timeouts["seed"],
            )
            if code != 0:
                diagnostic = _redact_output(out, err, str(self.attempt_env_file))
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
            [*self.gate_base, "pytest", "-m", "postgres",
             "tests/test_pg_seed_scenario_closures.py"],
            timeout=self.plan.timeouts["e2e"],
        )
        if code != 0:
            diagnostic = _redact_output(out, err, str(self.attempt_env_file))
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
        """清理 attempt 精确资源：drop verify DB + 删 attempt.env/RUNTIME_SHA。

        不删 container/image/network/PG/Redis；不 compose down；不 FLUSHALL。
        """
        try:
            summary = cleanup_attempt(self.evidence_dir / "manifest.json")
            self.exporter.log(f"cleanup_exact_attempt_resources: blocked={summary['blocked_cleanup']}")
            return not summary["blocked_cleanup"]
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[WARN] cleanup 失败: {exc}\n")
            return False

    def _clear_attempt_state(self) -> None:
        """删除 attempt 临时状态文件（attempt.env / RUNTIME_SHA）。

        必须在 verify_cleanup() 校验前执行，否则 verify_cleanup 会因文件尚在而返回 False。
        不删 container/image/network/PG/Redis；不 compose down；不 FLUSHALL。
        """
        for f in (self.attempt_env_file, self.runtime_dir / "RUNTIME_SHA"):
            try:
                if f.exists():
                    f.unlink()
                    self.exporter.log(f"_clear_attempt_state: removed {f}")
            except OSError as exc:
                self.exporter.log(f"_clear_attempt_state: 删除 {f} 失败（{exc}）")

    def verify_cleanup(self) -> bool:
        """清理后校验（状态校验，不再要求 compose==0，因容器常驻）：

        - verify DB 已删
        - attempt.env / RUNTIME_SHA 已清
        - panji-verify-python 仍健康（未被误删）
        - trading-postgres 仍健康
        失败记 blocked_cleanup。
        """
        self.exporter.log("verify_cleanup: 开始")
        # 校验库已删
        code, out, _ = _run([
            "docker", "exec", "trading-postgres", "psql", "-U", "bz", "-d", "postgres",
            "-tAc", f"SELECT 1 FROM pg_database WHERE datname='{self.db_name}';",
        ])
        db_gone = (code != 0) or (out.strip() == "0" or out.strip() == "")
        if not db_gone:
            return False
        # attempt 临时状态已清
        if self.attempt_env_file.exists():
            return False
        # 常驻容器仍健康
        code, out, _ = _run(["docker", "ps", "--format", "{{.Names}}"], timeout=30)
        running = set(out.split())
        if self.verify_container not in running:
            return False
        if "trading-postgres" not in running:
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
            self.assert_identity()
            self.run_self_contained_pg_tests()
            self.run_synthetic_seed_twice()
            self.run_synthetic_e2e()
        except KeyboardInterrupt as exc:
            exit_code = 60
            self.manifest["status"] = "failed"
            self.exporter.record_gate("attempt", False, detail=f"{type(exc).__name__}: {exc}")
            self.exporter.log(f"attempt 失败: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - evidence must capture every gate failure
            exit_code = 50
            self.manifest["status"] = "failed"
            self.exporter.record_gate("attempt", False, detail=f"{type(exc).__name__}: {exc}")
            self.exporter.log(f"attempt 失败: {type(exc).__name__}: {exc}")
        finally:
            self.export_evidence()
            # 失败路径的容器恢复由最外层 trap（run_remote_verification.sh cleanup_on_exit）统一负责，
            # 此处只做 attempt 级临时状态清理与校验。
            # 先清 attempt 临时状态（attempt.env / RUNTIME_SHA），再校验 cleanup
            self._clear_attempt_state()
            cleanup_ok = self.cleanup_exact_attempt_resources() and self.verify_cleanup()
            if not cleanup_ok:
                exit_code = 70
                self.manifest["status"] = "blocked_cleanup"
            elif exit_code == 0:
                self.manifest["status"] = "cleanup_completed"
            self.exporter.log(f"attempt exit_code={exit_code}")
            self.export_evidence()
        return exit_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sha", required=True)
    ap.add_argument("--plan", default="full-closure")
    ap.add_argument("--runtime-dir", required=True)
    ap.add_argument("--evidence-root", default="/root/.panji-verify/evidence")
    ap.add_argument("--compose-project", default=COMPOSE_PROJECT)
    ap.add_argument("--verify-container", default=VERIFY_CONTAINER)
    args = ap.parse_args()

    attempt = VerifyAttempt(
        target_sha=args.sha,
        runtime_dir=args.runtime_dir,
        evidence_root=args.evidence_root,
        compose_project=args.compose_project,
        verify_container=args.verify_container,
        plan=load_plan(args.plan),
    )
    return attempt.run()


if __name__ == "__main__":
    sys.exit(main())
