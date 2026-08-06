#!/usr/bin/env python3
"""V2.1 验证证据导出器（CHANGE-20260806-008, Phase 2）。

导出本次尝试的最小诊断证据（两类硬门禁：资源清理 + 数据库删除，必须由 panji-verify-run
在每次验证/调试尝试结束后先导出最小证据再强制清理）：

  evidence_dir/
    manifest.json       — attempt 身份模型（target_sha/attempt_id/verify_database/
                          compose_project/evidence_dir/verify_db_url/compose_file/env_file）
    gates.json          — 各阶段门禁结果（preflight/migration/pg_tests/seed_twice/e2e）
    pytest-report.xml   — pytest 原始报告（如存在）
    logs.txt            — 运行日志快照
    resources-<phase>.json — 各阶段创建的资源清单（容器/网络/库/目录）
    cleanup.json        — cleanup_runner 输出（由 cleanup_runner 写入）
    summary.md          — 人类可读摘要

本模块被 verify_attempt.py 在 finally 中调用，保证无论成功失败都落盘最小证据。
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys

logger = logging.getLogger("verify.evidence")
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GATES_JSON = "gates.json"
MANIFEST_JSON = "manifest.json"
SUMMARY_MD = "summary.md"
MAX_LOG_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvidenceExporter:
    """累积本次尝试的证据，最后统一导出到 evidence_dir。"""

    def __init__(self, evidence_dir: str | Path, manifest: dict) -> None:
        self.evidence_dir = Path(evidence_dir)
        self.manifest = manifest
        self.gates: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.resources: dict[str, list[str]] = {}

    def record_gate(self, name: str, passed: bool, *, detail: str = "", extra: dict | None = None) -> None:
        self.gates.append({
            "gate": name,
            "passed": bool(passed),
            "detail": detail,
            "extra": extra or {},
        })

    def log(self, line: str) -> None:
        ts = _utcnow()
        self.logs.append(f"[{ts}] {line}")

    def record_resource(self, phase: str, resource: str) -> None:
        self.resources.setdefault(phase, []).append(resource)

    def export(self) -> Path:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

        # manifest.json
        (self.evidence_dir / MANIFEST_JSON).write_text(
            json.dumps(self.manifest, indent=2, ensure_ascii=False)
        )

        # gates.json
        (self.evidence_dir / GATES_JSON).write_text(
            json.dumps(self.gates, indent=2, ensure_ascii=False)
        )

        # logs.txt
        logs = "\n".join(self.logs).encode("utf-8")[-MAX_LOG_BYTES:]
        (self.evidence_dir / "logs.txt").write_bytes(logs)

        # resources-<phase>.json（每个 phase 一个文件，便于精确清理核对）
        for phase, items in self.resources.items():
            (self.evidence_dir / f"resources-{phase}.json").write_text(
                json.dumps(items, indent=2, ensure_ascii=False)
            )

        # pytest-report.xml（若存在则复制）
        src_report = self.manifest.get("pytest_report_src")
        if src_report:
            try:
                shutil.copy(src_report, self.evidence_dir / "pytest-report.xml")
            except Exception as exc:  # noqa: BLE001
                logger.warning("复制 pytest-report.xml 失败（尽力而为）: %s", exc)

        # summary.md
        self._write_summary()
        total = sum(p.stat().st_size for p in self.evidence_dir.rglob("*") if p.is_file())
        if total > MAX_EVIDENCE_BYTES:
            raise RuntimeError(f"evidence exceeds {MAX_EVIDENCE_BYTES} byte budget: {total}")
        return self.evidence_dir

    def _write_summary(self) -> None:
        lines: list[str] = []
        lines.append(f"# V2.1 验证尝试摘要 — {self.manifest.get('attempt_id', 'unknown')}")
        lines.append("")
        lines.append(f"- target_sha: `{self.manifest.get('target_sha', 'n/a')}`")
        lines.append(f"- verify_database: `{self.manifest.get('verify_database', 'n/a')}`")
        lines.append(f"- compose_project: `{self.manifest.get('compose_project', 'n/a')}`")
        lines.append(f"- status: `{self.manifest.get('status', 'unknown')}`")
        lines.append(f"- exported_at: {_utcnow()}")
        lines.append("")
        lines.append("## 门禁结果")
        lines.append("")
        passed = sum(1 for g in self.gates if g["passed"])
        lines.append(f"总门禁 {len(self.gates)}，通过 {passed}，失败 {len(self.gates) - passed}")
        lines.append("")
        for g in self.gates:
            mark = "PASS" if g["passed"] else "FAIL"
            lines.append(f"- [{mark}] {g['gate']}: {g['detail']}")
        lines.append("")
        lines.append("## 创建资源")
        lines.append("")
        for phase, items in self.resources.items():
            lines.append(f"### {phase}")
            for it in items:
                lines.append(f"- {it}")
            lines.append("")
        (self.evidence_dir / SUMMARY_MD).write_text("\n".join(lines), encoding="utf-8")


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


def main() -> int:
    """CLI：从 manifest 读取 evidence_dir 并快速导出当前 gates（供独立补偿调用）。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    exporter = EvidenceExporter(manifest.get("evidence_dir", "/tmp/verify-evidence"), manifest)
    exporter.export()
    print(f"evidence exported to {exporter.evidence_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
