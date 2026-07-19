"""ref/ 目录隔离架构守护测试（CHANGE-20260718-004）。

验证 `ref/` 目录彻底隔离规则（AGENTS clause 59/60）：

1. 生产代码、工具脚本禁止在运行时 `import`/`open`/`read`/`glob` `ref/` 目录
   （`ref/` 仅允许人工阅读，非运行依赖）。
2. 测试代码（排除 fixtures）禁止 `open`/`read`/`glob` `ref/` 目录
   （fixtures 下的 README.md/CSV 等数据文件可引用 ref 路径作为人工说明）。
3. `docs/current/*.md` + `docs/maps/*.md` + `AGENTS.md` 不得把 `ref/` 文件
   称为"真源"、"运行依赖"、"fixture 生成器"；可称为"参考源（人工阅读）"
   或"历史路径"。
4. `git ls-files ref/` 不得包含 `ref/smc_user_export.pine`
   （CHANGE-20260718-004 已 `git rm --cached`）。
5. `docs/changes/records/*.md` + `docs/archive/**` 允许保留历史 ref 事实
   （作为不可变历史证据，不作为当前权威）。

算法真源必须是生产代码（`smc_pine_core.py`、`node_cluster_engine.py`、
`indicator_contract.py`、`indicator_semantics.py`），不是 `ref/` 下任何文件。
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

# 仓库根：backend/tests/test_ref_isolation.py -> parents[2] = repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "backend" / "app"
_TESTS_DIR = _REPO_ROOT / "backend" / "tests"
_TOOLS_DIR = _REPO_ROOT / "tools"
_DOCS_CURRENT_DIR = _REPO_ROOT / "docs" / "current"
_DOCS_MAPS_DIR = _REPO_ROOT / "docs" / "maps"
_DOCS_CHANGES_RECORDS_DIR = _REPO_ROOT / "docs" / "changes" / "records"
_DOCS_ARCHIVE_DIR = _REPO_ROOT / "docs" / "archive"
_AGENTS_FILE = _REPO_ROOT / "AGENTS.md"

# 测试 fixtures 目录（可能含 ref 路径作为人工说明，不参与运行时门禁）
_FIXTURES_DIR = _TESTS_DIR / "fixtures"

# 文本扫描时豁免的安全模式（出现即不视为违规）
# - 禁令形式：称为 —— "把 X 称为 Y" 是描述禁令本身（不是 claim）
# - 正确角色：参考源/人工阅读/非运行依赖 —— 显式使用正确术语
# - 历史上下文：历史路径 —— 显式标注为历史
# - 文件状态：git rm/不再纳入 git 跟踪 —— 描述文件移除
# - 测试描述：扫描禁止 —— 描述本测试自身的扫描规则
# 注意：不再使用宽泛的"禁止/不得/不作为/派生/迁移"等（它们可能描述行的其他部分，
# 不针对 ref/ claim，会导致 false negative）。
_SAFE_PATTERNS = (
    "称为",
    "参考源",
    "人工阅读",
    "非运行依赖",
    "历史路径",
    "git rm",
    "不再纳入 git 跟踪",
    "扫描禁止",
    "文本扫描禁止",
)

# 声明类关键词（出现在 ref/ 附近窗口内即视为"称 ref/ 为 ..."）
_CLAIM_KEYWORDS = ("真源", "运行依赖", "fixture 生成器", "fixture生成器")

# 特定禁止术语（行内出现即视为 claim，不论是否在 ref/ 窗口内）
# - "Pine 真源" —— 历史条款常用，必须改为"Pine 参考源"
# - "视觉真源" —— 视觉设计包应称"视觉参考源"
_PROHIBITED_TERMS = ("Pine 真源", "视觉真源")

# ref/ 字符串字面量前后扫描窗口大小（字符数）
# "真源" 出现在 ref/ 前 60 字符内或后 80 字符内即视为同窗口
_WINDOW_BEFORE = 60
_WINDOW_AFTER = 80

# 禁止术语（"Pine 真源"/"视觉真源"）的局部安全模式检查窗口
# safe pattern 必须在禁止术语前 40 字符或后 40 字符内才能豁免
# （避免"历史路径"出现在行尾却豁免行首的"Pine 真源"）
_PROHIBITED_TERM_WINDOW = 40


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _iter_python_files(*roots: Path, exclude: set[Path] | None = None) -> list[Path]:
    """枚举 roots 下所有 .py 文件（排除 __pycache__ 和 exclude 集合）。"""
    exclude_resolved = {p.resolve() for p in (exclude or ())}
    result: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.py")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts:
                continue
            if p.resolve() in exclude_resolved:
                continue
            result.append(p)
    return result


def _string_literal(value: object) -> str | None:
    """若 AST 节点是字符串字面量，返回其值；否则 None。"""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _is_ref_path_string(s: str) -> bool:
    """字符串是否引用 ref/ 目录（如 'ref/foo'、'./ref/bar'、'/abs/ref/baz'）。"""
    if not s:
        return False
    # 标准化：去前导 ./ 和绝对路径前缀，只看是否含 ref/ 段
    normalized = s.lstrip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    # 检查是否以 ref/ 开头或含 /ref/（避免误报 'reference' 等）
    if normalized.startswith("ref/"):
        return True
    if "/ref/" in normalized:
        return True
    return False


def _ast_imports_ref(tree: ast.AST) -> list[str]:
    """检查 AST 是否含 `import ref...` 或 `from ref... import ...`，返回违规描述列表。"""
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "ref" or alias.name.startswith("ref."):
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # node.module 可能为 None（相对导入），但 ref 是顶级包不会用相对导入
            if node.module and (node.module == "ref" or node.module.startswith("ref.")):
                violations.append(f"from {node.module} import ...")
    return violations


def _ast_opens_ref_path(tree: ast.AST) -> list[str]:
    """检查 AST 是否含 open('ref/...') / Path('ref/...') / glob('ref/...') 等调用。

    检测模式：
    - open(<str literal>) / io.open(<str literal>) 中字符串引用 ref/
    - Path(<str literal>) 中字符串引用 ref/
    - *.glob(<str literal>) / *.rglob(<str literal>) 中字符串引用 ref/
    - *.read_text(<str literal>) / *.write_text(...) 不检查（参数不是路径）
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # open(...) / io.open(...) 调用
        if isinstance(func, ast.Name) and func.id == "open":
            if node.args and (s := _string_literal(node.args[0])) and _is_ref_path_string(s):
                violations.append(f"open({s!r})")
        # Path(...) 调用 —— 仅当首参是 ref/ 字符串字面量
        elif isinstance(func, ast.Name) and func.id == "Path":
            if node.args and (s := _string_literal(node.args[0])) and _is_ref_path_string(s):
                violations.append(f"Path({s!r})")
        # *.glob(...) / *.rglob(...) / *.open(...) 调用
        elif isinstance(func, ast.Attribute) and func.attr in {"glob", "rglob", "open"}:
            if node.args and (s := _string_literal(node.args[0])) and _is_ref_path_string(s):
                violations.append(f".{func.attr}({s!r})")
    return violations


def _is_problematic_ref_claim(line: str) -> bool:
    """检查一行 markdown 是否把 ref/ 文件称为真源/运行依赖/fixture 生成器。

    判定（任一命中即视为 claim）：
    - 行含 `ref/` AND 行含特定禁止术语（"Pine 真源"/"视觉真源"）
      AND 禁止术语附近（前后 40 字符）无安全模式
    - OR 行含 `ref/` AND 在某个 `ref/` 出现位置的前后窗口内含声明关键词
      （真源/运行依赖/fixture 生成器） AND 整行不含安全模式

    安全模式：
    - 称为（禁令形式：把 X 称为 Y）
    - 参考源/人工阅读/非运行依赖（正确角色）
    - 历史路径（历史上下文）
    - git rm/不再纳入 git 跟踪（文件状态）
    - 扫描禁止/文本扫描禁止（测试描述自身）

    安全模式用于豁免禁令说明本身（如 clause 59 描述"不得称为真源"）。
    对禁止术语（"Pine 真源"/"视觉真源"）使用局部窗口检查，避免行尾的
    "历史路径"豁免行首的禁止术语。
    """
    if "ref/" not in line:
        return False

    # 检查特定禁止术语（"Pine 真源"/"视觉真源"）
    # safe pattern 必须在禁止术语附近（前后 _PROHIBITED_TERM_WINDOW 字符内）
    for term in _PROHIBITED_TERMS:
        idx = 0
        while True:
            pos = line.find(term, idx)
            if pos < 0:
                break
            window_start = max(0, pos - _PROHIBITED_TERM_WINDOW)
            window_end = min(len(line), pos + len(term) + _PROHIBITED_TERM_WINDOW)
            local_window = line[window_start:window_end]
            if not any(kw in local_window for kw in _SAFE_PATTERNS):
                return True  # 禁止术语附近无安全模式 → 违规
            idx = pos + len(term)

    # 检查通用声明关键词（真源/运行依赖/fixture 生成器）—— 必须在 ref/ 窗口内
    # 整行检查安全模式（通用 claim 较弱，用整行豁免）
    if any(kw in line for kw in _SAFE_PATTERNS):
        return False
    ref_positions: list[int] = []
    idx = 0
    while True:
        pos = line.find("ref/", idx)
        if pos < 0:
            break
        ref_positions.append(pos)
        idx = pos + 4
    for pos in ref_positions:
        start = max(0, pos - _WINDOW_BEFORE)
        end = min(len(line), pos + _WINDOW_AFTER)
        window = line[start:end]
        if any(kw in window for kw in _CLAIM_KEYWORDS):
            return True
    return False


def _collect_docs_to_check() -> list[Path]:
    """收集需要做 ref 隔离文本扫描的文档（current + maps + AGENTS.md）。

    历史目录（docs/changes/records/、docs/archive/）不参与扫描，
    允许保留历史 ref 事实作为不可变历史证据。
    """
    files: list[Path] = []
    if _DOCS_CURRENT_DIR.exists():
        files.extend(sorted(p for p in _DOCS_CURRENT_DIR.glob("*.md") if p.is_file()))
    if _DOCS_MAPS_DIR.exists():
        files.extend(sorted(p for p in _DOCS_MAPS_DIR.glob("*.md") if p.is_file()))
    if _AGENTS_FILE.exists():
        files.append(_AGENTS_FILE)
    return files


# ---------------------------------------------------------------------------
# 测试 1：生产代码 + 工具脚本禁止运行时 import/open/read/glob ref/
# ---------------------------------------------------------------------------


def test_no_production_module_imports_or_reads_ref() -> None:
    """生产代码和工具脚本禁止在运行时 import/open/read/glob ref/ 目录。

    `ref/` 仅允许人工阅读；生产代码（backend/app/**/*.py）和工具脚本
    （tools/**/*.py）不得在运行时通过 import、open、Path、glob 等方式
    访问 ref/ 目录下任何文件。

    例外：本测试文件本身（test_ref_isolation.py）需要扫描 ref/ 路径，
    但仅做字符串模式匹配，不实际打开 ref/ 文件。
    """
    files = _iter_python_files(_APP_DIR, _TOOLS_DIR)
    violations: list[str] = []
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            violations.append(f"{p.relative_to(_REPO_ROOT)}: 语法错误 {e}")
            continue
        rel = p.relative_to(_REPO_ROOT)
        for v in _ast_imports_ref(tree):
            violations.append(f"{rel}: {v}")
        for v in _ast_opens_ref_path(tree):
            violations.append(f"{rel}: {v}")
    assert not violations, (
        "违反 ref/ 隔离（AGENTS clause 59）：生产代码/工具在运行时访问 ref/ 目录:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 测试 2：测试代码（排除 fixtures）禁止运行时 open/read/glob ref/
# ---------------------------------------------------------------------------


def test_no_test_module_reads_ref_at_runtime() -> None:
    """测试代码（排除 fixtures）禁止运行时 open/read/glob ref/ 目录。

    fixtures 目录（backend/tests/fixtures/）下的 README.md/CSV 等数据文件
    可引用 ref/ 路径作为人工说明（不参与运行时门禁）；其他测试模块不得
    在运行时通过 open、Path、glob 等方式访问 ref/ 目录。

    SMC 算法真源是生产代码 `smc_pine_core.py`，测试不应从 ref/ 读取输入。
    """
    files = _iter_python_files(_TESTS_DIR, exclude={_FIXTURES_DIR})
    violations: list[str] = []
    for p in files:
        # 跳过本测试文件自身（其字符串模式匹配不实际打开 ref/）
        if p.name == "test_ref_isolation.py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            violations.append(f"{p.relative_to(_REPO_ROOT)}: 语法错误 {e}")
            continue
        rel = p.relative_to(_REPO_ROOT)
        # 测试也禁止 import ref（同样违反隔离）
        for v in _ast_imports_ref(tree):
            violations.append(f"{rel}: {v}")
        for v in _ast_opens_ref_path(tree):
            violations.append(f"{rel}: {v}")
    assert not violations, (
        "违反 ref/ 隔离（AGENTS clause 59）：测试代码在运行时访问 ref/ 目录:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 测试 3：current/maps/AGENTS 文档不得把 ref/ 称为真源/运行依赖
# ---------------------------------------------------------------------------


def test_docs_current_no_ref_runtime_dependency() -> None:
    """current/maps/AGENTS 文档不得把 ref/ 文件称为真源/运行依赖/fixture 生成器。

    允许的表述：参考源（人工阅读）、历史路径、派生文件、git rm 状态等。
    禁止的表述：真源 `ref/...`、ref/ 作为运行依赖、ref/ 是 fixture 生成器 等。

    历史目录（docs/changes/records/、docs/archive/）允许保留历史 ref 事实
    （由 test_changes_archive_history_allowed 显式豁免）。
    """
    files = _collect_docs_to_check()
    violations: list[str] = []
    for p in files:
        content = p.read_text(encoding="utf-8")
        rel = p.relative_to(_REPO_ROOT)
        for i, line in enumerate(content.splitlines(), start=1):
            if _is_problematic_ref_claim(line):
                violations.append(f"{rel}:{i}: {line.strip()[:200]}")
    assert not violations, (
        "违反 ref/ 隔离（AGENTS clause 59(2)）：文档把 ref/ 称为真源/运行依赖/"
        "fixture 生成器（应改用'参考源（人工阅读）'或'历史路径'）:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 测试 4：git ls-files ref/ 不得包含 smc_user_export.pine
# ---------------------------------------------------------------------------


def test_no_smc_user_export_in_git() -> None:
    """git ls-files ref/ 不得包含 ref/smc_user_export.pine。

    CHANGE-20260718-004 已 `git rm --cached ref/smc_user_export.pine`，
    该文件不再纳入 git 跟踪（.gitignore 的 ref/ 规则自动忽略）。
    ref/smc_user_source.pine（用户原创 Pine 源码，SHA256 0bd3d2ad，843 行）
    保留 git 跟踪（clause 46 明确要求，git add -f 例外）。
    """
    result = subprocess.run(
        ["git", "ls-files", "ref/"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest_fail_msg = (
            f"git ls-files ref/ 失败 (returncode={result.returncode}): "
            f"{result.stderr.strip()}"
        )
        raise AssertionError(pytest_fail_msg)

    tracked_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    forbidden = "ref/smc_user_export.pine"
    assert forbidden not in tracked_files, (
        f"违反 ref/ 隔离（AGENTS clause 59(3)）：{forbidden} 仍在 git 跟踪中。"
        f"CHANGE-20260718-004 已 git rm --cached。当前 git ls-files ref/ = {tracked_files}"
    )

    # 正向断言：ref/smc_user_source.pine 必须保留 git 跟踪
    required = "ref/smc_user_source.pine"
    assert required in tracked_files, (
        f"违反 AGENTS clause 46：{required} 必须保留 git 跟踪（git add -f 例外），"
        f"当前 git ls-files ref/ = {tracked_files}"
    )


# ---------------------------------------------------------------------------
# 测试 5：历史目录（changes/records + archive）允许保留历史 ref 事实
# ---------------------------------------------------------------------------


def test_changes_archive_history_allowed() -> None:
    """历史目录（docs/changes/records/、docs/archive/）允许保留历史 ref 事实。

    这测试文档化策略：历史 CHANGE record 和 archive 中的旧文档可以引用
    旧 ref 路径、SHA256 记录等，作为不可变历史证据，不作为当前权威。
    当前权威以 docs/current/ 和 docs/maps/ 为准（由 test 3 守护）。

    本测试通过断言 _collect_docs_to_check() 不包含历史目录来验证豁免策略。
    """
    files = _collect_docs_to_check()

    # 验证历史目录不在扫描集中
    for p in files:
        # 解析后的路径不得落在 changes/records/ 或 archive/ 下
        try:
            p.relative_to(_DOCS_CHANGES_RECORDS_DIR)
            raise AssertionError(
                f"历史目录 {p} 不应在 docs 扫描集中（应豁免）"
            )
        except ValueError:
            pass  # 不在 changes/records 下，OK
        try:
            p.relative_to(_DOCS_ARCHIVE_DIR)
            raise AssertionError(
                f"历史目录 {p} 不应在 docs 扫描集中（应豁免）"
            )
        except ValueError:
            pass  # 不在 archive 下，OK

    # 正向断言：changes/records/ 和 archive/ 目录若存在，则允许引用 ref 事实
    # （这里仅验证它们不被 test 3 扫描，不强制要求其存在）
    if _DOCS_CHANGES_RECORDS_DIR.exists():
        records_files = list(_DOCS_CHANGES_RECORDS_DIR.glob("*.md"))
        # 历史 record 文件允许引用 ref/（不参与 test 3 扫描）
        # 此处仅记录数量，不做内容断言
        assert isinstance(records_files, list)

    if _DOCS_ARCHIVE_DIR.exists():
        archive_files = list(_DOCS_ARCHIVE_DIR.rglob("*.md"))
        assert isinstance(archive_files, list)
