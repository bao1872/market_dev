"""Bounded, immutable, sha-pinned candidate snapshot shards.

Use as a helper in the existing strategy_candidate_snapshot owner.
No DB/network/ML imports. It does not change candidate business semantics.
"""
from __future__ import annotations
from collections import defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

MANIFEST_SCHEMA = "panji-strategy-candidate-shards-v1"
DATA_SCHEMA = "panji-strategy-candidates-v2"
MAX_SHARDS = 64
MAX_SHARD_BYTES = 48 * 1024 * 1024  # existing per-file bound is unchanged
MAX_MANIFEST_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 384 * 1024 * 1024  # separate explicit aggregate bound
SHARD_NAME = re.compile(r"candidate_shard_[0-9]{4}\.json\Z")


class ShardContractError(ValueError):
    """Reject incomplete, altered or ambiguous snapshot sets."""


def encode(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _atomic_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".panji-shard-")
    try:
        with os.fdopen(fd, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _partition(dataset: dict, target_bytes: int) -> list[dict]:
    if not 0 < target_bytes < MAX_SHARD_BYTES:
        raise ShardContractError("TARGET_SHARD_SIZE_OUT_OF_RANGE")
    if dataset.get("schema") != DATA_SCHEMA:
        raise ShardContractError("CANDIDATE_SCHEMA_MISMATCH")
    stocks, batches = dataset["stocks"], dataset["daily_batches"]
    by_symbol: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for batch in batches:
        day = batch["trade_date"]
        for item in batch["items"]:
            sym = item["symbol"]
            if sym not in stocks:
                raise ShardContractError("ORPHAN_DAILY_ITEM")
            by_symbol[sym][day].append(item)
    chunks: list[list[str]] = []
    current: list[str] = []
    running = 0
    for symbol in sorted(stocks):
        estimate = len(encode(stocks[symbol])) + len(symbol.encode()) + 8
        estimate += sum(len(encode(i)) + 1 for rows in by_symbol[symbol].values() for i in rows)
        if current and running + estimate > target_bytes:
            chunks.append(current)
            current, running = [], 0
        current.append(symbol)
        running += estimate
    if current or not chunks:
        chunks.append(current)
    if len(chunks) > MAX_SHARDS:
        raise ShardContractError("TOO_MANY_SHARDS")
    answer = []
    for symbols in chunks:
        group = set(symbols)
        sub = {
            "schema": DATA_SCHEMA,
            "metadata": dataset["metadata"],
            "stocks": {s: stocks[s] for s in symbols},
            "daily_batches": [
                {
                    "trade_date": b["trade_date"],
                    "release_id": b["release_id"],
                    "status": b["status"],
                    "items": [i for i in b["items"] if i["symbol"] in group],
                }
                for b in batches
            ],
        }
        answer.append(sub)
    return answer


def write_candidate_shards(
    dataset: dict,
    folder: Path,
    *,
    validate: Callable[[dict], dict],
    target_bytes: int = 12 * 1024 * 1024,
) -> dict:
    """Create one complete immutable snapshot directory, never a current pointer.

    The caller must ensure folder is a new staging directory. The reader contract
    and source cohort cannot be weakened by partitioning.
    """
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise ShardContractError("STAGING_DIRECTORY_NOT_EMPTY")
    validate(dataset)
    subdatasets = _partition(dataset, target_bytes)
    entries: list[dict] = []
    total = 0
    for index, sub in enumerate(subdatasets):
        validate(sub)
        data = encode(sub)
        if len(data) > MAX_SHARD_BYTES:
            raise ShardContractError("SHARD_EXCEEDS_EXISTING_48MIB_LIMIT")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ShardContractError("CANDIDATE_SHARDS_AGGREGATE_LIMIT")
        filename = f"candidate_shard_{index:04d}.json"
        _atomic_write(folder / filename, data)
        entries.append({
            "file": filename, "sha256": sha256(data).hexdigest(),
            "bytes": len(data), "stocks": len(sub["stocks"]),
        })
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "metadata": dataset["metadata"],
        "shards": entries,
        "total_bytes": total,
        "stock_count": len(dataset["stocks"]),
        "batch_days": [b["trade_date"] for b in dataset["daily_batches"]],
    }
    descriptor = encode(manifest)
    if len(descriptor) > MAX_MANIFEST_BYTES:
        raise ShardContractError("MANIFEST_EXCEEDS_LIMIT")
    _atomic_write(folder / "candidate_snapshot.json", descriptor)
    return manifest


def load_candidate_shards(
    manifest_path: Path,
    *,
    validate: Callable[[dict], dict],
) -> dict:
    """Read and verify one pinned staging directory; fail closed on any shard.

    Resolving the manifest path first binds it to one immutable release even
    if a separate current symlink is atomically updated while serving requests.
    """
    path = Path(manifest_path).resolve(strict=True)
    raw_manifest = path.read_bytes()
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise ShardContractError("MANIFEST_EXCEEDS_LIMIT")
    try:
        manifest = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ShardContractError("INVALID_MANIFEST_JSON") from ex
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ShardContractError("INVALID_MANIFEST_SCHEMA")
    entries = manifest.get("shards")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SHARDS:
        raise ShardContractError("INVALID_SHARD_COUNT")
    if not isinstance(manifest.get("metadata"), dict):
        raise ShardContractError("INVALID_MANIFEST_METADATA")
    batch_days = manifest.get("batch_days")
    if not isinstance(batch_days, list) or len(batch_days) != len(set(batch_days)):
        raise ShardContractError("INVALID_BATCH_DAYS")
    if len(batch_days) > 350:
        raise ShardContractError("TOO_MANY_BATCH_DAYS")
    if manifest.get("total_bytes", 0) > MAX_TOTAL_BYTES:
        raise ShardContractError("AGGREGATE_SIZE_EXCEEDED")
    all_stocks: dict[str, dict] = {}
    joined_days = {day: None for day in batch_days}
    total = 0
    seen_files = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ShardContractError("INVALID_SHARD_ENTRY")
        filename = entry.get("file")
        if not isinstance(filename, str) or not SHARD_NAME.fullmatch(filename) or filename in seen_files:
            raise ShardContractError("INVALID_SHARD_FILENAME")
        seen_files.add(filename)
        subpath = path.parent / filename
        if subpath.is_symlink() or not subpath.is_file():
            raise ShardContractError("MISSING_OR_SYMLINKED_SHARD")
        n = subpath.stat().st_size
        if not 0 < n <= MAX_SHARD_BYTES or n != entry.get("bytes"):
            raise ShardContractError("SHARD_BYTES_MISMATCH")
        payload = subpath.read_bytes()
        if len(payload) != n or sha256(payload).hexdigest() != entry.get("sha256"):
            raise ShardContractError("SHARD_SHA_MISMATCH")
        try:
            sub = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as ex:
            raise ShardContractError("INVALID_SHARD_JSON") from ex
        if sub.get("metadata") != manifest["metadata"] or sub.get("schema") != DATA_SCHEMA:
            raise ShardContractError("SHARD_RELEASE_OR_SCHEMA_MISMATCH")
        validate(sub)
        if len(sub["stocks"]) != entry.get("stocks"):
            raise ShardContractError("SHARD_STOCK_COUNT_MISMATCH")
        overlap = set(all_stocks) & set(sub["stocks"])
        if overlap:
            raise ShardContractError("DUPLICATE_CROSS_SHARD_STOCK")
        all_stocks.update(sub["stocks"])
        days = [b["trade_date"] for b in sub["daily_batches"]]
        if days != batch_days:
            raise ShardContractError("SHARD_BATCH_DAYS_MISMATCH")
        for batch in sub["daily_batches"]:
            day = batch["trade_date"]
            if joined_days[day] is None:
                joined_days[day] = {
                    "trade_date": day,
                    "release_id": batch["release_id"],
                    "status": batch["status"],
                    "items": [],
                }
            else:
                if joined_days[day]["release_id"] != batch["release_id"] or joined_days[day]["status"] != batch["status"]:
                    raise ShardContractError("CROSS_SHARD_BATCH_MISMATCH")
            joined_days[day]["items"].extend(batch["items"])
        total += n
        if total > MAX_TOTAL_BYTES:
            raise ShardContractError("AGGREGATE_SIZE_EXCEEDED")
    if total != manifest.get("total_bytes") or len(all_stocks) != manifest.get("stock_count"):
        raise ShardContractError("AGGREGATE_IDENTITY_MISMATCH")
    merged = {
        "schema": DATA_SCHEMA,
        "metadata": manifest["metadata"],
        "stocks": all_stocks,
        "daily_batches": [joined_days[d] for d in batch_days],
    }
    # Verifies globally unique candidate IDs and observation IDs across shards;
    # also checks every daily item points to one of those observations.
    validate(merged)
    return merged
