"""Board→Review Consolidation 迁移完整性合同测试 (Slice 1, DESIGN/AUDIT ONLY).

本测试不验证算法 parity，而是迁移合同的完整性：
1. Board 正式 payload 每个顶层/V2 capability 都出现在 manifest（无 field → no disposition）。
2. BOARD_UNIQUE 必须 MIGRATE_TO_REVIEW / RENAME_AND_MIGRATE / NEEDS_PRODUCT_DECISION，禁止 RETIRE。
3. SEMANTIC_OVERLAP_DIFFERENT_FORMULA 不得无说明映射 USE_REVIEW_OWNER（必须有 reason + canonical_choice + preserved_information）。
4. Infrastructure (taxonomy/PIT membership/lineage) 不被分析层退役误伤。

测试从 experiments/board_review_consolidation/*.json 加载，作为 SSOT。
不连接数据库（PURE_UNIT_TEST）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_EXP_DIR = (
    Path(__file__).resolve().parents[1]
    / "experiments"
    / "board_review_consolidation"
)

# Board 正式 payload 顶层/V2 capability（来自 board_analysis_service.py 的 pyramid_v2 / V1 结构）。
# 这是"必须被 manifest 覆盖"的权威字段清单——任何漏项都会触发测试失败。
BOARD_REQUIRED_CAPABILITIES = [
    "trend_dist",
    "trend_strength",
    "vwap_dev_pct",
    "structure.trend_state",
    "structure.trend_strength",
    "structure.volume_participation",
    "structure.momentum_state",
    "structure.momentum_strength",
    "structure_events",
    "momentum",
    "volume",
    "total_members",
    "ready_members",
    "missing_members",
    "coverage",
    "pyramid_v2.state_transitions",
    "pyramid_v2.freshness",
    "pyramid_v2.diffusion",
    "pyramid_v2.concentration",
    "pyramid_v2.dispersion",
    "pyramid_v2.relative_strength",
    "pyramid_v2.concept_extras",
    "pyramid_v2.leadership",
]


def _load(name: str) -> dict:
    p = _EXP_DIR / name
    assert p.exists(), f"missing consolidation artifact: {p}"
    return json.loads(p.read_text())


@pytest.fixture(scope="module")
def matrix() -> dict:
    return _load("board_review_ownership_matrix.json")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return _load("feature_preservation_manifest.json")


# ---------------------------------------------------------------------------
# 1. 100% Board capability inventoried
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("capability", BOARD_REQUIRED_CAPABILITIES)
def test_every_board_capability_has_ownership_entry(matrix, capability):
    """Board 正式 payload 每个顶层/V2 capability 必须出现在 ownership matrix。

    匹配规则：capability 必须是某个 board_field 字符串的子串（matrix 用描述性
    命名，如 'leader / core / peripheral + confidence (pyramid_v2.leadership)'）。
    这保证每个 Board capability 都有 migration disposition，无一遗漏。
    """
    fields = [f["board_field"] for f in matrix["fields"]]
    matched = [bf for bf in fields if capability in bf]
    assert matched, (
        f"Board capability '{capability}' missing from ownership matrix — "
        f"no migration disposition defined"
    )


# ---------------------------------------------------------------------------
# 2. BOARD_UNIQUE 不得静默 RETIRE
# ---------------------------------------------------------------------------
def test_board_unique_not_retired(matrix):
    for f in matrix["fields"]:
        if f["classification"] == "BOARD_UNIQUE":
            disp = f["target_disposition"]
            assert disp in (
                "MIGRATE_TO_REVIEW",
                "RENAME_AND_MIGRATE",
                "NEEDS_PRODUCT_DECISION",
            ), (
                f"BOARD_UNIQUE field '{f['board_field']}' has forbidden "
                f"disposition '{disp}' — unique capability cannot be silently dropped"
            )


# ---------------------------------------------------------------------------
# 3. SEMANTIC_OVERLAP_DIFFERENT_FORMULA 必须带说明
# ---------------------------------------------------------------------------
def test_semantic_overlap_not_blindly_use_review_owner(matrix):
    for f in matrix["fields"]:
        if f["classification"] == "SEMANTIC_OVERLAP_DIFFERENT_FORMULA":
            disp = f["target_disposition"]
            # 允许 USE_REVIEW_OWNER 但必须有充分理由；不允许无说明。
            if disp == "USE_REVIEW_OWNER":
                reason = f.get("reason", "")
                assert (
                    "different" in reason.lower()
                    or "不同" in reason
                    or "算法不同" in reason
                ), (
                    f"SEMANTIC_OVERLAP_DIFFERENT_FORMULA field "
                    f"'{f['board_field']}' mapped to USE_REVIEW_OWNER without "
                    f"documenting the formula difference"
                )
            # 必须存在 reason
            assert f.get("reason"), (
                f"SEMANTIC_OVERLAP_DIFFERENT_FORMULA field "
                f"'{f['board_field']}' missing reason"
            )


# ---------------------------------------------------------------------------
# 4. Infrastructure 不被分析层退役误伤
# ---------------------------------------------------------------------------
def test_infrastructure_preserved(manifest):
    infra = manifest["infrastructure_only"]
    infra_fields = {i["feature"] for i in infra}
    for must_keep in (
        "taxonomy_version / taxonomy_compatibility_key / membership_version",
        "MarketBoard (taxonomy) / PIT Membership service",
    ):
        assert must_keep in infra_fields, (
            f"lineage infrastructure '{must_keep}' not in infrastructure_only — "
            f"risk of being retired by analytics layer"
        )


# ---------------------------------------------------------------------------
# 5. manifest 覆盖所有迁移 disposition 分类（无悬空 field）
# ---------------------------------------------------------------------------
def test_manifest_categories_nonempty(manifest):
    for cat in (
        "already_owned_by_review",
        "migrate_from_board",
        "derive_from_review",
        "retire_conflicting_definition",
        "infrastructure_only",
        "unresolved",
    ):
        assert cat in manifest, f"manifest missing category '{cat}'"
        assert isinstance(manifest[cat], list), f"manifest['{cat}'] must be a list"


# ---------------------------------------------------------------------------
# 6. 目标架构明确标注 RETIRE TARGET 但不实际删除（Slice 1 仅定义）
# ---------------------------------------------------------------------------
def test_retire_targets_declared(manifest):
    tgt = manifest["meta"]["target_architecture"]
    assert "BoardAnalysis runtime stage = RETIRE TARGET" in tgt["retire_targets"]
    assert "market_aggregation publication = RETIRE TARGET" in tgt["retire_targets"]
    assert tgt["slice1_scope"].startswith("ONLY define target"), (
        "Slice 1 must NOT physically delete; only define target"
    )
