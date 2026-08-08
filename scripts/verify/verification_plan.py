"""Closed, declarative verification plans (schema v2).

Plans select audited profiles only. They cannot contain commands, paths,
environment overrides, resource names, or lifecycle switches.

Profiles (all closed + registered):
- migration:  upgrade_head | round_trip
- test:       pg_contract | none
- seed:       v21_synthetic | none
- e2e:        closure_v21 | none
- timeout:    standard | extended
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ALLOWED_KEYS = {
    "schema_version", "name", "migration_profile", "test_profile",
    "seed_profile", "e2e_profile", "timeout_profile",
}
PROFILE_REGISTRY = {
    "migration": {"upgrade_head", "round_trip"},
    "test": {"pg_contract", "none"},
    "seed": {"v21_synthetic", "none"},
    "e2e": {"closure_v21", "none"},
    "timeout": {"standard", "extended"},
}
TIMEOUTS = {
    "standard": {"migration": 300, "tests": 900, "seed": 900, "e2e": 600},
    "extended": {"migration": 600, "tests": 1800, "seed": 1800, "e2e": 1200},
}


@dataclass(frozen=True)
class VerificationPlan:
    name: str
    migration_profile: str
    test_profile: str
    seed_profile: str
    e2e_profile: str
    timeout_profile: str

    @property
    def timeouts(self) -> dict[str, int]:
        return TIMEOUTS[self.timeout_profile]

    @property
    def requires_pg(self) -> bool:
        return self.test_profile != "none"

    @property
    def requires_seed(self) -> bool:
        return self.seed_profile != "none"

    @property
    def requires_e2e(self) -> bool:
        return self.e2e_profile != "none"

    @property
    def needs_migration_round_trip(self) -> bool:
        return self.migration_profile == "round_trip"


def load_plan(path: str | Path) -> VerificationPlan:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("verification plan must be an object")
    unknown = set(data) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unsupported plan keys: {sorted(unknown)}")
    if data.get("schema_version") != 2:
        raise ValueError("verification plan schema_version must be 2")
    for key in ("name", "migration_profile", "test_profile", "seed_profile", "e2e_profile", "timeout_profile"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"verification plan requires string {key}")
    selections = {
        "migration": data["migration_profile"], "test": data["test_profile"],
        "seed": data["seed_profile"], "e2e": data["e2e_profile"],
        "timeout": data["timeout_profile"],
    }
    for kind, value in selections.items():
        if value not in PROFILE_REGISTRY[kind]:
            raise ValueError(f"unregistered {kind} profile: {value}")
    return VerificationPlan(
        name=data["name"], migration_profile=data["migration_profile"],
        test_profile=data["test_profile"], seed_profile=data["seed_profile"],
        e2e_profile=data["e2e_profile"], timeout_profile=data["timeout_profile"],
    )
