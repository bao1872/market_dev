"""INTERNAL-STRUCTURE-TYPE-CONTRACT-REFINEMENT-AND-PREFREEZE — pure unit tests.

Locks the deterministic building blocks of the R1–R6 contract refinement in
``scripts.review_scope_dynamics_probe``:

  * R1 — ``_cr_semantic_contract_lock``
  * R2 — ``_cr_fragmenting_core_reformation_map``（含 state_summary 切片回归）
  * R3 — ``_cr_core_led_threshold_surface`` / ``_cr_plateau_detection``
  * R4 — ``_cr_rotating_fragmenting_boundary``
  * R5 — ``_cr_policy_label`` / ``_cr_conflict_unclassified_policy``
  * R6 — ``_cr_window_split`` / ``_cr_label_churn`` / ``_cr_data_sufficiency_decision``
         / ``_cr_prefreeze_validation``
  * contract_v2 — ``_cr_contract_v2_candidate``（12-gate matrix 诚实接线）

Pure unit: no DB, no network, no dataset IO.  Synthetic inputs only.
"""

from __future__ import annotations

import pytest

from scripts.review_scope_dynamics_probe import (
    _CR_CONTRACT_V2_FILENAME,
    _IST_MC_REFERENCE,
    _MC_CORE_KEYS,
    _cr_abs_contract_summary,
    _cr_candidate_abs_families,
    _cr_conflict_unclassified_policy,
    _cr_contract_v2_candidate,
    _cr_core_led_threshold_surface,
    _cr_data_sufficiency_decision,
    _cr_fragmenting_core_reformation_map,
    _cr_high_pct,
    _cr_label_churn,
    _cr_monotone_nondecreasing,
    _cr_plateau_detection,
    _cr_policy_label,
    _cr_prefreeze_validation,
    _cr_rotating_fragmenting_boundary,
    _cr_run_summary_from_canonical,
    _cr_semantic_contract_lock,
    _cr_side_conflict,
    _cr_window_split,
    _cr_temporal_validation,
    _mc_abs_conditions,
    _mc_assignments_by_type,
    _mc_balanced_box_conditions,
    _mc_eval_family_hits,
    _mc_feature_distributions,
    _mc_in_balanced_box,
    _mc_preferred_families,
    _mc_rule_families,
    _mc_scope_hit_runs,
    _mc_slot_abs,
)


# ---------------------------------------------------------------------------
# synthetic frames（与 E1–E8 closure 测试同一 frame 契约）
# ---------------------------------------------------------------------------

def _row(
    scope: str, date: str,
    *,
    breadth: float = 0.1, hhi: float = 0.1, tilt: float = 0.1,
    migration: float = 0.1, lcr: float = 1.0,
    breadth_delta: float = 0.0, hhi_delta: float = 0.0,
    leader_fraction: float = 0.1,
    **extra,
) -> dict:
    r = {
        "scope_key": scope,
        "scope_name": scope,
        "scope_type": "concept",
        "trade_date": date,
        "size_bucket": "small",
        "member_count": 10,
        "leadership_status": "ready",
        "aligned_breadth_hist_pct": breadth,
        "aligned_breadth_delta5d": breadth_delta,
        "advance_ratio_hist_pct": breadth,
        "advance_ratio_delta5d": breadth_delta,
        "price_hhi_hist_pct": hhi,
        "price_hhi_delta5d": hhi_delta,
        "aligned_tilt_hist_pct": tilt,
        "migration_hist_pct": migration,
        "jaccard_stability": 0.5,
        "previous_retention": 0.5,
        "lcr": lcr,
        "leader_fraction_hist_pct": leader_fraction,
        "exit_minus_entrant": 0.0,
        "replacement_coverage": 1.0,
    }
    r.update(extra)
    return r


def _mk_balanced_frame() -> list[dict]:
    """Rows spanning the four working types + Balanced + a conflict row.

    HIGH percentile (p80) cleanly separates extreme rows: B/C/F/O are at the
    extremes, R/X at the middle, so thresholds and R2/R4 universes are stable.
    """
    rows = []
    n_per = 4
    for k in range(n_per):
        d = f"2026-01-0{k + 1}"
        rows.append(_row("B", d, breadth=0.95, hhi=0.05, tilt=0.1,
                         migration=0.05, breadth_delta=0.02, hhi_delta=-0.01,
                         lcr=1.0))
        rows.append(_row("C", d, breadth=0.05, hhi=0.95, tilt=0.95,
                         migration=0.05, hhi_delta=0.01, lcr=1.0))
        rows.append(_row("R", d, breadth=0.5, hhi=0.5, tilt=0.5,
                         migration=0.95, lcr=1.05))
        rows.append(_row("F", d, breadth=0.05, hhi=0.05, tilt=0.05,
                         migration=0.95, lcr=0.5, breadth_delta=-0.01,
                         hhi_delta=-0.02))
        rows.append(_row("X", d, breadth=0.5, hhi=0.5, tilt=0.5,
                         migration=0.5, lcr=1.0))
        rows.append(_row("O", d, breadth=0.5, hhi=0.95, tilt=0.95,
                         migration=0.95, lcr=0.5))
    return rows


@pytest.fixture(scope="module")
def frame() -> list[dict]:
    return _mk_balanced_frame()


@pytest.fixture(scope="module")
def dists(frame) -> dict:
    return _mc_feature_distributions(frame, list(_MC_CORE_KEYS))


@pytest.fixture(scope="module")
def slot_qs():
    return dict(_IST_MC_REFERENCE)


# ---------------------------------------------------------------------------
# R1 — semantic contract lock
# ---------------------------------------------------------------------------

def test_cr_semantic_contract_lock_five_types_and_frozen_flags():
    out = _cr_semantic_contract_lock({}, {}, {}, {})
    assert set(out["types"]) == {
        "Broadening", "Core-led", "Rotating", "Fragmenting", "Balanced",
    }
    for t in ("Broadening", "Core-led", "Rotating", "Balanced"):
        frozen = out["types"][t]["frozen"]
        assert frozen["semantics"] is True
        assert frozen["rule_form"] is True
        assert frozen["thresholds"] is False
    frag = out["types"]["Fragmenting"]["frozen"]
    assert frag["semantics"] is True
    # Fragmenting 语义被刷新，rule_form 尚未收口（R2 gate 决定）
    assert frag["rule_form"] is False
    assert frag["thresholds"] is False


def test_cr_semantic_contract_lock_e7_declared_facts_are_fixed():
    out = _cr_semantic_contract_lock({}, {}, {}, {})
    b = out["types"]["Broadening"]["blind_audit"]
    assert b == {"prototype_total": 15, "exact": 13, "different_label": 0}
    c = out["types"]["Core-led"]["blind_audit"]
    assert c["exact"] == 15
    # 数值阈值一律 RESEARCH-CANDIDATE，不冻结
    assert out["frozen_judgment"]["reinterpretation_forbidden"] is True


def test_cr_semantic_contract_lock_inherits_e_evidence():
    e3 = {"preferred_families": {"Core-led": {"family": "C1"}}}
    e4 = {"classifications": {"Core-led↔Fragmenting": {"classification": "NO_OVERLAP_BY_RULE_CONSTRUCTION"}}}
    e6 = {"per_family_robustness": {"Broadening": {"threshold_status": "STABLE"}}}
    out = _cr_semantic_contract_lock({}, e3, e4, e6)
    assert out["preferred_families"]["Core-led"]["family"] == "C1"
    assert out["conflict_classifications"]["Core-led↔Fragmenting"]["classification"] == (
        "NO_OVERLAP_BY_RULE_CONSTRUCTION"
    )
    assert out["types"]["Broadening"]["threshold_status"] == "STABLE"


# ---------------------------------------------------------------------------
# R2 — Fragmenting vs Core-Reformation Joint Map
# ---------------------------------------------------------------------------

def test_cr_fragmenting_state_summaries_not_all_zero(frame, dists, slot_qs):
    """回归：tilt 侧切片 bug（'tilt_low'[4:] == '_low'）曾导致 state_summary 全 0，
    进而把 R2 gate 误判成 INSUFFICIENT_DATA。修复后 B_core_reformation 必须命中
    conflict 行（hhi high + tilt high + migration high + lcr<1）。"""
    assignments = _mc_assignments_by_type(frame, _mc_rule_families(), slot_qs, dists)
    r2 = _cr_fragmenting_core_reformation_map(frame, dists, slot_qs, assignments)
    states = r2["state_summary"]
    assert states["B_core_reformation"]["count"] == 4  # O rows（每日期 1 行 × 4）
    assert states["A_fragmenting_like"]["count"] == 4  # F rows
    assert not all(v["count"] == 0 for v in states.values())


def test_cr_fragmenting_universe_filter(frame, dists, slot_qs):
    """Universe = migration >= HIGH(p80=0.95) AND lcr < 1.0 → 仅 F 和 O 行。
    R 行 lcr=1.05 被排除；B/C/X 行 migration=0.05/0.5 被排除。"""
    assignments = _mc_assignments_by_type(frame, _mc_rule_families(), slot_qs, dists)
    r2 = _cr_fragmenting_core_reformation_map(frame, dists, slot_qs, assignments)
    assert r2["universe_count"] == 8


def test_cr_fragmenting_gate_status_enum(frame, dists, slot_qs):
    assignments = _mc_assignments_by_type(frame, _mc_rule_families(), slot_qs, dists)
    r2 = _cr_fragmenting_core_reformation_map(frame, dists, slot_qs, assignments)
    gate = r2["coherent_new_core_gate"]
    assert gate["status"] in ("PASS", "PARTIAL", "INSUFFICIENT_DATA")
    # 合成 frame 只有 4 个 B 行 < min_core_subset=20 → 如实 INSUFFICIENT_DATA
    assert gate["status"] == "INSUFFICIENT_DATA"
    assert gate["core_subset_count"] == 4


# ---------------------------------------------------------------------------
# R3 — Core-led 2D Threshold Surface
# ---------------------------------------------------------------------------

def test_cr_core_led_surface_36_cells(frame, dists, slot_qs):
    assignments = _mc_assignments_by_type(frame, _mc_rule_families(), slot_qs, dists)
    r3 = _cr_core_led_threshold_surface(frame, dists, assignments, [])
    assert len(r3["surface_cells"]) == 36
    assert r3["hhi_grid"] == [0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
    assert r3["tilt_grid"] == [0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
    c = r3["surface_cells"]["0.80|0.80"]
    assert {"hit_count", "hit_rate", "run_persistence", "one_day_only_rate",
            "conflict_rate", "prototype_coverage"} <= set(c)
    assert r3["stable_plateau"]["status"] in ("PASS", "PARTIAL", "UNRESOLVED")


def test_cr_plateau_detection_rejects_family_drift():
    """A1-2：R3 stable cell 必须额外消费 P(TypeHit|Family) / P(TypeHit|Size) 稳定性。

    hit/conflict 完全平坦，但 family_conditional 在邻域内漂移（> _CR_PLATEAU_FAMILY_TOL）
    → 该 cell 局部不稳定，稳定区域不应把它全算进去。构造一个目前会因 family drift
    而不稳定的 cell，验证 plateau 不会把它当作稳定核心（PANELL）。"""
    from scripts.review_scope_dynamics_probe import _CR_PLATEAU_FAMILY_TOL
    hit = 0.2
    conf = 0.1
    base = {
        "hit_rate": hit, "conflict_rate": conf,
        "family_conditional": {"concept": 0.5},
        "size_conditional": {"small": 1.0},
    }
    grid = {}
    for i, h in enumerate([0.65, 0.70, 0.75]):
        for j, t in enumerate([0.65, 0.70, 0.75]):
            drift = 0.0 if (i == 1 and j == 1) else (2 * _CR_PLATEAU_FAMILY_TOL)
            cell = dict(base)
            cell["family_conditional"] = {"concept": 0.5 + drift}
            grid[f"{h:.2f}|{t:.2f}"] = cell
    plateau = _cr_plateau_detection(grid)
    # 中央 cell 因 family drift 不稳定，周边 8 格相对中心也 drif 超限 → 稳定区域应无大块。
    # 至少不应出现 3×3 全稳定。
    best_cells = (plateau["stable_regions"][0]["cell_count"]
                  if plateau["stable_regions"] else 0)
    assert best_cells < 9


def test_cr_plateau_detection_connected_region():
    grid = {}
    for i, h in enumerate([0.65, 0.70, 0.75]):
        for j, t in enumerate([0.65, 0.70, 0.75]):
            # 中间 3×3 全稳定：hit_rate=0.2, conflict=0.1（与邻域偏差 0）
            grid[f"{h:.2f}|{t:.2f}"] = {
                "hit_rate": 0.2, "conflict_rate": 0.1,
            }
    # 在边缘加一个不稳定格：hit_rate 突变为 0.5
    grid["0.65|0.65"]["hit_rate"] = 0.5
    plateau = _cr_plateau_detection(grid)
    assert plateau["status"] in ("PASS", "PARTIAL", "UNRESOLVED")
    assert isinstance(plateau["stable_regions"], list)
    # 稳定格必须有 4-连通区域（排除突变角点后仍剩 8 格）
    if plateau["stable_regions"]:
        assert plateau["stable_regions"][0]["cell_count"] >= 4


# ---------------------------------------------------------------------------
# R4 — Rotating / Fragmenting Boundary Surface
# ---------------------------------------------------------------------------

def test_cr_rotating_boundary_cutoffs_and_three_segment(frame, dists, slot_qs):
    assignments = _mc_assignments_by_type(frame, _mc_rule_families(), slot_qs, dists)
    r4 = _cr_rotating_fragmenting_boundary(frame, dists, slot_qs, assignments)
    assert r4["lcr_anchors"] == [0.60, 0.70, 0.80, 0.85, 0.90, 1.00]
    assert len(r4["cutoff_surfaces"]) == 6
    # high-Migration universe = R/F/O 行（migration=0.95）→ 12 行
    assert r4["universe_count"] == 12
    ts = r4["three_segment_support"]
    assert ts["status"] in ("PASS", "PARTIAL", "INSUFFICIENT_DATA")
    assert set(ts["counts"]) == {"strong_contraction", "transition", "capacity_preserved"}
    # A1-3：R4 拆两层结论，capacity axis 与 natural three-segment 必须都有。
    assert "capacity_axis_evidence" in r4
    assert "natural_three_segment_evidence" in r4
    assert r4["capacity_axis_evidence"]["status"] in ("PASS", "PARTIAL")
    assert r4["natural_three_segment_evidence"]["status"] in (
        "PASS", "PARTIAL", "INSUFFICIENT_DATA",
    )


def test_cr_r4_trivial_persistence_does_not_pass_three_segment():
    """A1-3：禁止 ``1.0>=1.0>=1.0`` trivial persistence monotonicity 直接 PASS。

    三段需要真实结构分离（breadth_weak / coherent-core gap >= _CR_THREE_SEGMENT_GAP）。
    构造三段在结构上完全同构（breadth/hhi/tilt 全部一致，仅 LCR 归段差异）→
    即使 persistence 单调且 axis_ok=True，``natural_three_segment_evidence`` 也不得 PASS
    （应只 PARTIAL，因为 structurally_separated=False）。用 min_segment=0 免除大样本。
    """
    rows = []
    date = 0
    for lcr in (0.50, 0.80, 1.00):  # SC / TR / CP 每段各 2 行，结构完全相同
        for k in range(2):
            date += 1
            rows.append(_row(
                f"S{lcr},{k}", f"2026-{date // 28 + 1:02d}-{date % 28 + 1:02d}",
                breadth=0.05, hhi=0.95, tilt=0.95, migration=0.95, lcr=lcr,
            ))
    frame_r4 = rows
    dists = _mc_feature_distributions(frame_r4, list(_MC_CORE_KEYS))
    slot_qs = dict(_IST_MC_REFERENCE)
    assignments = _mc_assignments_by_type(frame_r4, _mc_rule_families(), slot_qs, dists)
    r4 = _cr_rotating_fragmenting_boundary(
        frame_r4, dists, slot_qs, assignments, min_segment=0,
    )
    ts = r4["natural_three_segment_evidence"]
    # 结构同构 → 无法证明自然三段 → 不得 PASS，只能 PARTIAL。
    assert ts["status"] == "PARTIAL"
    assert ts["structural_separation"]["separated"] is False


def test_cr_monotone_nondecreasing():
    assert _cr_monotone_nondecreasing([1.0, 2.0, 2.5]) is True
    assert _cr_monotone_nondecreasing([2.5, 2.0, 1.0]) is False
    assert _cr_monotone_nondecreasing([None, 1.0, 1.5]) is True
    assert _cr_monotone_nondecreasing([None, None, None]) is True


# ---------------------------------------------------------------------------
# R5 — Conflict + Unclassified Policy
# ---------------------------------------------------------------------------

def test_cr_policy_label_single_vs_multi_hit():
    assert _cr_policy_label(0, {"Broadening"}, False, "B", []) == "Broadening"
    assert _cr_policy_label(1, {"Broadening", "Core-led"}, False, "B", []) == "Unclassified"
    assert _cr_policy_label(2, {"Broadening", "Core-led"}, False, "C", []) == "Unclassified"
    assert _cr_policy_label(3, set(), True, "B", []) == "Balanced"
    assert _cr_policy_label(4, set(), False, "B", []) == "Unclassified"
    assert _cr_policy_label(5, set(), False, "A", []) is None


def test_cr_policy_a_priority_negative_control():
    # Policy A：multi-hit → priority winner（Broadening 优先），仅 negative control
    assert _cr_policy_label(0, {"Core-led", "Broadening"}, False, "A", []) == "Broadening"
    assert _cr_policy_label(1, {"Fragmenting", "Rotating"}, False, "A", []) == "Rotating"


def test_cr_policy_c_resolution_only_for_approved_pair():
    c_pairs = ["Core-led↔Fragmenting"]
    # 无已批准 resolution（_CR_POLICY_C_RESOLUTIONS 默认空）→ 即便 pair 命中仍 Unclassified
    assert _cr_policy_label(0, {"Core-led", "Fragmenting"}, False, "C", c_pairs) == "Unclassified"


def test_cr_conflict_policy_function_structure(frame, dists, slot_qs):
    assignments = _mc_assignments_by_type(frame, _mc_rule_families(), slot_qs, dists)
    box_by_index = {i: _mc_in_balanced_box(r, slot_qs, dists)
                    for i, r in enumerate(frame)}
    prototypes = {"Broadening": [], "Core-led": [], "Rotating": [],
                  "Fragmenting": [], "Balanced": []}
    r5 = _cr_conflict_unclassified_policy(
        frame, assignments, box_by_index, {}, prototypes, []
    )
    assert set(r5["policies"]) == {"Policy A", "Policy B", "Policy C"}
    for p in r5["policies"].values():
        assert "metrics" in p
        assert "family_bias" in p
        assert "size_bias" in p
        assert "prototype_preservation" in p
        assert "conflict_recognition" in p
    assert r5["recommendation"]["recommended"] in ("Policy B", "REVIEW_REQUIRED")
    # E7 固定输入必须被携带
    assert r5["e7_declared"]["summary_facts"]["conflict"]["unclear"] == 25


# ---------------------------------------------------------------------------
# R6 — Pre-Freeze Validation + Data Sufficiency
# ---------------------------------------------------------------------------

def test_cr_window_split_partitions_all(frame):
    w = _cr_window_split(frame, 3)
    assert set(w) == {"early", "middle", "late"}
    total = sum(len(v) for v in w.values())
    assert total == len(frame)


def test_cr_label_churn_identical_and_flipped():
    a = {0: "Broadening", 1: "Core-led", 2: "Unclassified"}
    b = {0: "Broadening", 1: "Core-led", 2: "Unclassified"}
    assert _cr_label_churn(a, b)["churn_rate"] == 0.0
    c = {0: "Broadening", 1: "Rotating", 2: "Unclassified"}
    churn = _cr_label_churn(a, c)
    assert churn["flipped_count"] == 1
    assert churn["churn_rate"] == pytest.approx(1 / 3)


def test_cr_temporal_validation_splits_prevalence_and_semantic_gate(frame, dists, slot_qs):
    """A1-6：temporal_state_prevalence 只描述不判定；只有 temporal_semantic_stability
    作为 Freeze Gate。二者分离，且 prevalence 明确标记 descriptive_only。"""
    fams = _mc_rule_families()
    abs_fams = _cr_candidate_abs_families(
        fams, slot_qs, dists,
        core_led_hhi_q=0.8, core_led_tilt_q=0.8, lcr_cut=0.85,
    )
    slot_abs = _mc_slot_abs(slot_qs, dists)
    abs_box = _mc_abs_conditions(_mc_balanced_box_conditions(), slot_abs)
    temporal = _cr_temporal_validation(frame, abs_fams, abs_box)
    # 两个独立输出
    assert "temporal_state_prevalence" in temporal
    assert "temporal_semantic_stability" in temporal
    prev = temporal["temporal_state_prevalence"]
    assert prev["descriptive_only"] is True
    assert set(prev["per_window"]) == {"early", "middle", "late"}
    sem = temporal["temporal_semantic_stability"]
    assert set(sem["per_type"]) <= {"Broadening", "Core-led", "Rotating", "Fragmenting"}
    assert sem["status"] in ("PASS", "NEEDS_REVIEW", "UNRESOLVED")
    # Gate 状态来自 semantic_stability 而非 prevalence 漂移
    assert temporal["status"] == sem["status"]


def test_cr_data_sufficiency_three_options():
    # blocker（temporal semantic stability 失败 / broader drift / boundary churn）→ NOT_FREEZE_READY
    d1 = _cr_data_sufficiency_decision(
        membership_semantics="current_static_research_proxy",
        threshold_freeze_eligible=False, has_pit_membership=False,
        broader_status="NEEDS_REVIEW", temporal_semantic_status="PASS",
        boundary_status="PASS",
    )
    assert d1["decision"] == "NOT_FREEZE_READY"
    assert d1["threshold_freeze_eligible"] is False
    assert d1["allowed_to_freeze"] == []
    # 默认（无 PIT）→ SEMANTIC_FREEZE_ONLY
    d2 = _cr_data_sufficiency_decision(
        membership_semantics="current_static_research_proxy",
        threshold_freeze_eligible=False, has_pit_membership=False,
        broader_status="PASS", temporal_semantic_status="PASS",
        boundary_status="PASS",
    )
    assert d2["decision"] == "SEMANTIC_FREEZE_ONLY"
    assert d2["freeze_level"] == "SEMANTIC"
    assert d2["numerical_thresholds_status"] == "RESEARCH-CANDIDATE"
    assert d2["allowed_to_freeze"] == [
        "semantics", "dimension_ownership", "rule_form", "availability",
        "conflict_policy",
    ]
    # 真实 PIT + threshold eligible → FULL_FREEZE_READY
    d3 = _cr_data_sufficiency_decision(
        membership_semantics="pit", threshold_freeze_eligible=True,
        has_pit_membership=True,
        broader_status="PASS", temporal_semantic_status="PASS",
        boundary_status="PASS",
    )
    assert d3["decision"] == "FULL_FREEZE_READY"
    assert d3["threshold_freeze_eligible"] is True
    assert "numerical_thresholds" in d3["allowed_to_freeze"]


def test_cr_data_sufficiency_temporal_only_semantic_gate():
    """A1-6：Temporal 只由 semantic stability 阻塞；Type 频率漂移（prevalence）不阻塞。
    Boundary BORDERLINE_PASS 不阻塞（A1-7）。"""
    d = _cr_data_sufficiency_decision(
        membership_semantics="current_static_research_proxy",
        threshold_freeze_eligible=False, has_pit_membership=False,
        broader_status="PASS", temporal_semantic_status="NEEDS_REVIEW",
        boundary_status="BORDERLINE_PASS",
    )
    assert d["decision"] == "NOT_FREEZE_READY"
    assert any("temporal semantic stability" in b for b in d["blockers"])
    # boundary BORDERLINE_PASS 不应进入 blockers
    assert not any("boundary" in b for b in d["blockers"])


def test_cr_prefreeze_validation_structure(frame, dists, slot_qs):
    fams = _mc_rule_families()
    preferred = _mc_preferred_families(frame, fams, slot_qs, dists, len(frame))
    r6 = _cr_prefreeze_validation(
        frame, frame, fams, slot_qs, dists, preferred,
        core_led_hhi_q=0.8, core_led_tilt_q=0.8, lcr_cut=0.85,
    )
    assert set(r6) >= {
        "a_40_285_stability", "b_temporal_robustness", "c_boundary_perturbation",
        "leakage_audit", "d_data_sufficiency",
    }
    assert r6["a_40_285_stability"]["status"] in ("PASS", "NEEDS_REVIEW")
    assert r6["b_temporal_robustness"]["status"] in (
        "PASS", "NEEDS_REVIEW", "UNRESOLVED", "INSUFFICIENT",
    )
    assert r6["c_boundary_perturbation"]["status"] in (
        "PASS", "NEEDS_REVIEW", "BORDERLINE_PASS",
    )
    assert r6["d_data_sufficiency"]["decision"] in (
        "FULL_FREEZE_READY", "SEMANTIC_FREEZE_ONLY", "NOT_FREEZE_READY",
    )
    # base == broad → broader 无 drift → PASS
    assert r6["a_40_285_stability"]["status"] == "PASS"


def test_cr_abs_contract_summary_and_candidate_abs_families(frame, dists, slot_qs):
    fams = _mc_rule_families()
    abs_fams = _cr_candidate_abs_families(
        fams, slot_qs, dists,
        core_led_hhi_q=0.8, core_led_tilt_q=0.8, lcr_cut=0.85,
    )
    for t in ("Broadening", "Core-led", "Rotating", "Fragmenting"):
        assert t in abs_fams
        assert all(f["abs_conditions"] is not None for f in abs_fams[t])
    slot_abs = _mc_slot_abs(slot_qs, dists)
    abs_box = _mc_abs_conditions(_mc_balanced_box_conditions(), slot_abs)
    assert abs_box is not None
    res = _cr_abs_contract_summary(frame, abs_fams, abs_box)
    assert set(res) == {"assignments", "box_by_index", "summary"}
    assert res["summary"]["unclassified_rate"] is not None


# ---------------------------------------------------------------------------
# contract_v2 candidate + 12-gate matrix
# ---------------------------------------------------------------------------

def _minimal_r1() -> dict:
    out = _cr_semantic_contract_lock({}, {}, {}, {})
    return out


def _minimal_r6(sufficiency_decision: str) -> dict:
    sufficiency = {
        "decision": sufficiency_decision,
        "freeze_level": "NONE" if sufficiency_decision == "NOT_FREEZE_READY"
        else "SEMANTIC",
        "threshold_freeze_eligible": False,
        "reason": f"test {sufficiency_decision}",
        "blockers": ["temporal 三窗频率偏差超限"] if sufficiency_decision == "NOT_FREEZE_READY" else [],
    }
    broader = {"status": "PASS",
               "drift_report": {"conditional_drift": {"summary": {
                   "scope_type": {"max": 0.01}, "size_bucket": {"max": 0.01}}}},
               "max_gap": 0.01}
    return {
        "a_40_285_stability": broader,
        "b_temporal_robustness": {
            "status": "PASS",
            "temporal_state_prevalence": {"descriptive_only": True},
            "temporal_semantic_stability": {"status": "PASS", "max_drift": 0.01},
        },
        "c_boundary_perturbation": {"status": "PASS", "max_label_churn": 0.01},
        "leakage_audit": {"status": "PASS"},
        "d_data_sufficiency": sufficiency,
    }


def test_cr_contract_v2_gate_matrix_and_honest_status():
    r1 = _minimal_r1()
    r2 = {"coherent_new_core_gate": {"status": "INSUFFICIENT_DATA"}}
    r3 = {"stable_plateau": {"status": "PASS", "recommended_region": {
        "hhi_q": 0.8, "tilt_q": 0.8}}}
    r4 = {"three_segment_support": {"status": "PASS"}}
    r5 = {"recommendation": {"recommended": "Policy B"}}
    r6 = _minimal_r6("NOT_FREEZE_READY")
    cv = _cr_contract_v2_candidate(r1, r2, r3, r4, r5, r6, {}, {})
    assert set(cv["gate_matrix"]) == {
        "Broadening", "Core-led", "Rotating", "Fragmenting", "Balanced",
        "Conflict", "Unclassified", "Family/Size", "Temporal", "Boundary",
        "Leakage", "Data sufficiency",
    }
    # 诚实接线：Data sufficiency 非 FULL → 不得标记 FREEZE_ELIGIBLE
    assert cv["contract_status"] == "CONTRACT_V2_CANDIDATE_NOT_FROZEN"
    assert cv["threshold_freeze_eligible"] is False
    assert cv["pre_freeze"] == "NOT_FREEZE_READY"
    assert cv["frozen_judgment_sha256"].startswith("8e2e3957")
    # Fragmenting gate 非 PASS → 不得注入 coherent_core_exclusion
    assert "coherent_core_exclusion" not in cv["types"]["Fragmenting"]


def test_cr_contract_v2_injects_core_exclusion_when_gate_pass():
    r1 = _minimal_r1()
    r2 = {"coherent_new_core_gate": {"status": "PASS"}}
    r3 = {"stable_plateau": {"status": "PASS", "recommended_region": None}}
    r4 = {"three_segment_support": {"status": "PASS"}}
    r5 = {"recommendation": {"recommended": "Policy B"}}
    r6 = _minimal_r6("SEMANTIC_FREEZE_ONLY")
    cv = _cr_contract_v2_candidate(r1, r2, r3, r4, r5, r6, {}, {})
    assert "coherent_core_exclusion" in cv["types"]["Fragmenting"]
    assert cv["contract_status"] == "CONTRACT_V2_CANDIDATE_NOT_FROZEN"


def test_cr_contract_v2_filename_constant():
    assert _CR_CONTRACT_V2_FILENAME == "internal_structure_type_contract_v2_candidate.json"


# ---------------------------------------------------------------------------
# 轻量 helpers
# ---------------------------------------------------------------------------

def test_cr_high_pct_and_run_stats(frame, dists):
    v = _cr_high_pct(dists, "price_hhi_hist_pct", 0.5)
    assert v is not None
    idx = [i for i, r in enumerate(frame)
           if r.get("price_hhi_hist_pct", 0) >= 0.95]
    runs = _mc_scope_hit_runs(frame, set(idx))
    stats = _cr_run_summary_from_canonical(runs)
    assert stats["run_count"] == 2  # C 和 O 各一段连续 run
    assert stats["median_run_length"] == 4.0
    assert stats["one_day_only_rate"] == 0.0


def test_cr_run_gap_case_two_single_day_runs():
    """A1-1：T1 hit / T2 miss / T3 hit ⇒ 两个 length=1 runs，绝不能合并成 length=2。

    旧的 ``_cr_run_stats`` 只比较日期大小（新日期 > 旧日期 即连续），会把 T1/T3
    误并成长度 2；canonical ``_mc_scope_hit_runs`` 遍历完整时间序列，遇 miss 断开。
    """
    frame = [
        _row("S1", "2026-01-01", hhi=0.95),  # hit
        _row("S1", "2026-01-02", hhi=0.1),   # miss（T2 不在命中集合）
        _row("S1", "2026-01-03", hhi=0.95),  # hit
    ]
    runs = _mc_scope_hit_runs(frame, {0, 2})
    assert runs == [1, 1]
    stats = _cr_run_summary_from_canonical(runs)
    assert stats["run_count"] == 2
    assert stats["median_run_length"] == 1.0
    assert stats["one_day_only_rate"] == 1.0


def test_cr_run_summary_from_canonical_empty_and_contiguous():
    assert _cr_run_summary_from_canonical([]) == {
        "run_count": 0, "median_run_length": None, "one_day_only_rate": None,
    }
    contiguous = [4]
    s = _cr_run_summary_from_canonical(contiguous)
    assert s["run_count"] == 1
    assert s["median_run_length"] == 4.0
    assert s["one_day_only_rate"] == 0.0


def test_cr_side_conflict():
    assignments = {"Fragmenting": {1, 2, 3}, "Core-led": {2, 4}}
    assert _cr_side_conflict([1, 2, 3, 4], assignments, "Core-led") == pytest.approx(0.5)
    assert _cr_side_conflict([], assignments, "Core-led") is None
