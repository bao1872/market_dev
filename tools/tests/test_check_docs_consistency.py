"""tools/check_docs_consistency.py 治理规则测试。

覆盖 advice §8 第 1-10 条规则的 10 个反向/正向用例。
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


def _valid_header(sha: str = VALID_SHA) -> str:
    """生成合法的基线头部字段。"""
    return (
        "> 文档状态：CURRENT DESIGN BASELINE  \n"
        "> 设计基线日期：2026-07-03  \n"
        f"> 实现核对基线：{sha}  \n"
        "> 实现核对分支：main  \n"
        "> 最近一致性检查日期：2026-07-03  \n"
    )


def _setup_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_docs: dict[str, str] | None = None,
    readme: str | None = None,
) -> Path:
    """在 tmp_path 下创建 docs 结构并注入模块路径变量。

    Args:
        tmp_path: 临时目录
        monkeypatch: pytest monkeypatch
        current_docs: {文件名: 内容} 字典，创建 docs/current/ 下的文档
        readme: docs/README.md 内容

    Returns:
        tmp_path（作为 REPO_ROOT）
    """
    docs_dir = tmp_path / "docs"
    current_dir = docs_dir / "current"
    current_dir.mkdir(parents=True, exist_ok=True)

    if readme is not None:
        (docs_dir / "README.md").write_text(readme, encoding="utf-8")

    if current_docs:
        for name, content in current_docs.items():
            (current_dir / name).write_text(content, encoding="utf-8")

    # 注入模块路径变量
    monkeypatch.setattr(cdc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cdc, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(cdc, "CURRENT_DIR", current_dir)
    monkeypatch.setattr(cdc, "README_FILE", docs_dir / "README.md")
    monkeypatch.setattr(cdc, "AGENTS_FILE", tmp_path / "AGENTS.md")  # 不存在，跳过

    # 默认 mock git 校验为通过
    monkeypatch.setattr(cdc, "is_valid_commit", lambda sha: True)
    monkeypatch.setattr(cdc, "is_ancestor_of_head", lambda sha: True)

    return tmp_path


# ============================================================
# 测试用例（advice §8）
# ============================================================


class TestCheckDocsConsistency:
    """check_docs_consistency.py 10 条规则测试。"""

    def test_valid_unified_baseline_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 1/3/6：合法统一 baseline 通过。"""
        header = _valid_header()
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"00-test.md": header + "\n# 测试文档\n"},
            readme=header + "\n# README\n",
        )

        rc = cdc.main()
        assert rc == 0, f"合法 baseline 应通过，实际返回 {rc}"

    def test_invalid_sha_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 4：非法 SHA（非 40 位 hex）失败。"""
        # 38 位 hex，非 40 位
        short_sha = "a" * 38
        header = (
            "> 文档状态：CURRENT DESIGN BASELINE  \n"
            "> 设计基线日期：2026-07-03  \n"
            f"> 实现核对基线：{short_sha}  \n"
            "> 实现核对分支：main  \n"
            "> 最近一致性检查日期：2026-07-03  \n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"00-test.md": header + "\n# 测试\n"},
            readme=header + "\n# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "非法 SHA 应失败"

    def test_non_ancestor_sha_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 5：非祖先 SHA 失败。"""
        header = _valid_header()
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"00-test.md": header + "\n# 测试\n"},
            readme=header + "\n# README\n",
        )
        # mock is_ancestor_of_head 返回 False
        monkeypatch.setattr(cdc, "is_ancestor_of_head", lambda sha: False)

        rc = cdc.main()
        assert rc == 1, "非祖先 SHA 应失败"

    def test_missing_baseline_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 1/3：缺失 baseline 字段失败。"""
        # 只有部分字段，缺少 实现核对基线
        partial_header = (
            "> 文档状态：CURRENT DESIGN BASELINE  \n"
            "> 设计基线日期：2026-07-03  \n"
            "> 实现核对分支：main  \n"
            "> 最近一致性检查日期：2026-07-03  \n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"00-test.md": partial_header + "\n# 测试\n"},
            readme=partial_header + "\n# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "缺失 baseline 字段应失败"

    def test_inconsistent_baselines_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 6：baseline 不一致失败。"""
        header_a = _valid_header(VALID_SHA)
        header_b = _valid_header(ALT_SHA)
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={
                "00-a.md": header_a + "\n# A\n",
                "01-b.md": header_b + "\n# B\n",
            },
            readme=header_a + "\n# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "baseline 不一致应失败"

    def test_zero_matches_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 2：零匹配失败（有文档但无任何 baseline）。"""
        no_baseline_content = (
            "> 文档状态：DRAFT  \n"
            "> 无基线字段  \n"
            "\n# 测试文档\n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"00-test.md": no_baseline_content},
            readme=no_baseline_content,
        )

        rc = cdc.main()
        assert rc == 1, "零匹配应失败"

    def test_broken_local_link_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 9：失效本地链接失败。"""
        header = _valid_header()
        # 链接到不存在的文件
        content_with_bad_link = header + "\n# 测试\n\n[不存在](nonexistent.md)\n"
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"00-test.md": content_with_bad_link},
            readme=header + "\n# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "失效本地链接应失败"

    def test_placeholder_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 9：待填写占位符失败。"""
        header = _valid_header()
        content_with_placeholder = header + "\n# 测试\n\n这里是待填写内容\n"
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"00-test.md": content_with_placeholder},
            readme=header + "\n# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "待填写占位符应失败"

    def test_webhook_current_solution_regresses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 7：feishu_webhook 作为当前方案（非删除语境）失败。"""
        header = _valid_header()
        # feishu_webhook 出现但无删除语境关键词
        content_with_webhook = (
            header + "\n# 测试\n\n当前通知方式包括 feishu_webhook 和平台应用。\n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"00-test.md": content_with_webhook},
            readme=header + "\n# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "feishu_webhook 作为当前方案应失败"

    def test_webhook_vs_platform_app_open_regresses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """规则 8：Webhook vs Platform App 写成 OPEN 失败。"""
        header = _valid_header()
        # 17-open-decisions.md 中 Webhook + 仍需决定 且无 已决定
        open_content = (
            header
            + "\n# 17 未决设计问题\n\n"
            + "## OPEN-NOTIFY-001 飞书长期形态\n\n"
            + "仍需决定 Webhook 与平台应用的长期优先级。\n"
        )
        _setup_docs(
            tmp_path,
            monkeypatch,
            current_docs={"17-open-decisions.md": open_content},
            readme=header + "\n# README\n",
        )

        rc = cdc.main()
        assert rc == 1, "Webhook vs Platform App 写成 OPEN 应失败"
