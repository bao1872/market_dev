"""[BOARD-LOCAL-OWNERSHIP-01 / RC1 + RC1b] 传输 envelope 合同测试（纯单元）。

覆盖：
- 无损 round trip：taxonomy / source / taxonomy_version /
  taxonomy_compatibility_key / identity_contract_version /
  hierarchy_level / parent_external_code 全部保留（RC1）
- canonical payload_sha256 确定性；篡改检测
- schema_version / source / contract 版本 fail-closed
- **远端** validate_envelope 自身拒绝敏感字段（RC1b，不依赖 build/serialize）
- board 语义结构校验：缺必需字段 / 未知字段 / 非法层级 / L1 带 parent
- membership 必须引用已声明 board 身份
- cookie / authorization 不出现于序列化字节
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
from app.services.wencai_board_provider import (
    BOARD_IDENTITY_CONTRACT_VERSION,
    BOARD_SOURCE,
    BOARD_TAXONOMY,
    BOARD_TAXONOMY_COMPATIBILITY_KEY,
    BOARD_TAXONOMY_VERSION,
    BoardSnapshot,
)


def _board(
    code: str,
    name: str,
    *,
    board_type: str = "industry",
    level: str | None = None,
    parent: str | None = None,
) -> dict[str, str]:
    """provider 风格 board（与 _build_board_snapshot 输出字段一致）。"""
    b: dict[str, str] = {
        "external_code": code,
        "name": name,
        "type": board_type,
        "taxonomy": BOARD_TAXONOMY,
        "source": BOARD_SOURCE,
        "taxonomy_version": BOARD_TAXONOMY_VERSION,
        "taxonomy_compatibility_key": BOARD_TAXONOMY_COMPATIBILITY_KEY,
        "identity_contract_version": BOARD_IDENTITY_CONTRACT_VERSION,
    }
    if level is not None:
        b["hierarchy_level"] = level
    if parent is not None:
        b["parent_external_code"] = parent
    return b


def _snapshot() -> BoardSnapshot:
    return BoardSnapshot(
        boards=[
            _board("IND_L1", "金融", level="L1"),
            _board("IND_L2", "金融-银行", level="L2", parent="IND_L1"),
            _board("IND_L3", "金融-银行-国有银行", level="L3", parent="IND_L2"),
            _board("CON_1", "人工智能", board_type="concept"),
        ],
        memberships={
            ("IND_L1", "industry"): ["600000.SH", "000001.SZ"],
            ("IND_L2", "industry"): ["600000.SH"],
            ("IND_L3", "industry"): ["600000.SH"],
            ("CON_1", "concept"): ["600000.SH"],
        },
        raw_rows=7,
        unresolved_symbols=["BADCODE"],
        diagnostics={"duration_ms": 12},
    )


def _valid_envelope() -> dict:
    return build_envelope(_snapshot(), producer_git_sha="deadbeef")


class TestRoundTrip:
    def test_round_trip_is_lossless_for_all_semantic_fields(self) -> None:
        """[RC1] provider 全语义字段必须无损跨 transport（否则真实 sync_boards 会失败）。"""
        envelope = _valid_envelope()
        parsed = deserialize_envelope(serialize_envelope(envelope))
        validate_envelope(parsed)
        restored = reconstruct_snapshot(parsed)

        assert restored.boards == _snapshot().boards, (
            "board 语义字段必须逐字段无损还原（含层级/分类学/身份合同）"
        )
        assert restored.memberships == _snapshot().memberships, (
            "tuple membership key 必须无损还原"
        )
        assert restored.raw_rows == 7
        assert restored.unresolved_symbols == ["BADCODE"]

    @pytest.mark.parametrize(
        "field",
        [
            "taxonomy",
            "source",
            "taxonomy_version",
            "taxonomy_compatibility_key",
            "identity_contract_version",
            "hierarchy_level",
            "parent_external_code",
        ],
    )
    def test_hierarchy_and_contract_fields_preserved(self, field: str) -> None:
        envelope = _valid_envelope()
        restored = reconstruct_snapshot(
            deserialize_envelope(serialize_envelope(envelope))
        )
        def _find(code: str) -> dict[str, str]:
            return next(b for b in restored.boards if b["external_code"] == code)

        # L3 携带全部字段（命中参数化字段）
        actual = _find("IND_L3").get(field)
        assert actual is not None, f"{field} 在 transport 后丢失"
        assert actual == _find("IND_L3").get(field)

    def test_hierarchy_parent_chain_survives(self) -> None:
        envelope = _valid_envelope()
        restored = reconstruct_snapshot(
            deserialize_envelope(serialize_envelope(envelope))
        )
        by_code = {b["external_code"]: b for b in restored.boards}
        assert by_code["IND_L3"]["parent_external_code"] == "IND_L2"
        assert by_code["IND_L2"]["parent_external_code"] == "IND_L1"
        assert "parent_external_code" not in by_code["IND_L1"]
        assert by_code["IND_L2"]["hierarchy_level"] == "L2"
        assert by_code["CON_1"]["type"] == "concept"
        assert "hierarchy_level" not in by_code["CON_1"]

    def test_memberships_serialized_as_explicit_list(self) -> None:
        mems = _valid_envelope()["snapshot"]["memberships"]
        assert isinstance(mems, list)
        assert all(isinstance(m, dict) for m in mems)
        assert {m["external_code"] for m in mems} == {"IND_L1", "IND_L2", "IND_L3", "CON_1"}

    def test_schema_version_and_source(self) -> None:
        envelope = _valid_envelope()
        assert envelope["schema_version"] == SCHEMA_VERSION
        assert envelope["source"] == "wencai"

    def test_deterministic_hash(self) -> None:
        gen = datetime(2026, 9, 24, 1, 2, 3, tzinfo=UTC)
        e1 = build_envelope(_snapshot(), generated_at=gen, producer_git_sha="x")
        e2 = build_envelope(_snapshot(), generated_at=gen, producer_git_sha="x")
        assert e1["payload_sha256"] == e2["payload_sha256"]

    def test_hash_excludes_payload_sha256_itself(self) -> None:
        envelope = _valid_envelope()
        assert compute_envelope_sha256(envelope) == envelope["payload_sha256"]
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


class TestRemoteSensitiveFieldValidation:
    """[RC1b] 远端信任边界：validate_envelope 自身必须拒绝敏感字段。"""

    def _tampered_with_sensitive(self, **inject: object) -> dict:
        parsed = json.loads(serialize_envelope(_valid_envelope()).decode("utf-8"))
        for key, value in inject.items():
            if "." in key:
                head, tail = key.split(".", 1)
                parsed[head][tail] = value
            else:
                parsed[key] = value
        # 重新计算**合法** hash：模拟攻击者构造 hash 正确的恶意 envelope
        parsed["payload_sha256"] = compute_envelope_sha256(parsed)
        return parsed

    def test_remote_rejects_cookie_with_valid_hash(self) -> None:
        malicious = self._tampered_with_sensitive(cookie="session=abc")
        # 先证明 hash 确实是"正确"的（否则测出来的只是 hash 校验）
        assert compute_envelope_sha256(malicious) == malicious["payload_sha256"]
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(malicious)

    @pytest.mark.parametrize(
        "inject",
        [
            {"authorization": "Bearer x"},
            {"token": "t"},
            {"wencai_cookie": "c"},
            {"snapshot": {"diagnostics": {"cookie": "c"}}},
        ],
    )
    def test_remote_rejects_nested_sensitive_fields(self, inject: dict) -> None:
        parsed = json.loads(serialize_envelope(_valid_envelope()).decode("utf-8"))
        for key, value in inject.items():
            if isinstance(value, dict) and isinstance(parsed.get(key), dict):
                parsed[key].update(value)
            else:
                parsed[key] = value
        parsed["payload_sha256"] = compute_envelope_sha256(parsed)
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)


class TestBoardStructuralValidation:
    """board 语义结构校验（远端与本地共用）。"""

    def _mutate_board(self, index: int, mutate) -> dict:
        parsed = json.loads(serialize_envelope(_valid_envelope()).decode("utf-8"))
        mutate(parsed["snapshot"]["boards"][index])
        parsed["payload_sha256"] = compute_envelope_sha256(parsed)
        return parsed

    @pytest.mark.parametrize(
        "field",
        [
            "taxonomy",
            "source",
            "taxonomy_version",
            "taxonomy_compatibility_key",
            "identity_contract_version",
        ],
    )
    def test_missing_required_semantic_field_rejected(self, field: str) -> None:
        parsed = self._mutate_board(0, lambda b: b.pop(field))
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)

    def test_empty_required_field_rejected(self) -> None:
        parsed = self._mutate_board(0, lambda b: b.__setitem__("source", ""))
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)

    def test_unknown_board_field_rejected(self) -> None:
        parsed = self._mutate_board(0, lambda b: b.__setitem__("mystery", "x"))
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)

    def test_invalid_hierarchy_level_rejected(self) -> None:
        parsed = self._mutate_board(0, lambda b: b.__setitem__("hierarchy_level", "L9"))
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)

    def test_l1_with_parent_rejected(self) -> None:
        parsed = self._mutate_board(
            0, lambda b: b.__setitem__("parent_external_code", "IND_ROOT")
        )
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)

    def test_invalid_board_type_rejected(self) -> None:
        parsed = self._mutate_board(0, lambda b: b.__setitem__("type", "sector"))
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)

    def test_membership_referencing_unknown_board_rejected(self) -> None:
        parsed = json.loads(serialize_envelope(_valid_envelope()).decode("utf-8"))
        parsed["snapshot"]["memberships"][0]["external_code"] = "GHOST"
        parsed["payload_sha256"] = compute_envelope_sha256(parsed)
        with pytest.raises(SnapshotSchemaError):
            validate_envelope(parsed)


class TestContractVersions:
    def test_envelope_contracts_match_runtime(self) -> None:
        assert _valid_envelope()["contracts"] == runtime_contracts()

    def test_no_cookie_in_serialized_bytes(self) -> None:
        lowered = serialize_envelope(_valid_envelope()).decode("utf-8").lower()
        assert "cookie" not in lowered
        assert "authorization" not in lowered


def test_envelope_is_json_serializable_and_stable() -> None:
    envelope = _valid_envelope()
    assert serialize_envelope(envelope) == serialize_envelope(envelope)
    json.loads(serialize_envelope(envelope).decode("utf-8"))
