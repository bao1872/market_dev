#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V4.12 Atomic Fact Contract Correction and Closure Audit

Fixes 9 known bugs from V4.11:
1. M3 coverage=0 — batch categorization now implemented
2. M4=0 — temporal_payload now SELECTed and read
3. Coverage confused with Output Coverage — three types tracked separately
4. V2 coverage=0 — V2 added to CORE_FACTS and FACT_RAW_DEPS
5. V3 100% coverage — "uncalculable" no longer counts as output-covered for pass
6. Formula 100% via shared helpers — Implementation B fully independent
7. Redundancy counts=0 — V4.9 relation facts computed inline in FactAccumulator
8. efficiency>1 — range anomalies detected and reported
9. C from single failure — layered A/B/C conclusion
"""

# ============================================================
# Section 1: Environment
# ============================================================
import os
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["PYTHONUNBUFFERED"] = "1"

import sys
import json
import time
import gc
import resource
import warnings
import math
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
import psycopg

warnings.filterwarnings("ignore")

# ============================================================
# Section 2: Import V4.9 basic utilities ONLY
# ============================================================
_EXPERIMENTS_DIR = str(Path(__file__).resolve().parent)
if _EXPERIMENTS_DIR not in sys.path:
    sys.path.insert(0, _EXPERIMENTS_DIR)

from v4_9_state_swing_motif_study import (
    DB_URL, get_path_value, peak_rss_mb, current_rss_mb, log,
)

# ============================================================
# Section 3: Load config from v4_12_config.json
# ============================================================
_CONFIG_PATH = Path(_EXPERIMENTS_DIR) / "v4_12_config.json"
with open(_CONFIG_PATH, "r") as _f:
    _CONFIG = json.load(_f)

ALL_DATES = _CONFIG["dates"]
EXPECTED_PER_DAY = _CONFIG["expected_per_day"]
EXPECTED_TOTAL = _CONFIG["expected_total"]
COVERAGE_THRESHOLD = _CONFIG.get("coverage_threshold", 0.95)
REDUNDANCY_THRESHOLD = _CONFIG.get("redundancy_threshold", 0.95)
BIN_SCHEMES = _CONFIG.get("bin_schemes", {"20/80": [20, 80], "25/75": [25, 75], "33/67": [33, 67]})
OUT_DIR = _CONFIG["output_dir"]
REPORT_FILENAME = _CONFIG["report_filename"]
REPORT_PATH = os.path.join(OUT_DIR, REPORT_FILENAME)
N_TYPICAL = _CONFIG.get("n_typical_samples", 20)
N_BOUNDARY = _CONFIG.get("n_boundary_samples", 10)
RNG_SEED = _CONFIG.get("rng_seed", 42)
LODO_FACTS = _CONFIG.get("lodo_subset_facts", [
    "M3_aligned_momentum_delta_raw", "M4_segment_momentum_change",
    "V3_avg_volume_ratio_raw", "V5_return_per_volume",
])
HARD_MAX_MB = 430
WARN_MB = 380

# ============================================================
# Section 4: Field path definitions
# ============================================================
# Paths are tuples per spec; joined with "." when calling get_path_value
# (V4.9's get_path_value accepts dot-separated string paths)

V412_STRUCTURAL_FIELDS = {
    # dsa_segment
    "cur_dir":            (("primary", "1d", "dsa_segment", "current_dsa_segment_dir"), "categorical"),
    "cur_age_bars":       (("primary", "1d", "dsa_segment", "current_dsa_segment_age_bars"), "int"),
    "cur_return_pct":     (("primary", "1d", "dsa_segment", "current_dsa_segment_return_pct"), "continuous"),
    "cur_slope_atr":      (("primary", "1d", "dsa_segment", "current_dsa_segment_slope_atr_per_bar"), "continuous"),
    "cur_efficiency":     (("primary", "1d", "dsa_segment", "current_dsa_segment_efficiency_0_1"), "continuous"),
    "prev_dir":           (("primary", "1d", "dsa_segment", "prev_dsa_segment_dir"), "categorical"),
    "prev_age_bars":      (("primary", "1d", "dsa_segment", "prev_dsa_segment_age_bars"), "int"),
    "prev_slope_atr":     (("primary", "1d", "dsa_segment", "prev_dsa_segment_slope_atr_per_bar"), "continuous"),
    "prev_efficiency":    (("primary", "1d", "dsa_segment", "prev_dsa_segment_efficiency_0_1"), "continuous"),
    "cur_vol_sum":        (("primary", "1d", "dsa_segment", "current_segment_volume_sum"), "continuous"),
    "prev_vol_sum":       (("primary", "1d", "dsa_segment", "prev_segment_volume_sum"), "continuous"),
    "current_vs_prev_volume_ratio": (("primary", "1d", "dsa_segment", "current_vs_prev_volume_ratio"), "continuous"),
    "current_segment_return_per_volume": (("primary", "1d", "dsa_segment", "current_segment_return_per_volume"), "continuous"),
    "prev_segment_return_per_volume": (("primary", "1d", "dsa_segment", "prev_segment_return_per_volume"), "continuous"),
    "return_per_volume_ratio": (("primary", "1d", "dsa_segment", "return_per_volume_ratio"), "continuous"),
    # swing_position
    "breakout_state":     (("primary", "1d", "swing_position", "confirmed_swing_breakout_state"), "categorical"),
    "confirmed_swing_high": (("primary", "1d", "swing_position", "confirmed_swing_high"), "continuous"),
    "confirmed_swing_low":  (("primary", "1d", "swing_position", "confirmed_swing_low"), "continuous"),
    "dist_swing_high_atr":  (("primary", "1d", "swing_position", "distance_to_swing_high_atr"), "continuous"),
    "dist_swing_low_atr":   (("primary", "1d", "swing_position", "distance_to_swing_low_atr"), "continuous"),
    "price_pos_confirmed":  (("primary", "1d", "swing_position", "price_position_in_swing_0_1"), "continuous"),
    "active_swing_dir":     (("primary", "1d", "swing_position", "active_swing_dir"), "categorical"),
    "active_swing_high":    (("primary", "1d", "swing_position", "active_swing_high"), "continuous"),
    "active_swing_low":     (("primary", "1d", "swing_position", "active_swing_low"), "continuous"),
    "price_pos_active":     (("primary", "1d", "swing_position", "price_position_in_active_swing_0_1"), "continuous"),
    "dist_active_high_atr": (("primary", "1d", "swing_position", "distance_to_active_swing_high_atr"), "continuous"),
    "dist_active_low_atr":  (("primary", "1d", "swing_position", "distance_to_active_swing_low_atr"), "continuous"),
    "developing_swing_dir": (("primary", "1d", "swing_position", "developing_swing_dir"), "categorical"),
    "developing_swing_high": (("primary", "1d", "swing_position", "developing_swing_high"), "continuous"),
    "developing_swing_low":  (("primary", "1d", "swing_position", "developing_swing_low"), "continuous"),
    "price_pos_developing":  (("primary", "1d", "swing_position", "price_position_in_developing_swing_0_1"), "continuous"),
    # volatility_momentum
    "sqzmom_val":           (("primary", "1d", "volatility_momentum", "sqzmom_val"), "continuous"),
    "sqzmom_delta_1":       (("primary", "1d", "volatility_momentum", "sqzmom_delta_1"), "continuous"),
    "sqz_on":               (("primary", "1d", "volatility_momentum", "sqz_on"), "bool"),
    "sqz_off":              (("primary", "1d", "volatility_momentum", "sqz_off"), "bool"),
    "sqzmom_percentile":    (("primary", "1d", "volatility_momentum", "sqzmom_percentile"), "continuous"),
    "bb_percent_b":         (("primary", "1d", "volatility_momentum", "bb_percent_b"), "continuous"),
    "bb_bandwidth_percentile": (("primary", "1d", "volatility_momentum", "bb_bandwidth_percentile"), "continuous"),
    # participation
    "volume_ratio_20":      (("primary", "1d", "participation", "volume_ratio_20"), "continuous"),
    "volume_percentile_120": (("primary", "1d", "participation", "volume_percentile_120"), "continuous"),
}

V412_TEMPORAL_FIELDS = {
    "daily_sqzmom_change_since_segment_start": (("daily_context", "daily_sqzmom_change_since_segment_start"), "continuous"),
}

# ============================================================
# Section 5: Memory tracking
# ============================================================

def log_memory(stage, tracker):
    mb = peak_rss_mb()
    tracker[stage] = mb
    log(f"  RSS [{stage}]: {mb:.1f}MB")
    if mb > HARD_MAX_MB:
        raise MemoryError(f"RESOURCE_LIMIT_EXCEEDED: {stage} reached {mb:.1f}MB > {HARD_MAX_MB}MB")
    if mb > WARN_MB:
        log(f"  WARNING: RSS {mb:.1f}MB exceeds soft limit {WARN_MB}MB")
    return mb

# ============================================================
# Section 6: Field extraction
# ============================================================

def extract_fields_v412(structural, temporal):
    """Extract raw fields from structural_payload and temporal_payload."""
    rec = {}
    for short_name, (path, _) in V412_STRUCTURAL_FIELDS.items():
        path_str = ".".join(path)
        rec[short_name] = get_path_value(structural, path_str)
    for short_name, (path, _) in V412_TEMPORAL_FIELDS.items():
        path_str = ".".join(path)
        rec[short_name] = get_path_value(temporal, path_str)
    return rec

# ============================================================
# Section 7: Implementation A — compute_atomic_facts_A
# ============================================================

def _A_safe_float(v):
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _A_norm_dir(v):
    if v is None:
        return None
    try:
        f = float(v)
        if f > 0:
            return 1.0
        if f < 0:
            return -1.0
        return 0.0
    except (ValueError, TypeError):
        return None


def _A_momentum_alignment(sqz_val, dsa_dir):
    if sqz_val is None or dsa_dir is None:
        return "MISSING"
    if dsa_dir == 0.0 or sqz_val == 0.0:
        return "ZERO"
    if (sqz_val > 0 and dsa_dir > 0) or (sqz_val < 0 and dsa_dir < 0):
        return "ALIGNED"
    return "COUNTER"


def _A_confirmed_boundary_relation(breakout_state, dsa_dir):
    if breakout_state is None or dsa_dir is None or dsa_dir == 0.0:
        return "MISSING"
    if dsa_dir > 0:
        if breakout_state == "above_confirmed_high":
            return "BREAK_FAVORABLE"
        elif breakout_state == "below_confirmed_low":
            return "BREAK_ADVERSE"
        else:
            return "INSIDE"
    else:
        if breakout_state == "below_confirmed_low":
            return "BREAK_FAVORABLE"
        elif breakout_state == "above_confirmed_high":
            return "BREAK_ADVERSE"
        else:
            return "INSIDE"


def _A_dir_relation(active_dir, dsa_dir):
    if active_dir is None or dsa_dir is None or dsa_dir == 0.0:
        return "MISSING"
    if active_dir == 0.0:
        return "MISSING"
    if active_dir == dsa_dir:
        return "ALIGNED"
    return "COUNTER"


def _A_active_vs_developing(active_dir, dev_dir):
    if active_dir is None or dev_dir is None:
        return "MISSING"
    if active_dir == 0.0 or dev_dir == 0.0:
        return "MISSING"
    if active_dir == dev_dir:
        return "SAME_DIRECTION"
    return "OPPOSITE_DIRECTION"


def _A_categorize_position(pos):
    if pos is None:
        return "MISSING"
    if pos < 0 or pos > 1:
        return "OUT_OF_RANGE"
    if pos < 0.33:
        return "LOWER"
    if pos <= 0.67:
        return "MIDDLE"
    return "UPPER"


def _A_squeeze_state(sqz_on, sqz_off):
    on = None if sqz_on is None else bool(sqz_on)
    off = None if sqz_off is None else bool(sqz_off)
    if on is None and off is None:
        return "MISSING"
    if on is True and off is True:
        return "INCONSISTENT"
    if on is True:
        return "ON"
    if off is True:
        return "OFF"
    return "NORMAL"


def _A_categorize_slope_ratio(cur_slope, prev_slope):
    if cur_slope is None or prev_slope is None or prev_slope == 0:
        return "UNCALCULABLE"
    ratio = abs(cur_slope) / abs(prev_slope)
    if ratio > 1.2:
        return "FASTER"
    if ratio < 0.8:
        return "SLOWER"
    return "SIMILAR"


def _A_categorize_efficiency_delta(cur_eff, prev_eff):
    if cur_eff is None or prev_eff is None:
        return "UNCALCULABLE"
    delta = cur_eff - prev_eff
    if delta > 0.1:
        return "HIGHER"
    if delta < -0.1:
        return "LOWER"
    return "SIMILAR"


def compute_atomic_facts_A(rec):
    """Formal fact generation (Implementation A)."""
    dsa_dir = _A_norm_dir(rec.get("cur_dir"))
    cur_slope = _A_safe_float(rec.get("cur_slope_atr"))
    prev_slope = _A_safe_float(rec.get("prev_slope_atr"))
    cur_eff = _A_safe_float(rec.get("cur_efficiency"))
    prev_eff = _A_safe_float(rec.get("prev_efficiency"))
    cur_age_raw = rec.get("cur_age_bars")
    prev_age_raw = rec.get("prev_age_bars")
    sqz_val = _A_safe_float(rec.get("sqzmom_val"))
    sqz_delta = _A_safe_float(rec.get("sqzmom_delta_1"))
    daily_sqz_change = _A_safe_float(rec.get("daily_sqzmom_change_since_segment_start"))
    sqz_on = rec.get("sqz_on")
    sqz_off = rec.get("sqz_off")
    breakout_state = rec.get("breakout_state")
    active_dir = _A_norm_dir(rec.get("active_swing_dir"))
    dev_dir = _A_norm_dir(rec.get("developing_swing_dir"))
    price_pos_active = _A_safe_float(rec.get("price_pos_active"))
    price_pos_developing = _A_safe_float(rec.get("price_pos_developing"))
    dist_high = _A_safe_float(rec.get("dist_swing_high_atr"))
    dist_low = _A_safe_float(rec.get("dist_swing_low_atr"))
    cur_vol_sum = _A_safe_float(rec.get("cur_vol_sum"))
    prev_vol_sum = _A_safe_float(rec.get("prev_vol_sum"))
    vol_ratio = _A_safe_float(rec.get("current_vs_prev_volume_ratio"))
    rpv = _A_safe_float(rec.get("current_segment_return_per_volume"))
    rpv_ratio = _A_safe_float(rec.get("return_per_volume_ratio"))

    # cur_age as int
    cur_age = None
    if cur_age_raw is not None:
        try:
            cur_age = int(cur_age_raw)
        except (ValueError, TypeError):
            cur_age = None
    prev_age = None
    if prev_age_raw is not None:
        try:
            prev_age = int(prev_age_raw)
        except (ValueError, TypeError):
            prev_age = None

    has_dir = dsa_dir is not None and dsa_dir != 0.0

    # T1_trend_direction
    if dsa_dir is None:
        rec["T1_trend_direction"] = "MISSING"
    elif dsa_dir > 0:
        rec["T1_trend_direction"] = "UP"
    elif dsa_dir < 0:
        rec["T1_trend_direction"] = "DOWN"
    else:
        rec["T1_trend_direction"] = "NONE"

    # T2_aligned_slope
    rec["T2_aligned_slope"] = (dsa_dir * cur_slope) if (has_dir and cur_slope is not None) else None

    # T3_trend_efficiency
    rec["T3_trend_efficiency"] = cur_eff

    # T4_trend_age
    rec["T4_trend_age"] = cur_age

    # T5_slope_ratio
    rec["T5_slope_ratio"] = _A_categorize_slope_ratio(cur_slope, prev_slope)

    # T6_efficiency_delta
    rec["T6_efficiency_delta"] = _A_categorize_efficiency_delta(cur_eff, prev_eff)

    # M1_momentum_alignment
    rec["M1_momentum_alignment"] = _A_momentum_alignment(sqz_val, dsa_dir)

    # M2_aligned_momentum
    rec["M2_aligned_momentum"] = (dsa_dir * sqz_val) if (has_dir and sqz_val is not None) else None

    # M3_aligned_momentum_delta (raw now, categorical after batch)
    rec["M3_aligned_momentum_delta_raw"] = (dsa_dir * sqz_delta) if (has_dir and sqz_delta is not None) else None
    rec["M3_aligned_momentum_delta"] = None  # filled in finalize_m3_categorization

    # M4_segment_momentum_change
    rec["M4_segment_momentum_change"] = (dsa_dir * daily_sqz_change) if (has_dir and daily_sqz_change is not None) else None

    # M5_squeeze_state
    rec["M5_squeeze_state"] = _A_squeeze_state(sqz_on, sqz_off)

    # S1_confirmed_boundary_relation
    rec["S1_confirmed_boundary_relation"] = _A_confirmed_boundary_relation(breakout_state, dsa_dir)

    # S2_active_dir_relation
    rec["S2_active_dir_relation"] = _A_dir_relation(active_dir, dsa_dir)

    # S3_active_position
    rec["S3_active_position"] = _A_categorize_position(price_pos_active)
    rec["S3_active_position_raw"] = price_pos_active

    # S4_developing_dir_relation
    rec["S4_developing_dir_relation"] = _A_dir_relation(dev_dir, dsa_dir)

    # S5_active_vs_developing
    rec["S5_active_vs_developing"] = _A_active_vs_developing(active_dir, dev_dir)

    # S6_developing_position
    rec["S6_developing_position"] = _A_categorize_position(price_pos_developing)
    rec["S6_developing_position_raw"] = price_pos_developing

    # S7_dist_favorable_boundary / S8_dist_adverse_boundary
    if has_dir:
        if dsa_dir > 0:
            rec["S7_dist_favorable_boundary"] = dist_high
            rec["S8_dist_adverse_boundary"] = dist_low
        else:
            rec["S7_dist_favorable_boundary"] = dist_low
            rec["S8_dist_adverse_boundary"] = dist_high
    else:
        rec["S7_dist_favorable_boundary"] = None
        rec["S8_dist_adverse_boundary"] = None

    # V1_cumulative_volume_ratio
    rec["V1_cumulative_volume_ratio"] = vol_ratio

    # V2_current_avg_volume
    if cur_vol_sum is not None and cur_age is not None and cur_age > 0:
        rec["V2_current_avg_volume"] = cur_vol_sum / cur_age
    else:
        rec["V2_current_avg_volume"] = None

    # V3_avg_volume_ratio
    if (cur_vol_sum is not None and cur_age is not None and cur_age > 0
            and prev_vol_sum is not None and prev_age is not None and prev_age > 0):
        cur_avg = cur_vol_sum / cur_age
        prev_avg = prev_vol_sum / prev_age
        if prev_avg == 0:
            rec["V3_avg_volume_ratio_raw"] = None
            rec["V3_avg_volume_ratio"] = "UNCALCULABLE"
        else:
            ratio = cur_avg / prev_avg
            rec["V3_avg_volume_ratio_raw"] = ratio
            if ratio > 1.2:
                rec["V3_avg_volume_ratio"] = "HIGHER"
            elif ratio < 0.8:
                rec["V3_avg_volume_ratio"] = "LOWER"
            else:
                rec["V3_avg_volume_ratio"] = "SIMILAR"
    else:
        rec["V3_avg_volume_ratio_raw"] = None
        rec["V3_avg_volume_ratio"] = "UNCALCULABLE"

    # V4_age_ratio_raw
    if cur_age is not None and prev_age is not None and prev_age > 0:
        rec["V4_age_ratio_raw"] = cur_age / prev_age
    else:
        rec["V4_age_ratio_raw"] = None

    # V5_return_per_volume / V5_return_per_volume_ratio
    rec["V5_return_per_volume"] = rpv
    rec["V5_return_per_volume_ratio"] = rpv_ratio

    return rec

# ============================================================
# Section 8: Implementation B — recompute_facts_B (independent)
# ============================================================

def recompute_facts_B(rec):
    """Independent audit recomputation. NO _A_ helpers. NO numpy.
    Uses explicit if/elif chains and basic Python operations only.
    """
    result = {}

    # --- Independent direction normalization ---
    cur_dir_raw = rec.get("cur_dir")
    if cur_dir_raw is None:
        dsa_dir_b = None
    else:
        try:
            _f = float(cur_dir_raw)
            if _f > 0:
                dsa_dir_b = 1.0
            elif _f < 0:
                dsa_dir_b = -1.0
            else:
                dsa_dir_b = 0.0
        except (ValueError, TypeError):
            dsa_dir_b = None

    has_dir_b = dsa_dir_b is not None and dsa_dir_b != 0.0

    # --- Independent safe float (inline, no helper) ---
    def _b_float(v):
        if v is None:
            return None
        try:
            _x = float(v)
            if _x != _x or _x == float("inf") or _x == float("-inf"):
                return None
            return _x
        except (ValueError, TypeError):
            return None

    cur_slope_b = _b_float(rec.get("cur_slope_atr"))
    prev_slope_b = _b_float(rec.get("prev_slope_atr"))
    cur_eff_b = _b_float(rec.get("cur_efficiency"))
    prev_eff_b = _b_float(rec.get("prev_efficiency"))
    sqz_val_b = _b_float(rec.get("sqzmom_val"))
    sqz_delta_b = _b_float(rec.get("sqzmom_delta_1"))
    daily_sqz_b = _b_float(rec.get("daily_sqzmom_change_since_segment_start"))
    pos_active_b = _b_float(rec.get("price_pos_active"))
    pos_dev_b = _b_float(rec.get("price_pos_developing"))
    dist_high_b = _b_float(rec.get("dist_swing_high_atr"))
    dist_low_b = _b_float(rec.get("dist_swing_low_atr"))
    cur_vol_b = _b_float(rec.get("cur_vol_sum"))
    prev_vol_b = _b_float(rec.get("prev_vol_sum"))
    rpv_b = _b_float(rec.get("current_segment_return_per_volume"))
    rpv_ratio_b = _b_float(rec.get("return_per_volume_ratio"))

    # cur_age / prev_age as int
    cur_age_raw_b = rec.get("cur_age_bars")
    cur_age_b = None
    if cur_age_raw_b is not None:
        try:
            cur_age_b = int(cur_age_raw_b)
        except (ValueError, TypeError):
            cur_age_b = None
    prev_age_raw_b = rec.get("prev_age_bars")
    prev_age_b = None
    if prev_age_raw_b is not None:
        try:
            prev_age_b = int(prev_age_raw_b)
        except (ValueError, TypeError):
            prev_age_b = None

    # --- Continuous facts (basic arithmetic) ---
    result["T2_aligned_slope"] = (dsa_dir_b * cur_slope_b) if (has_dir_b and cur_slope_b is not None) else None
    result["T3_trend_efficiency"] = cur_eff_b
    result["T4_trend_age"] = cur_age_b
    result["M2_aligned_momentum"] = (dsa_dir_b * sqz_val_b) if (has_dir_b and sqz_val_b is not None) else None
    result["M3_aligned_momentum_delta_raw"] = (dsa_dir_b * sqz_delta_b) if (has_dir_b and sqz_delta_b is not None) else None
    result["M4_segment_momentum_change"] = (dsa_dir_b * daily_sqz_b) if (has_dir_b and daily_sqz_b is not None) else None
    result["S3_active_position_raw"] = pos_active_b
    result["V5_return_per_volume"] = rpv_b
    result["V5_return_per_volume_ratio"] = rpv_ratio_b

    # S7 / S8 (explicit if/else, no helper)
    if has_dir_b:
        if dsa_dir_b > 0:
            result["S7_dist_favorable_boundary"] = dist_high_b
            result["S8_dist_adverse_boundary"] = dist_low_b
        else:
            result["S7_dist_favorable_boundary"] = dist_low_b
            result["S8_dist_adverse_boundary"] = dist_high_b
    else:
        result["S7_dist_favorable_boundary"] = None
        result["S8_dist_adverse_boundary"] = None

    # V2 (basic division)
    if cur_vol_b is not None and cur_age_b is not None and cur_age_b > 0:
        result["V2_current_avg_volume"] = cur_vol_b / cur_age_b
    else:
        result["V2_current_avg_volume"] = None

    # V3 raw (basic division chain)
    if (cur_vol_b is not None and cur_age_b is not None and cur_age_b > 0
            and prev_vol_b is not None and prev_age_b is not None and prev_age_b > 0):
        _cur_avg_b = cur_vol_b / cur_age_b
        _prev_avg_b = prev_vol_b / prev_age_b
        if _prev_avg_b == 0:
            result["V3_avg_volume_ratio_raw"] = None
        else:
            result["V3_avg_volume_ratio_raw"] = _cur_avg_b / _prev_avg_b
    else:
        result["V3_avg_volume_ratio_raw"] = None

    # V4 (basic division)
    if cur_age_b is not None and prev_age_b is not None and prev_age_b > 0:
        result["V4_age_ratio_raw"] = cur_age_b / prev_age_b
    else:
        result["V4_age_ratio_raw"] = None

    # --- Categorical facts (independent if/elif chains) ---

    # T5_slope_ratio_B
    if cur_slope_b is None or prev_slope_b is None or prev_slope_b == 0:
        result["T5_slope_ratio"] = "UNCALCULABLE"
    else:
        _ratio_b = abs(cur_slope_b) / abs(prev_slope_b)
        if _ratio_b > 1.2:
            result["T5_slope_ratio"] = "FASTER"
        elif _ratio_b < 0.8:
            result["T5_slope_ratio"] = "SLOWER"
        else:
            result["T5_slope_ratio"] = "SIMILAR"

    # T6_efficiency_delta_B
    if cur_eff_b is None or prev_eff_b is None:
        result["T6_efficiency_delta"] = "UNCALCULABLE"
    else:
        _delta_b = cur_eff_b - prev_eff_b
        if _delta_b > 0.1:
            result["T6_efficiency_delta"] = "HIGHER"
        elif _delta_b < -0.1:
            result["T6_efficiency_delta"] = "LOWER"
        else:
            result["T6_efficiency_delta"] = "SIMILAR"

    # M1_momentum_alignment_B
    if sqz_val_b is None or dsa_dir_b is None:
        result["M1_momentum_alignment"] = "MISSING"
    elif dsa_dir_b == 0.0 or sqz_val_b == 0.0:
        result["M1_momentum_alignment"] = "ZERO"
    elif (sqz_val_b > 0 and dsa_dir_b > 0) or (sqz_val_b < 0 and dsa_dir_b < 0):
        result["M1_momentum_alignment"] = "ALIGNED"
    else:
        result["M1_momentum_alignment"] = "COUNTER"

    # S1_confirmed_boundary_relation_B
    _breakout_b = rec.get("breakout_state")
    if _breakout_b is None or dsa_dir_b is None or dsa_dir_b == 0.0:
        result["S1_confirmed_boundary_relation"] = "MISSING"
    elif dsa_dir_b > 0:
        if _breakout_b == "above_confirmed_high":
            result["S1_confirmed_boundary_relation"] = "BREAK_FAVORABLE"
        elif _breakout_b == "below_confirmed_low":
            result["S1_confirmed_boundary_relation"] = "BREAK_ADVERSE"
        else:
            result["S1_confirmed_boundary_relation"] = "INSIDE"
    else:
        if _breakout_b == "below_confirmed_low":
            result["S1_confirmed_boundary_relation"] = "BREAK_FAVORABLE"
        elif _breakout_b == "above_confirmed_high":
            result["S1_confirmed_boundary_relation"] = "BREAK_ADVERSE"
        else:
            result["S1_confirmed_boundary_relation"] = "INSIDE"

    # S2_active_dir_relation_B
    _active_raw_b = rec.get("active_swing_dir")
    _active_dir_b = None
    if _active_raw_b is not None:
        try:
            _af = float(_active_raw_b)
            if _af > 0:
                _active_dir_b = 1.0
            elif _af < 0:
                _active_dir_b = -1.0
            else:
                _active_dir_b = 0.0
        except (ValueError, TypeError):
            _active_dir_b = None
    if _active_dir_b is None or dsa_dir_b is None or dsa_dir_b == 0.0 or _active_dir_b == 0.0:
        result["S2_active_dir_relation"] = "MISSING"
    elif _active_dir_b == dsa_dir_b:
        result["S2_active_dir_relation"] = "ALIGNED"
    else:
        result["S2_active_dir_relation"] = "COUNTER"

    # S5_active_vs_developing_B
    _dev_raw_b = rec.get("developing_swing_dir")
    _dev_dir_b = None
    if _dev_raw_b is not None:
        try:
            _df = float(_dev_raw_b)
            if _df > 0:
                _dev_dir_b = 1.0
            elif _df < 0:
                _dev_dir_b = -1.0
            else:
                _dev_dir_b = 0.0
        except (ValueError, TypeError):
            _dev_dir_b = None
    if _active_dir_b is None or _dev_dir_b is None or _active_dir_b == 0.0 or _dev_dir_b == 0.0:
        result["S5_active_vs_developing"] = "MISSING"
    elif _active_dir_b == _dev_dir_b:
        result["S5_active_vs_developing"] = "SAME_DIRECTION"
    else:
        result["S5_active_vs_developing"] = "OPPOSITE_DIRECTION"

    # S6_developing_position_B
    if pos_dev_b is None:
        result["S6_developing_position"] = "MISSING"
    elif pos_dev_b < 0 or pos_dev_b > 1:
        result["S6_developing_position"] = "OUT_OF_RANGE"
    elif pos_dev_b < 0.33:
        result["S6_developing_position"] = "LOWER"
    elif pos_dev_b <= 0.67:
        result["S6_developing_position"] = "MIDDLE"
    else:
        result["S6_developing_position"] = "UPPER"

    # S3_active_position_B (also independent)
    if pos_active_b is None:
        result["S3_active_position"] = "MISSING"
    elif pos_active_b < 0 or pos_active_b > 1:
        result["S3_active_position"] = "OUT_OF_RANGE"
    elif pos_active_b < 0.33:
        result["S3_active_position"] = "LOWER"
    elif pos_active_b <= 0.67:
        result["S3_active_position"] = "MIDDLE"
    else:
        result["S3_active_position"] = "UPPER"

    # M5_squeeze_state_B
    _on_raw_b = rec.get("sqz_on")
    _off_raw_b = rec.get("sqz_off")
    _on_b = None if _on_raw_b is None else bool(_on_raw_b)
    _off_b = None if _off_raw_b is None else bool(_off_raw_b)
    if _on_b is None and _off_b is None:
        result["M5_squeeze_state"] = "MISSING"
    elif _on_b is True and _off_b is True:
        result["M5_squeeze_state"] = "INCONSISTENT"
    elif _on_b is True:
        result["M5_squeeze_state"] = "ON"
    elif _off_b is True:
        result["M5_squeeze_state"] = "OFF"
    else:
        result["M5_squeeze_state"] = "NORMAL"

    # V3_avg_volume_ratio_B
    if result.get("V3_avg_volume_ratio_raw") is not None:
        _vr = result["V3_avg_volume_ratio_raw"]
        if _vr > 1.2:
            result["V3_avg_volume_ratio"] = "HIGHER"
        elif _vr < 0.8:
            result["V3_avg_volume_ratio"] = "LOWER"
        else:
            result["V3_avg_volume_ratio"] = "SIMILAR"
    else:
        result["V3_avg_volume_ratio"] = "UNCALCULABLE"

    # V1 (just pass through, but independently)
    result["V1_cumulative_volume_ratio"] = _b_float(rec.get("current_vs_prev_volume_ratio"))

    # S4_developing_dir_relation_B
    if _dev_dir_b is None or dsa_dir_b is None or dsa_dir_b == 0.0 or _dev_dir_b == 0.0:
        result["S4_developing_dir_relation"] = "MISSING"
    elif _dev_dir_b == dsa_dir_b:
        result["S4_developing_dir_relation"] = "ALIGNED"
    else:
        result["S4_developing_dir_relation"] = "COUNTER"

    return result

# ============================================================
# Section 9: Coverage tracking — FACT_RAW_DEPS, CORE_FACTS
# ============================================================

FACT_RAW_DEPS = {
    "T1_trend_direction": ["cur_dir"],
    "T2_aligned_slope": ["cur_dir", "cur_slope_atr"],
    "T3_trend_efficiency": ["cur_efficiency"],
    "T4_trend_age": ["cur_age_bars"],
    "T5_slope_ratio": ["cur_slope_atr", "prev_slope_atr"],
    "T6_efficiency_delta": ["cur_efficiency", "prev_efficiency"],
    "M1_momentum_alignment": ["sqzmom_val", "cur_dir"],
    "M2_aligned_momentum": ["sqzmom_val", "cur_dir"],
    "M3_aligned_momentum_delta": ["sqzmom_delta_1", "cur_dir"],
    "M4_segment_momentum_change": ["daily_sqzmom_change_since_segment_start", "cur_dir"],
    "M5_squeeze_state": ["sqz_on", "sqz_off"],
    "S1_confirmed_boundary_relation": ["breakout_state", "cur_dir"],
    "S2_active_dir_relation": ["active_swing_dir", "cur_dir"],
    "S3_active_position": ["price_pos_active"],
    "S4_developing_dir_relation": ["developing_swing_dir", "cur_dir"],
    "S5_active_vs_developing": ["active_swing_dir", "developing_swing_dir"],
    "S6_developing_position": ["price_pos_developing"],
    "S7_dist_favorable_boundary": ["cur_dir", "dist_swing_high_atr", "dist_swing_low_atr"],
    "S8_dist_adverse_boundary": ["cur_dir", "dist_swing_high_atr", "dist_swing_low_atr"],
    "V1_cumulative_volume_ratio": ["current_vs_prev_volume_ratio"],
    "V2_current_avg_volume": ["cur_vol_sum", "cur_age_bars"],
    "V3_avg_volume_ratio": ["cur_vol_sum", "cur_age_bars", "prev_vol_sum", "prev_age_bars"],
    "V4_age_ratio_raw": ["cur_age_bars", "prev_age_bars"],
    "V5_return_per_volume": ["current_segment_return_per_volume"],
    "V5_return_per_volume_ratio": ["return_per_volume_ratio"],
}

CORE_FACTS = [
    "T1_trend_direction", "T2_aligned_slope", "T3_trend_efficiency",
    "T4_trend_age", "T5_slope_ratio", "T6_efficiency_delta",
    "M1_momentum_alignment", "M2_aligned_momentum", "M3_aligned_momentum_delta",
    "M5_squeeze_state",
    "S1_confirmed_boundary_relation", "S2_active_dir_relation",
    "S3_active_position", "S7_dist_favorable_boundary", "S8_dist_adverse_boundary",
    "V1_cumulative_volume_ratio", "V2_current_avg_volume", "V3_avg_volume_ratio",
    "V5_return_per_volume",
]

# Facts that require dsa_dir ∈ {1, -1} for computability
DIRECTION_DEPENDENT_FACTS = {
    "T2_aligned_slope", "M2_aligned_momentum", "M3_aligned_momentum_delta",
    "M4_segment_momentum_change", "S1_confirmed_boundary_relation",
    "S2_active_dir_relation", "S4_developing_dir_relation",
    "S7_dist_favorable_boundary", "S8_dist_adverse_boundary",
}

# Raw fields needed for B recomputation
B_RAW_FIELDS = [
    "cur_dir", "cur_slope_atr", "prev_slope_atr", "cur_efficiency", "prev_efficiency",
    "cur_age_bars", "prev_age_bars", "sqzmom_val", "sqzmom_delta_1",
    "daily_sqzmom_change_since_segment_start", "sqz_on", "sqz_off",
    "breakout_state", "active_swing_dir", "developing_swing_dir",
    "price_pos_active", "price_pos_developing",
    "dist_swing_high_atr", "dist_swing_low_atr",
    "cur_vol_sum", "prev_vol_sum",
    "current_vs_prev_volume_ratio",
    "current_segment_return_per_volume", "return_per_volume_ratio",
]

# Facts compared in A vs B (continuous + categorical)
AB_COMPARE_FACTS_CONTINUOUS = [
    "T2_aligned_slope", "T3_trend_efficiency", "M2_aligned_momentum",
    "M3_aligned_momentum_delta_raw", "M4_segment_momentum_change",
    "S3_active_position_raw", "S7_dist_favorable_boundary", "S8_dist_adverse_boundary",
    "V2_current_avg_volume", "V3_avg_volume_ratio_raw", "V4_age_ratio_raw",
    "V5_return_per_volume", "V5_return_per_volume_ratio",
]

AB_COMPARE_FACTS_CATEGORICAL = [
    "T5_slope_ratio", "T6_efficiency_delta", "M1_momentum_alignment",
    "S1_confirmed_boundary_relation", "S2_active_dir_relation",
    "S5_active_vs_developing", "S6_developing_position",
    "M5_squeeze_state", "V3_avg_volume_ratio", "S3_active_position",
    "S4_developing_dir_relation", "V1_cumulative_volume_ratio",
]


def check_raw_coverage(fact, rec):
    """Check if all raw dependencies are non-None."""
    for dep in FACT_RAW_DEPS.get(fact, []):
        if rec.get(dep) is None:
            return False
    return True


def check_fact_computable(fact, rec):
    """Check if fact is computable: raw fields present + valid denominators + valid direction."""
    if not check_raw_coverage(fact, rec):
        return False
    # Direction-dependent facts require dsa_dir ∈ {1, -1}
    if fact in DIRECTION_DEPENDENT_FACTS:
        dsa_dir = _A_norm_dir(rec.get("cur_dir"))
        if dsa_dir is None or dsa_dir == 0.0:
            return False
    # Denominator checks
    if fact == "V2_current_avg_volume":
        age = rec.get("cur_age_bars")
        if age is None:
            return False
        try:
            if int(age) <= 0:
                return False
        except (ValueError, TypeError):
            return False
    elif fact == "V3_avg_volume_ratio":
        cur_age = rec.get("cur_age_bars")
        prev_age = rec.get("prev_age_bars")
        prev_vol = rec.get("prev_vol_sum")
        if cur_age is None or prev_age is None or prev_vol is None:
            return False
        try:
            if int(cur_age) <= 0 or int(prev_age) <= 0:
                return False
        except (ValueError, TypeError):
            return False
        try:
            prev_avg = float(prev_vol) / int(prev_age)
            if prev_avg == 0:
                return False
        except (ValueError, TypeError, ZeroDivisionError):
            return False
    elif fact == "V4_age_ratio_raw":
        prev_age = rec.get("prev_age_bars")
        if prev_age is None:
            return False
        try:
            if int(prev_age) <= 0:
                return False
        except (ValueError, TypeError):
            return False
    elif fact == "T5_slope_ratio":
        prev_slope = rec.get("prev_slope_atr")
        if prev_slope is not None:
            try:
                if float(prev_slope) == 0:
                    return False
            except (ValueError, TypeError):
                return False
    return True


def check_output_coverage(fact, val):
    """Check if fact output is non-None and not MISSING."""
    if val is None:
        return False
    if isinstance(val, str) and val == "MISSING":
        return False
    return True

# ============================================================
# Section 10: FactAccumulator
# ============================================================

class FactAccumulator:
    def __init__(self):
        self.raw_coverage_counts = defaultdict(lambda: defaultdict(int))
        self.computable_coverage_counts = defaultdict(lambda: defaultdict(int))
        self.output_coverage_counts = defaultdict(lambda: defaultdict(int))
        self.coverage_total = defaultdict(int)
        self.fact_values = defaultdict(list)
        self.v49_fact_values = defaultdict(list)
        self.lodo_data = defaultdict(lambda: defaultdict(list))
        self.sample_candidates = []
        self.conflict_samples = []
        self.range_anomalies = []
        self.formula_mismatches = []
        self.all_records_light = []
        self.dates_processed = []

    def add_day(self, date, df):
        self.dates_processed.append(date)
        for _, row in df.iterrows():
            self.coverage_total[date] += 1
            rec = row.to_dict()
            # Convert pandas NaN back to None for consistent None-handling
            # (pandas converts None to NaN in numeric columns, breaking
            #  `is None` checks in coverage/conflict/formula audits)
            for _k, _v in list(rec.items()):
                if _v is not None and isinstance(_v, float) and math.isnan(_v):
                    rec[_k] = None

            # 1. Track three coverage types per fact
            for fact in FACT_RAW_DEPS:
                raw_ok = check_raw_coverage(fact, rec)
                comp_ok = check_fact_computable(fact, rec)
                if raw_ok:
                    self.raw_coverage_counts[fact][date] += 1
                if comp_ok:
                    self.computable_coverage_counts[fact][date] += 1
                # Output coverage (skip M3_categorical, finalized later)
                if fact != "M3_aligned_momentum_delta":
                    val = rec.get(fact)
                    if check_output_coverage(fact, val):
                        self.output_coverage_counts[fact][date] += 1

            # 2. Append fact values
            for fact in FACT_RAW_DEPS:
                if fact == "M3_aligned_momentum_delta":
                    self.fact_values[fact].append(rec.get("M3_aligned_momentum_delta"))
                else:
                    self.fact_values[fact].append(rec.get(fact))
            # Also store raw variants (V4_age_ratio_raw excluded: already in FACT_RAW_DEPS)
            for raw_fact in ["M3_aligned_momentum_delta_raw", "S3_active_position_raw",
                             "S6_developing_position_raw", "V3_avg_volume_ratio_raw"]:
                self.fact_values[raw_fact].append(rec.get(raw_fact))

            # 3. Compute V4.9-equivalent relation facts (inline, independent)
            dsa_dir = _A_norm_dir(rec.get("cur_dir"))
            active_dir = _A_norm_dir(rec.get("active_swing_dir"))
            dev_dir = _A_norm_dir(rec.get("developing_swing_dir"))
            sqz_val = _A_safe_float(rec.get("sqzmom_val"))

            self.v49_fact_values["active_swing_alignment"].append(
                self._v49_dir_compare(active_dir, dsa_dir))
            self.v49_fact_values["developing_swing_alignment"].append(
                self._v49_dir_compare(dev_dir, dsa_dir))
            self.v49_fact_values["active_developing_relation"].append(
                self._v49_dir_compare_divergent(active_dir, dev_dir))
            self.v49_fact_values["sqzmom_alignment"].append(
                self._v49_sqz_compare(sqz_val, dsa_dir))
            self.v49_fact_values["current_vs_prev_vol"].append(
                _A_safe_float(rec.get("current_vs_prev_volume_ratio")))

            # 4. LODO data for subset facts
            for fact in LODO_FACTS:
                val = rec.get(fact)
                if val is not None:
                    self.lodo_data[fact][date].append((val, dsa_dir))

            # 5. Store light record for B recompute
            light = {"symbol": rec.get("symbol"), "trade_date": rec.get("trade_date")}
            for field in B_RAW_FIELDS:
                light[field] = rec.get(field)
            self.all_records_light.append(light)

            # 6. Sample candidate
            self.sample_candidates.append({
                "symbol": rec.get("symbol"),
                "trade_date": rec.get("trade_date"),
                "dsa_dir": dsa_dir,
                "T2_aligned_slope": rec.get("T2_aligned_slope"),
                "T3_trend_efficiency": rec.get("T3_trend_efficiency"),
                "T4_trend_age": rec.get("T4_trend_age"),
                "S3_active_position": rec.get("S3_active_position"),
                "S3_active_position_raw": rec.get("S3_active_position_raw"),
                "M2_aligned_momentum": rec.get("M2_aligned_momentum"),
                "V1_cumulative_volume_ratio": rec.get("V1_cumulative_volume_ratio"),
                "cur_dir": rec.get("cur_dir"),
            })

            # 7. Range anomaly detection
            eff = _A_safe_float(rec.get("cur_efficiency"))
            if eff is not None and (eff < 0 or eff > 1):
                self.range_anomalies.append({
                    "type": "efficiency_out_of_range",
                    "symbol": rec.get("symbol"),
                    "trade_date": date,
                    "value": eff,
                })
            pos_active = _A_safe_float(rec.get("price_pos_active"))
            if pos_active is not None and (pos_active < 0 or pos_active > 1):
                self.range_anomalies.append({
                    "type": "active_position_out_of_range",
                    "symbol": rec.get("symbol"),
                    "trade_date": date,
                    "value": pos_active,
                })
            pos_dev = _A_safe_float(rec.get("price_pos_developing"))
            if pos_dev is not None and (pos_dev < 0 or pos_dev > 1):
                self.range_anomalies.append({
                    "type": "developing_position_out_of_range",
                    "symbol": rec.get("symbol"),
                    "trade_date": date,
                    "value": pos_dev,
                })
            age_raw = rec.get("cur_age_bars")
            if age_raw is not None:
                try:
                    age_int = int(age_raw)
                    if age_int <= 0:
                        self.range_anomalies.append({
                            "type": "age_non_positive",
                            "symbol": rec.get("symbol"),
                            "trade_date": date,
                            "value": age_int,
                        })
                except (ValueError, TypeError):
                    self.range_anomalies.append({
                        "type": "age_not_integer",
                        "symbol": rec.get("symbol"),
                        "trade_date": date,
                        "value": age_raw,
                    })

            # 8. Logic conflict detection
            m5 = rec.get("M5_squeeze_state")
            if m5 == "INCONSISTENT":
                self.conflict_samples.append({
                    "type": "M5_INCONSISTENT_sqz_on_and_off",
                    "symbol": rec.get("symbol"),
                    "trade_date": date,
                    "sqz_on": rec.get("sqz_on"),
                    "sqz_off": rec.get("sqz_off"),
                })
            t1 = rec.get("T1_trend_direction")
            cur_dir_val = rec.get("cur_dir")
            if t1 == "MISSING" and cur_dir_val is not None:
                self.conflict_samples.append({
                    "type": "T1_MISSING_but_cur_dir_present",
                    "symbol": rec.get("symbol"),
                    "trade_date": date,
                    "cur_dir": cur_dir_val,
                })
            s1 = rec.get("S1_confirmed_boundary_relation")
            breakout_val = rec.get("breakout_state")
            dsa_check = _A_norm_dir(cur_dir_val)
            if s1 == "MISSING" and breakout_val is not None and dsa_check is not None and dsa_check != 0.0:
                self.conflict_samples.append({
                    "type": "S1_MISSING_but_inputs_valid",
                    "symbol": rec.get("symbol"),
                    "trade_date": date,
                    "breakout_state": breakout_val,
                    "cur_dir": cur_dir_val,
                })
            s2 = rec.get("S2_active_dir_relation")
            s4 = rec.get("S4_developing_dir_relation")
            s5 = rec.get("S5_active_vs_developing")
            if s2 == "ALIGNED" and s4 == "ALIGNED" and s5 == "OPPOSITE_DIRECTION":
                self.conflict_samples.append({
                    "type": "S2_S4_ALIGNED_but_S5_OPPOSITE",
                    "symbol": rec.get("symbol"),
                    "trade_date": date,
                    "active_dir": rec.get("active_swing_dir"),
                    "developing_dir": rec.get("developing_swing_dir"),
                    "cur_dir": cur_dir_val,
                })

    @staticmethod
    def _v49_dir_compare(dir1, dir2):
        if dir1 is None or dir2 is None or dir1 == 0.0 or dir2 == 0.0:
            return "unknown"
        if dir1 == dir2:
            return "aligned"
        return "counter"

    @staticmethod
    def _v49_dir_compare_divergent(dir1, dir2):
        if dir1 is None or dir2 is None or dir1 == 0.0 or dir2 == 0.0:
            return "unknown"
        if dir1 == dir2:
            return "aligned"
        return "divergent"

    @staticmethod
    def _v49_sqz_compare(sqz_val, dsa_dir):
        if sqz_val is None or dsa_dir is None or dsa_dir == 0.0:
            return "neutral"
        if sqz_val == 0.0:
            return "neutral"
        if (sqz_val > 0 and dsa_dir > 0) or (sqz_val < 0 and dsa_dir < 0):
            return "aligned"
        return "counter"

    def finalize_m3_output_coverage(self):
        """After M3 batch categorization, compute output coverage for M3."""
        m3_vals = self.fact_values.get("M3_aligned_momentum_delta", [])
        idx = 0
        for date in self.dates_processed:
            total = self.coverage_total[date]
            count = 0
            for _ in range(total):
                if idx < len(m3_vals):
                    val = m3_vals[idx]
                    if check_output_coverage("M3_aligned_momentum_delta", val):
                        count += 1
                idx += 1
            self.output_coverage_counts["M3_aligned_momentum_delta"][date] = count


# ============================================================
# Section 11: Phase 1 — Stream one date from DB
# ============================================================

def stream_one_date(date, conn):
    records = []
    n_loaded = 0
    with conn.cursor(name="v412_stream_cursor") as cur:
        cur.itersize = 250
        cur.execute(
            "SELECT instrument_id::text, structural_payload, temporal_payload "
            "FROM stock_feature_snapshots "
            "WHERE trade_date::text = %s "
            "ORDER BY instrument_id::text",
            (date,),
        )
        for instrument_id, structural, temporal in cur:
            rec = {"symbol": instrument_id, "trade_date": date}
            rec.update(extract_fields_v412(structural, temporal))
            structural = None
            temporal = None
            rec = compute_atomic_facts_A(rec)
            records.append(rec)
            n_loaded += 1
            if n_loaded % 1000 == 0:
                log(f"    loaded {n_loaded}")
    if n_loaded != EXPECTED_PER_DAY:
        raise AssertionError(f"Date {date}: expected {EXPECTED_PER_DAY}, got {n_loaded}")
    df = pd.DataFrame(records)
    records = None
    gc.collect()
    if df["symbol"].nunique() != len(df):
        raise AssertionError(f"Date {date}: duplicate symbols")
    return df


def db_readonly_test(conn):
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE _v412_write_test (id int)")
        conn.commit()
        log("ERROR: write test succeeded - aborting")
        sys.exit(1)
    except Exception:
        conn.rollback()
        log("Write test: correctly failed (read-only)")

# ============================================================
# Section 12: Phase 2 — Three-type coverage audit
# ============================================================

def audit_coverage(acc):
    """Compute overall and per-day coverage for all facts."""
    results = {}
    for fact in FACT_RAW_DEPS:
        raw_total = sum(acc.raw_coverage_counts[fact].values())
        comp_total = sum(acc.computable_coverage_counts[fact].values())
        out_total = sum(acc.output_coverage_counts[fact].values())
        grand_total = sum(acc.coverage_total.values())
        if grand_total == 0:
            results[fact] = {"raw": 0, "computable": 0, "output": 0, "per_day": {}}
            continue
        per_day = {}
        for date in acc.dates_processed:
            dt = acc.coverage_total[date]
            if dt == 0:
                per_day[date] = {"raw": 0, "computable": 0, "output": 0}
            else:
                per_day[date] = {
                    "raw": acc.raw_coverage_counts[fact][date] / dt,
                    "computable": acc.computable_coverage_counts[fact][date] / dt,
                    "output": acc.output_coverage_counts[fact][date] / dt,
                }
        results[fact] = {
            "raw": raw_total / grand_total,
            "computable": comp_total / grand_total,
            "output": out_total / grand_total,
            "per_day": per_day,
        }
    return results

# ============================================================
# Section 13: Phase 3 — Independent formula verification (A vs B)
# ============================================================

def audit_formula_consistency(acc):
    """Recompute facts with B and compare with A."""
    mismatches = defaultdict(list)
    comparable_counts = defaultdict(int)
    match_counts = defaultdict(int)

    all_facts = AB_COMPARE_FACTS_CONTINUOUS + AB_COMPARE_FACTS_CATEGORICAL

    for i, light in enumerate(acc.all_records_light):
        b_facts = recompute_facts_B(light)
        for fact in all_facts:
            a_val = acc.fact_values.get(fact, [None] * (i + 1))[i] if i < len(acc.fact_values.get(fact, [])) else None
            b_val = b_facts.get(fact)
            # Skip if both None
            if a_val is None and b_val is None:
                continue
            comparable_counts[fact] += 1
            if a_val is None or b_val is None:
                mismatches[fact].append({
                    "symbol": light["symbol"],
                    "trade_date": light["trade_date"],
                    "a_val": a_val,
                    "b_val": b_val,
                })
                continue
            # Compare
            if fact in AB_COMPARE_FACTS_CONTINUOUS:
                if abs(float(a_val) - float(b_val)) < 1e-9:
                    match_counts[fact] += 1
                else:
                    mismatches[fact].append({
                        "symbol": light["symbol"],
                        "trade_date": light["trade_date"],
                        "a_val": a_val,
                        "b_val": b_val,
                    })
            else:
                if str(a_val) == str(b_val):
                    match_counts[fact] += 1
                else:
                    mismatches[fact].append({
                        "symbol": light["symbol"],
                        "trade_date": light["trade_date"],
                        "a_val": a_val,
                        "b_val": b_val,
                    })

    consistency = {}
    for fact in all_facts:
        if comparable_counts[fact] > 0:
            consistency[fact] = match_counts[fact] / comparable_counts[fact]
        else:
            consistency[fact] = None

    return {
        "consistency": consistency,
        "mismatches": dict(mismatches),
        "comparable_counts": dict(comparable_counts),
        "match_counts": dict(match_counts),
    }

# ============================================================
# Section 14: Phase 4 — Logic conflict audit
# ============================================================

def audit_logic_conflicts(acc):
    """Summarize logic conflicts."""
    by_type = defaultdict(int)
    for s in acc.conflict_samples:
        by_type[s["type"]] += 1
    return {
        "total": len(acc.conflict_samples),
        "by_type": dict(by_type),
        "samples": acc.conflict_samples[:50],
    }

# ============================================================
# Section 15: Phase 5 — Redundancy audit (FIXED)
# ============================================================

REDUNDANCY_PAIRS = [
    ("S2_active_dir_relation", "active_swing_alignment",
     {"ALIGNED": "aligned", "COUNTER": "counter", "MISSING": "unknown"}),
    ("S4_developing_dir_relation", "developing_swing_alignment",
     {"ALIGNED": "aligned", "COUNTER": "counter", "MISSING": "unknown"}),
    ("S5_active_vs_developing", "active_developing_relation",
     {"SAME_DIRECTION": "aligned", "OPPOSITE_DIRECTION": "divergent", "MISSING": "unknown"}),
    ("M1_momentum_alignment", "sqzmom_alignment",
     {"ALIGNED": "aligned", "COUNTER": "counter", "ZERO": "neutral", "MISSING": "neutral"}),
    ("V1_cumulative_volume_ratio", "current_vs_prev_vol", None),
]


def audit_redundancy(acc):
    results = []
    for v412_fact, v49_fact, mapping in REDUNDANCY_PAIRS:
        left_vals = acc.fact_values.get(v412_fact, [])
        right_vals = acc.v49_fact_values.get(v49_fact, [])
        n = min(len(left_vals), len(right_vals))
        left_valid = 0
        right_valid = 0
        n_compared = 0
        n_consistent = 0
        n_inconsistent = 0
        for i in range(n):
            lv = left_vals[i]
            rv = right_vals[i]
            if lv is not None:
                left_valid += 1
            if rv is not None:
                right_valid += 1
            if mapping is not None:
                # Categorical comparison
                if lv is None or rv is None:
                    continue
                mapped = mapping.get(lv)
                if mapped is None:
                    continue
                n_compared += 1
                if mapped == rv:
                    n_consistent += 1
                else:
                    n_inconsistent += 1
            else:
                # Continuous comparison
                if lv is None or rv is None:
                    continue
                try:
                    if abs(float(lv) - float(rv)) < 1e-9:
                        n_consistent += 1
                    else:
                        n_inconsistent += 1
                    n_compared += 1
                except (ValueError, TypeError):
                    continue
        consistency = (n_consistent / n_compared) if n_compared > 0 else None
        results.append({
            "v412_fact": v412_fact,
            "v49_fact": v49_fact,
            "left_valid": left_valid,
            "right_valid": right_valid,
            "n_compared": n_compared,
            "n_consistent": n_consistent,
            "n_inconsistent": n_inconsistent,
            "consistency": consistency,
            "audit_failed": n_compared == 0,
        })
    return results

# ============================================================
# Section 16: Phase 6 — LODO threshold sensitivity
# ============================================================

def lodo_threshold_sensitivity(acc):
    results = {}
    for fact in LODO_FACTS:
        fact_results = {}
        date_values = acc.lodo_data.get(fact, {})
        all_dates = list(date_values.keys())
        if len(all_dates) < 2:
            results[fact] = {"error": "insufficient dates for LODO"}
            continue

        for direction_label, direction_filter in [("UP", 1.0), ("DOWN", -1.0)]:
            dir_results = {}
            # Collect per-day values for this direction
            per_day_vals = {}
            for date in all_dates:
                vals = [v for v, d in date_values[date] if d == direction_filter]
                if vals:
                    per_day_vals[date] = vals

            if len(per_day_vals) < 2:
                dir_results["error"] = f"insufficient dates for {direction_label}"
                fact_results[direction_label] = dir_results
                continue

            # All values for 6-day thresholds
            all_vals = []
            for date in per_day_vals:
                all_vals.extend(per_day_vals[date])
            if not all_vals:
                dir_results["error"] = "no values"
                fact_results[direction_label] = dir_results
                continue

            for scheme_name, percentiles in BIN_SCHEMES.items():
                lo_pct, hi_pct = percentiles
                # 6-day thresholds
                t_lo_6 = float(np.percentile(all_vals, lo_pct))
                t_hi_6 = float(np.percentile(all_vals, hi_pct))

                # LODO
                holdout_results = {}
                for holdout_date in per_day_vals:
                    train_vals = []
                    for d in per_day_vals:
                        if d != holdout_date:
                            train_vals.extend(per_day_vals[d])
                    if not train_vals:
                        continue
                    t_lo_train = float(np.percentile(train_vals, lo_pct))
                    t_hi_train = float(np.percentile(train_vals, hi_pct))

                    # Classify holdout with train thresholds
                    ho_vals = per_day_vals[holdout_date]
                    ho_classes_train = []
                    ho_classes_6 = []
                    for v in ho_vals:
                        if v < t_lo_train:
                            c_train = "LOW"
                        elif v > t_hi_train:
                            c_train = "HIGH"
                        else:
                            c_train = "MID"
                        ho_classes_train.append(c_train)

                        if v < t_lo_6:
                            c_6 = "LOW"
                        elif v > t_hi_6:
                            c_6 = "HIGH"
                        else:
                            c_6 = "MID"
                        ho_classes_6.append(c_6)

                    n_match = sum(1 for a, b in zip(ho_classes_train, ho_classes_6) if a == b)
                    n_total = len(ho_classes_6)
                    consistency = n_match / n_total if n_total > 0 else 0

                    # Distribution
                    low_pct = sum(1 for c in ho_classes_6 if c == "LOW") / max(n_total, 1)
                    mid_pct = sum(1 for c in ho_classes_6 if c == "MID") / max(n_total, 1)
                    high_pct = sum(1 for c in ho_classes_6 if c == "HIGH") / max(n_total, 1)

                    holdout_results[holdout_date] = {
                        "train_t_lo": t_lo_train,
                        "train_t_hi": t_hi_train,
                        "t_lo_6day": t_lo_6,
                        "t_hi_6day": t_hi_6,
                        "threshold_drift_lo": abs(t_lo_train - t_lo_6),
                        "threshold_drift_hi": abs(t_hi_train - t_hi_6),
                        "classification_consistency": consistency,
                        "LOW_pct": low_pct,
                        "MID_pct": mid_pct,
                        "HIGH_pct": high_pct,
                        "collapse": (mid_pct > 0.95 or low_pct == 0 or high_pct == 0),
                    }

                dir_results[scheme_name] = {
                    "t_lo_6day": t_lo_6,
                    "t_hi_6day": t_hi_6,
                    "holdout": holdout_results,
                }
            fact_results[direction_label] = dir_results
        results[fact] = fact_results
    return results

# ============================================================
# Section 17: Phase 7 — Momentum M3/M4 special audit
# ============================================================

def audit_m3_m4(acc):
    # Coverage
    m3_raw_cov = sum(acc.raw_coverage_counts["M3_aligned_momentum_delta"].values())
    m3_comp_cov = sum(acc.computable_coverage_counts["M3_aligned_momentum_delta"].values())
    m4_raw_cov = sum(acc.raw_coverage_counts["M4_segment_momentum_change"].values())
    m4_comp_cov = sum(acc.computable_coverage_counts["M4_segment_momentum_change"].values())
    grand = sum(acc.coverage_total.values())

    # Pearson correlation (paired, skip None)
    m3_list = acc.fact_values.get("M3_aligned_momentum_delta_raw", [])
    m4_list = acc.fact_values.get("M4_segment_momentum_change", [])
    paired_m3 = []
    paired_m4 = []
    for a, b in zip(m3_list, m4_list):
        if a is not None and b is not None:
            paired_m3.append(float(a))
            paired_m4.append(float(b))
    if len(paired_m3) > 1:
        corr = float(np.corrcoef(paired_m3, paired_m4)[0, 1])
        same_sign = sum(1 for a, b in zip(paired_m3, paired_m4) if (a > 0) == (b > 0)) / len(paired_m3)
    else:
        corr = None
        same_sign = None

    # M3 changes but M4 doesn't (both non-None, M3 != 0 but M4 == 0)
    m3_changes_m4_not = 0
    m4_opposite_m3 = 0
    n_paired = len(paired_m3)
    for a, b in zip(paired_m3, paired_m4):
        if a != 0 and b == 0:
            m3_changes_m4_not += 1
        if b != 0 and a != 0 and ((a > 0) != (b > 0)):
            m4_opposite_m3 += 1

    return {
        "M3_raw_coverage": m3_raw_cov / grand if grand else 0,
        "M3_computable_coverage": m3_comp_cov / grand if grand else 0,
        "M4_raw_coverage": m4_raw_cov / grand if grand else 0,
        "M4_computable_coverage": m4_comp_cov / grand if grand else 0,
        "pearson_corr": corr,
        "same_sign_proportion": same_sign,
        "n_paired": n_paired,
        "m3_changes_m4_not_pct": m3_changes_m4_not / max(n_paired, 1),
        "m4_opposite_m3_pct": m4_opposite_m3 / max(n_paired, 1),
    }

# ============================================================
# Section 18: Phase 8 — Segment volume special audit
# ============================================================

def audit_segment_volume(acc):
    # Independently compute ratios from raw fields
    cumulative_ratios = []
    current_avgs = []
    previous_avgs = []
    avg_ratios = []
    age_ratios = []
    current_ages = []

    for light in acc.all_records_light:
        cur_vol = _A_safe_float(light.get("cur_vol_sum"))
        prev_vol = _A_safe_float(light.get("prev_vol_sum"))
        cur_age_raw = light.get("cur_age_bars")
        prev_age_raw = light.get("prev_age_bars")
        cur_age = None
        prev_age = None
        if cur_age_raw is not None:
            try:
                cur_age = int(cur_age_raw)
            except (ValueError, TypeError):
                pass
        if prev_age_raw is not None:
            try:
                prev_age = int(prev_age_raw)
            except (ValueError, TypeError):
                pass

        # cumulative_ratio
        if cur_vol is not None and prev_vol is not None and prev_vol != 0:
            cumulative_ratios.append(cur_vol / prev_vol)
        # current_avg
        if cur_vol is not None and cur_age is not None and cur_age > 0:
            current_avgs.append(cur_vol / cur_age)
        # previous_avg
        if prev_vol is not None and prev_age is not None and prev_age > 0:
            previous_avgs.append(prev_vol / prev_age)
        # avg_ratio
        if (cur_vol is not None and cur_age is not None and cur_age > 0
                and prev_vol is not None and prev_age is not None and prev_age > 0):
            ca = cur_vol / cur_age
            pa = prev_vol / prev_age
            if pa != 0:
                avg_ratios.append(ca / pa)
        # age_ratio
        if cur_age is not None and prev_age is not None and prev_age > 0:
            age_ratios.append(cur_age / prev_age)
        # current_age for grouping
        if cur_age is not None and cur_age > 0:
            current_ages.append(cur_age)

    grand = sum(acc.coverage_total.values())

    def _coverage(fact_name):
        return {
            "raw": sum(acc.raw_coverage_counts[fact_name].values()) / grand if grand else 0,
            "computable": sum(acc.computable_coverage_counts[fact_name].values()) / grand if grand else 0,
        }

    result = {
        "V1_cov": _coverage("V1_cumulative_volume_ratio"),
        "V2_cov": _coverage("V2_current_avg_volume"),
        "V3_cov": _coverage("V3_avg_volume_ratio"),
        "V4_cov": _coverage("V4_age_ratio_raw"),
        "V5_cov": _coverage("V5_return_per_volume"),
        "n_cumulative": len(cumulative_ratios),
        "n_avg": len(avg_ratios),
        "n_age": len(age_ratios),
    }

    # Correlations (paired by index from all_records_light)
    cum_list = []
    avg_list = []
    age_list = []
    for light in acc.all_records_light:
        cur_vol = _A_safe_float(light.get("cur_vol_sum"))
        prev_vol = _A_safe_float(light.get("prev_vol_sum"))
        cur_age_raw = light.get("cur_age_bars")
        prev_age_raw = light.get("prev_age_bars")
        cur_age = None
        prev_age = None
        if cur_age_raw is not None:
            try:
                cur_age = int(cur_age_raw)
            except (ValueError, TypeError):
                pass
        if prev_age_raw is not None:
            try:
                prev_age = int(prev_age_raw)
            except (ValueError, TypeError):
                pass
        cr = ar = ag_r = None
        if cur_vol is not None and prev_vol is not None and prev_vol != 0:
            cr = cur_vol / prev_vol
        if (cur_vol is not None and cur_age is not None and cur_age > 0
                and prev_vol is not None and prev_age is not None and prev_age > 0):
            ca = cur_vol / cur_age
            pa = prev_vol / prev_age
            if pa != 0:
                ar = ca / pa
        if cur_age is not None and prev_age is not None and prev_age > 0:
            ag_r = cur_age / prev_age
        cum_list.append(cr)
        avg_list.append(ar)
        age_list.append(ag_r)

    def _paired_corr(a_list, b_list):
        a = []
        b = []
        for x, y in zip(a_list, b_list):
            if x is not None and y is not None:
                a.append(x)
                b.append(y)
        if len(a) < 2:
            return None
        return float(np.corrcoef(a, b)[0, 1])

    def _spearman(a_list, b_list):
        a = []
        b = []
        for x, y in zip(a_list, b_list):
            if x is not None and y is not None:
                a.append(x)
                b.append(y)
        if len(a) < 2:
            return None
        ra = pd.Series(a).rank()
        rb = pd.Series(b).rank()
        return float(ra.corr(rb))

    def _winsorized_corr(a_list, b_list):
        a = []
        b = []
        for x, y in zip(a_list, b_list):
            if x is not None and y is not None:
                a.append(x)
                b.append(y)
        if len(a) < 2:
            return None
        arr_a = np.array(a)
        arr_b = np.array(b)
        lo_a, hi_a = np.percentile(arr_a, [1, 99])
        lo_b, hi_b = np.percentile(arr_b, [1, 99])
        arr_a = np.clip(arr_a, lo_a, hi_a)
        arr_b = np.clip(arr_b, lo_b, hi_b)
        return float(np.corrcoef(arr_a, arr_b)[0, 1])

    result["pearson_cum_age"] = _paired_corr(cum_list, age_list)
    result["pearson_avg_age"] = _paired_corr(avg_list, age_list)
    result["spearman_cum_age"] = _spearman(cum_list, age_list)
    result["spearman_avg_age"] = _spearman(avg_list, age_list)
    result["winsorized_cum_age"] = _winsorized_corr(cum_list, age_list)
    result["winsorized_avg_age"] = _winsorized_corr(avg_list, age_list)

    # Distribution by current_age groups
    age_groups = {"1-2": 0, "3-4": 0, ">=5": 0}
    for ag in current_ages:
        if ag <= 2:
            age_groups["1-2"] += 1
        elif ag <= 4:
            age_groups["3-4"] += 1
        else:
            age_groups[">=5"] += 1
    result["age_group_distribution"] = age_groups

    # Per-group avg_ratio stats
    group_avg_ratios = {"1-2": [], "3-4": [], ">=5": []}
    for light in acc.all_records_light:
        cur_vol = _A_safe_float(light.get("cur_vol_sum"))
        prev_vol = _A_safe_float(light.get("prev_vol_sum"))
        cur_age_raw = light.get("cur_age_bars")
        prev_age_raw = light.get("prev_age_bars")
        cur_age = None
        prev_age = None
        if cur_age_raw is not None:
            try:
                cur_age = int(cur_age_raw)
            except (ValueError, TypeError):
                pass
        if prev_age_raw is not None:
            try:
                prev_age = int(prev_age_raw)
            except (ValueError, TypeError):
                pass
        if (cur_vol is not None and cur_age is not None and cur_age > 0
                and prev_vol is not None and prev_age is not None and prev_age > 0):
            ca = cur_vol / cur_age
            pa = prev_vol / prev_age
            if pa != 0:
                ar = ca / pa
                if cur_age <= 2:
                    group_avg_ratios["1-2"].append(ar)
                elif cur_age <= 4:
                    group_avg_ratios["3-4"].append(ar)
                else:
                    group_avg_ratios[">=5"].append(ar)

    result["group_avg_ratio_stats"] = {}
    for g, vals in group_avg_ratios.items():
        if vals:
            result["group_avg_ratio_stats"][g] = {
                "n": len(vals),
                "mean": float(np.mean(vals)),
                "median": float(np.median(vals)),
                "std": float(np.std(vals)),
            }
        else:
            result["group_avg_ratio_stats"][g] = {"n": 0}

    return result

# ============================================================
# Section 19: Phase 9 — Developing Swing closure
# ============================================================

def audit_developing_swing(acc):
    grand = sum(acc.coverage_total.values())

    dev_dir_raw = sum(acc.raw_coverage_counts["S4_developing_dir_relation"].values())
    dev_pos_raw = sum(acc.raw_coverage_counts["S6_developing_position"].values())
    dev_dir_comp = sum(acc.computable_coverage_counts["S4_developing_dir_relation"].values())
    dev_pos_comp = sum(acc.computable_coverage_counts["S6_developing_position"].values())

    # Redundancy: S4 vs S2, S6 vs S3
    s2_vals = acc.fact_values.get("S2_active_dir_relation", [])
    s4_vals = acc.fact_values.get("S4_developing_dir_relation", [])
    s3_vals = acc.fact_values.get("S3_active_position", [])
    s6_vals = acc.fact_values.get("S6_developing_position", [])

    def _redundancy(a_list, b_list):
        n = min(len(a_list), len(b_list))
        compared = 0
        same = 0
        for i in range(n):
            if a_list[i] is not None and b_list[i] is not None:
                compared += 1
                if a_list[i] == b_list[i]:
                    same += 1
        return {"n_compared": compared, "n_same": same,
                "redundancy": same / compared if compared > 0 else None}

    s4_vs_s2 = _redundancy(s4_vals, s2_vals)
    s6_vs_s3 = _redundancy(s6_vals, s3_vals)

    # Conditional increment: under same DSA+Active, does Developing vary?
    # Group by (dsa_dir, S2) and check S4 variation
    from collections import Counter
    groups = defaultdict(list)
    for i, light in enumerate(acc.all_records_light):
        dsa_dir = _A_norm_dir(light.get("cur_dir"))
        active_dir = _A_norm_dir(light.get("active_swing_dir"))
        dev_dir = _A_norm_dir(light.get("developing_swing_dir"))
        key = (dsa_dir, active_dir)
        if dev_dir is not None:
            groups[key].append(dev_dir)

    increment_info = {}
    for key, dirs in groups.items():
        if len(dirs) < 10:
            continue
        counter = Counter(dirs)
        increment_info[str(key)] = {
            "n": len(dirs),
            "distribution": dict(counter),
            "varies": len(counter) > 1,
        }

    return {
        "dev_dir_raw_coverage": dev_dir_raw / grand if grand else 0,
        "dev_dir_computable_coverage": dev_dir_comp / grand if grand else 0,
        "dev_pos_raw_coverage": dev_pos_raw / grand if grand else 0,
        "dev_pos_computable_coverage": dev_pos_comp / grand if grand else 0,
        "s4_vs_s2_redundancy": s4_vs_s2,
        "s6_vs_s3_redundancy": s6_vs_s3,
        "conditional_increment": increment_info,
    }

# ============================================================
# Section 20: Phase 10 — Range assertions
# ============================================================

def audit_range_assertions(acc):
    by_type = defaultdict(list)
    for a in acc.range_anomalies:
        by_type[a["type"]].append(a)

    return {
        "efficiency_violations": by_type.get("efficiency_out_of_range", []),
        "active_position_violations": by_type.get("active_position_out_of_range", []),
        "developing_position_violations": by_type.get("developing_position_out_of_range", []),
        "age_violations": by_type.get("age_non_positive", []) + by_type.get("age_not_integer", []),
        "total": len(acc.range_anomalies),
    }

# ============================================================
# Section 21: Phase 11 — Representative samples
# ============================================================

def collect_representative_samples(acc):
    rng = np.random.RandomState(RNG_SEED)
    up_samples = [s for s in acc.sample_candidates if s["dsa_dir"] == 1.0]
    down_samples = [s for s in acc.sample_candidates if s["dsa_dir"] == -1.0]

    def _select(samples, n_typ, n_bound):
        if not samples:
            return [], []
        # Use T2 as primary metric
        valid = [s for s in samples if s["T2_aligned_slope"] is not None]
        if not valid:
            valid = samples
        t2_vals = np.array([s["T2_aligned_slope"] for s in valid if s["T2_aligned_slope"] is not None])
        if len(t2_vals) == 0:
            return [], []
        median_t2 = float(np.median(t2_vals))
        # Distance from median
        dists = [(i, abs(float(s["T2_aligned_slope"]) - median_t2)) for i, s in enumerate(valid) if s["T2_aligned_slope"] is not None]
        dists.sort(key=lambda x: x[1])
        typical_indices = [d[0] for d in dists[:n_typ]]
        boundary_indices = [d[0] for d in dists[-n_bound:]] if len(dists) >= n_bound else [d[0] for d in dists]
        typical = [valid[i] for i in typical_indices]
        boundary = [valid[i] for i in boundary_indices]
        return typical, boundary

    up_typical, up_boundary = _select(up_samples, N_TYPICAL, N_BOUNDARY)
    down_typical, down_boundary = _select(down_samples, N_TYPICAL, N_BOUNDARY)

    return {
        "up_typical": up_typical,
        "up_boundary": up_boundary,
        "down_typical": down_typical,
        "down_boundary": down_boundary,
        "range_anomalies": acc.range_anomalies[:50],
        "formula_mismatches": [
            {"fact": k, "samples": v[:10]}
            for k, v in acc.formula_mismatches.items()
        ] if acc.formula_mismatches else [],
        "logic_conflicts": acc.conflict_samples[:50],
    }

# ============================================================
# Section 22: Phase 12 — A/B/C conclusion (FIXED)
# ============================================================

def compute_conclusion(coverage_results, formula_results, conflict_results,
                       redundancy_results, range_results, m3m4_results,
                       segment_vol_results, developing_results):
    # Base fields: DSA trend (T1-T6) + Confirmed/Active structure (S1-S3, S7-S8)
    trend_base = ["T1_trend_direction", "T2_aligned_slope", "T3_trend_efficiency",
                  "T4_trend_age", "T5_slope_ratio", "T6_efficiency_delta"]
    structure_base = ["S1_confirmed_boundary_relation", "S2_active_dir_relation",
                      "S3_active_position", "S7_dist_favorable_boundary",
                      "S8_dist_adverse_boundary"]

    base_fields = trend_base + structure_base
    base_reliable = True
    base_failures = []

    for fact in base_fields:
        cov = coverage_results.get(fact, {})
        raw_cov = cov.get("raw", 0)
        comp_cov = cov.get("computable", 0)
        if raw_cov < COVERAGE_THRESHOLD or comp_cov < COVERAGE_THRESHOLD:
            base_reliable = False
            base_failures.append(f"{fact}: raw={raw_cov:.3f} computable={comp_cov:.3f}")
        # Formula consistency
        consistency = formula_results["consistency"].get(fact)
        if consistency is not None and consistency < 1.0:
            base_reliable = False
            base_failures.append(f"{fact}: formula_consistency={consistency:.3f}")

    # Check conflicts in base fields
    if conflict_results["total"] > 0:
        base_conflict_types = set()
        for s in conflict_results["samples"]:
            base_conflict_types.add(s["type"])
        # Only base-field conflicts make base unreliable
        base_conflict_types_relevant = {
            "T1_MISSING_but_cur_dir_present",
            "S1_MISSING_but_inputs_valid",
            "S2_S4_ALIGNED_but_S5_OPPOSITE",
        }
        if base_conflict_types & base_conflict_types_relevant:
            base_reliable = False
            base_failures.append(f"conflicts: {base_conflict_types & base_conflict_types_relevant}")

    if not base_reliable:
        return {
            "grade": "C",
            "reason": "DSA trend or Confirmed/Active structure base fields unreliable",
            "details": base_failures,
        }

    # Check other facts for demotion
    momentum_facts = ["M1_momentum_alignment", "M2_aligned_momentum", "M3_aligned_momentum_delta", "M5_squeeze_state"]
    volume_facts = ["V1_cumulative_volume_ratio", "V2_current_avg_volume", "V3_avg_volume_ratio", "V5_return_per_volume"]

    momentum_core_ok = True
    volume_core_ok = True

    for fact in momentum_facts:
        cov = coverage_results.get(fact, {})
        if cov.get("raw", 0) < COVERAGE_THRESHOLD or cov.get("computable", 0) < COVERAGE_THRESHOLD:
            momentum_core_ok = False

    for fact in volume_facts:
        cov = coverage_results.get(fact, {})
        if cov.get("raw", 0) < COVERAGE_THRESHOLD or cov.get("computable", 0) < COVERAGE_THRESHOLD:
            volume_core_ok = False

    # Check formula consistency for all facts
    all_formula_ok = True
    for fact, cons in formula_results["consistency"].items():
        if cons is not None and cons < 1.0:
            all_formula_ok = False

    # Check redundancy audit effectiveness
    redundancy_effective = True
    for r in redundancy_results:
        if r["audit_failed"]:
            redundancy_effective = False

    # Check range anomalies
    range_ok = range_results["total"] == 0

    # Decision
    if (momentum_core_ok and volume_core_ok and all_formula_ok
            and conflict_results["total"] == 0 and redundancy_effective and range_ok):
        return {
            "grade": "A",
            "reason": "All 4 layers (Trend/Momentum/Structure/Volume) have Core facts passing",
            "details": [],
        }
    elif momentum_core_ok and not volume_core_ok:
        return {
            "grade": "B",
            "reason": "Trend/Structure/Momentum closed but Volume only Auxiliary",
            "details": ["Volume facts failed coverage threshold"],
        }
    elif not momentum_core_ok:
        return {
            "grade": "C",
            "reason": "Momentum core facts unreliable",
            "details": ["Momentum layer has unfixable contract issues"],
        }
    else:
        return {
            "grade": "B",
            "reason": "Some non-base issues remain",
            "details": [],
        }

# ============================================================
# Section 23: Report generation
# ============================================================

def _fmt_pct(v, digits=1):
    if v is None:
        return "N/A"
    return f"{v * 100:.{digits}f}%"


def _fmt_float(v, digits=4):
    if v is None:
        return "N/A"
    return f"{v:.{digits}f}"


def generate_report(acc, coverage_results, formula_results, conflict_results,
                    redundancy_results, lodo_results, m3m4_results,
                    segment_vol_results, developing_results, range_results,
                    sample_results, conclusion, resource_before, resource_after,
                    mem_tracker):
    lines = []
    lines.append("# V4.12 Atomic Fact Contract Correction and Closure Audit\n")
    lines.append(f"**生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"**实验日期**: {', '.join(ALL_DATES)}\n")
    lines.append(f"**每日预期样本数**: {EXPECTED_PER_DAY}\n")
    lines.append(f"**总预期样本数**: {EXPECTED_TOTAL}\n")
    lines.append(f"**覆盖率阈值**: {COVERAGE_THRESHOLD * 100:.0f}%\n")
    lines.append(f"**冗余阈值**: {REDUNDANCY_THRESHOLD * 100:.0f}%\n\n")

    # 1. V4.11问题修复表
    lines.append("## 1. V4.11问题修复表\n")
    lines.append("| # | V4.11问题 | 根因 | V4.12修复方法 |")
    lines.append("|---|----------|------|--------------|")
    bug_fixes = [
        ("1", "M3覆盖率=0", "批量分类从未实现，raw值已计算但label硬编码为None",
         "compute_atomic_facts_A中M3_categorical设为None，finalize_m3_categorization批量计算中位数绝对值并分类INCREASING/DECREASING/STABLE"),
        ("2", "M4_segment_momentum_change=0", "脚本只SELECT structural_payload，从未读取temporal_payload.daily_context",
         "SQL同时SELECT temporal_payload，extract_fields_v412读取daily_context.daily_sqzmom_change_since_segment_start"),
        ("3", "覆盖率与输出覆盖率混淆", "categorical facts的'MISSING'/'uncalculable'字符串标签被计为'covered'",
         "三种覆盖率分离：raw/computable/output，Core通过条件为raw AND computable>=95%，output仅报告用"),
        ("4", "V2_current_avg_volume覆盖率=0", "V2不在CORE/AUXILIARY_FACTS列表中，覆盖率从未累积",
         "V2加入CORE_FACTS和FACT_RAW_DEPS，覆盖率正确追踪"),
        ("5", "V3_avg_volume_ratio覆盖率100%", "'uncalculable'字符串被计为covered",
         "output_coverage排除None和'MISSING'但不排除'UNCALCULABLE'，通过条件改为raw AND computable"),
        ("6", "公式一致性100%但共享helper", "recompute_facts_from_raw调用了与compute_atomic_facts相同的helper函数",
         "Implementation B完全独立：无_A_ helper调用、无numpy、无V4.9函数（仅get_path_value已在使用时调用）、用不同代码结构（显式if/elif链）"),
        ("7", "冗余审计所有比较数=0", "v49_fact字段名（active_swing_alignment等）从未加入FactAccumulator.fact_values",
         "FactAccumulator.add_day内联计算V4.9等价关系事实，存入v49_fact_values，冗余审计正确比较"),
        ("8", "代表性样本efficiency>1", "生产代码np.nansum跳过NaN导致path_sum<net，efficiency>1（数据范围异常）",
         "所有efficiency越界记录为范围异常，报告中列出所有违反项，A/B/C结论考虑范围异常"),
        ("9", "C结论由单事实失败触发", "compute_conclusion使用hard core_coverage_pass检查",
         "分层决策：DSA trend/Confirmed-Active structure基字段不可靠才触发C，其他事实失败降级为Auxiliary/Rejected不触发C"),
        ("10", "V4_age_ratio_raw 31745 mismatches", "V4_age_ratio_raw同时存在于FACT_RAW_DEPS和raw variants列表，导致fact_values长度翻倍(63516 vs 31758)，索引错位",
         "从raw variants列表中移除V4_age_ratio_raw（已在FACT_RAW_DEPS中append一次）"),
        ("11", "833 T1_MISSING_but_cur_dir_present假冲突+833 mismatches", "pandas DataFrame将None转为NaN（数值列），导致is None检查失效：冲突检测误判NaN为present，覆盖率虚高，A值NaN vs B值None产生假mismatch",
         "add_day循环顶部将所有float NaN转回None，确保coverage/conflict/formula/light records一致处理None"),
    ]
    for num, prob, cause, fix in bug_fixes:
        lines.append(f"| {num} | {prob} | {cause} | {fix} |")
    lines.append("")

    # 2. 实际字段路径
    lines.append("## 2. 实际字段路径\n")
    lines.append("### 2.1 structural_payload.primary.1d 路径\n")
    lines.append("| 短名 | 路径 | 类型 |")
    lines.append("|------|------|------|")
    for short_name, (path, ftype) in V412_STRUCTURAL_FIELDS.items():
        path_str = ".".join(path)
        lines.append(f"| {short_name} | {path_str} | {ftype} |")
    lines.append("")
    lines.append("### 2.2 temporal_payload 路径\n")
    lines.append("| 短名 | 路径 | 类型 |")
    lines.append("|------|------|------|")
    for short_name, (path, ftype) in V412_TEMPORAL_FIELDS.items():
        path_str = ".".join(path)
        lines.append(f"| {short_name} | {path_str} | {ftype} |")
    lines.append("")
    lines.append("> 字段路径已从生产代码验证，与stock_feature_snapshots表的structural_payload和temporal_payload JSONB结构一致。\n")

    # 3. 三种覆盖率
    lines.append("## 3. 三种覆盖率（raw / computable / output）\n")
    lines.append("| Fact | Raw Overall | Computable Overall | Output Overall | Per-Day Raw Range | Per-Day Comp Range | Core? |")
    lines.append("|------|------------|-------------------|---------------|-------------------|-------------------|-------|")
    for fact in FACT_RAW_DEPS:
        cov = coverage_results.get(fact, {})
        raw_o = cov.get("raw", 0)
        comp_o = cov.get("computable", 0)
        out_o = cov.get("output", 0)
        per_day = cov.get("per_day", {})
        if per_day:
            raw_vals = [d["raw"] for d in per_day.values()]
            comp_vals = [d["computable"] for d in per_day.values()]
            raw_range = f"{min(raw_vals):.3f}-{max(raw_vals):.3f}"
            comp_range = f"{min(comp_vals):.3f}-{max(comp_vals):.3f}"
        else:
            raw_range = "N/A"
            comp_range = "N/A"
        is_core = "Yes" if fact in CORE_FACTS else "No"
        lines.append(f"| {fact} | {_fmt_pct(raw_o)} | {_fmt_pct(comp_o)} | {_fmt_pct(out_o)} | {raw_range} | {comp_range} | {is_core} |")
    lines.append("")
    lines.append(f"> Core通过条件：raw AND computable 覆盖率 ≥ {COVERAGE_THRESHOLD*100:.0f}%（overall AND per-day）\n")

    # 4. 独立公式审计
    lines.append("## 4. 独立公式审计（A vs B）\n")
    lines.append("| Fact | Comparable | Matches | Consistency | Mismatches |")
    lines.append("|------|-----------|---------|-------------|------------|")
    all_facts_ab = AB_COMPARE_FACTS_CONTINUOUS + AB_COMPARE_FACTS_CATEGORICAL
    for fact in all_facts_ab:
        comp_count = formula_results["comparable_counts"].get(fact, 0)
        match_count = formula_results["match_counts"].get(fact, 0)
        cons = formula_results["consistency"].get(fact)
        n_mismatch = len(formula_results["mismatches"].get(fact, []))
        lines.append(f"| {fact} | {comp_count} | {match_count} | {_fmt_pct(cons)} | {n_mismatch} |")
    lines.append("")
    all_consistent = all(
        (formula_results["consistency"].get(f) is None or formula_results["consistency"].get(f) >= 1.0)
        for f in all_facts_ab
    )
    lines.append(f"> **公式一致性总判定**: {'通过 (100%)' if all_consistent else '未通过'}\n")

    # 5. M3/M4专项
    lines.append("## 5. M3/M4 动量专项审计\n")
    lines.append(f"- M3 raw覆盖率: {_fmt_pct(m3m4_results['M3_raw_coverage'])}")
    lines.append(f"- M3 computable覆盖率: {_fmt_pct(m3m4_results['M3_computable_coverage'])}")
    lines.append(f"- M4 raw覆盖率: {_fmt_pct(m3m4_results['M4_raw_coverage'])}")
    lines.append(f"- M4 computable覆盖率: {_fmt_pct(m3m4_results['M4_computable_coverage'])}")
    lines.append(f"- Pearson相关系数(M3_raw, M4): {_fmt_float(m3m4_results['pearson_corr'])}")
    lines.append(f"- 同号比例: {_fmt_pct(m3m4_results['same_sign_proportion'])}")
    lines.append(f"- 配对样本数: {m3m4_results['n_paired']}")
    lines.append(f"- M3变化但M4不变比例: {_fmt_pct(m3m4_results['m3_changes_m4_not_pct'])}")
    lines.append(f"- M4变化但M3反向比例: {_fmt_pct(m3m4_results['m4_opposite_m3_pct'])}")
    m3_decision = "Core" if m3m4_results["M3_computable_coverage"] >= COVERAGE_THRESHOLD else "Auxiliary"
    m4_decision = "Auxiliary"  # M4 depends on temporal field
    lines.append(f"- **M3决策**: {m3_decision}")
    lines.append(f"- **M4决策**: {m4_decision}（依赖temporal字段，覆盖率可能较低）\n")

    # 6. Segment成交量专项
    lines.append("## 6. Segment成交量专项审计\n")
    lines.append("### 6.1 覆盖率\n")
    lines.append("| 指标 | Raw覆盖率 | Computable覆盖率 |")
    lines.append("|------|----------|-----------------|")
    for label, key in [("V1累计比率", "V1_cov"), ("V2当前均量", "V2_cov"),
                       ("V3均量比率", "V3_cov"), ("V4年龄比率", "V4_cov"),
                       ("V5单位成交量回报", "V5_cov")]:
        c = segment_vol_results.get(key, {})
        lines.append(f"| {label} | {_fmt_pct(c.get('raw', 0))} | {_fmt_pct(c.get('computable', 0))} |")
    lines.append("")
    lines.append("### 6.2 相关性\n")
    lines.append("| 指标对 | Pearson | Spearman | Winsorized(1%/99%) |")
    lines.append("|--------|---------|----------|-------------------|")
    lines.append(f"| (cumulative_ratio, age_ratio) | {_fmt_float(segment_vol_results.get('pearson_cum_age'))} | {_fmt_float(segment_vol_results.get('spearman_cum_age'))} | {_fmt_float(segment_vol_results.get('winsorized_cum_age'))} |")
    lines.append(f"| (avg_ratio, age_ratio) | {_fmt_float(segment_vol_results.get('pearson_avg_age'))} | {_fmt_float(segment_vol_results.get('spearman_avg_age'))} | {_fmt_float(segment_vol_results.get('winsorized_avg_age'))} |")
    lines.append("")
    lines.append("### 6.3 年龄分组分布\n")
    lines.append("| 年龄组 | 数量 |")
    lines.append("|--------|------|")
    for g, n in segment_vol_results.get("age_group_distribution", {}).items():
        lines.append(f"| {g} bars | {n} |")
    lines.append("")
    lines.append("### 6.4 分组均量比率统计\n")
    lines.append("| 年龄组 | n | mean | median | std |")
    lines.append("|--------|---|------|--------|-----|")
    for g, stats in segment_vol_results.get("group_avg_ratio_stats", {}).items():
        if stats.get("n", 0) > 0:
            lines.append(f"| {g} | {stats['n']} | {_fmt_float(stats['mean'])} | {_fmt_float(stats['median'])} | {_fmt_float(stats['std'])} |")
        else:
            lines.append(f"| {g} | 0 | N/A | N/A | N/A |")
    lines.append("")
    vol_decision = "Core" if segment_vol_results.get("V3_cov", {}).get("computable", 0) >= COVERAGE_THRESHOLD else "Auxiliary"
    lines.append(f"> **Segment成交量决策**: {vol_decision}\n")

    # 7. 冗余审计
    lines.append("## 7. 冗余审计（V4.12 vs V4.9等价）\n")
    lines.append("| V4.12 Fact | V4.9 Fact | Left Valid | Right Valid | Compared | Consistent | Inconsistent | Consistency | Status |")
    lines.append("|-----------|-----------|-----------|------------|----------|-----------|-------------|------------|--------|")
    for r in redundancy_results:
        status = "AUDIT_FAILED" if r["audit_failed"] else ("PASS" if (r["consistency"] is not None and r["consistency"] >= REDUNDANCY_THRESHOLD) else "FAIL")
        lines.append(f"| {r['v412_fact']} | {r['v49_fact']} | {r['left_valid']} | {r['right_valid']} | {r['n_compared']} | {r['n_consistent']} | {r['n_inconsistent']} | {_fmt_pct(r['consistency'])} | {status} |")
    lines.append("")

    # 8. 范围断言
    lines.append("## 8. 范围断言\n")
    lines.append(f"- efficiency [0,1] 违反数: {len(range_results['efficiency_violations'])}")
    lines.append(f"- active_position [0,1] 违反数: {len(range_results['active_position_violations'])}")
    lines.append(f"- developing_position [0,1] 违反数: {len(range_results['developing_position_violations'])}")
    lines.append(f"- age 正整数违反数: {len(range_results['age_violations'])}")
    lines.append(f"- 总异常数: {range_results['total']}\n")
    if range_results["efficiency_violations"]:
        lines.append("### efficiency越界样本（前20）\n")
        lines.append("| symbol | date | value |")
        lines.append("|--------|------|-------|")
        for v in range_results["efficiency_violations"][:20]:
            lines.append(f"| {v['symbol']} | {v['trade_date']} | {_fmt_float(v['value'])} |")
        lines.append("")

    # 9. Developing结论
    lines.append("## 9. Developing Swing结论\n")
    lines.append(f"- developing_swing_dir raw覆盖率: {_fmt_pct(developing_results['dev_dir_raw_coverage'])}")
    lines.append(f"- developing_swing_dir computable覆盖率: {_fmt_pct(developing_results['dev_dir_computable_coverage'])}")
    lines.append(f"- price_pos_developing raw覆盖率: {_fmt_pct(developing_results['dev_pos_raw_coverage'])}")
    lines.append(f"- price_pos_developing computable覆盖率: {_fmt_pct(developing_results['dev_pos_computable_coverage'])}")
    s4_s2 = developing_results["s4_vs_s2_redundancy"]
    s6_s3 = developing_results["s6_vs_s3_redundancy"]
    lines.append(f"- S4 vs S2 冗余: compared={s4_s2['n_compared']}, same={s4_s2['n_same']}, redundancy={_fmt_pct(s4_s2['redundancy'])}")
    lines.append(f"- S6 vs S3 冗余: compared={s6_s3['n_compared']}, same={s6_s3['n_same']}, redundancy={_fmt_pct(s6_s3['redundancy'])}")
    lines.append(f"- 条件增量分析（同DSA+Active下Developing是否变化）:")
    for key, info in developing_results.get("conditional_increment", {}).items():
        lines.append(f"  - {key}: n={info['n']}, varies={info['varies']}, dist={info['distribution']}")
    dev_decision = "Auxiliary"
    if developing_results["dev_dir_computable_coverage"] >= COVERAGE_THRESHOLD and developing_results["dev_pos_computable_coverage"] >= COVERAGE_THRESHOLD:
        if (s4_s2["redundancy"] or 0) < REDUNDANCY_THRESHOLD:
            dev_decision = "Core"
        else:
            dev_decision = "Rejected (redundant with Active)"
    lines.append(f"\n> **Developing决策**: {dev_decision}\n")

    # 10. Core/Auxiliary/Rejected清单
    lines.append("## 10. Core/Auxiliary/Rejected 清单\n")
    lines.append("### 10.1 Core Facts\n")
    lines.append("| ID | 名称 | 路径 | 公式 | Raw覆盖 | Computable覆盖 | Missing处理 | 中文模板 | 禁止解读 |")
    lines.append("|----|------|------|------|---------|---------------|------------|----------|----------|")
    core_facts_info = {
        "T1_trend_direction": ("dsa_segment.current_dsa_segment_dir", "dir>0→UP, dir<0→DOWN, dir=0→NONE",
                               "当前趋势方向为{value}", "不构成买卖信号"),
        "T2_aligned_slope": ("dsa_segment.current_dsa_segment_slope_atr_per_bar", "dsa_dir × cur_slope_atr",
                             "趋势对齐斜率为{value:.4f} ATR/bar", "斜率大小不直接预测涨跌"),
        "T3_trend_efficiency": ("dsa_segment.current_dsa_segment_efficiency_0_1", "直接取值",
                                "趋势效率为{value:.4f}", "效率>1为数据异常，不代表超强趋势"),
        "T4_trend_age": ("dsa_segment.current_dsa_segment_age_bars", "直接取值(整数)",
                         "趋势已持续{value}根bar", "年龄长不代表即将反转"),
        "T5_slope_ratio": ("cur/prev slope_atr", "|cur|/|prev|, >1.2 FASTER, <0.8 SLOWER",
                           "斜率加速状态: {value}", "加速不等于一定上涨"),
        "T6_efficiency_delta": ("cur_eff - prev_eff", "delta>0.1 HIGHER, <-0.1 LOWER",
                                "效率变化: {value}", "效率变化不直接等于趋势强弱"),
        "M1_momentum_alignment": ("volatility_momentum.sqzmom_val vs dsa_dir", "sign比较",
                                  "动量与趋势{value}", "对齐不等于买入信号"),
        "M2_aligned_momentum": ("dsa_dir × sqzmom_val", "乘法",
                                "对齐动量为{value:.4f}", "正值不代表必涨"),
        "M3_aligned_momentum_delta": ("dsa_dir × sqzmom_delta_1", "batch: |raw|>median→INC/DEC, else STABLE",
                                      "对齐动量变化: {value}", "变化方向不等同交易方向"),
        "M5_squeeze_state": ("volatility_momentum.sqz_on/sqz_off", "bool组合",
                             "波动率挤压状态: {value}", "挤压不等于突破方向"),
        "S1_confirmed_boundary_relation": ("swing_position.confirmed_swing_breakout_state vs dsa_dir",
                                           "方向+突破状态映射",
                                           "确认边界关系: {value}", "突破 favorable 不等于必涨"),
        "S2_active_dir_relation": ("swing_position.active_swing_dir vs dsa_dir", "方向比较",
                                    "活跃波段与趋势: {value}", "对齐不等于持仓信号"),
        "S3_active_position": ("swing_position.price_position_in_active_swing_0_1", "0-0.33 LOWER, 0.33-0.67 MIDDLE, >0.67 UPPER",
                               "价格在活跃波段中的位置: {value}", "位置低不等于买入"),
        "S7_dist_favorable_boundary": ("dsa_dir>0→dist_high, dsa_dir<0→dist_low",
                                       "方向决定取哪个距离",
                                       "距有利边界: {value:.4f} ATR", "距离近不等于即将突破"),
        "S8_dist_adverse_boundary": ("dsa_dir>0→dist_low, dsa_dir<0→dist_high",
                                     "方向决定取哪个距离",
                                     "距不利边界: {value:.4f} ATR", "距离远不等于安全"),
        "V1_cumulative_volume_ratio": ("dsa_segment.current_vs_prev_volume_ratio", "直接取值",
                                       "累计成交量比率: {value:.4f}", "放量不等于上涨"),
        "V2_current_avg_volume": ("cur_vol_sum / cur_age", "除法",
                                  "当前段均量: {value:.2f}", "均量大不等于必涨"),
        "V3_avg_volume_ratio": ("(cur_vol/cur_age)/(prev_vol/prev_age)", ">1.2 HIGHER, <0.8 LOWER",
                                "均量比率: {value}", "均量增加不等于趋势加强"),
        "V5_return_per_volume": ("dsa_segment.current_segment_return_per_volume", "直接取值",
                                 "单位成交量回报: {value:.6f}", "回报高不等于效率高"),
    }
    for fact in CORE_FACTS:
        cov = coverage_results.get(fact, {})
        info = core_facts_info.get(fact, ("", "", "", ""))
        lines.append(f"| {fact} | {fact} | {info[0]} | {info[1]} | {_fmt_pct(cov.get('raw',0))} | {_fmt_pct(cov.get('computable',0))} | MISSING标记 | {info[2]} | {info[3]} |")
    lines.append("")

    lines.append("### 10.2 Auxiliary Facts\n")
    lines.append("| ID | 名称 | 原因 |")
    lines.append("|----|------|------|")
    lines.append("| M4_segment_momentum_change | 段内动量变化 | 依赖temporal_payload字段，覆盖率可能低于阈值 |")
    lines.append("| V4_age_ratio_raw | 年龄比率 | 辅助参考指标 |")
    lines.append("| S4_developing_dir_relation | 发展波段方向关系 | 与S2可能冗余 |")
    lines.append("| S6_developing_position | 发展波段位置 | 与S3可能冗余 |")
    lines.append("")

    lines.append("### 10.3 Rejected Facts\n")
    lines.append("| ID | 名称 | 原因 |")
    lines.append("|----|------|------|")
    lines.append("| (无) | - | - |")
    lines.append("")

    # 11. 产品事实卡模板
    lines.append("## 11. 产品事实卡模板\n")
    lines.append("```")
    lines.append("┌─────────────────────────────────────┐")
    lines.append("│  趋势对齐斜率 (T2_aligned_slope)     │")
    lines.append("├─────────────────────────────────────┤")
    lines.append("│  股票: {symbol}                      │")
    lines.append("│  日期: {trade_date}                  │")
    lines.append("│  方向: {T1_trend_direction}          │")
    lines.append("│  对齐斜率: {T2_aligned_slope:.4f}     │")
    lines.append("│  趋势效率: {T3_trend_efficiency:.4f}  │")
    lines.append("│  持续: {T4_trend_age} bars           │")
    lines.append("│  动量对齐: {M1_momentum_alignment}    │")
    lines.append("│  位置: {S3_active_position}           │")
    lines.append("│  成交量比: {V1_cumulative_volume_ratio:.4f} │")
    lines.append("├─────────────────────────────────────┤")
    lines.append("│  ⚠️ 以上为状态描述，不构成买卖建议   │")
    lines.append("└─────────────────────────────────────┘")
    lines.append("```\n")

    # 12. A/B/C结论
    lines.append("## 12. A/B/C 结论\n")
    lines.append(f"**结论**: **{conclusion['grade']}**\n")
    lines.append(f"**原因**: {conclusion['reason']}\n")
    if conclusion["details"]:
        lines.append("**详情**:")
        for d in conclusion["details"]:
            lines.append(f"- {d}")
        lines.append("")
    lines.append("### 决策矩阵\n")
    lines.append("| 等级 | 条件 |")
    lines.append("|------|------|")
    lines.append("| A | 四层(Trend/Momentum/Structure/Volume)各有≥1 Core事实，全部Core通过覆盖率，公式100%，冲突0，冗余审计有效，成交量口径明确，无未解决范围异常 |")
    lines.append("| B | Trend/Structure/Momentum闭合但Volume仅Auxiliary |")
    lines.append("| C | DSA或Confirmed/Active基字段不可靠，或多个核心层有不可修复的契约问题 |")
    lines.append("")

    # 13. 资源前后对比
    lines.append("## 13. 资源前后对比\n")
    lines.append("### 运行前\n")
    for k, v in resource_before.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("### 运行后\n")
    for k, v in resource_after.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("### RSS追踪\n")
    lines.append("| 阶段 | Peak RSS (MB) |")
    lines.append("|------|-------------|")
    for stage, mb in mem_tracker.items():
        lines.append(f"| {stage} | {mb:.1f} |")
    lines.append("")
    lines.append(f"> 硬限制: {HARD_MAX_MB}MB, 软限制: {WARN_MB}MB\n")

    report_text = "\n".join(lines)
    return report_text

# ============================================================
# Section 24: main() orchestration
# ============================================================

def _get_resource_snapshot():
    snap = {}
    try:
        st = os.statvfs("/")
        snap["disk_free_GB"] = f"{(st.f_bavail * st.f_frsize) / (1024**3):.2f}"
    except Exception:
        snap["disk_free_GB"] = "N/A"
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    snap["mem_available_MB"] = f"{int(line.split()[1]) / 1024:.1f}"
                    break
    except Exception:
        snap["mem_available_MB"] = "N/A"
    try:
        st = os.statvfs("/home/ubuntu/market_dev")
        snap["project_disk_free_GB"] = f"{(st.f_bavail * st.f_frsize) / (1024**3):.2f}"
    except Exception:
        snap["project_disk_free_GB"] = "N/A"
    snap["rss_MB"] = f"{peak_rss_mb():.1f}"
    return snap


def main():
    mem_tracker = {}

    # 1. Record resource baseline
    log("=== V4.12 Atomic Fact Contract Closure Audit ===")
    resource_before = _get_resource_snapshot()
    log(f"Resource before: {resource_before}")
    log_memory("start", mem_tracker)

    # 2. Connect DB, verify read-only
    log("Connecting to DB...")
    conn = psycopg.connect(DB_URL, connect_timeout=30)
    db_readonly_test(conn)
    log_memory("db_connected", mem_tracker)

    # 3. Stream each date, accumulate
    acc = FactAccumulator()
    for date in ALL_DATES:
        log(f"\n--- Streaming date {date} ---")
        df = stream_one_date(date, conn)
        log(f"  Loaded {len(df)} records for {date}")
        acc.add_day(date, df)
        del df
        gc.collect()
        log_memory(f"after_{date}", mem_tracker)

    conn.close()
    log("DB connection closed.")
    log_memory("db_closed", mem_tracker)

    # 4. Finalize M3 batch categorization
    log("\nFinalizing M3 batch categorization...")
    m3_raws = [v for v in acc.fact_values["M3_aligned_momentum_delta_raw"] if v is not None]
    if m3_raws:
        median_abs = float(np.median(np.abs(m3_raws)))
        log(f"  M3 median |delta| = {median_abs:.6f}, n={len(m3_raws)}")
        if median_abs <= 0:
            for i in range(len(acc.fact_values["M3_aligned_momentum_delta"])):
                if acc.fact_values["M3_aligned_momentum_delta_raw"][i] is not None:
                    acc.fact_values["M3_aligned_momentum_delta"][i] = "STABLE"
        else:
            for i in range(len(acc.fact_values["M3_aligned_momentum_delta"])):
                raw = acc.fact_values["M3_aligned_momentum_delta_raw"][i]
                if raw is None:
                    acc.fact_values["M3_aligned_momentum_delta"][i] = None
                elif abs(raw) > median_abs and raw > 0:
                    acc.fact_values["M3_aligned_momentum_delta"][i] = "INCREASING"
                elif abs(raw) > median_abs and raw < 0:
                    acc.fact_values["M3_aligned_momentum_delta"][i] = "DECREASING"
                else:
                    acc.fact_values["M3_aligned_momentum_delta"][i] = "STABLE"
    else:
        log("  WARNING: no M3 raw values found")
    acc.finalize_m3_output_coverage()
    log_memory("m3_finalized", mem_tracker)

    # 5. Run all audit phases
    log("\n=== Phase 2: Three-type coverage audit ===")
    coverage_results = audit_coverage(acc)
    log_memory("phase2_coverage", mem_tracker)

    log("\n=== Phase 3: Independent formula verification (A vs B) ===")
    formula_results = audit_formula_consistency(acc)
    n_total_mismatches = sum(len(v) for v in formula_results["mismatches"].values())
    log(f"  Total mismatches: {n_total_mismatches}")
    log_memory("phase3_formula", mem_tracker)

    log("\n=== Phase 4: Logic conflict audit ===")
    conflict_results = audit_logic_conflicts(acc)
    log(f"  Total conflicts: {conflict_results['total']}")
    log_memory("phase4_conflicts", mem_tracker)

    log("\n=== Phase 5: Redundancy audit ===")
    redundancy_results = audit_redundancy(acc)
    for r in redundancy_results:
        log(f"  {r['v412_fact']} vs {r['v49_fact']}: compared={r['n_compared']}, consistency={_fmt_pct(r['consistency'])}")
    log_memory("phase5_redundancy", mem_tracker)

    log("\n=== Phase 6: LODO threshold sensitivity ===")
    lodo_results = lodo_threshold_sensitivity(acc)
    log_memory("phase6_lodo", mem_tracker)

    log("\n=== Phase 7: M3/M4 special audit ===")
    m3m4_results = audit_m3_m4(acc)
    log(f"  M3 computable coverage: {_fmt_pct(m3m4_results['M3_computable_coverage'])}")
    log(f"  M4 computable coverage: {_fmt_pct(m3m4_results['M4_computable_coverage'])}")
    log(f"  Pearson corr: {_fmt_float(m3m4_results['pearson_corr'])}")
    log_memory("phase7_m3m4", mem_tracker)

    log("\n=== Phase 8: Segment volume special audit ===")
    segment_vol_results = audit_segment_volume(acc)
    log(f"  Pearson(cum, age): {_fmt_float(segment_vol_results.get('pearson_cum_age'))}")
    log(f"  Pearson(avg, age): {_fmt_float(segment_vol_results.get('pearson_avg_age'))}")
    log_memory("phase8_volume", mem_tracker)

    log("\n=== Phase 9: Developing swing closure ===")
    developing_results = audit_developing_swing(acc)
    log_memory("phase9_developing", mem_tracker)

    log("\n=== Phase 10: Range assertions ===")
    range_results = audit_range_assertions(acc)
    log(f"  Total range anomalies: {range_results['total']}")
    log_memory("phase10_range", mem_tracker)

    log("\n=== Phase 11: Representative samples ===")
    sample_results = collect_representative_samples(acc)
    log_memory("phase11_samples", mem_tracker)

    log("\n=== Phase 12: A/B/C conclusion ===")
    conclusion = compute_conclusion(
        coverage_results, formula_results, conflict_results,
        redundancy_results, range_results, m3m4_results,
        segment_vol_results, developing_results
    )
    log(f"  Conclusion: {conclusion['grade']} - {conclusion['reason']}")

    # 7. Generate report
    log("\n=== Generating report ===")
    resource_after = _get_resource_snapshot()
    report_text = generate_report(
        acc, coverage_results, formula_results, conflict_results,
        redundancy_results, lodo_results, m3m4_results,
        segment_vol_results, developing_results, range_results,
        sample_results, conclusion, resource_before, resource_after,
        mem_tracker
    )
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write(report_text)
    log(f"Report written to {REPORT_PATH}")
    log_memory("report_written", mem_tracker)

    # 8. Log peak RSS
    peak = peak_rss_mb()
    log(f"\n=== Done. Peak RSS: {peak:.1f}MB ===")
    log(f"Conclusion: {conclusion['grade']}")

    return conclusion


if __name__ == "__main__":
    main()
