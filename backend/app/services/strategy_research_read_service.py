"""Read-only owner for pre-audited strategy research snapshots (S1).

Do NOT infer strategy labels, generate signals, or read raw K-lines here.
STRATEGY_RESEARCH_SNAPSHOT is a trusted server-side absolute JSON path.
The exporter (S2) owns labels and opportunity calculations; this module owns
only descriptive aggregation of a frozen, complete, ex-ante signal cohort.
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any

SCHEMA = "panji-strategy-evaluation-v1"
MAX_BYTES = 96 * 1024 * 1024
MAX_EVENTS = 400_000
KINDS = {"BUY", "SELL"}
OUTCOMES = {"SUCCESS", "FAIL", "UNRESOLVED"}


class SnapshotUnavailable(Exception):
    """Snapshot not configured or does not exist."""


class SnapshotInvalid(Exception):
    """Untrusted, incomplete or corrupt snapshot; fail closed."""


def _number(v: Any, name: str, *, min_value: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v):
        raise SnapshotInvalid(f"{name}: expected finite number")
    a = float(v)
    if min_value is not None and a < min_value:
        raise SnapshotInvalid(f"{name}: less than {min_value}")
    return a


def _optional_number(row: dict[str, Any], name: str, *, min_value: float | None = None) -> float | None:
    val = row.get(name)
    return None if val is None else _number(val, name, min_value=min_value)


def _check_snapshot(blob: Any) -> dict[str, Any]:
    if not isinstance(blob, dict) or blob.get("schema") != SCHEMA:
        raise SnapshotInvalid("unsupported schema")
    meta = blob.get("metadata")
    models = blob.get("models")
    events = blob.get("events")
    if not isinstance(meta, dict) or not isinstance(models, list) or not isinstance(events, list):
        raise SnapshotInvalid("missing metadata/models/events")
    for key in ("strategy_key", "strategy_name", "version", "generated_at", "evaluation_protocol", "model_sha256", "data_sha256", "audit_ref"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise SnapshotInvalid(f"metadata.{key} missing")
    if meta.get("cohort") != "ALL_EX_ANTE_ALERTS" or meta.get("status") != "AUDITED":
        raise SnapshotInvalid("cohort/status must be ALL_EX_ANTE_ALERTS/AUDITED")
    if len(events) > MAX_EVENTS:
        raise SnapshotInvalid("event limit exceeded")
    if not models or len(models) > 20:
        raise SnapshotInvalid("model count out of bounds")
    ids: set[str] = set()
    for m in models:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m.get("name") or m.get("side") not in KINDS:
            raise SnapshotInvalid("invalid model identity")
        if m["id"] in ids:
            raise SnapshotInvalid("duplicate model id")
        ids.add(m["id"])
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(events):
        if not isinstance(row, dict):
            raise SnapshotInvalid(f"event[{i}] not object")
        for field in ("event_id", "model_id", "symbol", "signal_time"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise SnapshotInvalid(f"event[{i}].{field} missing")
        if row["model_id"] not in ids:
            raise SnapshotInvalid("unknown event model")
        ref_model = next(m for m in models if m["id"] == row["model_id"])
        if row.get("side") != ref_model["side"]:
            raise SnapshotInvalid("model/event direction mismatch")
        if row.get("outcome") not in OUTCOMES:
            raise SnapshotInvalid("invalid event outcome")
        if (row["model_id"], row["event_id"]) in seen:
            raise SnapshotInvalid("duplicate model event")
        seen.add((row["model_id"], row["event_id"]))
        for name in ("endpoint_pct", "mfe_pct", "mae_pct", "mdd_pct"):
            _optional_number(row, name, min_value=(0 if name in ("mae_pct", "mdd_pct") else None))
        if row["outcome"] == "UNRESOLVED" and any(row.get(k) is not None for k in ("endpoint_pct", "mfe_pct", "mae_pct", "mdd_pct")):
            raise SnapshotInvalid("unresolved events cannot carry ex-post metrics")
        if row["outcome"] != "UNRESOLVED" and (row.get("mfe_pct") is None or row.get("mae_pct") is None):
            raise SnapshotInvalid("resolved event missing MFE/MAE")
        path = row.get("path")
        if path is not None:
            if not isinstance(path, list) or len(path) > 500:
                raise SnapshotInvalid("path must contain <=500 daily bars")
            for bar in path:
                if not isinstance(bar, dict):
                    raise SnapshotInvalid("invalid path bar")
                _number(bar.get("favorable_pct"), "path.favorable_pct")
                _number(bar.get("adverse_pct"), "path.adverse_pct", min_value=0)
    return blob


@lru_cache(maxsize=2)
def _load_cached(filepath: str, mtime_ns: int, size: int) -> dict[str, Any]:
    if size > MAX_BYTES:
        raise SnapshotInvalid("snapshot too large")
    try:
        with open(filepath, "rb") as stream:
            payload = stream.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise SnapshotInvalid("snapshot too large")
        return _check_snapshot(json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SnapshotInvalid("malformed JSON") from e


def read_snapshot() -> dict[str, Any]:
    filepath = os.environ.get("STRATEGY_RESEARCH_SNAPSHOT", "")
    if not filepath:
        raise SnapshotUnavailable("研究评价数据尚未接入")
    p = Path(filepath)
    if not p.is_absolute() or not p.is_file():
        raise SnapshotUnavailable("研究评价数据尚未接入")
    try:
        st = p.stat()
        return _load_cached(str(p), st.st_mtime_ns, st.st_size)
    except OSError as e:
        raise SnapshotUnavailable("研究评价快照不可读取") from e


def _quantile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    a = sorted(values)
    x = (len(a) - 1) * p
    lo, hi = int(x), min(int(x) + 1, len(a) - 1)
    return round(a[lo] + (a[hi] - a[lo]) * (x - lo), 4)


def _hist(values: list[float], bins: int = 24) -> list[dict[str, float | int]]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        return [{"low": lo - .001, "high": hi + .001, "count": len(values)}]
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / step))] += 1
    return [{"low": round(lo + i * step, 6), "high": round(lo + (i + 1) * step, 6), "count": cnt} for i, cnt in enumerate(counts)]


def _touch_order(path: list[dict[str, float]] | None, gain: float, risk: float) -> str:
    if not path:
        return "UNAVAILABLE"
    for bar in path:
        target = bar["favorable_pct"] >= gain
        adverse = bar["adverse_pct"] >= risk
        if target and adverse:
            return "SAME_BAR_UNKNOWN"
        if target:
            return "TARGET_FIRST"
        if adverse:
            return "RISK_FIRST"
    return "NEITHER"


def evaluate(snapshot: dict[str, Any], side: str, symbol: str | None, gain: float, risk: float) -> dict[str, Any]:
    if side not in KINDS:
        raise ValueError("invalid side")
    _number(gain, "gain", min_value=.01)
    _number(risk, "risk", min_value=.01)
    if gain > 100 or risk > 100:
        raise ValueError("threshold out of range")
    rows = [e for e in snapshot["events"] if e["side"] == side and (symbol is None or e["symbol"] == symbol)]
    results = []
    points = []
    table = []
    for m in snapshot["models"]:
        if m["side"] != side:
            continue
        one = [e for e in rows if e["model_id"] == m["id"]]
        resolved = [e for e in one if e["outcome"] != "UNRESOLVED"]
        valid = [e for e in resolved if e.get("mfe_pct") is not None and e.get("mae_pct") is not None]
        success = sum(e["outcome"] == "SUCCESS" for e in resolved)
        joint = sum(e["mfe_pct"] >= gain and e["mae_pct"] <= risk for e in valid)
        metrics = {k: [float(e[k]) for e in resolved if e.get(k) is not None] for k in ("endpoint_pct", "mfe_pct", "mae_pct", "mdd_pct")}
        orders = [_touch_order(e.get("path"), gain, risk) for e in resolved]
        # Stable, uniform sample by source order; never describe scatter sample as the full population.
        stride = max(1, math.ceil(len(valid) / 550))
        for e in valid[::stride][:550]:
            points.append({"event_id": e["event_id"], "model_id": m["id"], "symbol": e["symbol"], "signal_time": e["signal_time"], "mfe_pct": e["mfe_pct"], "mae_pct": e["mae_pct"], "outcome": e["outcome"]})
        results.append({
            "model_id": m["id"], "model_name": m["name"], "n_signals": len(one),
            "n_resolved": len(resolved), "n_unresolved": len(one) - len(resolved),
            "n_success": success, "success_rate": round(success / len(resolved), 6) if resolved else None,
            "n_path": len(valid), "joint_count": joint,
            "joint_rate": round(joint / len(valid), 6) if valid else None,
            "endpoint_p50": _quantile(metrics["endpoint_pct"], .5),
            "mfe_p50": _quantile(metrics["mfe_pct"], .5),
            "mfe_p75": _quantile(metrics["mfe_pct"], .75),
            "mae_p80": _quantile(metrics["mae_pct"], .8),
            "mdd_p90": _quantile(metrics["mdd_pct"], .9),
            "touch_order": {k: orders.count(k) for k in ("TARGET_FIRST", "RISK_FIRST", "SAME_BAR_UNKNOWN", "NEITHER", "UNAVAILABLE")},
            "distributions": {k: _hist(v) for k, v in metrics.items()},
        })
        for e in sorted(one, key=lambda x: (x["signal_time"], x["event_id"]), reverse=True)[:80]:
            table.append({k: e.get(k) for k in ("event_id", "model_id", "symbol", "signal_time", "outcome", "mfe_pct", "mae_pct", "mdd_pct", "endpoint_pct")})
    table.sort(key=lambda e: (e["signal_time"], e["model_id"], e["event_id"]), reverse=True)
    return {
        "metadata": snapshot["metadata"], "side": side, "symbol": symbol,
        "gain_pct": gain, "risk_pct": risk,
        "models": results, "points": points,
        "points_sampled": sum(m["n_path"] for m in results) > len(points),
        "table": table[:180],
    }
