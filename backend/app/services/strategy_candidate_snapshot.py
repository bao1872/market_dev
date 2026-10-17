"""Read-only, fail-closed as-of candidate snapshots.

This service NEVER computes ML forecasts. Data must come from a separately
verified panji_lab exporter; missing data is unavailable, not a zero-signal day.
Snapshot is trusted local operator-controlled file, not user-supplied JSON.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from app.services.strategy_candidate_shards import (
    MANIFEST_SCHEMA,
    ShardContractError,
    load_candidate_shards,
)

SCHEMA = "panji-strategy-candidates-v2"
MAX_BYTES = 48 * 1024 * 1024
MAX_STOCKS = 8000
SYMBOL_RE = re.compile(r"^(SH|SZ|BJ):[0-9]{6}$")

# Bounded cache for pinned immutable shard releases (see `read_dataset`). A single
# entry bounds resident memory for the largest full-market snapshot; the key is an
# immutable release identity (pinned physical directory + descriptor digest),
# never the mutable `current` symlink path.
_SHARD_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_SHARD_CACHE_MAX = 1


class DatasetUnavailable(Exception):
    pass


class DatasetInvalid(Exception):
    pass


def _day(v: Any, field: str) -> str:
    if not isinstance(v, str) or len(v) != 10:
        raise DatasetInvalid(f"{field}: invalid date")
    try:
        if date.fromisoformat(v).isoformat() != v:
            raise ValueError(v)
    except ValueError as exc:
        raise DatasetInvalid(f"{field}: invalid date") from exc
    return v


def _score(v: Any, field: str) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1:
        raise DatasetInvalid(f"{field}: expected finite 0..1 score")
    return float(v)


def _positive(v: Any, field: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise DatasetInvalid(f"{field}: expected positive price")
    return float(v)


def validate(blob: Any) -> dict[str, Any]:
    if not isinstance(blob, dict) or blob.get("schema") not in (SCHEMA, "panji-strategy-candidates-v3"):
        raise DatasetInvalid("unsupported candidate schema")
    fourteen = blob["schema"] == "panji-strategy-candidates-v3"
    meta = blob.get("metadata")
    stocks = blob.get("stocks")
    daily = blob.get("daily_batches")
    if not isinstance(meta, dict) or not isinstance(stocks, dict) or not isinstance(daily, list):
        raise DatasetInvalid("metadata/stocks/daily_batches required")
    if meta.get("status") != "AUDITED":
        raise DatasetInvalid("only externally audited data may be presented")
    for key in ("strategy_id", "release_id", "feature_sha", "audit_ref", "generated_at"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise DatasetInvalid(f"metadata.{key} required")
    if meta["strategy_id"] != "zigzag_two_stage" or len(stocks) > MAX_STOCKS:
        raise DatasetInvalid("unsupported strategy or stock count")
    if fourteen and (not isinstance(meta.get("model_version"), str) or not meta["model_version"] or meta.get("scoring_architecture") != "GLOBAL_FOURTEEN"):
        raise DatasetInvalid("fourteen global model identity required")
    observation_index: dict[str, dict[str, Any]] = {}
    candidate_index: dict[str, str] = {}
    for symbol, stock in stocks.items():
        if not SYMBOL_RE.fullmatch(symbol) or not isinstance(stock, dict):
            raise DatasetInvalid(f"invalid stock {symbol}")
        bars = stock.get("bars")
        candidates = stock.get("candidates")
        if not isinstance(bars, list) or not isinstance(candidates, list) or len(bars) > 15000 or len(candidates) > 15000:
            raise DatasetInvalid(f"{symbol}: invalid bar/candidate arrays")
        prev = ""
        bar_days: set[str] = set()
        bar_by_day: dict[str, dict[str, Any]] = {}
        for bar in bars:
            if not isinstance(bar, dict):
                raise DatasetInvalid("bar must be object")
            d = _day(bar.get("date"), "bar.date")
            if d <= prev:
                raise DatasetInvalid("bars must be strictly ascending")
            prev = d
            bar_days.add(d)
            bar_by_day[d] = bar
            op, hi, lo, cl = (_positive(bar.get(k), f"bar.{k}") for k in ("open", "high", "low", "close"))
            if not (lo <= min(op, cl) <= max(op, cl) <= hi):
                raise DatasetInvalid("OHLC inconsistent")
        for cand in candidates:
            if not isinstance(cand, dict):
                raise DatasetInvalid("candidate must be object")
            cid = cand.get("candidate_id")
            if not isinstance(cid, str) or not cid or cid in candidate_index:
                raise DatasetInvalid("duplicate/empty candidate_id")
            candidate_index[cid] = symbol
            anchor = _day(cand.get("anchor_date"), "candidate.anchor_date")
            visible = _day(cand.get("visible_at"), "candidate.visible_at")
            if anchor not in bar_days or visible < anchor or visible not in bar_days:
                raise DatasetInvalid("invalid candidate visibility")
            if cand.get("kind") not in ("H", "L"):
                raise DatasetInvalid("kind must be H/L")
            _positive(cand.get("anchor_price"), "candidate.anchor_price")
            obs = cand.get("observations")
            if not isinstance(obs, list) or len(obs) > 4:
                raise DatasetInvalid("candidate may have at most three observations")
            stages = set()
            prev_decision = ""
            for row in obs:
                if not isinstance(row, dict):
                    raise DatasetInvalid("observation must be object")
                oid = row.get("observation_id")
                if not isinstance(oid, str) or not oid or oid in observation_index:
                    raise DatasetInvalid("duplicate/empty observation_id")
                stage = row.get("stage")
                if stage not in (("D0", "D1", "D2", "D3") if fourteen else ("D1", "D2", "D3")) or stage in stages:
                    raise DatasetInvalid("invalid/duplicate stage")
                stages.add(stage)
                decision = _day(row.get("decision_date"), "observation.decision_date")
                cutoff = _day(row.get("data_cutoff"), "observation.data_cutoff")
                if decision not in bar_days or decision < visible or decision <= prev_decision or cutoff > decision:
                    raise DatasetInvalid("observation violates PIT/ordering")
                prev_decision = decision
                if fourteen:
                    if row.get("scoring_status", "SCORED") not in ("SCORED", "CANDIDATE_INACTIVE", "INSUFFICIENT_HISTORY"):
                        raise DatasetInvalid("invalid scoring availability")
                    if row.get("scoring_status") in ("CANDIDATE_INACTIVE", "INSUFFICIENT_HISTORY") and any(row.get(k) is not None for k in ("l1_score", "l2_score", "d0_score")):
                        raise DatasetInvalid("inactive candidate cannot carry current scores")
                    _score(row.get("d0_score"), "d0_score")
                    _score(row.get("d0_threshold"), "d0_threshold")
                    if stage == "D0" and (decision != anchor or row.get("d0_score") is None or row.get("l1_score") is not None or row.get("l2_score") is not None or row.get("joint_alert")):
                        raise DatasetInvalid("invalid D0 observation")
                    if stage != "D0" and row.get("d0_score") is not None:
                        raise DatasetInvalid("D0 score on D123 observation")
                    anchor_index = next(i for i, b in enumerate(bars) if b["date"] == anchor)
                    decision_index = next(i for i, b in enumerate(bars) if b["date"] == decision)
                    if decision_index - anchor_index != int(stage[1:]):
                        raise DatasetInvalid("stage trading-bar offset mismatch")
                    if row.get('scoring_status') == 'INSUFFICIENT_HISTORY':
                        count = row.get('history_bars_available')
                        offset = stock.get('tv_zigzag', {}).get('barCount', len(bars))-len(bars)
                        if isinstance(count, bool) or not isinstance(count, int) or count != offset+decision_index+1 or not 1 <= count < 21:
                            raise DatasetInvalid('invalid insufficient-history evidence')
                    price_key = "high" if cand["kind"] == "H" else "low"
                    anchor_price = bar_by_day[anchor][price_key]
                    if cand['anchor_price'] != anchor_price or row.get('model_version') != meta['model_version']:
                        raise DatasetInvalid("fourteen anchor/model identity mismatch")
                    if stage != 'D0' and row.get('scoring_status', 'SCORED') == 'SCORED' and (row.get('l1_score') is None or row.get('l2_score') is None or row.get('l1_threshold') is None or row.get('l2_threshold') is None):
                        raise DatasetInvalid("fourteen D123 scores required")
                    if row.get('l2_evaluation_domain_verified') is not False:
                        raise DatasetInvalid("L2 domain must remain conditional")
                    if row.get("scoring_status", "SCORED") == "SCORED" and any(b[price_key] > anchor_price if cand["kind"] == "H" else b[price_key] < anchor_price for b in bars[anchor_index+1:decision_index+1]):
                        raise DatasetInvalid("superseded fourteen candidate")
                _score(row.get("l1_score"), "l1_score")
                _score(row.get("l2_score"), "l2_score")
                _score(row.get("l1_threshold"), "l1_threshold")
                _score(row.get("l2_threshold"), "l2_threshold")
                if not isinstance(row.get("joint_alert"), bool):
                    raise DatasetInvalid("joint_alert must be bool")
                if row["joint_alert"] and (
                    row.get("l1_score") is None or row.get("l2_score") is None or
                    row.get("l1_threshold") is None or row.get("l2_threshold") is None or
                    row["l1_score"] < row["l1_threshold"] or row["l2_score"] < row["l2_threshold"]
                ):
                    raise DatasetInvalid("joint_alert inconsistent with thresholds")
                ref = row.get("reference_price")
                confirmed = row.get("reference_confirmed_at")
                if (ref is None) != (confirmed is None):
                    raise DatasetInvalid("reference price/confirmation must be paired")
                if ref is not None:
                    _positive(ref, "reference_price")
                    if _day(confirmed, "reference_confirmed_at") > decision:
                        raise DatasetInvalid("future reference confirmation")
                    if fourteen and confirmed > anchor:
                        raise DatasetInvalid("reference must be frozen by anchor close")
                first = row.get("first_breakout")
                if not isinstance(first, bool):
                    raise DatasetInvalid("first_breakout must be bool")
                if first and ref is None:
                    raise DatasetInvalid("first breakout missing observable reference")
                if first:
                    decision_bar = bar_by_day[decision]
                    crossed = (float(decision_bar["high"]) > float(ref)) if cand["kind"] == "L" else (float(decision_bar["low"]) < float(ref))
                    if not crossed:
                        raise DatasetInvalid("first breakout not supported by decision-day OHLC")
                if fourteen:
                    fact = row.get('breakout')
                    if fact is None and first:
                        raise DatasetInvalid('first breakout fact required')
                    if fact is not None:
                        from app.strategy_integration.breakout import event_identity
                        if not isinstance(fact, dict) or stage == 'D0':
                            raise DatasetInvalid('invalid breakout fact')
                        first_day = _day(fact.get('first_date'), 'breakout.first_date')
                        first_index = next((i for i, b in enumerate(bars) if b['date'] == first_day), -1)
                        if not 1 <= first_index-anchor_index <= 3 or first_day > decision:
                            raise DatasetInvalid('breakout first date outside D123')
                        if fact.get('first_stage') != f'D{first_index-anchor_index}' or fact.get('is_first_today') is not first or first != (first_day == decision):
                            raise DatasetInvalid('breakout first stage/date mismatch')
                        if fact.get('reference_price') != ref or fact.get('reference_confirmed_at') != confirmed or ref is None:
                            raise DatasetInvalid('breakout reference mismatch')
                        direction = 'HIGH' if cand['kind'] == 'L' else 'LOW'
                        price_field = 'high' if direction == 'HIGH' else 'low'
                        def crossed_at(b):
                            return b[price_field] > ref if direction == 'HIGH' else b[price_field] < ref
                        if fact.get('direction') != direction or not crossed_at(bars[first_index]) or any(crossed_at(b) for b in bars[anchor_index:first_index]):
                            raise DatasetInvalid('breakout is not strict first occurrence')
                        if fact.get('event_id') != event_identity(symbol, anchor, cand['kind'], meta['release_id']):
                            raise DatasetInvalid('breakout event identity mismatch')
                        holding = bars[decision_index]['close'] > ref if direction == 'HIGH' else bars[decision_index]['close'] < ref
                        if fact.get('holding_beyond_reference') is not holding:
                            raise DatasetInvalid('breakout holding state mismatch')
                observation_index[oid] = {"symbol": symbol, "candidate_id": cid, "stage": stage, "decision_date": decision,
                                          "kind": cand["kind"], "l1_score": row.get("l1_score"), "l2_score": row.get("l2_score"),
                                          "joint_alert": row["joint_alert"], "first_breakout": first,
                                          **({k: row.get(k) for k in ('d0_score', 'd0_threshold', 'breakout', 'segment', 'scoring_status', 'reference_price', 'reference_confirmed_at')} if fourteen else {})}
            expected_stages = ["D0"] if fourteen and "D0" in stages else ["D1", "D2", "D3"][:len(stages)]
            if expected_stages != [o["stage"] for o in obs]:
                raise DatasetInvalid("stage must be a D1-first prefix")
            if sum(bool(o["first_breakout"]) for o in obs) > 1:
                raise DatasetInvalid("first breakout may be emitted once per candidate")
    if len(daily) > 350:
        raise DatasetInvalid("daily batch count too large")
    seen_batches = set()
    for batch in daily:
        if not isinstance(batch, dict):
            raise DatasetInvalid("batch must be object")
        td = _day(batch.get("trade_date"), "daily.trade_date")
        if td in seen_batches:
            raise DatasetInvalid("duplicate trade_date batch")
        seen_batches.add(td)
        if batch.get("status") != "PUBLISHED":
            raise DatasetInvalid("only published batches may appear")
        if batch.get("release_id") != meta["release_id"]:
            raise DatasetInvalid("mixed release in candidate snapshot")
        items = batch.get("items")
        if not isinstance(items, list) or len(items) > 120000:
            raise DatasetInvalid("invalid batch items")
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise DatasetInvalid("batch item must be object")
            oid = item.get("observation_id")
            if not isinstance(oid, str) or oid not in observation_index or oid in seen:
                raise DatasetInvalid("unknown or duplicate daily observation")
            seen.add(oid)
            ref = observation_index[oid]
            if ref["decision_date"] != td:
                raise DatasetInvalid("daily item mismatched decision date")
            for k in ("symbol", "candidate_id", "stage", "kind", "l1_score", "l2_score", "joint_alert", "first_breakout", *(("d0_score", "d0_threshold", "breakout", "segment", "scoring_status", "reference_price", "reference_confirmed_at") if fourteen else ())):
                if item.get(k) != ref[k]:
                    raise DatasetInvalid(f"daily item {k} mismatch with observation SSOT")
    return blob


def read_dataset(*, symbol=None, trade_date=None) -> dict[str, Any]:
    """Load the pinned release; the 48 MiB per-file bound is never relaxed.

    The physical release directory is resolved BEFORE any read so an atomic
    `current` symlink swap cannot mix files belonging to two releases. A shard
    descriptor and a legacy single-file snapshot both end in the same in-memory
    shape validated by `validate()`.
    """
    path = os.getenv("PANJI_STRATEGY_CANDIDATE_SNAPSHOT", "")
    if not path and not os.getenv("PANJI_STRATEGY_RELEASE_ROOT"):
        raise DatasetUnavailable("尚未配置已审计的候选事件快照")
    from app.strategy_integration.release_store import PublishError, published_snapshot_path
    try:
        p = published_snapshot_path("candidate_snapshot.json", path)
    except PublishError as exc:
        raise DatasetInvalid(str(exc)) from exc
    if not p.is_absolute() or not p.is_file():
        raise DatasetUnavailable("候选快照不存在或超过大小限制")
    pinned = p.resolve()
    if pinned.stat().st_size > MAX_BYTES:
        raise DatasetInvalid("candidate snapshot too large")
    try:
        raw = pinned.read_bytes()
    except OSError as exc:
        raise DatasetInvalid("candidate snapshot not readable") from exc
    try:
        blob = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetInvalid("candidate snapshot not valid JSON") from exc
    from .strategy_partitioned_snapshot import SCHEMA as PARTITIONED, read_candidates
    if isinstance(blob, dict) and blob.get('schema') == PARTITIONED:
        try:
            return read_candidates(pinned, symbol=symbol, trade_date=trade_date)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise DatasetInvalid('partitioned candidate contract failed') from exc
    if isinstance(blob, dict) and blob.get("schema") == MANIFEST_SCHEMA:
        # Bounded cache keyed by the PINNED physical release directory plus the
        # descriptor digest: swapping `current` to another batch changes the key,
        # and rewriting a file in place changes the digest. Never keyed on the
        # mutable symlink path alone.
        key = (str(pinned), hashlib.sha256(raw).hexdigest())
        cached = _SHARD_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            merged = load_candidate_shards(pinned, validate=validate)
        except ShardContractError as exc:
            raise DatasetInvalid(f"candidate shard contract: {exc}") from exc
        if len(_SHARD_CACHE) >= _SHARD_CACHE_MAX:
            _SHARD_CACHE.clear()
        _SHARD_CACHE[key] = merged
        return merged
    return validate(blob)


def get_stock(dataset: dict[str, Any], symbol: str) -> dict[str, Any]:
    if not SYMBOL_RE.fullmatch(symbol):
        raise DatasetInvalid("invalid symbol")
    value = dataset["stocks"].get(symbol)
    if value is None:
        raise DatasetUnavailable("股票不在已发布快照中")
    return {"symbol": symbol, "release_id": dataset["metadata"]["release_id"], "metadata": dataset["metadata"], **value}


def get_today(dataset: dict[str, Any], trade_date: str | None = None) -> dict[str, Any]:
    batches = dataset["daily_batches"]
    if trade_date is not None:
        _day(trade_date, "trade_date")
        selected = next((b for b in batches if b["trade_date"] == trade_date), None)
    else:
        selected = max(batches, key=lambda b: b["trade_date"]) if batches else None
    if selected is None:
        raise DatasetUnavailable("查询日期没有已发布批次（并非零机会）")
    return {"metadata": dataset["metadata"], **selected}
