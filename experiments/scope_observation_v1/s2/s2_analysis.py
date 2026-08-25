"""S2 — Short-window Scope Observation Structure Experiment.

纯函数与 DB-native 查询逻辑。无评分、无权重、无预测。

复用 S1 PIT resolver 语义（board_membership_service.resolve_board_membership_at）：
    definition.effective_from <= trade_date
    AND (effective_to IS NULL OR effective_to > trade_date)

所有 DB 查询为只读 SELECT，符合 §18 资源限制（work_mem<=64MB 等由调用方设置）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# PIT membership resolver (pure semantics, mirrors board_membership_service)
# ---------------------------------------------------------------------------
@dataclass
class PitResolution:
    board_id: str
    trade_date: str
    definition_effective_from: Optional[str]
    membership_version: Optional[str]
    pit_member_count: int
    member_ids: tuple = field(default_factory=tuple)  # frozen set of instrument_id
    valid: bool = False  # True iff pit_member_count > 0


def resolve_pit_members(
    definition_rows: list[dict], trade_date: str
) -> PitResolution:
    """Given all (effective_from, effective_to, version_id, member_ids) rows for a
    board, resolve the active membership at trade_date.

    definition_rows: list of dict with keys
        effective_from (str date), effective_to (Optional[str]),
        membership_version (str), member_ids (tuple[str]).
    Pure function — no DB. Used by tests and by the DB loader.
    """
    best_version = None
    best_from = None
    best_members: tuple = ()
    for r in definition_rows:
        ef = r["effective_from"]
        et = r.get("effective_to")
        if ef <= trade_date and (et is None or et > trade_date):
            # latest active version wins (mirror resolve_board_membership_at)
            if best_from is None or ef > best_from:
                best_from = ef
                best_version = r["membership_version"]
                best_members = r["member_ids"]
    if best_version is None:
        return PitResolution(
            board_id=r.get("board_id", "?"),
            trade_date=trade_date,
            definition_effective_from=None,
            membership_version=None,
            pit_member_count=0,
            member_ids=(),
            valid=False,
        )
    return PitResolution(
        board_id=r.get("board_id", "?"),
        trade_date=trade_date,
        definition_effective_from=best_from,
        membership_version=best_version,
        pit_member_count=len(best_members),
        member_ids=best_members,
        valid=len(best_members) > 0,
    )


# ---------------------------------------------------------------------------
# Membership change (member SET diff, not count)
# ---------------------------------------------------------------------------
@dataclass
class MembershipChange:
    membership_changed: bool
    membership_added_count: int
    membership_removed_count: int


def diff_membership(
    t_members: tuple, t_minus1_members: tuple
) -> MembershipChange:
    s_t = set(t_members)
    s_prev = set(t_minus1_members)
    added = len(s_t - s_prev)
    removed = len(s_prev - s_t)
    return MembershipChange(
        membership_changed=(added > 0 or removed > 0),
        membership_added_count=added,
        membership_removed_count=removed,
    )


# ---------------------------------------------------------------------------
# Canonical T-1
# ---------------------------------------------------------------------------
def canonical_previous_trading_day(trade_dates: list[str], current: str) -> Optional[str]:
    """Return the immediately preceding trade_date present in the set.

    No SQL LAG across missing days. If no T-1 exists -> None (UNAVAILABLE).
    """
    ordered = sorted(trade_dates)
    idx = ordered.index(current) if current in ordered else None
    if idx is None or idx == 0:
        return None
    return ordered[idx - 1]


# ---------------------------------------------------------------------------
# Transition denominator = common valid members
# ---------------------------------------------------------------------------
def transition_denominator(
    pit_t: tuple, pit_t1: tuple, valid_fp_t: set, valid_fp_t1: set
) -> set:
    """PIT(T) ∩ PIT(T-1) ∩ validFP(T) ∩ validFP(T-1)."""
    return set(pit_t) & set(pit_t1) & valid_fp_t & valid_fp_t1


# ---------------------------------------------------------------------------
# Categorical axis helpers (regime/swing/internal/momentum)
# ---------------------------------------------------------------------------
def ratio_counts(counts: dict, denominator: int) -> dict:
    if denominator <= 0:
        return {}
    return {k: v / denominator for k, v in counts.items()}


def classify_regime(regime_value: Optional[int]) -> str:
    if regime_value is None:
        return "unknown"
    if regime_value > 0:
        return "up"
    if regime_value < 0:
        return "down"
    return "neutral"


def classify_swing_bias(v: Optional[int]) -> str:
    if v is None:
        return "unknown"
    if v > 0:
        return "up"
    if v < 0:
        return "down"
    return "neutral"


def classify_internal_bias(v: Optional[int]) -> str:
    return classify_swing_bias(v)


def classify_momentum(direction: Optional[str]) -> str:
    if direction is None:
        return "unknown"
    if direction == "expanding":
        return "expanding"
    if direction == "contracting":
        return "contracting"
    if direction == "flat":
        return "flat"
    return "unknown"


def classify_structure_alignment(alignment: Optional[str]) -> str:
    if alignment is None:
        return "unknown"
    if alignment == "共振":
        return "resonance"
    if alignment == "背离":
        return "divergence"
    return "unknown"


# ---------------------------------------------------------------------------
# Transition classification (categorical T-1 -> T)
# ---------------------------------------------------------------------------
def transition_label(prev_state: str, curr_state: str) -> str:
    return f"{prev_state}->{curr_state}"


# ---------------------------------------------------------------------------
# Concentration — HHI (must keep price HHI and amount HHI separate)
# ---------------------------------------------------------------------------
def hhi(shares: list[float]) -> float:
    """Herfindahl-Hirschman Index = sum(share^2).

    shares must already be normalized fractions (sum ~= 1).
    """
    if not shares:
        return 0.0
    return sum(s * s for s in shares)


def normalized_hhi(raw_hhi, n: int) -> Optional[float]:
    """Size-adjusted (N-bias corrected) HHI = (HHI - 1/N) / (1 - 1/N).

    0 = perfectly uniform for the current member count; 1 = a single member
    dominates. N must be the actual valid-member denominator of that metric.
    This is a mathematical normalization, NOT a score.
    N<=1 -> NULL (undefined).
    """
    if n is None or n <= 1 or raw_hhi is None:
        return None
    denom = 1.0 - 1.0 / n
    if denom <= 0:
        return None
    return (raw_hhi - 1.0 / n) / denom


def price_contribution_hhi(price_change_shares: list[float]) -> float:
    """Member absolute price-change contribution share^2 sum.

    price_change_share = |member_return| / sum(|all member returns|).
    """
    return hhi(price_change_shares)


def amount_contribution_hhi(amount_shares: list[float]) -> float:
    """Member amount share^2 sum. amount_share = amount / sum(amount)."""
    return hhi(amount_shares)


def price_change_shares(returns: list[float]) -> list[float]:
    abs_sum = sum(abs(r) for r in returns)
    if abs_sum <= 0:
        return [0.0] * len(returns)
    return [abs(r) / abs_sum for r in returns]


def amount_shares(amounts: list[float]) -> list[float]:
    tot = sum(amounts)
    if tot <= 0:
        return [0.0] * len(amounts)
    return [a / tot for a in amounts]


# ---------------------------------------------------------------------------
# Diffusion delta (trailing PIT history; unavailable -> None)
# ---------------------------------------------------------------------------
def diffusion_delta(series: list[Optional[float]], lag: int) -> Optional[float]:
    """delta over `lag` trading days using trailing PIT history.

    series: ordered list of ratio values (oldest -> newest) for the SAME
    PIT scope at each historical date. If the lag-th previous value is
    unavailable (None or index out of range) -> None (NOT 0).
    """
    if len(series) <= lag:
        return None
    curr = series[-1]
    prev = series[-(lag + 1)]
    if curr is None or prev is None:
        return None
    return curr - prev


# ---------------------------------------------------------------------------
# Quantile (percentile) for threshold-free participation distribution
# ---------------------------------------------------------------------------
def percentile(values: list[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile. q in [0,1]. Empty -> None."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


# ---------------------------------------------------------------------------
# Objective contrast-case selection (CORRECTED)
#   - missing value never becomes 0 (a None/0 conversion is forbidden)
#   - a candidate pair participates ONLY if both rows are complete on the
#     required fields for that contrast test (similarity + contrast keys)
#   - all analytical dimensions are scaled uniformly via cross-sectional
#     rank before distance computation (experimental distance only, NOT a score)
#   - transition counts and diffusion ratios are NEVER mixed on the same axis
#     (callers pass distinct, already-ratio/normalized key sets)
# ---------------------------------------------------------------------------
def _row_has_all(rec: dict, keys: list[str]) -> bool:
    """True iff every key in `keys` is present AND not None in `rec`."""
    for k in keys:
        v = rec.get(k)
        if v is None:
            return False
    return True


def rank_scale(records: list[dict], keys: list[str]) -> dict:
    """Cross-sectional TIE-AWARE average rank (midrank) of each field in [0,1].

    Equal raw values MUST receive equal rank (average-rank / midrank). No random
    tie-break; input order is never used as rank information. Deterministic.

    Returns {field: {row_index: rank_01}}. Missing rows excluded. Only rows that
    are complete on the field get a rank. n==1 -> 0.5.
    """
    out = {}
    for k in keys:
        idx = [i for i, r in enumerate(records) if r.get(k) is not None]
        n = len(idx)
        ranks = {}
        if n == 0:
            out[k] = ranks
            continue
        if n == 1:
            out[k] = {idx[0]: 0.5}
            continue
        # stable sort by value (tie-aware: equal value -> equal average rank)
        idx.sort(key=lambda i: records[i][k])
        pos = 0
        while pos < n:
            j = pos
            v = records[idx[pos]][k]
            while j < n and records[idx[j]][k] == v:
                j += 1
            # average 1-based rank over the tie block positions [pos, j): (pos+1 + j)/2
            # e.g. positions 0,1,2 (1-based 1,2,3) -> avg 2. Then scale to [0,1].
            avg_rank_1based = (pos + 1 + j) / 2.0
            r01 = (avg_rank_1based - 1) / (n - 1)
            for t in range(pos, j):
                ranks[idx[t]] = r01
            pos = j
        out[k] = ranks
    return out


def _distance_on(ranks: dict, keys: list[str], i: int, j: int) -> float:
    d = 0.0
    for k in keys:
        if i in ranks[k] and j in ranks[k]:
            d += (ranks[k][i] - ranks[k][j]) ** 2
    return d ** 0.5


def select_contrast_cases(
    records: list[dict],
    similarity_keys: list[str],
    contrast_keys: list[str],
    top_n: int = 20,
) -> dict:
    """Deterministic objective contrast selection (rank-scaled, missing-aware).

    For each record i, find its nearest neighbor j among rows that are
    COMPLETE on BOTH similarity_keys and contrast_keys, using rank-scaled
    Euclidean distance over similarity_keys. Then compute contrast distance
    over contrast_keys (rank-scaled). A pair with any missing required field
    is UNAVAILABLE and does not participate.

    Returns {cases: [...], unavailable_pairs: n, similarity_key_count,
             contrast_key_count}. Never fills missing with 0.
    """
    required = list(dict.fromkeys(similarity_keys + contrast_keys))
    eligible = [i for i, r in enumerate(records) if _row_has_all(r, required)]
    if len(eligible) < 2:
        return {
            "cases": [],
            "unavailable_pairs": 0,
            "eligible_rows": len(eligible),
            "total_rows": len(records),
            "similarity_key_count": len(similarity_keys),
            "contrast_key_count": len(contrast_keys),
        }
    sim_ranks = rank_scale(records, similarity_keys)
    con_ranks = rank_scale(records, contrast_keys)

    pairs = []
    for i in eligible:
        # nearest neighbor by similarity distance; ties broken deterministically by
        # the neighbor's scope_day (order-independent, no random/input-order tie-break)
        best_sim = None
        best_j = -1
        for j in eligible:
            if i == j:
                continue
            sim = _distance_on(sim_ranks, similarity_keys, i, j)
            if best_j < 0 or sim < best_sim - 1e-9 or (
                abs(sim - best_sim) <= 1e-9 and records[j].get("scope_day") < records[best_j].get("scope_day")
            ):
                best_sim = sim
                best_j = j
        if best_j < 0:
            continue
        contrast = _distance_on(con_ranks, contrast_keys, i, best_j)
        pairs.append(
            {
                "a": records[i].get("scope_day"),
                "b": records[best_j].get("scope_day"),
                "similarity_distance": round(best_sim, 4),
                "contrast_distance": round(contrast, 4),
            }
        )
    pairs.sort(key=lambda p: p["contrast_distance"], reverse=True)
    return {
        "cases": pairs[:top_n],
        "unavailable_pairs": len(records) * (len(records) - 1) - len(eligible) * (len(eligible) - 1),
        "eligible_rows": len(eligible),
        "total_rows": len(records),
        "similarity_key_count": len(similarity_keys),
        "contrast_key_count": len(contrast_keys),
    }


# ---------------------------------------------------------------------------
# Row-aligned correlation (CORRECTED)
#   Pairwise-complete only on the SAME scope-day row. For each record:
#   if A and B are both available -> append (A,B). No independent drop-null
#   on each column then zip (that misaligns scope-days).
# ---------------------------------------------------------------------------
def row_aligned_correlation(records: list[dict], field_a: str, field_b: str) -> dict:
    """Pearson rho over records where BOTH field_a and field_b are available.

    Returns {'rho': float|None, 'n_pairwise_complete': int}. If n is too small
    (<=2) rho is None — we report n and the conclusion layer stays INCONCLUSIVE.
    No self-invented minimum-n product threshold.
    """
    pairs = [
        (r[field_a], r[field_b])
        for r in records
        if r.get(field_a) is not None and r.get(field_b) is not None
    ]
    n = len(pairs)
    if n < 3:
        return {"rho": None, "n_pairwise_complete": n}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return {"rho": None, "n_pairwise_complete": n}
    return {"rho": round(num / (dx * dy), 4), "n_pairwise_complete": n}


# ---------------------------------------------------------------------------
# Transition ratio (CORRECTED): count / transition_denominator, NULL when denom NULL/0.
# ---------------------------------------------------------------------------
def transition_ratio(count, denominator):
    if not denominator:
        return None
    return round(count / denominator, 6)


# ---------------------------------------------------------------------------
# Cross-horizon net divergence (CORRECTED, Q6)
#   Net distribution differences — NOT scores. 0 is a natural directional
#   boundary (equal up/down), NOT a strong/weak threshold.
# ---------------------------------------------------------------------------
def net_divergence(up_ratio, down_ratio):
    """Net = up_ratio - down_ratio over a categorical distribution. None if either missing."""
    if up_ratio is None or down_ratio is None:
        return None
    return up_ratio - down_ratio


def cross_horizon_signature(rec: dict) -> dict:
    """slow/medium/fast net directions:
       trend_net = regime_up - regime_down          (slow: TREND)
       swing_net = swing_up - swing_down            (medium: STRUCTURE)
       internal_net = internal_up - internal_down   (medium: STRUCTURE)
       momentum_net = expanding - contracting       (fast: MOMENTUM)
    0 is the natural neutral boundary (no artificial strong/weak threshold)."""
    return {
        "trend_net": net_divergence(rec.get("regime_up_ratio"), rec.get("regime_down_ratio")),
        "swing_net": net_divergence(rec.get("swing_up_ratio"), rec.get("swing_down_ratio")),
        "internal_net": net_divergence(rec.get("internal_up_ratio"), rec.get("internal_down_ratio")),
        "momentum_net": net_divergence(rec.get("expanding_ratio"), rec.get("contracting_ratio")),
    }


def sign(v):
    """Natural directional boundary at 0: -1 / 0 / +1. Not a strength threshold."""
    if v is None:
        return None
    return -1 if v < 0 else (1 if v > 0 else 0)


def is_same_direction(a, b, c, d) -> bool:
    """slow/medium/fast all non-zero and share the same sign."""
    signs = [sign(x) for x in (a, b, c, d)]
    if any(s is None for s in signs) or any(s == 0 for s in signs):
        return False
    return len(set(signs)) == 1


def is_slow_fast_reverse(a, d) -> bool:
    """slow (trend_net) vs fast (momentum_net) opposite non-zero signs."""
    sa, sd = sign(a), sign(d)
    if sa is None or sd is None or sa == 0 or sd == 0:
        return False
    return sa != sd


def no_scoring_fields(record: dict) -> bool:
    """Assert no score/rank/prediction fields leaked into an observation record."""
    forbidden = {
        "score", "rank", "ranking", "signal", "prediction", "opportunity",
        "risk_label", "weight",
    }
    return not bool(forbidden & set(record.keys()))


def as_jsonable(obj) -> object:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return obj
