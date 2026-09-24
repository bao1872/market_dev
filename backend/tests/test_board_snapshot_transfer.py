"""[BOARD-LOCAL-OWNERSHIP-01] 传输 envelope 合同测试（任务 §18 D，纯单元）。

覆盖：
- BoardSnapshot → envelope → BoardSnapshot round trip（tuple membership key 还原）
- canonical payload_sha256 确定性
- payload 被篡改 → 拒绝
- 错误 schema_version / source → 拒绝
- contract 版本不一致 → FAIL CLOSED
- cookie / authorization 字段被显式拒绝
- 超大 envelope / 非 JSON → 拒绝
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.services.board_snapshot_transfer import (
    SCHEMA_VERSION,
    SnapshotContractMismatchError,
    SnapshotHashMismatchError,
    SnapshotSchemaError,
    build_envelope,
    compute_envelope_sha256,
    deserialize_envelope,
    reconstruct_snapshot,
    runtime_contracts,
    serialize_envelope,
    validate_envelope,
)
from app.services.wencai_board_provider import BoardSnapshot


def _snapshot() -> BoardSnapshot:
    return BoardSnapshot(
        boards=[
            {"external_code": "BK1", "name": "半导体", "type": "industry"},
            {"external_code": "BK2", "name": "人工智能", "type": "concept"},
        ],
        memberships={
            ("BK1", "industry"): ["600000.SH", "000001.SZ"],
            ("BK2", "concept"): ["600000.SH"],
        },
        raw_rows=7,
        unresolved_symbols=["BADCODE"],
        diagnostics={"duration_ms": 12},
    )


def _valid_envelope() -> dict:
    return build_envelope(_snapshot(), producer_git_sha="deadbeef")


class TestRoundTrip:
    def test_round_trip_restores_snapshot(self) -> None:
        envelope = _valid_envelope()
        raw = serialize_envelope(envelope)
        parsed = deserialize_envelope(raw)
        validate_envelope(parsed)
        restored = reconstruct_snapshot(parsed)

        assert restored.boards == _snapshot().boards
        assert restored.memberships == _snapshot().memberships, (
            "tuple membership key 必须无损还原"
        )
        assert restored.raw_rows == 7
        assert restored.unresolved_symbols == ["BADCODE"]

    def test_memberships_serialized_as_explicit_list(self) -> None:
        envelope = _valid_envelope()
        mems = envelope["snapshot"]["memberships"]
        assert isinstance(mems, list)
        assert all(isinstance(m, dict) for m in mems)
        assert {m["external_code"] for m in mems} == {"BK1", "BK2"}

    def test_schema_version_and_source(self) -> None:
        envelope = _valid_envelope()
        assert envelope["schema_version"] == SCHEMA_VERSION
        assert envelope["source"] == "wencai"

    def test_deterministic_hash(self) -> None:
        """相同 snapshot + 相同 generated_at → 相同 payload_sha256。"""
        gen = datetime(2026, 9, 24, 1, 2, 3, tzinfo=UTC)
        e1 = build_envelope(_snapshot(), generated_at=gen, producer_git_sha="x")
        e2 = build_envelope(_snapshot(), generated_at=gen, producer_git_sha="x")
        assert e1["payload_sha256"] == e2["payload_sha256"]

    def test_hash_excludes_payload_sha256_itself(self) -> None:
        envelope = _valid_envelope()
        assert compute_envelope_sha256(envelope) == envelope["payload_sha256"]
        # 改 payload_sha256 本身不改变重算值
        tampered = dict(envelope)
        tampered["payload_sha256"] = "0" * 64
        assert compute_envelope_sha256(tampered) == envelope["payload_sha256"]


class TestRejections:
    def test_tampered_payload_rejected(self) -> None:
        parsed = deserialize_envelope(serialize_envelope(_valid_envelope()))
        parsed["snapshot"]["raw_rows"] = 999999
        with pytest.raises(SnapshotHashMismatchError):
            validate_envelope(parsed)

    def test_wrong_schema_version_rejected(self) -> None:
        parsed = deserialize_envelope(serialize_envelope(_valid_envelope()))
        parsed["schema_version"] = 2
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)

    def test_wrong_source_rejected(self) -> None:
        parsed = deserialize_envelope(serialize_envelope(_valid_envelope()))
        parsed["source"] = "qstock"
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)

    def test_contract_version_mismatch_fails_closed(self) -> None:
        parsed = deserialize_envelope(serialize_envelope(_valid_envelope()))
        parsed["contracts"]["taxonomy_version"] = "other-taxonomy"
        parsed["payload_sha256"] = compute_envelope_sha256(parsed)
        with pytest.raises(SnapshotContractMismatchError):
            validate_envelope(parsed)

    def test_producer_git_sha_is_diagnostic_only(self) -> None:
        """producer_git_sha 不同不构成失败（schema/contract 一致即可）。"""
        envelope = build_envelope(_snapshot(), producer_git_sha="aaa")
        envelope["producer_git_sha"] = "bbb"
        envelope["payload_sha256"] = compute_envelope_sha256(envelope)
        validate_envelope(envelope)  # 不抛

    def test_cookie_field_rejected_on_serialize(self) -> None:
        envelope = _valid_envelope()
        envelope["cookie"] = "secret-value"
        with pytest.raises(SnapshotSchemaError):
            serialize_envelope(envelope)

    def test_authorization_field_rejected_on_build(self) -> None:
        snapshot = _snapshot()
        snapshot.diagnostics = {"authorization": "Bearer x"}
        with pytest.raises(SnapshotSchemaError):
            build_envelope(snapshot)

    def test_oversized_envelope_rejected(self, monkeypatch) -> None:
        import app.services.board_snapshot_transfer as mod

        monkeypatch.setattr(mod, "MAX_ENVELOPE_BYTES", 16)
        with pytest.raises(SnapshotSchemaError):
            deserialize_envelope(serialize_envelope(_valid_envelope()))

    def test_non_json_rejected(self) -> None:
        with pytest.raises(SnapshotSchemaError):
            deserialize_envelope(b"not-json{")

    def test_empty_rejected(self) -> None:
        with pytest.raises(SnapshotSchemaError):
            deserialize_envelope(b"")


class TestContractVersions:
    def test_envelope_contracts_match_runtime(self) -> None:
        envelope = _valid_envelope()
        assert envelope["contracts"] == runtime_contracts()

    def test_no_cookie_in_serialized_bytes(self) -> None:
        raw = serialize_envelope(_valid_envelope()).decode("utf-8")
        lowered = raw.lower()
        assert "cookie" not in lowered
        assert "authorization" not in lowered


def test_envelope_is_json_serializable_and_stable() -> None:
    envelope = _valid_envelope()
    first = serialize_envelope(envelope)
    second = serialize_envelope(envelope)
    assert first == second
    json.loads(first.decode("utf-8"))
