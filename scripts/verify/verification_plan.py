"""Closed, declarative verification plans.

Plans select audited profiles only. They cannot contain commands, paths,
environment overrides, resource names, or lifecycle switches.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ALLOWED_KEYS = {
    "schema_version", "name", "runtime_profile", "test_profile",
    "seed_profile", "e2e_profile", "timeout_profile",
}
PROFILE_REGISTRY = {
    "runtime": {"after_close"},
    "test": {"pg_contract"},
    "seed": {"v21_synthetic"},
    "e2e": {"closure_v21"},
    "timeout": {"standard", "extended"},
}
TIMEOUTS = {
    "standard": {"migration": 300, "runtime": 300, "tests": 900, "seed": 900, "e2e": 600},
    "extended": {"migration": 600, "runtime": 600, "tests": 1800, "seed": 1800, "e2e": 1200},
}


@dataclass(frozen=True)
class VerificationPlan:
    name: str
    runtime_profile: str
    test_profile: str
    seed_profile: str
    e2e_profile: str
    timeout_profile: str

    @property
    def timeouts(self) -> dict[str, int]:
        return TIMEOUTS[self.timeout_profile]


def load_plan(path: str | Path) -> VerificationPlan:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("verification plan must be an object")
    unknown = set(data) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unsupported plan keys: {sorted(unknown)}")
    if data.get("schema_version") != 1:
        raise ValueError("verification plan schema_version must be 1")
    for key in ("name", "runtime_profile", "test_profile", "seed_profile", "e2e_profile", "timeout_profile"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"verification plan requires string {key}")
    selections = {
        "runtime": data["runtime_profile"], "test": data["test_profile"],
        "seed": data["seed_profile"], "e2e": data["e2e_profile"],
        "timeout": data["timeout_profile"],
    }
    for kind, value in selections.items():
        if value not in PROFILE_REGISTRY[kind]:
            raise ValueError(f"unregistered {kind} profile: {value}")
    return VerificationPlan(
        name=data["name"], runtime_profile=data["runtime_profile"],
        test_profile=data["test_profile"], seed_profile=data["seed_profile"],
        e2e_profile=data["e2e_profile"], timeout_profile=data["timeout_profile"],
    )
