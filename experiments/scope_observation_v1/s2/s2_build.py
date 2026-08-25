"""S2 — post-process DB-native aggregation into scope-day observation table + JSON.

DB-native inputs (read-only, sql/*.sql):
  out/_axis.csv         : per (scope, trade_date) categorical counts + participation percentiles
  out/_members.csv      : per (scope, trade_date) pit_members + valid_member_ids arrays
  out/_state.csv        : per (scope, trade_date, instrument) valid categorical state
  out/_price_hhi.csv    : per (scope, trade_date) price_contribution_hhi + amount_contribution_hhi
  out/_price_facts.csv  : per (scope, trade_date) return level/distribution/breadth
  out/_contribution.csv : per (scope, trade_date, member) top-N price/amount contributors (process-only)

Outputs:
  out/s2_scope_observation_daily.csv
  out/s2_incremental_information.json
  out/s2_contrast_cases.json
  out/s2_chip_participation_analysis.json
  out/s2_price_facts.json
  out/s2_member_contribution_evidence.json

No scoring / weighting / prediction. DB-fact only.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

import sys
sys.path.insert(0, str(Path(__file__).parent))
from s2_analysis import (  # noqa: E402
    diff_membership, canonical_previous_trading_day, transition_denominator,
    classify_regime, classify_swing_bias, classify_internal_bias, classify_momentum,
    classify_structure_alignment, percentile, select_contrast_cases, no_scoring_fields,
)

OUT = Path(__file__).parent / "out"
TRADE_DATES = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"]


def parse_pg_array(s: str):
    if s is None:
        return []
    s = s.strip()
    if not s or s == "{}":
        return []
    inner = s[1:-1] if s.startswith("{") and s.endswith("}") else s
    if not inner:
        return []
    return [x.strip().strip('"') for x in inner.split(",") if x.strip()]


def ratio(numer: int, denom: int):
    if not denom:
        return None
    return round(numer / denom, 6)


def fnum(x):
    return float(x) if x not in (None, "") else None


AXIS_FIELDS = [
    "board_id", "board_name", "board_type", "trade_date", "definition_effective_from",
    "membership_version", "pit_member_count", "fp_row_count",
    "regime_up", "regime_neutral", "regime_down", "regime_strength_median",
    "swing_up", "swing_neutral", "swing_down",
    "internal_up", "internal_neutral", "internal_down",
    "alignment_resonance", "alignment_divergence",
    "momentum_expanding", "momentum_flat", "momentum_contracting",
    "vol_ratio20_p25", "vol_ratio20_p50", "vol_ratio20_p75",
    "amt_ratio20_p25", "amt_ratio20_p50", "amt_ratio20_p75",
    "vol_pct20_median", "amt_pct200_median", "seg_vol_ratio_median",
]
MEMBERS_FIELDS = ["board_id", "board_type", "trade_date", "pit_members", "valid_member_ids"]
STATE_FIELDS = ["board_id", "board_type", "trade_date", "instrument_id", "regime_value", "swing_bias", "internal_bias", "momentum_direction"]
HHI_FIELDS = ["board_id", "board_type", "trade_date", "price_candidate_count", "price_valid_count",
              "missing_exact_t1_count", "amount_valid_count",
              "price_contribution_hhi", "price_contribution_hhi_normalized",
              "amount_contribution_hhi", "amount_contribution_hhi_normalized"]
PRICE_FACTS_FIELDS = [
    "board_id", "board_type", "trade_date", "price_candidate_count", "price_valid_count",
    "missing_exact_t1_count",
    "equal_weight_return_mean", "return_median", "return_p25", "return_p50", "return_p75",
    "return_p10", "return_p90", "advance_count", "decline_count", "unchanged_count",
]
CONTRIB_FIELDS = [
    "board_id", "board_name", "board_type", "trade_date", "instrument_id",
    "symbol", "name", "member_return_1d",
    "signed_return_contribution", "abs_price_change_share", "amount_share",
    "total_abs_ret", "total_amount", "price_valid_count", "amount_valid_count",
    "member_count", "price_rank", "amount_rank",
]


def _load_named(path, fields):
    rows = []
    with open(path) as f:
        for parts in csv.reader(f):
            if not parts or len(parts) < len(fields):
                continue
            # skip a header row if present (psql --csv may emit one)
            if parts[0] == fields[0]:
                continue
            rows.append(dict(zip(fields, parts)))
    return rows


def _load_named_header(path):
    """Header-aware loader: maps columns by header name. Keeps ALL columns."""
    rows = []
    with open(path) as f:
        rdr = csv.reader(f)
        header = next(rdr, None)
        if header is None:
            return rows
        for parts in rdr:
            if not parts:
                continue
            rows.append({header[i]: parts[i] for i in range(min(len(header), len(parts)))})
    return rows


def load_axis():
    rows = {}
    for r in _load_named(OUT / "_axis.csv", AXIS_FIELDS):
        rows[(r["board_type"], r["board_id"], r["trade_date"])] = r
    # market axis has extra columns (market_universe_count, fp_valid_count) -> header-aware
    for r in _load_named_header(OUT / "_axis_market.csv"):
        rows[(r["board_type"], r["board_id"], r["trade_date"])] = r
    return rows


def load_members():
    rows = {}
    for r in _load_named(OUT / "_members.csv", MEMBERS_FIELDS):
        rows[(r["board_type"], r["board_id"], r["trade_date"])] = r
    # market control: pit_members = valid cross-section (derived from state_market)
    mkt = defaultdict(set)
    for r in _load_named(OUT / "_state_market.csv", STATE_FIELDS):
        mkt[(r["trade_date"])].add(r["instrument_id"])
    for td, insts in mkt.items():
        rows[("market", "FULL_MARKET", td)] = {
            "board_id": "FULL_MARKET", "board_type": "market", "trade_date": td,
            "pit_members": "{" + ",".join(sorted(insts)) + "}",
            "valid_member_ids": "{" + ",".join(sorted(insts)) + "}",
        }
    return rows


def load_hhi():
    rows = {}
    for r in _load_named(OUT / "_price_hhi.csv", HHI_FIELDS):
        rows[(r["board_type"], r["board_id"], r["trade_date"])] = r
    for r in _load_named(OUT / "_price_hhi_market.csv", HHI_FIELDS):
        rows[(r["board_type"], r["board_id"], r["trade_date"])] = r
    return rows


def load_price_facts():
    rows = {}
    for r in _load_named(OUT / "_price_facts.csv", PRICE_FACTS_FIELDS):
        rows[(r["board_type"], r["board_id"], r["trade_date"])] = r
    for r in _load_named(OUT / "_price_facts_market.csv", PRICE_FACTS_FIELDS):
        rows[(r["board_type"], r["board_id"], r["trade_date"])] = r
    return rows


def load_contributions():
    """(type, board_id, trade_date) -> list of member contribution dicts (top-N only)."""
    d = defaultdict(list)
    for r in _load_named(OUT / "_contribution.csv", CONTRIB_FIELDS):
        d[(r["board_type"], r["board_id"], r["trade_date"])].append(r)
    for r in _load_named(OUT / "_contribution_market.csv", CONTRIB_FIELDS):
        d[(r["board_type"], r["board_id"], r["trade_date"])].append(r)
    return d


def load_state():
    """(type, board_id, trade_date) -> {instrument_id: (regime, swing, internal, momentum)}"""
    d = defaultdict(dict)
    for r in _load_named(OUT / "_state.csv", STATE_FIELDS):
        key = (r["board_type"], r["board_id"], r["trade_date"])
        d[key][r["instrument_id"]] = (
            int(r["regime_value"]) if r["regime_value"] not in (None, "") else None,
            int(r["swing_bias"]) if r["swing_bias"] not in (None, "") else None,
            int(r["internal_bias"]) if r["internal_bias"] not in (None, "") else None,
            r["momentum_direction"],
        )
    for r in _load_named(OUT / "_state_market.csv", STATE_FIELDS):
        key = (r["board_type"], r["board_id"], r["trade_date"])
        d[key][r["instrument_id"]] = (
            int(r["regime_value"]) if r["regime_value"] not in (None, "") else None,
            int(r["swing_bias"]) if r["swing_bias"] not in (None, "") else None,
            int(r["internal_bias"]) if r["internal_bias"] not in (None, "") else None,
            r["momentum_direction"],
        )
    return d


def build():
    axis = load_axis()
    members = load_members()
    hhi = load_hhi()
    state = load_state()
    price_facts = load_price_facts()
    contribs = load_contributions()

    records = []
    for key, r in axis.items():
        btype, bid, td = key
        h = hhi.get(key, {})
        pf = price_facts.get(key, {})
        denom = int(r["fp_row_count"])

        regime_up = int(r["regime_up"]); regime_neutral = int(r["regime_neutral"]); regime_down = int(r["regime_down"])
        swing_up = int(r["swing_up"]); swing_neutral = int(r["swing_neutral"]); swing_down = int(r["swing_down"])
        internal_up = int(r["internal_up"]); internal_neutral = int(r["internal_neutral"]); internal_down = int(r["internal_down"])
        align_res = int(r["alignment_resonance"]); align_div = int(r["alignment_divergence"])
        mom_exp = int(r["momentum_expanding"]); mom_flat = int(r["momentum_flat"]); mom_con = int(r["momentum_contracting"])

        # market control: market_universe_count = ALL state rows that day; fp_valid_count = valid subset.
        # Market has NO board PIT membership -> pit_member_count is NOT set to the valid count.
        market_universe = int(r["market_universe_count"]) if (r.get("market_universe_count") or "").strip() not in ("", "None") else None
        fp_valid = int(r["fp_valid_count"]) if (r.get("fp_valid_count") or "").strip() not in ("", "None") else None
        is_market = (btype == "market")

        rec = {
            "scope_type": btype, "board_id": bid, "board_name": r["board_name"], "trade_date": td,
            "definition_effective_from": r["definition_effective_from"], "membership_version": r["membership_version"],
            "pit_member_count": None if is_market else int(r["pit_member_count"]),
            "fp_row_count": denom,
            "market_universe_count": market_universe,
            "fp_valid_count": fp_valid,
            "market_valid_universe_changed": None,
            # Trend
            "regime_up_ratio": ratio(regime_up, denom), "regime_neutral_ratio": ratio(regime_neutral, denom),
            "regime_down_ratio": ratio(regime_down, denom),
            "regime_strength_median": fnum(r["regime_strength_median"]),
            # Structure
            "swing_up_ratio": ratio(swing_up, denom), "swing_neutral_ratio": ratio(swing_neutral, denom),
            "swing_down_ratio": ratio(swing_down, denom),
            "internal_up_ratio": ratio(internal_up, denom), "internal_neutral_ratio": ratio(internal_neutral, denom),
            "internal_down_ratio": ratio(internal_down, denom),
            "resonance_ratio": ratio(align_res, denom), "divergence_ratio": ratio(align_div, denom),
            # Momentum
            "expanding_ratio": ratio(mom_exp, denom), "flat_ratio": ratio(mom_flat, denom),
            "contracting_ratio": ratio(mom_con, denom),
            # Participation (threshold-free)
            "vol_ratio20_p25": fnum(r["vol_ratio20_p25"]), "vol_ratio20_p50": fnum(r["vol_ratio20_p50"]),
            "vol_ratio20_p75": fnum(r["vol_ratio20_p75"]),
            "amt_ratio20_p25": fnum(r["amt_ratio20_p25"]), "amt_ratio20_p50": fnum(r["amt_ratio20_p50"]),
            "amt_ratio20_p75": fnum(r["amt_ratio20_p75"]),
            "vol_pct20_median": fnum(r["vol_pct20_median"]), "amt_pct200_median": fnum(r["amt_pct200_median"]),
            "seg_vol_ratio_median": fnum(r["seg_vol_ratio_median"]),
            # Concentration (raw HHI is a fact; normalized HHI for cross-scope Q4/Q5)
            "price_contribution_hhi": fnum(h.get("price_contribution_hhi")),
            "price_contribution_hhi_normalized": fnum(h.get("price_contribution_hhi_normalized")),
            "amount_contribution_hhi": fnum(h.get("amount_contribution_hhi")),
            "amount_contribution_hhi_normalized": fnum(h.get("amount_contribution_hhi_normalized")),
            # Price Facts (return level / distribution / breadth) — EXACT-T1 diagnostics
            # price_candidate_count / missing_exact_t1_count from HHI (scope-level); price/amount
            # valid counts are independent universes (never fallback into each other).
            "price_candidate_count": fnum(h.get("price_candidate_count")),
            "price_valid_count": fnum(h.get("price_valid_count")),
            "missing_exact_t1_count": fnum(h.get("missing_exact_t1_count")),
            "amount_valid_count": fnum(h.get("amount_valid_count")),
            "equal_weight_return_mean": fnum(pf.get("equal_weight_return_mean")),
            "return_median": fnum(pf.get("return_median")),
            "return_p25": fnum(pf.get("return_p25")),
            "return_p50": fnum(pf.get("return_p50")),
            "return_p75": fnum(pf.get("return_p75")),
            "return_p10": fnum(pf.get("return_p10")),
            "return_p90": fnum(pf.get("return_p90")),
            "advance_count": fnum(pf.get("advance_count")),
            "decline_count": fnum(pf.get("decline_count")),
            "unchanged_count": fnum(pf.get("unchanged_count")),
            # membership change + transition + diffusion (filled below)
            "membership_changed": None, "membership_added_count": None, "membership_removed_count": None,
            "transition_denominator": None,
            "regime_neutral_to_up": None, "regime_neutral_to_down": None,
            "regime_up_to_neutral": None, "regime_down_to_neutral": None,
            "regime_up_to_down": None, "regime_down_to_up": None,
            "swing_transition_count": None, "internal_transition_count": None, "momentum_transition_count": None,
            # Transition RATIOS = count / transition_denominator (NULL when denominator NULL/0)
            "regime_neutral_to_up_ratio": None, "regime_neutral_to_down_ratio": None,
            "regime_up_to_neutral_ratio": None, "regime_down_to_neutral_ratio": None,
            "regime_up_to_down_ratio": None, "regime_down_to_up_ratio": None,
            "swing_transition_ratio": None, "internal_transition_ratio": None, "momentum_transition_ratio": None,
            "transition_state_ratio_median": None,
            "diffusion_regime_up_d1": None, "diffusion_regime_up_d3": None, "diffusion_regime_up_d5": None,
            "diffusion_regime_down_d1": None, "diffusion_regime_down_d3": None, "diffusion_regime_down_d5": None,
            "diffusion_swing_up_d1": None, "diffusion_swing_up_d3": None, "diffusion_swing_up_d5": None,
            "diffusion_internal_up_d1": None, "diffusion_internal_up_d3": None, "diffusion_internal_up_d5": None,
            "diffusion_resonance_d1": None, "diffusion_resonance_d3": None, "diffusion_resonance_d5": None,
            "diffusion_expanding_d1": None, "diffusion_expanding_d3": None, "diffusion_expanding_d5": None,
            "diffusion_contracting_d1": None, "diffusion_contracting_d3": None, "diffusion_contracting_d5": None,
        }
        assert no_scoring_fields(rec)
        records.append(rec)

    rec_by_key = {(x["scope_type"], x["board_id"], x["trade_date"]): x for x in records}

    # index by (type, board_id) for trailing / T-1 logic using final records (with ratios)
    by_board = defaultdict(dict)
    for rec in records:
        by_board[(rec["scope_type"], rec["board_id"])][rec["trade_date"]] = rec

    # ---- membership change + transition (member-accurate) ----
    for rec in records:
        btype, bid, td = rec["scope_type"], rec["board_id"], rec["trade_date"]
        mem = members.get((btype, bid, td), {})
        t_members = parse_pg_array(mem.get("pit_members"))
        st_t = state.get((btype, bid, td), {})

        # Market control: no board PIT membership. Record valid-universe change, not membership_changed.
        if btype == "market":
            rec["membership_changed"] = False
            rec["membership_added_count"] = 0
            rec["membership_removed_count"] = 0
            t_minus1_mkt = canonical_previous_trading_day(TRADE_DATES, td)
            prev_mkt = by_board.get(("market", bid), {}).get(t_minus1_mkt) if t_minus1_mkt else None
            # market_valid_universe_changed: True/False when a comparable T-1 exists, else UNAVAILABLE (None)
            if prev_mkt is not None and prev_mkt.get("fp_valid_count") is not None and rec["fp_valid_count"] is not None:
                rec["market_valid_universe_changed"] = (int(prev_mkt["fp_valid_count"]) != int(rec["fp_valid_count"]))
            else:
                rec["market_valid_universe_changed"] = None
            continue

        t_minus1 = canonical_previous_trading_day(TRADE_DATES, td)
        if t_minus1 is None:
            rec["membership_changed"] = False
            rec["membership_added_count"] = 0
            rec["membership_removed_count"] = 0
            continue

        prev_members = parse_pg_array(members.get((btype, bid, t_minus1), {}).get("pit_members"))
        mc = diff_membership(tuple(t_members), tuple(prev_members))
        rec["membership_changed"] = mc.membership_changed
        rec["membership_added_count"] = mc.membership_added_count
        rec["membership_removed_count"] = mc.membership_removed_count

        st_prev = state.get((btype, bid, t_minus1), {})
        # transition_denominator = common valid members (both days valid)
        common = set(st_t.keys()) & set(st_prev.keys())
        rec["transition_denominator"] = len(common)

        if common:
            rv_up = rv_nd = rv_un = rv_dn = rv_du = rv_ud = 0
            sw_ch = ib_ch = mo_ch = 0
            trans_ratios = []
            for inst in common:
                rv_t, sb_t, ib_t, md_t = st_t[inst]
                rv_p, sb_p, ib_p, md_p = st_prev[inst]
                if rv_p == 0 and rv_t == 1: rv_up += 1
                elif rv_p == 0 and rv_t == -1: rv_nd += 1
                elif rv_p == 1 and rv_t == 0: rv_un += 1
                elif rv_p == -1 and rv_t == 0: rv_dn += 1
                elif rv_p == 1 and rv_t == -1: rv_du += 1
                elif rv_p == -1 and rv_t == 1: rv_ud += 1
                if sb_t != sb_p: sw_ch += 1
                if ib_t != ib_p: ib_ch += 1
                if md_t != md_p: mo_ch += 1
                changed = (rv_t != rv_p) or (sb_t != sb_p) or (ib_t != ib_p) or (md_t != md_p)
                trans_ratios.append(1 if changed else 0)
            rec["regime_neutral_to_up"] = rv_up
            rec["regime_neutral_to_down"] = rv_nd
            rec["regime_up_to_neutral"] = rv_un
            rec["regime_down_to_neutral"] = rv_dn
            rec["regime_up_to_down"] = rv_du
            rec["regime_down_to_up"] = rv_ud
            rec["swing_transition_count"] = sw_ch
            rec["internal_transition_count"] = ib_ch
            rec["momentum_transition_count"] = mo_ch
            rec["transition_state_ratio_median"] = round(sum(trans_ratios) / len(trans_ratios), 6) if trans_ratios else None
            # Transition RATIOS = count / denominator (NULL when denominator NULL/0). Cross-scope analysis MUST use ratios.
            denom_t = len(common)
            rec["regime_neutral_to_up_ratio"] = ratio(rv_up, denom_t)
            rec["regime_neutral_to_down_ratio"] = ratio(rv_nd, denom_t)
            rec["regime_up_to_neutral_ratio"] = ratio(rv_un, denom_t)
            rec["regime_down_to_neutral_ratio"] = ratio(rv_dn, denom_t)
            rec["regime_up_to_down_ratio"] = ratio(rv_du, denom_t)
            rec["regime_down_to_up_ratio"] = ratio(rv_ud, denom_t)
            rec["swing_transition_ratio"] = ratio(sw_ch, denom_t)
            rec["internal_transition_ratio"] = ratio(ib_ch, denom_t)
            rec["momentum_transition_ratio"] = ratio(mo_ch, denom_t)

    # ---- Diffusion (trailing PIT history per board) ----
    for (btype, bid), daymap in by_board.items():
        ordered = [d for d in TRADE_DATES if d in daymap]
        for axis_name, getter in [
            ("regime_up", lambda x: x["regime_up_ratio"]),
            ("regime_down", lambda x: x["regime_down_ratio"]),
            ("swing_up", lambda x: x["swing_up_ratio"]),
            ("internal_up", lambda x: x["internal_up_ratio"]),
            ("resonance", lambda x: x["resonance_ratio"]),
            ("expanding", lambda x: x["expanding_ratio"]),
            ("contracting", lambda x: x["contracting_ratio"]),
        ]:
            series = [getter(daymap[d]) for d in ordered]
            for i, d in enumerate(ordered):
                rec = rec_by_key[(btype, bid, d)]
                for lag, suf in [(1, "d1"), (3, "d3"), (5, "d5")]:
                    rec[f"diffusion_{axis_name}_{suf}"] = _diffusion_delta(series[: i + 1], lag)

    # write daily csv
    fieldnames = list(records[0].keys())
    with open(OUT / "s2_scope_observation_daily.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for rec in records:
            w.writerow(rec)
    return records


def _diffusion_delta(series, lag):
    if len(series) <= lag:
        return None
    curr = series[-1]; prev = series[-(lag + 1)]
    if curr is None or prev is None:
        return None
    return round(curr - prev, 6)


if __name__ == "__main__":
    recs = build()
    print(f"scope-day records: {len(recs)}")
    print(f"membership_changed=true: {sum(1 for r in recs if r['membership_changed'])}")
    print(f"transition_denominator>0: {sum(1 for r in recs if (r['transition_denominator'] or 0) > 0)}")
    print("wrote out/s2_scope_observation_daily.csv")
