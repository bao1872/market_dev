"""Full reader-contract tests for an optional immutable sharded candidate snapshot."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import pytest
from app.services.strategy_candidate_snapshot import validate
from app.services.strategy_candidate_shards import (
    ShardContractError, load_candidate_shards, write_candidate_shards,
)


def fixture(stocks=6):
    day0="2026-08-14"; day1="2026-08-17"
    ss={}; items=[]
    for i in range(stocks):
        symbol=f"SH:{600000+i:06d}"
        oid=f"obs-{i}"
        cid=f"cand-{i}"
        obs={"observation_id":oid,"stage":"D1","decision_date":day1,"data_cutoff":day1,
             "l1_score":.7,"l2_score":.8,"l1_threshold":.5,"l2_threshold":.6,
             "joint_alert":True,"reference_price":None,"reference_confirmed_at":None,
             "first_breakout":False}
        ss[symbol]={"bars":[{"date":day0,"open":10,"high":12,"low":9,"close":11},
                            {"date":day1,"open":11,"high":12,"low":10,"close":11}],
            "candidates":[{"candidate_id":cid,"anchor_date":day0,"visible_at":day1,
                           "anchor_price":9,"kind":"L","observations":[obs]}]}
        items.append({"observation_id":oid,"candidate_id":cid,"symbol":symbol,"kind":"L",
                      "stage":"D1","l1_score":.7,"l2_score":.8,
                      "joint_alert":True,"first_breakout":False})
    return {"schema":"panji-strategy-candidates-v2",
            "metadata":{"strategy_id":"zigzag_two_stage","release_id":"r1","feature_sha":"f1",
                        "audit_ref":"a1","generated_at":"2026-08-18","status":"AUDITED"},
            "stocks":ss,
            "daily_batches":[{"trade_date":day1,"release_id":"r1","status":"PUBLISHED","items":items}]}


def make(tmp_path, count=6):
    dataset=fixture(count)
    manifest=write_candidate_shards(dataset,tmp_path,validate=validate,target_bytes=1024)
    return dataset,manifest


def test_shard_roundtrip_keeps_every_stock_and_daily_event(tmp_path):
    original,manifest=make(tmp_path)
    loaded=load_candidate_shards(tmp_path/"candidate_snapshot.json",validate=validate)
    assert loaded == original
    assert len(manifest["shards"])>=2
    assert len(loaded["daily_batches"][0]["items"]) == 6
    assert all(x["bytes"]<=48*1024*1024 for x in manifest["shards"])


def test_altered_shard_is_rejected(tmp_path):
    _,manifest=make(tmp_path)
    shard=tmp_path/manifest["shards"][0]["file"]
    shard.write_bytes(shard.read_bytes()+b" ")
    with pytest.raises(ShardContractError,match="SHARD_BYTES_MISMATCH"):
        load_candidate_shards(tmp_path/"candidate_snapshot.json",validate=validate)


def test_missing_shard_is_rejected(tmp_path):
    _,manifest=make(tmp_path)
    (tmp_path/manifest["shards"][0]["file"]).unlink()
    with pytest.raises(ShardContractError,match="MISSING_OR_SYMLINKED_SHARD"):
        load_candidate_shards(tmp_path/"candidate_snapshot.json",validate=validate)


def test_symlink_shard_is_rejected(tmp_path):
    _,manifest=make(tmp_path)
    filename=manifest["shards"][0]["file"]
    first=tmp_path/filename
    real=tmp_path/"elsewhere.json"
    first.replace(real)
    first.symlink_to(real)
    with pytest.raises(ShardContractError,match="MISSING_OR_SYMLINKED_SHARD"):
        load_candidate_shards(tmp_path/"candidate_snapshot.json",validate=validate)


def test_path_traversal_fails_closed(tmp_path):
    _,manifest=make(tmp_path)
    pointer=tmp_path/"candidate_snapshot.json"
    m=json.loads(pointer.read_text())
    m["shards"][0]["file"]="../secret.json"
    pointer.write_text(json.dumps(m))
    with pytest.raises(ShardContractError,match="INVALID_SHARD_FILENAME"):
        load_candidate_shards(pointer,validate=validate)


def test_cross_shard_duplicate_candidate_is_rejected(tmp_path):
    _,manifest=make(tmp_path)
    entries=manifest["shards"]
    assert len(entries)>=2
    p=tmp_path/entries[1]["file"]
    second=json.loads(p.read_text())
    first=json.loads((tmp_path/entries[0]["file"]).read_text())
    first_cid=next(iter(first["stocks"].values()))["candidates"][0]["candidate_id"]
    stock=next(iter(second["stocks"].values()))
    original_cid=stock["candidates"][0]["candidate_id"]
    stock["candidates"][0]["candidate_id"]=first_cid
    # Keep this shard internally consistent with its own daily items, so the
    # per-shard SSOT checks pass and the GLOBAL cross-shard duplicate check is
    # the one that fires (that is the behaviour under test).
    if original_cid != first_cid:
        for batch in second["daily_batches"]:
            for item in batch["items"]:
                if item.get("candidate_id")==original_cid:
                    item["candidate_id"]=first_cid
    from app.services.strategy_candidate_shards import encode
    from hashlib import sha256
    payload=encode(second);p.write_bytes(payload)
    pointer=tmp_path/"candidate_snapshot.json"
    m=json.loads(pointer.read_text())
    m["shards"][1]["sha256"]=sha256(payload).hexdigest()
    m["shards"][1]["bytes"]=len(payload)
    m["total_bytes"]=sum(x["bytes"] for x in m["shards"])
    pointer.write_bytes(encode(m))
    with pytest.raises(Exception,match="duplicate/empty candidate_id"):
        load_candidate_shards(pointer,validate=validate)


def test_shard_metadata_is_immutable(tmp_path):
    _,manifest=make(tmp_path)
    p=tmp_path/manifest["shards"][0]["file"]
    contents=json.loads(p.read_text())
    contents["metadata"]["release_id"]="other"
    from app.services.strategy_candidate_shards import encode
    from hashlib import sha256
    # Dataset metadata shares content with manifest on disk only, not python refs.
    payload=encode(contents);p.write_bytes(payload)
    pointer=tmp_path/"candidate_snapshot.json"
    m=json.loads(pointer.read_text())
    m["shards"][0]["sha256"]=sha256(payload).hexdigest()
    m["shards"][0]["bytes"]=len(payload)
    m["total_bytes"]=sum(x["bytes"] for x in m["shards"])
    pointer.write_bytes(encode(m))
    with pytest.raises(ShardContractError,match="SHARD_RELEASE_OR_SCHEMA_MISMATCH"):
        load_candidate_shards(pointer,validate=validate)
