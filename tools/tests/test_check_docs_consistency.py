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

import check_docs_consistency as cdc  # noqa: E402

# 测试用合法 SHA（40 位 hex，非真实提交，通过 mock 通过 git 校验）
VALID_SHA = "a" * 40
ALT_SHA = "b" * 40


def _manifest_content(sha: str = VALID_SHA) -> str:
    """生成合法的 v2 MANIFEST.md 内容（含全局基线字段）。"""
    return (
        "# Current Docs Manifest\n\n"
        "> 文档状态：CURRENT DESIGN BASELINE  \n"
        f"> 实现核对基线：`{sha}`  \n"
        "> 设计基线日期：2026-07-03  \n"
        "> 注意：该文件是 v2 唯一基线头；其他 current 文档不再重复基线字段。\n\n"
        "## 1. 文档状态定义\n\n"
        "| 状态 | 含义 |\n|---|---|\n| CURRENT | 当前确认采用的设计 |\n"
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

    Args:
        tmp_path: 临时目录
        monkeypatch: pytest monkeypatch
        manifest: docs/current/MANIFEST.md 内容；None 则不创建
        current_docs: {文件名: 内容} 字典，创建 docs/current/ 下的其他文档
        maps_docs: {文件名: 内容} 字典，创建 docs/maps/ 下的文档
        archive_docs: {文件名: 内容} 字典，创建 docs/archive/current-legacy-20260703/ 下的文档
        readme: docs/README.md 内容
        agents: AGENTS.md 内容

    Returns:
        tmp_path（作为 REPO_ROOT）

    说明：自动创建规则 13/15 要求的必需文件（08-indicator-calculation-contracts.md、
    indicator-computation-map.md、CHANGE-20260718-004.md、CHANGELOG.md），
    使 "passes" 场景在新规则下仍返回 rc==0。
    """
    docs_dir = tmp_path / "docs"
    current_dir = docs_dir / "current"
    maps_dir = docs_dir / "maps"
    archive_dir = docs_dir / "archive" / "current-legacy-20260703"
    changes_dir = docs_dir / "changes"
    records_dir = changes_dir / "records"
    current_dir.mkdir(parents=True, exist_ok=True)
    maps_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    records_dir.mkdir(parents=True, exist_ok=True)

    if readme is not None:
        (docs_dir / "README.md").write_text(readme, encoding="utf-8")

    if manifest is not None:
        (current_dir / "MANIFEST.md").write_text(manifest, encoding="utf-8")

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

    # 规则 13 必需新文档（若测试未显式提供则创建最小内容）
    _required_current = "08-indicator-calculation-contracts.md"
    _required_maps = "indicator-computation-map.md"
    if not (current_dir / _required_current).exists():
        (current_dir / _required_current).write_text(
            "# 指标计算合同\n\nNode Cluster 语义合同（测试占位）。\n",
            encoding="utf-8",
        )
    if not (maps_dir / _required_maps).exists():
        (maps_dir / _required_maps).write_text(
            "# 指标计算地图\n\n三链指标计算入口地图（测试占位）。\n",
            encoding="utf-8",
        )

    # 规则 15 必需 CHANGE 记录 + CHANGELOG 引用
    _change_id = "CHANGE-20260718-004"
    _record_file = records_dir / f"{_change_id}.md"
    if not _record_file.exists():
        _record_file.write_text(
            f"# {_change_id}\n\nNode Cluster 合同 + ref 隔离（测试占位）。\n",
            encoding="utf-8",
        )
    _changelog = changes_dir / "CHANGELOG.md"
    if not _changelog.exists():
        _changelog.write_text(
            f"# CHANGELOG\n\n- {_change_id}: Node Cluster 合同 + ref 隔离。\n",
            encoding="utf-8",
        )

    # 注入模块路径变量（v2 新增 MANIFEST_FILE 与 MAPS_DIR）
    monkeypatch.setattr(cdc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cdc, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(cdc, "CURRENT_DIR", current_dir)
    monkeypatch.setattr(cdc, "MANIFEST_FILE", current_dir / "MANIFEST.md")
    monkeypatch.setattr(cdc, "MAPS_DIR", maps_dir)
    monkeypatch.setattr(cdc, "ARCHIVE_DIR", archive_dir.parent)
    monkeypatch.setattr(cdc, "AGENTS_FILE", tmp_path / "AGENTS.md")

    # 默认 mock git 校验为通过
    monkeypatch.setattr(cdc, "is_valid_commit", lambda sha: True)
    monkeypatch.setattr(cdc, "is_ancestor_of_head", lambda sha: True)
    # 规则 16 默认 mock：baseline 在窗口内（既有 11 个场景不应因新规则失败）
    # 单独测试场景 12 会覆盖此 mock 触发失败
    monkeypatch.setattr(cdc, "count_commits_ahead_of_baseline", lambda sha: 10)

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
        """场景 2：MANIFEST 缺 baseline 字段失败。"""
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

        rc = cdc.main()
        assert rc == 1, "MANIFEST 缺 baseline 字段应失败"

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

        rc = cdc.main()
        assert rc == 1, "非 40 位 SHA 应失败"

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

        rc = cdc.main()
        assert rc == 1, "非真实 commit 的 SHA 应失败"

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

        rc = cdc.main()
        assert rc == 1, "非 HEAD 祖先的 SHA 应失败"

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

        rc = cdc.main()
        assert rc == 1, "baseline 落后 HEAD 88 个 commit（超过窗口 50）应失败"

        captured = capsys.readouterr()
        assert "严重落后" in captured.out, "错误信息应包含'严重落后'"
        assert "88" in captured.out, "错误信息应包含落后 commit 数量"

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
