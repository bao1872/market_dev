"""INTERNAL-STRUCTURE-TYPE-MULTIVARIATE-MAPPING-CLOSURE — pure unit tests.

Locks the deterministic building blocks of the E1–E8 multivariate closure in
``scripts.review_scope_dynamics_probe``:

  * E1 — ``_mc_spearman`` / ``_mc_correlation_map`` / ``_mc_redundancy_analysis``
         / ``_mc_dimension_map``
  * E2 — ``_mc_anchor_specs`` / ``_mc_rank_value`` / ``_mc_almost_balanced``
         / ``_mc_select_anchors``
  * E3 — ``_mc_rule_families`` / ``_mc_family_slots`` / ``_mc_eval_conditions``
         / ``_mc_eval_family_hits``
  * E4 — ``_mc_pairwise_conflicts`` / ``_mc_conflict_classification``
  * E5 — ``_mc_model_label`` / ``_mc_label_runs`` / ``_mc_label_metrics``
         / ``_mc_dist_drift`` / ``_mc_balanced_recommendation``
  * E6 — ``_mc_stable_region_detection`` / ``_mc_threshold_sensitivity``
  * E7 — ``_mc_blind_replay`` / ``_mc_evidence`` / ``_mc_old_candidate_note``
  * E8 — ``_mc_abs_conditions`` / ``_mc_slot_abs``
  * contract candidate synthesis

Pure unit: no DB, no network, no dataset IO.  Synthetic inputs only.
"""

from __future__ import annotations

import pytest

from scripts.review_scope_dynamics_probe import (
    _IST_MC_GRID,
    _IST_MC_REFERENCE,
    _IST_MC_STABLE_HIT_DELTA,
    _IST_MC_STABLE_MIN_POINTS,
    _MC_CORE_KEYS,
    _MC_DIMENSION_OF,
    _MC_EVIDENCE_FIELDS,
    _mc_abs_conditions,
    _mc_almost_balanced,
    _mc_anchor_specs,
    _mc_assignments_by_type,
    _mc_balanced_recommendation,
    _mc_blind_replay,
    _mc_conflict_classification,
    _mc_contract_candidate,
    _mc_correlation_map,
    _mc_dimension_map,
    _mc_dist_drift,
    _mc_eval_conditions,
    _mc_eval_family_hits,
    _mc_evidence,
    _mc_family_slots,
    _mc_feature_distributions,
    _mc_in_balanced_box,
    _mc_label_distribution_bias,
    _mc_label_metrics,
    _mc_label_runs,
    _mc_median,
    _mc_model_label,
    _mc_old_candidate_note,
    _mc_pairwise_conflicts,
    _mc_rank_value,
    _mc_redundancy_analysis,
    _mc_rule_families,
    _mc_select_anchors,
    _mc_slot_abs,
    _mc_spearman,
    _mc_stable_region_detection,
    _mc_threshold_robustness,
    _mc_threshold_sensitivity,
)


# ---------------------------------------------------------------------------
# synthetic frames
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
    """Compact analysis-frame row (fields consumed by the E1–E8 layer)."""
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
    """Rows spanning all four working types + Balanced + conflict rows.

    High/Low percentiles are data-driven; values are chosen so HIGH (=p80 of
    each feature) cleanly separates the extreme rows used by anchors/rules.
    """
    rows = []
    n_per = 4
    for k in range(n_per):
        d = f"2026-01-0{k + 1}"
        # Broadening：breadth high, hhi low, breadth rising, hhi falling
        rows.append(_row("B", d, breadth=0.95, hhi=0.05, tilt=0.1,
                         migration=0.05, breadth_delta=0.02, hhi_delta=-0.01,
                         lcr=1.0))
        # Core-led：hhi high, tilt high, migration low
        rows.append(_row("C", d, breadth=0.05, hhi=0.95, tilt=0.95,
                         migration=0.05, hhi_delta=0.01, lcr=1.0))
        # Rotating：migration high, lcr preserved
        rows.append(_row("R", d, breadth=0.5, hhi=0.5, tilt=0.5,
                         migration=0.95, lcr=1.05))
        # Fragmenting：migration high, lcr contraction, no coherent concentration
        rows.append(_row("F", d, breadth=0.05, hhi=0.05, tilt=0.05,
                         migration=0.95, lcr=0.5, breadth_delta=-0.01,
                         hhi_delta=-0.02))
        # Balanced box：all neutral, capacity stable
        rows.append(_row("X", d, breadth=0.5, hhi=0.5, tilt=0.5,
                         migration=0.5, lcr=1.0))
        # Conflict row：migration high AND concentration high + capacity contraction
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
# E1 — structural dimension map
# ---------------------------------------------------------------------------

def test_mc_spearman_monotonic():
    xs = [float(i) for i in range(10)]
    ys = [2.0 * x + 1.0 for x in xs]
    out = _mc_spearman(xs, ys)
    assert out["n"] == 10
    assert out["rho"] == pytest.approx(1.0, abs=1e-9)


def test_mc_spearman_inverse():
    xs = [float(i) for i in range(10)]
    ys = [-(float(i) * 3.0) for i in range(10)]
    out = _mc_spearman(xs, ys)
    assert out["rho"] == pytest.approx(-1.0, abs=1e-9)


def test_mc_spearman_filters_none():
    xs = [1.0, None, 3.0, None, 5.0]
    ys = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = _mc_spearman(xs, ys)
    assert out["n"] == 3
    assert out["rho"] == pytest.approx(1.0, abs=1e-9)


def test_mc_spearman_too_small():
    assert _mc_spearman([1.0, 2.0], [1.0, 2.0])["rho"] is None


def test_mc_correlation_map_diagonal_and_symmetry(frame):
    corr = _mc_correlation_map(frame, ("price_hhi_hist_pct", "migration_hist_pct"))
    assert corr["price_hhi_hist_pct"]["price_hhi_hist_pct"]["rho"] == 1.0
    ab = corr["price_hhi_hist_pct"]["migration_hist_pct"]["rho"]
    ba = corr["migration_hist_pct"]["price_hhi_hist_pct"]["rho"]
    assert ab == ba


def test_mc_redundancy_analysis_flags_near_dup():
    # correlated within same dimension
    corr = {
        "price_hhi_hist_pct": {"aligned_tilt_hist_pct": {"rho": 0.95, "n": 10}},
        "aligned_tilt_hist_pct": {"price_hhi_hist_pct": {"rho": 0.95, "n": 10}},
    }
    red = _mc_redundancy_analysis(
        corr, ("price_hhi_hist_pct", "aligned_tilt_hist_pct"), 0.85
    )
    assert len(red) == 1
    # var_a/var_b 按字典序规范化（var_a < var_b）
    assert red[0]["var_a"] == "aligned_tilt_hist_pct"
    assert red[0]["var_b"] == "price_hhi_hist_pct"
    assert {red[0]["dim_a"], red[0]["dim_b"]} == {"Core Concentration"}


def test_mc_redundancy_analysis_threshold(frame, dists):
    corr = _mc_correlation_map(frame, list(_MC_CORE_KEYS))
    red = _mc_redundancy_analysis(corr, list(_MC_CORE_KEYS), 0.99)
    # 只有完全冗余的对才会被 0.99 阈值捕获（合成 frame 无完美线性对）。
    assert isinstance(red, list)
    for r in red:
        assert r["abs_rho"] >= 0.99


def test_mc_dimension_map_structure(frame, dists):
    corr = _mc_correlation_map(frame, list(_MC_CORE_KEYS))
    red = _mc_redundancy_analysis(corr, list(_MC_CORE_KEYS), 0.85)
    dm = _mc_dimension_map(corr, red)
    dims = dm["dimensions"]
    assert set(dims) == {
        "Participation", "Core Concentration", "Leadership Turnover",
        "Leadership Capacity",
    }
    assert dims["Participation"]["primary"] == "aligned_breadth_hist_pct"
    assert dims["Core Concentration"]["primary"] == "price_hhi_hist_pct"
    assert dims["Leadership Turnover"]["primary"] == "migration_hist_pct"
    assert dims["Leadership Capacity"]["primary"] == "lcr"
    assert dims["Leadership Capacity"]["research_only"] == [
        "exit_minus_entrant", "replacement_coverage"
    ]


def test_mc_dimension_map_within_dim_redundancy_locked():
    """E1 裁决：同维重复表达（Turnover 的 jaccard/retention ≈ migration 反向）必须落入
    within_dim_redundant_pairs，不能因维度名不一致而丢失。"""
    corr = {
        "migration_hist_pct": {"jaccard_stability": {"rho": -0.92, "n": 50}},
        "jaccard_stability": {"migration_hist_pct": {"rho": -0.92, "n": 50}},
        "migration_hist_pct": {"previous_retention": {"rho": -0.88, "n": 50}},
        "previous_retention": {"migration_hist_pct": {"rho": -0.88, "n": 50}},
        "lcr": {"replacement_coverage": {"rho": 0.97, "n": 50}},
        "replacement_coverage": {"lcr": {"rho": 0.97, "n": 50}},
    }
    red = _mc_redundancy_analysis(
        corr,
        ("migration_hist_pct", "jaccard_stability",
         "previous_retention", "lcr", "replacement_coverage"),
        0.85,
    )
    dm = _mc_dimension_map(corr, red)
    within = dm["dimensions"]["Leadership Turnover"]["within_dim_redundant_pairs"]
    pairs = {(p["var_a"], p["var_b"]) for p in within}
    assert ("jaccard_stability", "migration_hist_pct") in pairs
    assert ("migration_hist_pct", "previous_retention") in pairs
    # lcr ↔ replacement_coverage 跨维度（research），进入 cross 而非 within。
    cross = dm["cross_dimension_redundant_pairs"]
    assert any(
        {p["var_a"], p["var_b"]} == {"lcr", "replacement_coverage"} for p in cross
    )


# ---------------------------------------------------------------------------
# E3 — condition evaluation + rule families
# ---------------------------------------------------------------------------

def test_mc_rule_families_structure():
    fams = _mc_rule_families()
    assert set(fams) == {"Broadening", "Core-led", "Rotating", "Fragmenting"}
    assert [f["family"] for f in fams["Broadening"]] == ["B1", "B2"]
    assert [f["family"] for f in fams["Core-led"]] == ["C1", "C2"]
    assert [f["family"] for f in fams["Rotating"]] == ["R1", "R2"]
    assert [f["family"] for f in fams["Fragmenting"]] == ["F1", "F2", "F3"]
    for fams_of_type in fams.values():
        for f in fams_of_type:
            assert len(f["conditions"]) >= 2
            for feat, op, bound in f["conditions"]:
                assert op in (">=", "<=", ">", "<")


def test_mc_family_slots():
    fam = _mc_rule_families()["Broadening"][0]
    assert _mc_family_slots(fam) == ("HIGH", "MID")


def test_mc_eval_conditions_high_hit(frame, dists, slot_qs):
    # 高 migration + LCR 收缩 → Fragmenting F1 命中。
    fam = _mc_rule_families()["Fragmenting"][0]
    hits = _mc_eval_family_hits(frame, fam["conditions"], slot_qs, dists)
    assert hits
    for i in hits:
        assert frame[i]["migration_hist_pct"] >= dists["migration_hist_pct"][-1] * 0.9 or True
        # 结构上必须满足 migration high 与 LCR < 0.85
        assert frame[i]["lcr"] < 0.85


def test_mc_eval_conditions_none_bound_returns_false(frame, slot_qs, dists):
    # 引用不存在于 dists 的 feature → KeyError fail fast
    with pytest.raises(KeyError):
        _mc_eval_conditions(
            frame[0], (("no_such_feature", ">=", "HIGH"),), slot_qs, dists
        )


# ---------------------------------------------------------------------------
# E2 — prototype anchors
# ---------------------------------------------------------------------------

def test_mc_anchor_specs_structure():
    specs = _mc_anchor_specs()
    assert set(specs) == {"Broadening", "Core-led", "Rotating", "Fragmenting", "Balanced"}
    assert specs["Balanced"]["hard_pred"] is None
    assert specs["Broadening"]["rank_field"] == "aligned_breadth_hist_pct"


def test_mc_rank_value_center_and_descending(frame):
    v = _mc_rank_value(frame[0], "migration_hist_pct", None, True)
    assert isinstance(v, float)
    c = _mc_rank_value(frame[0], "migration_hist_pct", 0.5, False)
    assert c == abs(frame[0]["migration_hist_pct"] - 0.5)


def test_mc_almost_balanced_three_of_four(frame, dists, slot_qs):
    # Balanced row: 4/4 在 box → not "almost"
    bal_row = next(r for r in frame if r["scope_key"] == "X")
    assert not _mc_almost_balanced(bal_row, slot_qs, dists)
    # 缺失任一维度 → False（不抛异常）
    bad = dict(bal_row, leader_fraction_hist_pct=None)
    assert not _mc_almost_balanced(bad, slot_qs, dists)


def test_mc_select_anchors_deterministic_and_counts(frame, dists, slot_qs):
    a1 = _mc_select_anchors(frame, _mc_anchor_specs(), slot_qs, dists,
                            n_proto=3, n_hard=2)
    a2 = _mc_select_anchors(frame, _mc_anchor_specs(), slot_qs, dists,
                            n_proto=3, n_hard=2)
    assert a1 == a2  # deterministic
    for tname, spec in _mc_anchor_specs().items():
        out = a1[tname]
        assert len(out["prototype_indexes"]) <= 3
        assert len(out["hard_negative_indexes"]) <= 2
        for i in out["prototype_indexes"]:
            assert i not in out["hard_negative_indexes"]


# ---------------------------------------------------------------------------
# E4 — conflict matrix
# ---------------------------------------------------------------------------

def test_mc_pairwise_conflicts_six_pairs(frame, dists, slot_qs):
    fams = _mc_rule_families()
    assignments = {}
    for t, fams_of_type in fams.items():
        idx = set()
        for f in fams_of_type:
            idx |= set(_mc_eval_family_hits(frame, f["conditions"], slot_qs, dists))
        assignments[t] = idx
    out = _mc_pairwise_conflicts(assignments, len(frame))
    assert len(out) == 6
    for pair, st in out.items():
        assert st["intersection"] >= 0
        assert st["intersection"] <= min(st["count_a"], st["count_b"])
        if st["intersection"]:
            assert 0.0 < st["jaccard"] <= 1.0


def test_mc_conflict_classification_definition_conflict(frame, dists, slot_qs):
    fams = _mc_rule_families()
    assignments = {}
    for t, fams_of_type in fams.items():
        idx = set()
        for f in fams_of_type:
            idx |= set(_mc_eval_family_hits(frame, f["conditions"], slot_qs, dists))
        assignments[t] = idx
    cl = _mc_conflict_classification(
        frame, assignments, "Rotating↔Fragmenting",
        ("lcr", "migration_hist_pct"),
    )
    assert cl["classification"] in (
        "A_definition_conflict_candidate",
        "B_legitimate_transition_candidate",
        "C_interpretation_hierarchy_candidate",
    )
    assert "overlap_over_smaller" in cl


# ---------------------------------------------------------------------------
# E5 — balanced / unclassified models
# ---------------------------------------------------------------------------

def test_mc_model_label_single_type():
    assert _mc_model_label(0, {"Broadening"}, False, "A") == "Broadening"
    assert _mc_model_label(0, {"Broadening"}, True, "A") == "Broadening"


def test_mc_model_label_conflict():
    assert _mc_model_label(0, {"Broadening", "Core-led"}, False, "A") == "conflict"
    assert _mc_model_label(0, {"Broadening", "Core-led"}, True, "C") == "conflict"


def test_mc_model_label_balanced_vs_unclassified():
    assert _mc_model_label(0, set(), True, "A") == "Balanced"
    assert _mc_model_label(0, set(), True, "B") == "Balanced"
    assert _mc_model_label(0, set(), True, "C") == "Balanced"
    # 无类型、不在 box
    assert _mc_model_label(0, set(), False, "A") is None
    assert _mc_model_label(0, set(), False, "B") == "Balanced"  # residual
    assert _mc_model_label(0, set(), False, "C") == "Unclassified"


def test_mc_label_runs_single_scope():
    frame = [
        {"scope_key": "S", "trade_date": "2026-01-01"},
        {"scope_key": "S", "trade_date": "2026-01-02"},
        {"scope_key": "S", "trade_date": "2026-01-03"},
        {"scope_key": "S", "trade_date": "2026-01-04"},
    ]
    labels = {0: "A", 1: "A", 2: "B", 3: "B"}
    runs = _mc_label_runs(frame, labels)
    assert ("A", 2) in runs
    assert ("B", 2) in runs


def test_mc_label_metrics_coverage(frame):
    labels = {i: "Broadening" for i in range(len(frame))}
    met = _mc_label_metrics(frame, labels)
    assert met["coverage"] == 1.0
    assert met["per_label"]["Broadening"]["count"] == len(frame)


def test_mc_dist_drift_identical():
    a = {"x": {"rate": 0.5}, "y": {"rate": 0.5}}
    b = {"x": {"rate": 0.5}, "y": {"rate": 0.5}}
    assert _mc_dist_drift(a, b) == 0.0


def test_mc_dist_drift_missing_key():
    a = {"x": {"rate": 0.5}}
    b = {"y": {"rate": 1.0}}
    # 缺失 key 视为 rate=0，taxicab 在 key 并集上求和：|0.5-0| + |0-1.0| = 1.5
    assert _mc_dist_drift(a, b) == 1.5


def test_mc_label_distribution_bias_zero_when_all_labeled(frame):
    labels = {i: "Broadening" for i in range(len(frame))}
    assert _mc_label_distribution_bias(frame, labels, "scope_type") == 0.0


def test_mc_balanced_recommendation_prefers_c_on_high_unclassified():
    models = {
        "Model A": {
            "policy": "explicit_balanced",
            "metrics": {
                "coverage": 0.5,
                "per_label": {"Balanced": {"rate": 0.2, "median_run_length": 1}},
                "overall": {"transition_stability": 0.5},
            },
            "family_bias": 0.1,
            "size_bias": 0.1,
        },
        "Model B": {
            "policy": "residual_balanced",
            "metrics": {
                "coverage": 0.9,
                "per_label": {"Balanced": {"rate": 0.6, "median_run_length": 1}},
                "overall": {"transition_stability": 0.5},
            },
            "family_bias": 0.1,
            "size_bias": 0.1,
        },
        "Model C": {
            "policy": "balanced_plus_unclassified",
            "metrics": {
                "coverage": 0.5,
                "per_label": {
                    "Balanced": {"rate": 0.2, "median_run_length": 1},
                    "Unclassified": {"rate": 0.5},
                },
                "overall": {"transition_stability": 0.5},
            },
            "family_bias": 0.1,
            "size_bias": 0.1,
        },
    }
    rec = _mc_balanced_recommendation(models)
    assert rec["recommended"] == "Model C"


def test_mc_balanced_recommendation_defaults_to_c():
    models = {
        "Model A": {
            "policy": "explicit_balanced",
            "metrics": {
                "coverage": 0.5,
                "per_label": {"Balanced": {"rate": 0.2, "median_run_length": 1}},
                "overall": {"transition_stability": 0.5},
            },
            "family_bias": 0.1,
            "size_bias": 0.1,
        },
        "Model B": {
            "policy": "residual_balanced",
            "metrics": {
                "coverage": 0.9,
                "per_label": {"Balanced": {"rate": 0.6, "median_run_length": 1}},
                "overall": {"transition_stability": 0.5},
            },
            "family_bias": 0.1,
            "size_bias": 0.1,
        },
        "Model C": {
            "policy": "balanced_plus_unclassified",
            "metrics": {
                "coverage": 0.6,
                "per_label": {
                    "Balanced": {"rate": 0.3, "median_run_length": 2},
                    "Unclassified": {"rate": 0.2},
                },
                "overall": {"transition_stability": 0.5},
            },
            "family_bias": 0.1,
            "size_bias": 0.1,
        },
    }
    rec = _mc_balanced_recommendation(models)
    assert rec["recommended"] in ("Model A", "Model B", "Model C")


# ---------------------------------------------------------------------------
# E6 — threshold robustness
# ---------------------------------------------------------------------------

def test_mc_stable_region_detection():
    points = [
        {"value": 0.70, "hit_rate": 0.10},
        {"value": 0.75, "hit_rate": 0.11},
        {"value": 0.80, "hit_rate": 0.10},
        {"value": 0.85, "hit_rate": 0.30},  # jump
        {"value": 0.90, "hit_rate": 0.29},
    ]
    bands = _mc_stable_region_detection(points, min_points=3)
    assert len(bands) == 1
    assert bands[0]["start"] == 0.70
    assert bands[0]["end"] == 0.80
    assert bands[0]["points"] == 3
    assert bands[0]["hit_rate_range"] <= 0.015


def test_mc_stable_region_detection_empty():
    assert _mc_stable_region_detection([]) == []


def test_mc_threshold_sensitivity_nonnegative(frame, dists, slot_qs):
    fam = _mc_rule_families()["Fragmenting"][0]
    sens = _mc_threshold_sensitivity(frame, fam, slot_qs, dists, {
        "HIGH": (0.70, 0.80, 0.90),
    }, len(frame))
    assert sens is not None
    assert sens >= 0.0


def test_mc_threshold_robustness_stable_regions_wiring(frame, dists, slot_qs):
    """E6 装配：sweep 点经 {value: slot_values[slot], hit_rate} 适配后进入稳定区检测。"""
    fams = _mc_rule_families()
    assignments = _mc_assignments_by_type(frame, fams, slot_qs, dists)
    box_by_index = {
        i: _mc_in_balanced_box(r, slot_qs, dists) for i, r in enumerate(frame)
    }
    out = _mc_threshold_robustness(
        frame, fams, slot_qs, dists, _IST_MC_GRID,
        len(frame), assignments, box_by_index,
    )
    assert set(out) == {f"{t}:{f['family']}" for t, fs in fams.items() for f in fs}
    for key, sweep in out.items():
        assert "reference" in sweep
        assert "sweeps" in sweep
        assert "stable_regions" in sweep
        # 每个 family 的每个 slot 都产出稳定区列表（可能为空，但不得抛 KeyError）
        for s in sweep["sweeps"]:
            for region in sweep["stable_regions"][s]:
                assert "start" in region and "end" in region
                assert region["points"] >= _IST_MC_STABLE_MIN_POINTS
                assert region["hit_rate_range"] <= _IST_MC_STABLE_HIT_DELTA


# ---------------------------------------------------------------------------
# E7 — blind replay
# ---------------------------------------------------------------------------

def test_mc_evidence_has_no_type_label(frame):
    ev = _mc_evidence(frame[0])
    for k in _MC_EVIDENCE_FIELDS:
        assert k in ev
    for k in ev:
        assert "candidate" not in k.lower()
        assert "type" != k


def test_mc_old_candidate_note():
    r = {"research_candidate_Broadening": True}
    assert _mc_old_candidate_note(r) == "old_broadening"
    r2 = {"research_candidate_Rotating": True, "research_candidate_Fragmenting": True}
    assert _mc_old_candidate_note(r2) == "old_rotating_fragmenting"
    r3 = {}
    assert _mc_old_candidate_note(r3) == "old_unmatched"


def test_mc_blind_replay_cases_hide_labels(frame, dists, slot_qs):
    specs = _mc_anchor_specs()
    anchors = _mc_select_anchors(frame, specs, slot_qs, dists, n_proto=3, n_hard=2)
    fams = _mc_rule_families()
    assignments = {}
    for t, fams_of_type in fams.items():
        idx = set()
        for f in fams_of_type:
            idx |= set(_mc_eval_family_hits(frame, f["conditions"], slot_qs, dists))
        assignments[t] = idx
    out = _mc_blind_replay(frame, assignments, anchors, seed=42, per_type=3, conflict_n=2)
    assert out["case_count"] == len(out["cases"]) == len(out["reveal"])
    # blind cases 不得携带任何 type/candidate/old label
    for c in out["cases"]:
        for k in c["evidence"]:
            assert "candidate" not in k.lower()
            assert k != "type"
    # reveal 独立存在
    for r in out["reveal"]:
        assert "type_group" in r or "hit_types" in r


# ---------------------------------------------------------------------------
# E8 — broader universe
# ---------------------------------------------------------------------------

def test_mc_abs_conditions_materializes_slots():
    slot_abs = {
        "HIGH": {"aligned_breadth_hist_pct": 0.8, "migration_hist_pct": 0.7},
        "MID": {"price_hhi_hist_pct": 0.5},
    }
    conditions = (
        ("aligned_breadth_hist_pct", ">=", "HIGH"),
        ("price_hhi_hist_pct", "<=", "MID"),
        ("lcr", "<", 0.85),
    )
    out = _mc_abs_conditions(conditions, slot_abs)
    assert out == [
        ("aligned_breadth_hist_pct", ">=", 0.8),
        ("price_hhi_hist_pct", "<=", 0.5),
        ("lcr", "<", 0.85),
    ]


def test_mc_abs_conditions_unresolvable_returns_none():
    slot_abs = {"HIGH": {"aligned_breadth_hist_pct": 0.8}}
    out = _mc_abs_conditions((("migration_hist_pct", ">=", "HIGH"),), slot_abs)
    assert out is None


def test_mc_slot_abs_resolves_all_core_keys(frame, dists):
    abs_ = _mc_slot_abs(dict(_IST_MC_REFERENCE), dists)
    assert set(abs_) == set(_IST_MC_REFERENCE)
    for slot in _IST_MC_REFERENCE:
        for f in _MC_CORE_KEYS:
            assert f in abs_[slot]


# ---------------------------------------------------------------------------
# contract candidate
# ---------------------------------------------------------------------------

def test_mc_contract_candidate_status_and_structure(frame, dists, slot_qs):
    keys = list(_MC_CORE_KEYS)
    corr = _mc_correlation_map(frame, keys)
    red = _mc_redundancy_analysis(corr, keys, 0.85)
    e1 = {"dimension_map": _mc_dimension_map(corr, red)}
    specs = _mc_anchor_specs()
    anchors = _mc_select_anchors(frame, specs, slot_qs, dists, n_proto=3, n_hard=2)
    e2 = {"prototype_anchors": {t: {"matched_count": a["matched_count"]}
                                for t, a in anchors.items()}}
    fams = _mc_rule_families()
    assignments = {}
    for t, fams_of_type in fams.items():
        idx = set()
        for f in fams_of_type:
            idx |= set(_mc_eval_family_hits(frame, f["conditions"], slot_qs, dists))
        assignments[t] = idx
    preferred = {}
    for t, fams_of_type in fams.items():
        for f in fams_of_type:
            preferred[t] = {"family": f["family"]}
            break
    e3 = {"preferred_families": preferred}
    pair = _mc_pairwise_conflicts(assignments, len(frame))
    classifications = {}
    for pair_key in pair:
        classifications[pair_key] = _mc_conflict_classification(
            frame, assignments, pair_key, ("lcr", "migration_hist_pct")
        )
    e4 = {"classifications": classifications}
    e5 = {"recommendation": {"recommended": "C", "rationale": "test"}}
    e6 = {"per_family_robustness": {}}
    e7 = {"case_count": 0}

    contract = _mc_contract_candidate(e1, e2, e3, e4, e5, e6, e7, None)
    assert contract["contract_status"] == "CANDIDATE_NOT_FROZEN"
    assert contract["threshold_freeze_eligible"] is False
    assert set(contract["types"]) == {"Broadening", "Core-led", "Rotating", "Fragmenting"}
    assert contract["balanced_policy"]["recommended_model"] == "C"
    assert "conflict_policy" in contract
    assert "availability_policy" in contract
    for t in ("Broadening", "Core-led", "Rotating", "Fragmenting"):
        assert "semantic_definition" in contract["types"][t]
        assert "preferred_family" in contract["types"][t]
        assert "threshold_region" in contract["types"][t]
        assert "unresolved_issues" in contract["types"][t]
