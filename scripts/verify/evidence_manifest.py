"""Validated contract-to-test-to-gate registry for formal verification."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_LEVELS = {1, 2, 3}
ALLOWED_GATES = {"targeted-pg", "full-closure", "migration-roundtrip"}
ALLOWED_STATUSES = {
    "passed",
    "failed",
    "skipped",
    "deselected",
    "not_registered",
    "not_run",
    "blocked",
}
TOP_LEVEL_KEYS = {"schema_version", "contracts"}
CONTRACT_KEYS = {
    "contract_id",
    "governance_level",
    "semantic_owner",
    "gate",
    "required",
    "test_selectors",
    "claim",
}
FORBIDDEN_SELECTOR_TOKENS = ("*", "?", "[", "]", "{", "}")


@dataclass(frozen=True)
class EvidenceContract:
    contract_id: str
    governance_level: int
    semantic_owner: str
    gates: tuple[str, ...]
    required: bool
    test_selectors: tuple[str, ...]
    claim: str


@dataclass(frozen=True)
class EvidenceManifest:
    schema_version: int
    contracts: tuple[EvidenceContract, ...]

    def contracts_for_gate(self, gate: str) -> tuple[EvidenceContract, ...]:
        if gate not in ALLOWED_GATES:
            raise ValueError(f"unregistered evidence gate: {gate}")
        return tuple(contract for contract in self.contracts if gate in contract.gates)

    def selectors_for_gate(self, gate: str) -> tuple[str, ...]:
        selectors: list[str] = []
        seen: set[str] = set()
        for contract in self.contracts_for_gate(gate):
            for selector in contract.test_selectors:
                if selector not in seen:
                    selectors.append(selector)
                    seen.add(selector)
        if not selectors:
            raise ValueError(f"gate has no registered evidence: {gate}")
        return tuple(selectors)


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"evidence contract {field} must be non-empty text")
    return value.strip()


def _validate_selector(selector: Any, *, repo_root: Path | None) -> str:
    value = _require_text(selector, "test_selector")
    if any(token in value for token in FORBIDDEN_SELECTOR_TOKENS):
        raise ValueError(f"evidence selector must be explicit, not a glob: {value}")
    file_part = value.split("::", 1)[0]
    if not file_part.startswith("tests/") or not file_part.endswith(".py"):
        raise ValueError(f"evidence selector must target backend tests: {value}")
    if ".." in Path(file_part).parts:
        raise ValueError(f"evidence selector cannot escape tests: {value}")
    if repo_root is not None and not (repo_root / "backend" / file_part).is_file():
        raise ValueError(f"evidence selector is not registered in the repository: {value}")
    return value


def load_evidence_manifest(path: str | Path, *, repo_root: str | Path | None = None) -> EvidenceManifest:
    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != TOP_LEVEL_KEYS:
        raise ValueError("evidence manifest has unsupported top-level keys")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported evidence manifest schema_version")
    raw_contracts = payload.get("contracts")
    if not isinstance(raw_contracts, list) or not raw_contracts:
        raise ValueError("evidence manifest must contain contracts")

    root = Path(repo_root) if repo_root is not None else None
    contracts: list[EvidenceContract] = []
    ids: set[str] = set()
    for raw in raw_contracts:
        if not isinstance(raw, dict) or set(raw) != CONTRACT_KEYS:
            raise ValueError("evidence contract has unsupported or missing keys")
        contract_id = _require_text(raw["contract_id"], "contract_id")
        if contract_id in ids:
            raise ValueError(f"duplicate evidence contract_id: {contract_id}")
        ids.add(contract_id)
        level = raw["governance_level"]
        if level not in ALLOWED_LEVELS:
            raise ValueError(f"invalid governance_level for {contract_id}: {level}")
        owner = _require_text(raw["semantic_owner"], "semantic_owner")
        claim = _require_text(raw["claim"], "claim")
        if not isinstance(raw["required"], bool):
            raise TypeError(f"required must be boolean for {contract_id}")
        gates = raw["gate"]
        if not isinstance(gates, list) or not gates or any(g not in ALLOWED_GATES for g in gates):
            raise ValueError(f"invalid gate registration for {contract_id}")
        if len(gates) != len(set(gates)):
            raise ValueError(f"duplicate gate registration for {contract_id}")
        selectors = raw["test_selectors"]
        if not isinstance(selectors, list) or not selectors:
            raise ValueError(f"test_selectors must be non-empty for {contract_id}")
        validated = tuple(_validate_selector(item, repo_root=root) for item in selectors)
        if len(validated) != len(set(validated)):
            raise ValueError(f"duplicate test selector for {contract_id}")
        contracts.append(EvidenceContract(
            contract_id=contract_id,
            governance_level=level,
            semantic_owner=owner,
            gates=tuple(gates),
            required=raw["required"],
            test_selectors=validated,
            claim=claim,
        ))
    return EvidenceManifest(schema_version=1, contracts=tuple(contracts))


def evaluate_contract_coverage(
    contracts: tuple[EvidenceContract, ...], report: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        return [
            _coverage_record(contract, "blocked", [], "missing or invalid pytest evidence report")
            for contract in contracts
        ]

    collected = report.get("collected")
    deselected = report.get("deselected")
    tests = report.get("tests")
    if not isinstance(collected, list) or not isinstance(deselected, list) or not isinstance(tests, dict):
        return [
            _coverage_record(contract, "blocked", [], "malformed pytest evidence report")
            for contract in contracts
        ]

    results: list[dict[str, Any]] = []
    for contract in contracts:
        matching = _matching_nodeids(contract.test_selectors, collected)
        matching_deselected = _matching_nodeids(contract.test_selectors, deselected)
        if matching_deselected:
            results.append(_coverage_record(
                contract, "deselected", matching_deselected, "required evidence was deselected"
            ))
            continue
        if not matching:
            results.append(_coverage_record(
                contract, "not_registered", [], "selector collected no executable tests"
            ))
            continue
        statuses = [tests.get(nodeid, {}).get("status", "not_run") for nodeid in matching]
        invalid = [status for status in statuses if status not in ALLOWED_STATUSES]
        if invalid:
            results.append(_coverage_record(contract, "blocked", matching, "invalid test status"))
        elif "failed" in statuses:
            results.append(_coverage_record(contract, "failed", matching, "one or more tests failed"))
        elif "skipped" in statuses:
            results.append(_coverage_record(contract, "skipped", matching, "one or more tests skipped"))
        elif "deselected" in statuses:
            results.append(_coverage_record(contract, "deselected", matching, "required evidence deselected"))
        elif any(status != "passed" for status in statuses):
            results.append(_coverage_record(contract, "not_run", matching, "one or more tests did not run"))
        else:
            results.append(_coverage_record(contract, "passed", matching, "all collected tests passed"))
    return results


def _matching_nodeids(selectors: tuple[str, ...], nodeids: list[Any]) -> list[str]:
    matches: list[str] = []
    for nodeid in nodeids:
        if not isinstance(nodeid, str):
            continue
        for selector in selectors:
            if "::" in selector:
                matched = nodeid == selector or nodeid.startswith(selector + "[")
            else:
                matched = nodeid == selector or nodeid.startswith(selector + "::")
            if matched:
                matches.append(nodeid)
                break
    return sorted(set(matches))


def _coverage_record(
    contract: EvidenceContract, status: str, nodeids: list[str], reason: str
) -> dict[str, Any]:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"invalid evidence status: {status}")
    return {
        "contract_id": contract.contract_id,
        "governance_level": contract.governance_level,
        "semantic_owner": contract.semantic_owner,
        "required": contract.required,
        "claim": contract.claim,
        "status": status,
        "nodeids": nodeids,
        "reason": reason,
    }
