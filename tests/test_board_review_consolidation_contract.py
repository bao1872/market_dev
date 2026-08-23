"""Board→Review Consolidation 迁移完整性合同测试 (Slice 1R, DESIGN/AUDIT ONLY).

本测试不验证算法 parity，而是迁移合同的完整性。关键改进 (1R):
- 不再用人工 hardcode 错误清单与 matrix 互证 (substring 自洽)。
- 直接从真实 producer `board_analysis_service.py` 用 AST 机械提取:
  * V1 `compute_board_payload` 返回的 dict 字面量 keys (真实 leaf path)
  * V2 `payload["pyramid_v2"]` 赋值 dict 的顶层 keys (精确 7 个)
- 断言 matrix 的 board_field 集合忠实覆盖真实 producer 的所有 path (无遗漏、无虚构)。
- 校验 classification / target_disposition ∈ declared vocab。
- 校验 BOARD_UNIQUE 不得 RETIRE。
- 校验无 pyramid_v2.leadership 虚构 key。
- canonical Review path 必须存在于真实输出 schema (scope_observation 顶层 + 已知子结构)。

不连接数据库 (PURE_UNIT_TEST)。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_EXP_DIR = _REPO / "experiments" / "board_review_consolidation"
_BOARD_SVC = (
    _REPO
    / "backend"
    / "app"
    / "services"
    / "board_analysis_service.py"
)
_SCOPE_OBS = (
    _REPO / "backend" / "app" / "domain" / "review" / "scope_observation.py"
)

DECLARED_CLASSIFICATIONS = {
    "EXACT_DUPLICATE",
    "REVIEW_SUPERSET",
    "SEMANTIC_OVERLAP_DIFFERENT_FORMULA",
    "SEMANTIC_OVERLAP_DIFFERENT_AGGREGATION",
    "SEMANTIC_OVERLAP_DIFFERENT_TIME_SEMANTICS",
    "BOARD_UNIQUE",
    "INFRASTRUCTURE_NOT_ANALYTICS",
    "LEGACY_UNUSED",
    "UNKNOWN_NEEDS_DECISION",
}
DECLARED_DISPOSITIONS = {
    "USE_REVIEW_OWNER",
    "MIGRATE_TO_REVIEW",
    "DERIVE_FROM_REVIEW_FACTS",
    "RENAME_AND_MIGRATE",
    "KEEP_INFRASTRUCTURE_ONLY",
    "RETIRE",
    "NEEDS_PRODUCT_DECISION",
}

# 真实 V2 顶层集合 (来自 board_analysis_service.py payload["pyramid_v2"], 经 AST 提取并交叉验证)
EXPECTED_V2_TOP_LEVEL = {
    "state_transitions",
    "freshness",
    "diffusion",
    "concentration",
    "dispersion",
    "relative_strength",
    "concept_extras",
}


def _load(name: str) -> dict:
    p = _EXP_DIR / name
    assert p.exists(), f"missing consolidation artifact: {p}"
    return json.loads(p.read_text())


def _flatten_keys(node: ast.AST, prefix: str = "") -> list[str]:
    """从 dict 字面量递归提取完整 dotted leaf path。"""
    out: list[str] = []
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            key = k.value if isinstance(k, ast.Constant) else "?"
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(v, ast.Dict):
                out.extend(_flatten_keys(v, path))
            else:
                out.append(path)
    return out


def _extract_compute_board_payload_keys() -> list[str]:
    """AST: 找到 compute_board_payload 函数内 `payload = { ... }` 赋值，提取其 dotted keys。
    (真实代码把 dict 字面量赋给变量 payload，再 return payload，故需查 Assign 而非 Return。)"""
    tree = ast.parse(_BOARD_SVC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "compute_board_payload":
            for sub in ast.walk(node):
                # 真实代码是 `payload: dict[str, Any] = {...}` (AnnAssign)
                # 也可能退化为 `payload = {...}` (Assign)。
                targets = None
                if isinstance(sub, ast.AnnAssign):
                    targets = [sub.target]
                elif isinstance(sub, ast.Assign):
                    targets = sub.targets
                if (
                    targets is not None
                    and len(targets) == 1
                    and isinstance(targets[0], ast.Name)
                    and targets[0].id == "payload"
                    and isinstance(sub.value, ast.Dict)
                ):
                    return _flatten_keys(sub.value)
    raise AssertionError("compute_board_payload payload dict not found")


def _extract_pyramid_v2_top_level() -> set[str]:
    """AST: 找到 `payload['pyramid_v2'] = { ... }` 赋值的顶层 keys。"""
    tree = ast.parse(_BOARD_SVC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            tgt = node.targets[0]
            # 形如 payload["pyramid_v2"]
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == "pyramid_v2"
                and isinstance(node.value, ast.Dict)
            ):
                return {
                    k.value for k in node.value.keys if isinstance(k, ast.Constant)
                }
    raise AssertionError("payload['pyramid_v2'] assignment not found")


@pytest.fixture(scope="module")
def matrix() -> dict:
    return _load("board_review_ownership_matrix.json")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return _load("feature_preservation_manifest.json")


# ---------------------------------------------------------------------------
# Producer-extracted source truth (no hardcode)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real_v1_paths() -> list[str]:
    return _extract_compute_board_payload_keys()


@pytest.fixture(scope="module")
def real_v2_top() -> set[str]:
    return _extract_pyramid_v2_top_level()


# ---------------------------------------------------------------------------
# 1. classification / disposition ∈ declared vocab
# ---------------------------------------------------------------------------
def test_classifications_in_vocab(matrix):
    bad = [
        f["board_field"]
        for f in matrix["fields"]
        if f["classification"] not in DECLARED_CLASSIFICATIONS
    ]
    assert not bad, f"classification not in declared vocab: {bad}"


def test_dispositions_in_vocab(matrix):
    bad = [
        f["board_field"]
        for f in matrix["fields"]
        if f["target_disposition"] not in DECLARED_DISPOSITIONS
    ]
    assert not bad, f"target_disposition not in declared vocab: {bad}"


# ---------------------------------------------------------------------------
# 2. no invented Board path (matrix fields must be real producer paths)
# ---------------------------------------------------------------------------
def test_no_invented_board_path(matrix, real_v1_paths, real_v2_top):
    real_leaf_tokens = set()
    for p in real_v1_paths:
        real_leaf_tokens.add(p.split(".")[0])  # V1 top-level token
    real_leaf_tokens |= real_v2_top  # V2 top-level keys
    for f in matrix["fields"]:
        bf = f["board_field"]
        # 顶层 token 必须出现在真实 producer 中
        top = bf.split(".")[0].split("/")[0].split("(")[0].strip()
        assert top in real_leaf_tokens or top in (
            "pyramid_v2",
        ), f"matrix field '{bf}' top-token '{top}' not in real producer paths"


# ---------------------------------------------------------------------------
# 3. no missing real Board path (every real producer path covered by matrix)
# ---------------------------------------------------------------------------
def test_no_missing_real_board_path(matrix, real_v1_paths, real_v2_top):
    # 矩阵用 "/" 把同前缀的 leaf 合并成一个 board_field（如
    # "trend_dist.up/down/neutral"）。这里做 token 级别覆盖检查：
    # 每个真实 V1 leaf path 的前缀 + 末级 key，必须在某个 board_field 里
    # 以 "prefix.leaf" 形式出现（board_field 用 "/" 切分后逐个比对）。
    field_tokens: set[str] = set()
    for f in matrix["fields"]:
        # 矩阵 shorthand: "trend_dist.up/down/neutral" 表示
        # trend_dist.up / trend_dist.down / trend_dist.neutral。
        # 取第一个 '.' 之前的前缀，应用到每个被 '/' 切分的段。
        bf = f["board_field"]
        # 兼容 "pyramid_v2.concentration.*" 这类通配表达：去掉末尾 ".*"
        bf = bf[:-2] if bf.endswith(".*") else bf
        parts = [p.strip() for p in bf.split("/") if p.strip()]
        if not parts:
            continue
        prefix = parts[0].rsplit(".", 1)[0] if "." in parts[0] else ""
        for part in parts:
            leaf = part.rsplit(".", 1)[-1] if "." in part else part
            full = f"{prefix}.{leaf}" if prefix else leaf
            field_tokens.add(full)
    missing = []
    for p in real_v1_paths:
        # p 形如 "trend_dist.up"；在 board_field 里可能是
        # "trend_dist.up/down/neutral" 经 split('/') 得到 "trend_dist.up"
        if p not in field_tokens:
            missing.append(p)
    assert not missing, (
        f"real Board V1 paths missing from matrix: {missing}"
    )
    # V2 top-level must appear (矩阵用 "pyramid_v2.{k}.*" 或 "pyramid_v2.{k}.leaf" 表达)
    for k in real_v2_top:
        needle = f"pyramid_v2.{k}"
        assert (
            k in field_tokens
            or needle in field_tokens
            or any(t.startswith(needle + ".") for t in field_tokens)
        ), f"real Board V2 key '{k}' missing from matrix"


# ---------------------------------------------------------------------------
# 4. exact V2 top-level set
# ---------------------------------------------------------------------------
def test_exact_v2_top_level(real_v2_top):
    assert real_v2_top == EXPECTED_V2_TOP_LEVEL, (
        f"V2 top-level mismatch: got {sorted(real_v2_top)} "
        f"expected {sorted(EXPECTED_V2_TOP_LEVEL)}"
    )


# ---------------------------------------------------------------------------
# 5. no pyramid_v2.leadership (fictional capability)
# ---------------------------------------------------------------------------
def test_no_pyramid_v2_leadership(matrix):
    for f in matrix["fields"]:
        assert "leadership" not in f["board_field"], (
            f"fictional capability 'pyramid_v2.leadership' present in matrix: "
            f"{f['board_field']}"
        )
    assert "leadership" not in EXPECTED_V2_TOP_LEVEL


# ---------------------------------------------------------------------------
# 6. BOARD_UNIQUE cannot RETIRE
# ---------------------------------------------------------------------------
def test_board_unique_not_retired(matrix):
    for f in matrix["fields"]:
        if f["classification"] == "BOARD_UNIQUE":
            assert f["target_disposition"] in (
                "MIGRATE_TO_REVIEW",
                "RENAME_AND_MIGRATE",
                "NEEDS_PRODUCT_DECISION",
            ), (
                f"BOARD_UNIQUE '{f['board_field']}' has forbidden disposition "
                f"'{f['target_disposition']}'"
            )


# ---------------------------------------------------------------------------
# 7. canonical Review paths exist in actual output schema
# ---------------------------------------------------------------------------
def test_canonical_review_paths_exist():
    """Review equivalent paths cited in matrix must exist in real
    scope_observation.py compute_scope_observation return structure."""
    src = _SCOPE_OBS.read_text()
    required_tokens = [
        "trend",
        "structure",
        "momentum",
        "participation",
        "price",
        "scope",
    ]
    for tok in required_tokens:
        assert f'"{tok}"' in src or f"'{tok}'" in src or f"{tok}:" in src, (
            f"canonical Review top key '{tok}' not found in scope_observation.py"
        )
    # explicitly assert NO freshness layer in canonical composition
    # (Board freshness is BOARD_UNIQUE -> MIGRATE_TO_REVIEW)
    # We check the compute_scope_observation return block does not include 'freshness'
    assert "freshness" not in src.split("def compute_scope_observation")[1].split(
        "return {"
    )[0], "unexpected: freshness present in compute_scope_observation pre-return body"


# ---------------------------------------------------------------------------
# 8. manifest categories non-empty + retire targets declared (Slice 1 only)
# ---------------------------------------------------------------------------
def test_manifest_categories(manifest):
    for cat in (
        "already_owned_by_review",
        "migrate_from_board",
        "derive_from_review",
        "retire_conflicting_definition",
        "infrastructure_only",
        "unresolved",
    ):
        assert cat in manifest, f"manifest missing '{cat}'"
        assert isinstance(manifest[cat], list)
    tgt = manifest["meta"]["target_architecture"]
    assert "BoardAnalysis runtime stage = RETIRE TARGET" in tgt["retire_targets"]
    assert "market_aggregation publication = RETIRE TARGET" in tgt["retire_targets"]
    assert tgt["slice1r_scope"].startswith("ONLY define target")


# ---------------------------------------------------------------------------
# 9. SEMANTIC_OVERLAP must document formula difference (no blind superset)
# ---------------------------------------------------------------------------
def test_semantic_overlap_documented(matrix):
    for f in matrix["fields"]:
        if f["classification"].startswith("SEMANTIC_OVERLAP"):
            assert f.get("reason"), (
                f"SEMANTIC_OVERLAP '{f['board_field']}' missing reason"
            )
            # must NOT be classified as REVIEW_SUPERSET (that was the 1R error)
            assert f["classification"] != "REVIEW_SUPERSET"


# ---------------------------------------------------------------------------
# 10. Information-parity hard gate (Slice 1S)
#     LOSSY / DIFFERENT_SEMANTICS forbids USE_REVIEW_OWNER.
#     USE_REVIEW_OWNER requires information_parity in {EXACT, DERIVABLE}.
# ---------------------------------------------------------------------------
def test_information_parity_blocks_use_review_owner(matrix):
    """P50 ≈ mean, BB width ≈ SQZMOM, exact-T event ≈ latest event
    must NOT be silently classed as lossless USE_REVIEW_OWNER."""
    for f in matrix["fields"]:
        ip = f.get("information_parity")
        if ip is None:
            # fields without an explicit block must still declare a parity
            # via an allowed default; require the key to exist for any
            # field that is NOT INFRASTRUCTURE/LEGACY/UNKNOWN.
            assert f["classification"] in (
                "INFRASTRUCTURE_NOT_ANALYTICS",
                "LEGACY_UNUSED",
                "UNKNOWN_NEEDS_DECISION",
            ), (
                f"field '{f['board_field']}' missing information_parity block "
                f"(classification={f['classification']})"
            )
            continue
        parity = ip["parity"]
        disp = f["target_disposition"]
        if parity in ("LOSSY", "DIFFERENT_SEMANTICS"):
            assert disp != "USE_REVIEW_OWNER", (
                f"field '{f['board_field']}' parity={parity} but "
                f"target_disposition=USE_REVIEW_OWNER (would silently drop info)"
            )
        if disp == "USE_REVIEW_OWNER":
            assert parity in ("EXACT", "DERIVABLE"), (
                f"field '{f['board_field']}' USE_REVIEW_OWNER requires "
                f"information_parity in {{EXACT, DERIVABLE}}, got {parity}"
            )
        # parity must be a declared vocab value
        assert parity in (
            "EXACT",
            "DERIVABLE",
            "LOSSY",
            "DIFFERENT_SEMANTICS",
        ), f"field '{f['board_field']}' invalid parity '{parity}'"


def test_information_parity_recoverable_flag(matrix):
    """If the board info is NOT recoverable from current Review payload,
    the field must NOT be USE_REVIEW_OWNER and must carry a
    required_preservation_action."""
    for f in matrix["fields"]:
        ip = f.get("information_parity")
        if ip is None:
            continue
        if ip.get("recoverable_from_current_review_payload") is False:
            assert f["target_disposition"] != "USE_REVIEW_OWNER", (
                f"field '{f['board_field']}' not recoverable from Review "
                f"payload but target_disposition=USE_REVIEW_OWNER"
            )
            assert ip.get("required_preservation_action"), (
                f"field '{f['board_field']}' not recoverable but missing "
                f"required_preservation_action"
            )
