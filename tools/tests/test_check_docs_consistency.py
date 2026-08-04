"""tools/check_docs_consistency.py v2 治理规则测试。

覆盖 v2 MANIFEST 集中基线规则的 13 个场景：
1. MANIFEST 合法 baseline 通过；
2. MANIFEST 缺 baseline 失败；
3. baseline 非 40 位 SHA 失败；
4. baseline 非真实 commit 失败；
5. baseline 非 HEAD 祖先失败；
6. current 文档无重复 baseline 也通过；
7. 坏本地链接失败；
8. 待填写占位符失败；
9. feishu_webhook 当前方案失败（删除语境豁免）；
10. open-decisions 写回 Webhook OPEN 失败；
11. archive 旧 baseline 不触发失败。
12. baseline 落后 HEAD 超过窗口失败（CP-19 规则 16）；
13. baseline 在窗口内通过（CP-19 规则 16）。

使用 tmp_path + monkeypatch 注入临时文档目录，不修改真实文档。

运行:
    python -m pytest tools/tests/test_check_docs_consistency.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 将 tools/ 加入 sys.path 以导入 check_docs_consistency
_TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_docs_consistency as cdc

# 测试用合法 SHA（40 位 hex，非真实提交，通过 mock 通过 git 校验）
VALID_SHA = "a" * 40
ALT_SHA = "b" * 40


def _manifest_content(sha: str = VALID_SHA) -> str:
    """生成合法的 baseline 文档内容（含全局基线字段）。

    [eaffb11 文档重构] baseline 字段从 docs/current/MANIFEST.md 迁移到
    docs/maps/00-system-overview.md 的"核验提交"字段。
    """
    return (
        "# 系统全貌 Map\n\n"
        f"核验提交：`{sha}`（测试 mock）\n"
        "> 本文件基于真实代码、数据、日志或运行结果填写。\n\n"
        "## 1. 当前实现摘要\n\n测试占位。\n"
    )


def _setup_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: str | None = None,
    current_docs: dict[str, str] | None = None,
    maps_docs: dict[str, str] | None = None,
    archive_docs: dict[str, str] | None = None,
    readme: str | None = None,
    agents: str | None = None,
) -> Path:
    """在 tmp_path 下创建 v2 docs 结构并注入模块路径变量。

    [eaffb11 文档重构] docs/current/ 替换为 docs/prd/；
    MANIFEST baseline 改在 docs/maps/00-system-overview.md；
    CHANGE records 在 docs/changes/records/ 或 docs/changes/YYYY/；
    CHANGELOG.md 替换为 docs/changes/INDEX.md。

    Args:
        tmp_path: 临时目录
        monkeypatch: pytest monkeypatch
        manifest: docs/maps/00-system-overview.md 内容（含 baseline 字段）；None 则不创建
        current_docs: {文件名: 内容} 字典，创建 docs/prd/ 下的文档
        maps_docs: {文件名: 内容} 字典，创建 docs/maps/ 下的文档
        archive_docs: {文件名: 内容} 字典，创建 docs/archive/current-legacy-20260703/ 下的文档
        readme: docs/README.md 内容
        agents: AGENTS.md 内容

    Returns:
        tmp_path（作为 REPO_ROOT）
    """
    docs_dir = tmp_path / "docs"
    # [eaffb11] docs/current/ 替换为 docs/prd/
    current_dir = docs_dir / "prd"
    maps_dir = docs_dir / "maps"
    archive_dir = docs_dir / "archive" / "current-legacy-20260703"
    changes_dir = docs_dir / "changes"
    records_dir = changes_dir / "records"
    changes_year_dir = changes_dir / "2026"
    current_dir.mkdir(parents=True, exist_ok=True)
    maps_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    records_dir.mkdir(parents=True, exist_ok=True)
    changes_year_dir.mkdir(parents=True, exist_ok=True)

    if readme is not None:
        (docs_dir / "README.md").write_text(readme, encoding="utf-8")

    # [eaffb11] baseline 写入 docs/maps/00-system-overview.md
    manifest_path = maps_dir / "00-system-overview.md"
    if manifest is not None:
        manifest_path.write_text(manifest, encoding="utf-8")
    else:
        # 默认创建一个合法的 baseline 文件
        manifest_path.write_text(_manifest_content(VALID_SHA), encoding="utf-8")

    if current_docs:
        for name, content in current_docs.items():
            (current_dir / name).write_text(content, encoding="utf-8")

    if maps_docs:
        for name, content in maps_docs.items():
            (maps_dir / name).write_text(content, encoding="utf-8")

    if archive_docs:
        for name, content in archive_docs.items():
            (archive_dir / name).write_text(content, encoding="utf-8")

    if agents is not None:
        (tmp_path / "AGENTS.md").write_text(agents, encoding="utf-8")

    # [eaffb11] 规则 13 必需新文档改为 docs/prd/20-quant-model.md 和 docs/maps/20-quant-model.md
    _required_prd = "20-quant-model.md"
    _required_maps = "20-quant-model.md"
    if not (current_dir / _required_prd).exists():
        (current_dir / _required_prd).write_text(
            "# 量化模型 PRD\n\n测试占位。\n",
            encoding="utf-8",
        )
    if not (maps_dir / _required_maps).exists():
        (maps_dir / _required_maps).write_text(
            "# 量化模型 Map\n\n测试占位。\n",
            encoding="utf-8",
        )

    # [eaffb11] 规则 15 必需 CHANGE 记录改为 CHANGE-20260726-001
    _change_id = "CHANGE-20260726-001"
    _record_file_new = changes_year_dir / f"{_change_id}-documentation-governance.md"
    if not _record_file_new.exists():
        _record_file_new.write_text(
            f"# {_change_id}\n\n文档体系重构（测试占位）。\n",
            encoding="utf-8",
        )
    # [eaffb11] CHANGELOG.md 替换为 INDEX.md
    _index_file = changes_dir / "INDEX.md"
    if not _index_file.exists():
        _index_file.write_text(
            f"# Change Index\n\n| Change ID | 日期 | 标题 |\n|---|---|---|\n"
            f"| {_change_id} | 2026-07-26 | 文档体系重构 |\n",
            encoding="utf-8",
        )
    # [P0-3] 规则 17：默认创建一份合法验收矩阵（基线=VALID_SHA），避免既有场景因缺矩阵误失败
    _matrix_file = changes_year_dir / "PRD-Acceptance-Matrix-2026-08-04.md"
    if not _matrix_file.exists():
        _matrix_file.write_text(
            f"# PRD 完整验收矩阵\n\n**基线**: `{VALID_SHA}`\n\n"
            f"**当前判断**: `code_ready = false`\n",
            encoding="utf-8",
        )

    # 注入模块路径变量
    monkeypatch.setattr(cdc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cdc, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(cdc, "CURRENT_DIR", current_dir)
    monkeypatch.setattr(cdc, "MANIFEST_FILE", manifest_path)
    monkeypatch.setattr(cdc, "MAPS_DIR", maps_dir)
    monkeypatch.setattr(cdc, "ARCHIVE_DIR", archive_dir.parent)
    monkeypatch.setattr(cdc, "AGENTS_FILE", tmp_path / "AGENTS.md")
    monkeypatch.setattr(cdc, "ACCEPTANCE_MATRIX_DIR", changes_year_dir)

    # 默认 mock git 校验为通过
    monkeypatch.setattr(cdc, "is_valid_commit", lambda sha: True)
    monkeypatch.setattr(cdc, "is_ancestor_of_head", lambda sha: True)
    # 规则 16 默认 mock：baseline 在窗口内（既有 11 个场景不应因新规则失败）
    # 单独测试场景 12 会覆盖此 mock 触发失败
    monkeypatch.setattr(cdc, "count_commits_ahead_of_baseline", lambda sha: 10)
    # 规则 17 默认 mock：HEAD/origin/dev 解析为 None（不依赖真实 git）
    monkeypatch.setattr(cdc, "get_rev_sha", lambda rev: None)
    # [P0-3] 规则 17 漂移门禁默认 mock：baseline 是任意 rev 祖先且在窗口内
    # 默认落后 1 个纯文档提交（严格窗口 2 内），保证不涉及规则 17 的既有场景
    # 不会因默认值 10（旧窗口 50 时的取值）超过新窗口 2 而误失败。
    monkeypatch.setattr(cdc, "is_ancestor_of_rev", lambda sha, rev: True)
    monkeypatch.setattr(cdc, "count_commits_ahead", lambda rev, sha: 1)

    return tmp_path


# ============================================================
# 测试用例（v2 MANIFEST 集中基线规则 11 个场景）
# ============================================================


class TestCheckDocsConsistencyV2:
    """check_docs_consistency.py v2 11 条规则测试。"""

    def test_01_manifest_valid_baseline_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 1：MANIFEST 合法 baseline 通过。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": "# 产品业务\n"},
            maps_docs={"api-route-map.md": "# API 路由\n"},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 0, f"合法 baseline 应通过，实际返回 {rc}"

    def test_02_manifest_missing_baseline_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 2：MANIFEST 缺 baseline 字段失败。

        [P0-3] 直接测 check_manifest_baseline()：main() 不强制 MANIFEST
        每次提交更新（见 main() 注释），基线规则由该函数独立承载。
        """
        manifest_no_baseline = (
            "# Current Docs Manifest\n\n"
            "> 文档状态：CURRENT DESIGN BASELINE  \n"
            "> 设计基线日期：2026-07-03  \n"
            "> 注意：无 baseline 字段。\n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=manifest_no_baseline,
            readme="# README\n",
        )

        errors, baseline = cdc.check_manifest_baseline()
        assert errors, "MANIFEST 缺 baseline 字段应返回错误"
        assert baseline is None, "缺 baseline 时解析结果应为 None"

    def test_03_baseline_invalid_sha_format_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 3：baseline 非 40 位 SHA 失败。"""
        # 38 位 hex，非 40 位
        short_sha = "a" * 38
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(short_sha),
            readme="# README\n",
        )

        errors, _ = cdc.check_manifest_baseline()
        assert errors, "非 40 位 SHA 应返回错误"
        assert any(
            "缺少" in e or "格式非法" in e for e in errors
        ), "应报告缺少合格的 baseline 字段"

    def test_04_baseline_not_real_commit_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 4：baseline 非真实 git 提交失败。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            readme="# README\n",
        )
        # mock is_valid_commit 返回 False
        monkeypatch.setattr(cdc, "is_valid_commit", lambda sha: False)

        errors, _ = cdc.check_manifest_baseline()
        assert errors, "非真实 commit 的 SHA 应返回错误"
        assert any("不是有效的 git 提交" in e for e in errors)

    def test_05_baseline_not_head_ancestor_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 5：baseline 非 HEAD 祖先失败。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            readme="# README\n",
        )
        # mock is_ancestor_of_head 返回 False
        monkeypatch.setattr(cdc, "is_ancestor_of_head", lambda sha: False)

        errors, _ = cdc.check_manifest_baseline()
        assert errors, "非 HEAD 祖先的 SHA 应返回错误"
        assert any("不是当前 HEAD 的祖先" in e for e in errors)

    def test_06_current_docs_without_baseline_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 6：current 其他文档无重复 baseline 字段也通过（v2 核心规则）。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={
                # current 其他文档不含 baseline 字段
                "00-product-business.md": "# 产品业务\n\n无基线头。\n",
                "01-system-architecture.md": "# 系统架构\n\n无基线头。\n",
                "open-decisions.md": "# 未决问题\n\n已决定 Webhook 已永久删除。\n",
            },
            maps_docs={"api-route-map.md": "# API 路由\n"},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 0, "current 其他文档无 baseline 也应通过"

    def test_07_broken_local_link_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 7：坏本地链接失败。"""
        # 链接到不存在的文件
        content_with_bad_link = "# 产品业务\n\n[不存在](nonexistent.md)\n"
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": content_with_bad_link},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "失效本地链接应失败"

    def test_08_placeholder_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 8：待填写占位符失败。"""
        content_with_placeholder = "# 产品业务\n\n这里是待填写内容\n"
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": content_with_placeholder},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "待填写占位符应失败"

    def test_09_feishu_webhook_current_solution_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 9：feishu_webhook 作为当前方案（非删除语境）失败。"""
        content_with_webhook = (
            "# 产品业务\n\n当前通知方式包括 feishu_webhook 和平台应用。\n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": content_with_webhook},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "feishu_webhook 作为当前方案应失败"

    def test_09b_feishu_webhook_deletion_context_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 9b：feishu_webhook 在删除语境中通过（豁免）。"""
        content_with_deletion = (
            "# 产品业务\n\nfeishu_webhook 已永久删除，禁止恢复。\n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": content_with_deletion},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 0, "feishu_webhook 在删除语境中应通过"

    def test_10_open_decisions_webhook_open_regresses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 10：open-decisions.md 把 Webhook vs Platform App 写成 OPEN 失败。"""
        open_content = (
            "# 未决设计问题\n\n"
            "## OPEN-NOTIFY-001 飞书长期形态\n\n"
            "仍需决定 Webhook 与平台应用的长期优先级。\n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"open-decisions.md": open_content},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "open-decisions.md 把 Webhook 写成 OPEN 应失败"

    def test_10b_open_decisions_webhook_decided_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 10b：open-decisions.md Webhook 已决定通过（豁免）。"""
        decided_content = (
            "# 未决设计问题\n\n"
            "## NOTIFY-001 飞书长期形态\n\n"
            "已决定 Webhook 已永久删除，仅保留 Platform App。\n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"open-decisions.md": decided_content},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 0, "open-decisions.md Webhook 已决定应通过"

    def test_11_archive_legacy_baseline_not_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 11：archive 旧文档含 baseline 不触发一致性检查失败（v2 规则 6）。

        旧 current 文档归档到 docs/archive/current-legacy-20260703/，
        其中可能含旧 baseline 字段，但 v2 不对其做 baseline 一致性检查。
        """
        # 旧 current 文档含旧 baseline 头（与 MANIFEST baseline 不同）
        legacy_header = (
            "> 文档状态：CURRENT DESIGN BASELINE  \n"
            f"> 实现核对基线：{ALT_SHA}  \n"
            "> 设计基线日期：2026-07-02  \n"
        )
        legacy_content = legacy_header + "\n# 旧产品概述\n\n这是归档旧文档。\n"
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": "# 产品业务\n"},
            archive_docs={"00-project-overview.md": legacy_content},
            readme="# README\n",
        )

        rc = cdc.main()
        assert rc == 0, (
            "archive 旧文档 baseline 不应触发一致性检查失败；"
            f"实际返回 {rc}"
        )

    def test_12_baseline_stale_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 12：baseline 落后 HEAD 超过窗口失败（CP-19 规则 16）。

        修复 PROMPT.md §4 指出的问题：旧规则 4 只要求 baseline 是 HEAD 祖先，
        即使 baseline 落后 88 个 commit 仍能通过。新规则 16 要求 baseline
        必须在最近 N 个 commit 内，防止文档与代码严重脱节。
        """
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            readme="# README\n",
        )
        # mock count_commits_ahead_of_baseline 返回超过窗口的值
        # 窗口默认 50，返回 88 模拟当前生产 baseline 落后 88 commit
        monkeypatch.setattr(
            cdc, "count_commits_ahead_of_baseline", lambda sha: 88
        )

        errors, _ = cdc.check_manifest_baseline()
        assert errors, "baseline 严重落后应返回错误"
        assert any(
            "严重落后" in e and "88" in e for e in errors
        ), "错误信息应包含'严重落后'和落后 commit 数量 88"

    def test_13_baseline_within_window_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 13：baseline 在窗口内通过（CP-19 规则 16）。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": "# 产品业务\n"},
            maps_docs={"api-route-map.md": "# API 路由\n"},
            readme="# README\n",
        )
        # mock count_commits_ahead_of_baseline 返回窗口内的值
        monkeypatch.setattr(
            cdc, "count_commits_ahead_of_baseline", lambda sha: 10
        )

        rc = cdc.main()
        assert rc == 0, "baseline 落后 HEAD 10 个 commit（窗口 50 内）应通过"

    def test_13b_baseline_at_window_boundary_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 13b：baseline 正好在窗口边界通过（CP-19 规则 16 边界）。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": "# 产品业务\n"},
            maps_docs={"api-route-map.md": "# API 路由\n"},
            readme="# README\n",
        )
        # 边界：窗口 50，正好落后 50 commit 应通过（> 才失败）
        monkeypatch.setattr(
            cdc, "count_commits_ahead_of_baseline", lambda sha: 50
        )

        rc = cdc.main()
        assert rc == 0, "baseline 落后 HEAD 50 commit（等于窗口）应通过"

    # ===== 规则 17：验收矩阵基线（P0-3） =====

    @staticmethod
    def _write_acceptance_matrix(tmp_path: Path, sha: str | None) -> None:
        """在临时 changes/2026 目录写入验收矩阵文件。"""
        content = "# PRD 完整验收矩阵\n\n"
        if sha is not None:
            content += f"**基线**: `{sha}`\n"
        content += "\n**当前判断**: `code_ready = false`\n"
        matrix_path = (
            tmp_path / "docs" / "changes" / "2026"
            / "PRD-Acceptance-Matrix-2026-08-04.md"
        )
        matrix_path.write_text(content, encoding="utf-8")

    def test_14_acceptance_matrix_baseline_matches_head_and_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """场景 14：矩阵基线 == HEAD == origin/dev 时通过。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": "# 产品业务\n"},
            maps_docs={"api-route-map.md": "# API 路由\n"},
            readme="# README\n",
        )
        self._write_acceptance_matrix(tmp_path, VALID_SHA)
        # HEAD 与 origin/dev 都解析为矩阵基线
        monkeypatch.setattr(cdc, "get_rev_sha", lambda rev: VALID_SHA)

        rc = cdc.main()
        assert rc == 0, "矩阵基线 == HEAD == origin/dev 应通过"

    def test_15_acceptance_matrix_baseline_stale_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """场景 15：矩阵基线严重落后 HEAD 时失败（规则 17 防漂移）。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": "# 产品业务\n"},
            maps_docs={"api-route-map.md": "# API 路由\n"},
            readme="# README\n",
        )
        self._write_acceptance_matrix(tmp_path, VALID_SHA)
        # 矩阵基线仍停留在 VALID_SHA，但 HEAD/origin/dev 已前进 88 commit
        monkeypatch.setattr(cdc, "count_commits_ahead", lambda rev, sha: 88)

        rc = cdc.main()
        assert rc == 1, "矩阵基线严重落后 HEAD 应失败"
        captured = capsys.readouterr()
        assert "验收矩阵基线" in captured.out, "应报告验收矩阵基线问题"

    def test_16_acceptance_matrix_missing_baseline_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """场景 16：验收矩阵缺少 `**基线**` SHA 字段时失败。"""
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": "# 产品业务\n"},
            maps_docs={"api-route-map.md": "# API 路由\n"},
            readme="# README\n",
        )
        self._write_acceptance_matrix(tmp_path, None)

        rc = cdc.main()
        assert rc == 1, "验收矩阵缺少 **基线** 字段应失败"

    def test_17_acceptance_matrix_strict_window_2(self, tmp_path, monkeypatch):
        """场景 17（P0-3）：验收矩阵基线只允许落后 1~2 个纯文档提交。

        与 MANIFEST 宽松窗口 50 解耦：ACCEPTANCE_MATRIX_FRESHNESS_WINDOW=2。
        落后 2 commit（含）通过；落后 3 commit 起失败。
        """
        _setup_docs(
            tmp_path,
            monkeypatch,
            manifest=_manifest_content(VALID_SHA),
            current_docs={"00-product-business.md": "# 产品业务\n"},
            maps_docs={"api-route-map.md": "# API 路由\n"},
            readme="# README\n",
        )
        self._write_acceptance_matrix(tmp_path, VALID_SHA)
        monkeypatch.setattr(cdc, "get_rev_sha", lambda rev: VALID_SHA)
        assert cdc.ACCEPTANCE_MATRIX_FRESHNESS_WINDOW == 2, (
            "验收矩阵基线窗口必须收敛到 2（P0-3，不再允许 50）"
        )

        # 落后 2 个纯文档提交 → 通过
        monkeypatch.setattr(cdc, "count_commits_ahead", lambda rev, sha: 2)
        assert cdc.main() == 0, "矩阵基线落后 2 commit（窗口边界）应通过"

        # 落后 3 个 commit → 失败
        monkeypatch.setattr(cdc, "count_commits_ahead", lambda rev, sha: 3)
        assert cdc.main() == 1, "矩阵基线落后 3 commit 应失败"
