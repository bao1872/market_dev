"""Board/Concept 快照传输合同（[BOARD-LOCAL-OWNERSHIP-01]，schema_version=1）。

职责边界（严格遵守）：
- 本模块**只**负责序列化 / 反序列化 / 校验，**不做**任何 DB 写入，**不做**任何网络调用，
  也不读取任何 cookie / 凭证。

数据流：
    本地 Mac：wencai_board_provider.fetch_board_snapshot() → BoardSnapshot
              → build_envelope() → serialize_envelope() → stdin
    生产侧：  deserialize_envelope() + parse+validate → reconstruct_snapshot()
              → board_sync_service.sync_boards()

安全合同：
- envelope 中**绝不允许**出现 cookie / authorization 等敏感字段：序列化前递归拒绝，
  fail-closed（不静默剔除，避免掩盖问题）。
- 不得把 provider 原始 HTTP 响应塞进 envelope；只允许结构化、脱敏后的 diagnostics。

校验合同：
- `schema_version` 必须等于 1；`source` 必须等于 "wencai"。
- contract 版本必须与生产运行时常量完全一致：不一致 → FAIL CLOSED。
- `producer_git_sha` 仅作诊断：schema 与语义 contract 版本一致时，不要求 Git SHA 精确相同。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.services.wencai_board_provider import (
    BOARD_IDENTITY_CONTRACT_VERSION,
    BOARD_NORMALIZATION_CONTRACT_VERSION,
    BOARD_PROVIDER_CONTRACT_VERSION,
    BOARD_QUALITY_GATE_VERSION,
    BOARD_SOURCE,
    BOARD_TAXONOMY_COMPATIBILITY_KEY,
    BOARD_TAXONOMY_VERSION,
    BoardSnapshot,
)

# =============================================================================
# 常量
# =============================================================================

SCHEMA_VERSION = 1
SOURCE_WENCAI = "wencai"

#: 允许的最大 envelope 字节数（防恶意/误传超大 payload）。正常快照 ~数 MB。
MAX_ENVELOPE_BYTES = 64 * 1024 * 1024  # 64 MiB

#: 合法板块类型
_VALID_BOARD_TYPES = frozenset({"industry", "concept"})

#: 禁止出现在 envelope 中的敏感字段名（小写比较）。序列化前 fail-closed 拒绝。
_FORBIDDEN_FIELD_NAMES = frozenset({
    "cookie",
    "wencai_cookie",
    "authorization",
    "auth",
    "password",
    "token",
    "secret",
    "set-cookie",
    "raw_response",
    "http_response",
})


class BoardSnapshotTransferError(Exception):
    """传输/校验错误基类。"""


class SnapshotSchemaError(BoardSnapshotTransferError):
    """envelope schema 非法（版本/结构/字段）。"""


class SnapshotHashMismatchError(BoardSnapshotTransferError):
    """payload_sha256 与重算值不一致（payload 被篡改或截断）。"""


class SnapshotContractMismatchError(BoardSnapshotTransferError):
    """contract 版本与生产运行时不一致（FAIL CLOSED）。"""


# =============================================================================
# 运行时 contract 版本（生产侧校验基准；单一来源 = wencai_board_provider）
# =============================================================================


def runtime_contracts() -> dict[str, str]:
    """返回生产运行时冻结的 contract 版本集合（envelope.contracts 的权威基准）。"""
    return {
        "provider_contract_version": BOARD_PROVIDER_CONTRACT_VERSION,
        "normalization_contract_version": BOARD_NORMALIZATION_CONTRACT_VERSION,
        "identity_contract_version": BOARD_IDENTITY_CONTRACT_VERSION,
        "taxonomy_version": BOARD_TAXONOMY_VERSION,
        "taxonomy_compatibility_key": BOARD_TAXONOMY_COMPATIBILITY_KEY,
        "quality_gate_version": BOARD_QUALITY_GATE_VERSION,
    }


# =============================================================================
# canonical JSON / hash / 敏感字段防护
# =============================================================================


def canonical_json(obj: Any) -> str:
    """确定性 canonical JSON：sort_keys + 稳定 separators + UTF-8（不转义非 ASCII）。"""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _reject_sensitive_fields(obj: Any, *, path: str = "<root>") -> None:
    """递归拒绝敏感字段名。发现即 fail-closed 抛错（不静默剔除）。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).strip().lower()
            if key_l in _FORBIDDEN_FIELD_NAMES:
                raise SnapshotSchemaError(
                    f"envelope 含被禁止的敏感字段名: {path}.{key!r}"
                )
            _reject_sensitive_fields(value, path=f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _reject_sensitive_fields(item, path=f"{path}[{idx}]")


def _envelope_hash_material(envelope: dict[str, Any]) -> dict[str, Any]:
    """hash 覆盖 envelope 全部字段，**排除** payload_sha256 自身。"""
    return {k: v for k, v in envelope.items() if k != "payload_sha256"}


def compute_envelope_sha256(envelope: dict[str, Any]) -> str:
    """计算 envelope 的 canonical SHA256（排除 payload_sha256 自身）。"""
    return hashlib.sha256(
        canonical_json(_envelope_hash_material(envelope)).encode("utf-8")
    ).hexdigest()


# =============================================================================
# 构建 envelope
# =============================================================================


def build_envelope(
    snapshot: BoardSnapshot,
    *,
    producer_git_sha: str | None = None,
    generated_at: datetime | None = None,
    contracts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把 BoardSnapshot 构造为版本化传输 envelope（含 payload_sha256）。

    Args:
        snapshot: wencai_board_provider 构建的完整 BoardSnapshot。
        producer_git_sha: 生产端 Git SHA（仅诊断，可选）。
        generated_at: 生成时间（默认 UTC now）。
        contracts: 显式 contract 版本；默认取运行时冻结值（单一来源）。

    Returns:
        JSON-safe envelope dict。

    Raises:
        SnapshotSchemaError: 含敏感字段名 / 结构非法。
    """
    gen = generated_at or datetime.now(UTC)
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=UTC)

    snapshot_payload = {
        "raw_rows": int(snapshot.raw_rows),
        # boards: 保持 [{external_code,name,type}]；显式复制为 JSON-safe
        "boards": [
            {
                "external_code": str(b.get("external_code", "")),
                "name": str(b.get("name", "")),
                "type": str(b.get("type", "")),
            }
            for b in snapshot.boards
        ],
        # memberships: tuple key (external_code, type) → 显式 JSON-safe 列表
        "memberships": [
            {
                "external_code": str(external_code),
                "type": str(board_type),
                "symbols": [str(s) for s in symbols],
            }
            for (external_code, board_type), symbols in snapshot.memberships.items()
        ],
        "unresolved_symbols": [str(s) for s in snapshot.unresolved_symbols],
        "diagnostics": dict(snapshot.diagnostics or {}),
    }

    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_WENCAI,
        "generated_at": gen.astimezone(UTC).isoformat(),
        "producer_git_sha": producer_git_sha,
        "contracts": dict(contracts or runtime_contracts()),
        "snapshot": snapshot_payload,
    }

    # 敏感字段防护（在计算 hash 前，确保 cookie 永不进入 envelope）
    _reject_sensitive_fields(envelope)

    envelope["payload_sha256"] = compute_envelope_sha256(envelope)
    return envelope


def serialize_envelope(envelope: dict[str, Any]) -> bytes:
    """序列化 envelope 为 UTF-8 bytes（canonical JSON）。"""
    _reject_sensitive_fields(envelope)
    return canonical_json(envelope).encode("utf-8")


# =============================================================================
# 解析 + 校验 envelope
# =============================================================================


def deserialize_envelope(raw: bytes | str) -> dict[str, Any]:
    """解析 envelope 原文为 dict，并做大小与结构校验（不校验 hash/contract）。

    Raises:
        SnapshotSchemaError: 超大 / 非法 JSON / 非对象。
    """
    if isinstance(raw, str):
        data = raw.encode("utf-8")
    elif isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        raise SnapshotSchemaError(f"envelope 类型非法: {type(raw).__name__}")

    if len(data) > MAX_ENVELOPE_BYTES:
        raise SnapshotSchemaError(
            f"envelope 过大: {len(data)} bytes > {MAX_ENVELOPE_BYTES}"
        )
    if not data.strip():
        raise SnapshotSchemaError("envelope 为空")

    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotSchemaError(f"envelope JSON 解析失败: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SnapshotSchemaError(
            f"envelope 顶层必须是对象，实际={type(parsed).__name__}"
        )
    return parsed


def validate_envelope(
    envelope: dict[str, Any],
    *,
    expected_contracts: dict[str, str] | None = None,
) -> None:
    """校验 envelope：schema/ source / hash / contract 版本。

    Raises:
        SnapshotSchemaError: schema_version / source / 结构非法。
        SnapshotHashMismatchError: payload_sha256 不匹配。
        SnapshotContractMismatchError: contract 版本与运行时不一致。
    """
    schema_version = envelope.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise SnapshotSchemaError(
            f"schema_version 不支持: {schema_version!r} != {SCHEMA_VERSION}"
        )
    if envelope.get("source") != SOURCE_WENCAI:
        raise SnapshotSchemaError(
            f"source 必须是 {SOURCE_WENCAI!r}，实际={envelope.get('source')!r}"
        )

    payload_sha = envelope.get("payload_sha256")
    if not isinstance(payload_sha, str) or not payload_sha:
        raise SnapshotSchemaError("payload_sha256 缺失或非字符串")
    recomputed = compute_envelope_sha256(envelope)
    if recomputed != payload_sha:
        raise SnapshotHashMismatchError(
            f"payload_sha256 不匹配: 声明={payload_sha} 实算={recomputed}"
        )

    contracts = envelope.get("contracts")
    if not isinstance(contracts, dict):
        raise SnapshotSchemaError("contracts 缺失或非对象")
    expected = expected_contracts or runtime_contracts()
    for key, expected_value in expected.items():
        actual = contracts.get(key)
        if actual != expected_value:
            raise SnapshotContractMismatchError(
                f"contract 版本不匹配 {key}: 声明={actual!r} 期望={expected_value!r}"
            )

    snapshot_payload = envelope.get("snapshot")
    if not isinstance(snapshot_payload, dict):
        raise SnapshotSchemaError("snapshot 缺失或非对象")
    if not isinstance(snapshot_payload.get("boards"), list):
        raise SnapshotSchemaError("snapshot.boards 必须是列表")
    if not isinstance(snapshot_payload.get("memberships"), list):
        raise SnapshotSchemaError("snapshot.memberships 必须是列表")


def reconstruct_snapshot(envelope: dict[str, Any]) -> BoardSnapshot:
    """从已校验 envelope 重建 BoardSnapshot（memberships tuple key 还原）。

    调用方必须先 validate_envelope()。此处只做结构重建。
    """
    payload = envelope["snapshot"]
    boards: list[dict[str, str]] = []
    for idx, board in enumerate(payload["boards"]):
        if not isinstance(board, dict):
            raise SnapshotSchemaError(f"boards[{idx}] 必须是对象")
        external_code = str(board.get("external_code", ""))
        name = str(board.get("name", ""))
        board_type = str(board.get("type", ""))
        if board_type not in _VALID_BOARD_TYPES:
            raise SnapshotSchemaError(
                f"boards[{idx}].type 非法: {board_type!r} ∉ {sorted(_VALID_BOARD_TYPES)}"
            )
        if not external_code:
            raise SnapshotSchemaError(f"boards[{idx}].external_code 为空")
        boards.append({
            "external_code": external_code,
            "name": name,
            "type": board_type,
        })

    memberships: dict[tuple[str, str], list[str]] = {}
    for idx, mem in enumerate(payload["memberships"]):
        if not isinstance(mem, dict):
            raise SnapshotSchemaError(f"memberships[{idx}] 必须是对象")
        external_code = str(mem.get("external_code", ""))
        board_type = str(mem.get("type", ""))
        if board_type not in _VALID_BOARD_TYPES:
            raise SnapshotSchemaError(
                f"memberships[{idx}].type 非法: {board_type!r}"
            )
        if not external_code:
            raise SnapshotSchemaError(f"memberships[{idx}].external_code 为空")
        raw_symbols = mem.get("symbols", [])
        if not isinstance(raw_symbols, list):
            raise SnapshotSchemaError(f"memberships[{idx}].symbols 必须是列表")
        memberships[(external_code, board_type)] = [str(s) for s in raw_symbols]

    unresolved = payload.get("unresolved_symbols", [])
    if not isinstance(unresolved, list):
        raise SnapshotSchemaError("snapshot.unresolved_symbols 必须是列表")

    diagnostics = payload.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise SnapshotSchemaError("snapshot.diagnostics 必须是对象")

    return BoardSnapshot(
        boards=boards,
        memberships=memberships,
        raw_rows=int(payload.get("raw_rows", 0)),
        unresolved_symbols=[str(s) for s in unresolved],
        diagnostics=dict(diagnostics),
    )


__all__ = [
    "MAX_ENVELOPE_BYTES",
    "SCHEMA_VERSION",
    "SOURCE_WENCAI",
    "BoardSnapshotTransferError",
    "SnapshotContractMismatchError",
    "SnapshotHashMismatchError",
    "SnapshotSchemaError",
    "build_envelope",
    "canonical_json",
    "compute_envelope_sha256",
    "deserialize_envelope",
    "reconstruct_snapshot",
    "runtime_contracts",
    "serialize_envelope",
    "validate_envelope",
]
