"""4A-1R2 / E 步骤：用 production resolve_core_run_context() + 本地 frozen resolver
生成 expected_core_run_context（脱敏可提交副本）。

修复 4A-1R 的 blocker：
- Blocker 1: universe 必须用 eligible_universe.json 的权威字段 `sorted_ids`，
  不支持隐式 `eligible_instrument_ids` alias；且做 hard gate（count/unique/hash）。
- Blocker 2: 保存完整 production CoreRunContext（整个 config，不手工挑字段拆 projection）。
- portability: 删除硬编码 /Users/zhenbao 路径，全部从 __file__ 推导 repo root。

不下载任何 bars，不连生产 DB（resolver 从本地 JSON 读取）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

# 从 __file__ 推导 repo root（不激活 venv，要求调用者用正确 Python 运行）
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.core_run_context import (  # noqa: E402
    ReleasedConfigResolver,
    resolve_core_run_context,
)

DATA_DIR = (
    BACKEND_ROOT
    / ".perfdata"
    / "afterclose"
    / "afterclose-20260817-v1"
)
RELEASED_CONFIG = DATA_DIR / "released_core_config.json"
ELIGIBLE_UNIVERSE = DATA_DIR / "eligible_universe.json"
OUTPUT = DATA_DIR / "expected_core_run_context.json"

TARGET_TRADE_DATE = date(2026, 8, 17)
SNAPSHOT_RUN_ID = "2b7c5877-7d36-4396-84c3-7186dc911073"
FROZEN_UNIVERSE_COUNT = 5293


class LocalFrozenReleasedResolver(ReleasedConfigResolver):
    """从已冻结的 released_core_config.json 读取，不连 DB。

    完整 CoreRunContext 由 production resolve_core_run_context() 构造。
    """

    def __init__(self, config_path: Path):
        with open(config_path, "r", encoding="utf-8") as f:
            self._cfg = json.load(f)

    async def resolve_released_dsa_config(self, trade_date):
        eff = self._cfg.get("effective_dsa_config", {})
        algo = self._cfg.get("algorithm_versions", {})
        return {
            "dsa_version": algo.get("dsa", self._cfg.get("version")),
            "dsa_build_hash": algo.get(
                "dsa_build_hash", self._cfg.get("build_hash")
            ),
            "dsa_effective_config": eff,
        }


def _load_eligible_ids() -> list[str]:
    """Blocker 1 修复：只认权威字段 sorted_ids，fail-closed 不接受 alias。"""
    with open(ELIGIBLE_UNIVERSE, "r", encoding="utf-8") as f:
        elig_doc = json.load(f)
    if "eligible_instrument_ids" in elig_doc:
        raise AssertionError(
            "eligible_universe.json 不应含 eligible_instrument_ids 别名；"
            "权威字段是 sorted_ids"
        )
    sorted_ids = elig_doc["sorted_ids"]
    universe_hash = elig_doc["universe_hash"]
    # hard gates
    assert len(sorted_ids) == elig_doc["count"] == FROZEN_UNIVERSE_COUNT, (
        f"sorted_ids({len(sorted_ids)}) != count({elig_doc['count']}) "
        f"!= {FROZEN_UNIVERSE_COUNT}"
    )
    assert len(set(sorted_ids)) == FROZEN_UNIVERSE_COUNT, "sorted_ids 非唯一"
    # SHA256(sorted ids) 必须等于 frozen universe_hash
    computed = (
        "sha256:"
        + hashlib.sha256(
            "\x00".join(sorted_ids).encode()
        ).hexdigest()
    )
    assert computed == universe_hash, (
        f"universe hash mismatch: computed={computed} frozen={universe_hash}"
    )
    return sorted_ids


def main() -> None:
    if not RELEASED_CONFIG.exists():
        print(f"ERROR: {RELEASED_CONFIG} not found", file=sys.stderr)
        sys.exit(1)

    resolver = LocalFrozenReleasedResolver(RELEASED_CONFIG)
    eligible_ids = _load_eligible_ids()

    ctx = asyncio.run(
        resolve_core_run_context(
            trade_date=TARGET_TRADE_DATE,
            snapshot_run_id=SNAPSHOT_RUN_ID,
            eligible_instrument_ids=eligible_ids,
            resolver=resolver,
            run_mode="after_close",
            universe_version="v1",
        )
    )

    cfg = ctx.config

    # Blocker 2 修复：production 组装什么就冻结什么，不拆第二套 projection
    payload = {
        "frozen_at": "2026-08-17",
        "generated_by": "generate_expected_core_context.py (4A-1R2)",
        "source": (
            "production resolve_core_run_context() + LocalFrozenReleasedResolver"
        ),
        "trade_date": str(ctx.trade_date),
        "run_mode": ctx.run_mode,
        "source_cutoff": ctx.source_cutoff,
        "execution_contract_version": ctx.execution_contract_version,
        "algorithm_versions": ctx.algorithm_versions,
        "parameter_hash": ctx.parameter_hash,
        "config": cfg,
    }

    # config 级 hard gates（确保完整 CoreRunContext 合同未被截断）
    assert cfg["eligible_universe_size"] == FROZEN_UNIVERSE_COUNT, (
        f"config.eligible_universe_size={cfg['eligible_universe_size']} "
        f"!= {FROZEN_UNIVERSE_COUNT}"
    )
    # universe_hash 在 eligible_universe.json 是 "sha256:..." 全串；
    # 而 production config.eligible_universe_hash 是去掉 "sha256:" 前缀后取前 16 hex。
    with open(ELIGIBLE_UNIVERSE, "r", encoding="utf-8") as f:
        elig_doc = json.load(f)
    frozen_hash_full = elig_doc["universe_hash"]
    assert frozen_hash_full.startswith("sha256:"), "universe_hash 格式异常"
    full_hex = frozen_hash_full[len("sha256:"):]  # 64-hex
    expected_hash = full_hex[:16]  # production 存前 16 hex
    assert cfg["eligible_universe_hash"] == expected_hash, (
        f"config.eligible_universe_hash={cfg['eligible_universe_hash']} "
        f"!= frozen {expected_hash}"
    )
    assert cfg["adjustment_as_of"] == "2026-08-17", (
        f"config.adjustment_as_of={cfg['adjustment_as_of']}"
    )
    assert "market_data_contract_version" in cfg, "缺 market_data_contract_version"
    assert "adjustment_contract_version" in cfg, "缺 adjustment_contract_version"

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

    print(f"WROTE {OUTPUT}")
    print(f"  trade_date={ctx.trade_date}")
    print(f"  run_mode={ctx.run_mode}")
    print(f"  parameter_hash={ctx.parameter_hash}")
    print(f"  eligible_universe_size={cfg['eligible_universe_size']}")
    print(f"  eligible_universe_hash={cfg['eligible_universe_hash']}")
    print(f"  adjustment_as_of={cfg['adjustment_as_of']}")
    print(f"  config keys={sorted(cfg.keys())}")


if __name__ == "__main__":
    main()
