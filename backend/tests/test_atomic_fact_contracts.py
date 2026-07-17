"""AFC V1 双合同结构一致性测试（冻结研究合同 + 产品展示合同分离）。

验证：
1. 冻结合同（V4.13）除原字段外不含任何产品层字段（public_key/public_label）。
2. presentation 恰好覆盖 14 Core + 允许展示的 8 Auxiliary（共 22）。
3. 两份合同 Fact ID 一一对应（presentation 的 core/aux ID 集合 == 冻结合同
   core/aux ID 去掉 T3/T6）。
4. T3/T6（效率 fact，flag 关闭）不进入普通用户展示（不在 presentation）。
5. V1（rejected）没有任何 presentation 映射。

用法：
    pytest backend/tests/test_atomic_fact_contracts.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_CONTRACTS = Path(__file__).resolve().parent.parent / "app" / "contracts"
FROZEN_PATH = _CONTRACTS / "atomic_fact_contract_v1.json"
PRES_PATH = _CONTRACTS / "atomic_fact_presentation_v1.json"

# V4.13 原字段（HEAD 提交版本中 core_facts 的并集，产品层不得新增）
_FROZEN_ORIGINAL_CORE_KEYS = {
    "classification_rule", "default_ui_enabled", "dimension", "display_order",
    "display_template", "formula", "id", "legacy_aliases", "level", "null_policy",
    "prohibited_interpretations", "raw_type", "source_paths", "thresholds_ref",
    "ui_rule", "unit",
}
_PRODUCT_FIELDS = {"public_key", "public_label"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def frozen():
    return _load(FROZEN_PATH)


@pytest.fixture(scope="module")
def presentation():
    return _load(PRES_PATH)


def test_frozen_has_no_product_fields(frozen):
    """冻结合同除 V4.13 原字段外不含产品字段。"""
    for fact in frozen["core_facts"] + frozen["auxiliary_facts"] + frozen.get("rejected_facts", []):
        for bad in _PRODUCT_FIELDS:
            assert bad not in fact, f"{fact.get('id')} 仍含产品字段 {bad}"
    # core 事实字段集合必须是 V4.13 原字段子集（仅本研究字段，无新增产品字段）
    for fact in frozen["core_facts"]:
        extra = set(fact.keys()) - _FROZEN_ORIGINAL_CORE_KEYS
        assert not extra, f"core fact 出现非 V4.13 原字段: {extra}"


def test_presentation_covers_14_core_and_8_aux(presentation):
    """presentation 恰好覆盖 14 Core + 8 Auxiliary。"""
    facts = presentation["facts"]
    core = [f for f in facts if f["level"] == "core"]
    aux = [f for f in facts if f["level"] == "auxiliary"]
    assert len(core) == 14, len(core)
    assert len(aux) == 8, len(aux)
    # 每项具备 6 个映射字段
    required = {"id", "level", "publicKey", "publicLabel", "visualKind", "valuePrecision", "groupTitle", "secondaryLabel"}
    for f in facts:
        assert required <= set(f.keys()), f"{f.get('id')} 缺映射字段"


def test_presentation_ids_match_frozen_excluding_t3_t6(frozen, presentation):
    """两份合同 Fact ID 一一对应（presentation == frozen core/aux 去掉 T3/T6）。"""
    frozen_ids = {f["id"] for f in frozen["core_facts"]} | {f["id"] for f in frozen["auxiliary_facts"]}
    frozen_ids -= {"T3_trend_efficiency", "T6_efficiency_delta"}  # flag 关闭，不进展示
    pres_ids = {f["id"] for f in presentation["facts"]}
    assert pres_ids == frozen_ids, f"ID 不对应:\n缺失 {frozen_ids - pres_ids}\n多余 {pres_ids - frozen_ids}"


def test_t3_t6_not_in_presentation(presentation):
    """T3/T6 不进入普通用户展示。"""
    pres_ids = {f["id"] for f in presentation["facts"]}
    assert "T3_trend_efficiency" not in pres_ids
    assert "T6_efficiency_delta" not in pres_ids


def test_v1_not_in_presentation(frozen, presentation):
    """V1（rejected）没有 presentation 映射。"""
    pres_ids = {f["id"] for f in presentation["facts"]}
    assert "V1_cumulative_volume_ratio" not in pres_ids
    # 冻结合同中 V1 仍存在（研究记录），但普通用户永不展示
    assert any(f["id"] == "V1_cumulative_volume_ratio" for f in frozen.get("rejected_facts", []))
