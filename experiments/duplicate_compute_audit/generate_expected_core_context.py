"""4A-1R / E 步骤：用 production resolve_core_run_context() + 本地 frozen resolver
生成 expected_core_run_context.json。

背景：4A-1 落盘的 released_core_config.json 只冻结了 dsa_selector 部分配置，
未包含完整 CoreRunContext（缺 smc/bollinger/sqzmom/volume_context/swing_zones 等）。
本脚本构造一个从已冻结 released_core_config.json 读取的本地 resolver，
调用 production resolve_core_run_context() 得到完整的 expected CoreRunContext，
落盘为 expected_core_run_context.json，供 4A-1R 后续 FrozenMDAS 比对。

不下载任何 bars，不连生产 DB（resolver 从本地 JSON 读取）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 激活 backend venv（与生产代码同环境）
VENV_PY = Path("/Users/zhenbao/Desktop/coding/market_dev/backend/.venv/bin/python")
if not VENV_PY.exists():
    print("ERROR: backend venv not found", file=sys.stderr)
    sys.exit(1)

import importlib.util

# 将 backend 加入 sys.path 以便 import production 模块
BACKEND_ROOT = Path("/Users/zhenbao/Desktop/coding/market_dev/backend")
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.core_run_context import (  # noqa: E402
    CoreRunContext,
    ReleasedConfigResolver,
    resolve_core_run_context,
)

DATA_DIR = Path(
    "/Users/zhenbao/Desktop/coding/market_dev/backend/.perfdata/afterclose/"
    "afterclose-20260817-v1"
)
RELEASED_CONFIG = DATA_DIR / "released_core_config.json"
OUTPUT = DATA_DIR / "expected_core_run_context.json"


class LocalFrozenReleasedResolver(ReleasedConfigResolver):
    """从已冻结的 released_core_config.json 读取，不连 DB（Evid3 修复：
    完整 CoreRunContext 由 production resolve_core_run_context() 构造）。"""

    def __init__(self, config_path: Path):
        with open(config_path, "r", encoding="utf-8") as f:
            self._cfg = json.load(f)

    async def resolve_released_dsa_config(self, trade_date):
        # 从冻结的 released_core_config.json 返回 DSA 配置
        eff = self._cfg.get("effective_dsa_config", {})
        algo = self._cfg.get("algorithm_versions", {})
        return {
            "dsa_version": algo.get("dsa", self._cfg.get("version")),
            "dsa_build_hash": algo.get("dsa_build_hash", self._cfg.get("build_hash")),
            "dsa_effective_config": eff,
        }


def main() -> None:
    if not RELEASED_CONFIG.exists():
        print(f"ERROR: {RELEASED_CONFIG} not found", file=sys.stderr)
        sys.exit(1)

    resolver = LocalFrozenReleasedResolver(RELEASED_CONFIG)

    import asyncio
    from datetime import date, datetime, timezone

    # 从 eligible_universe.json 读取 eligible_instrument_ids（8/17 持久化的权威 universe）
    elig_path = DATA_DIR / "eligible_universe.json"
    with open(elig_path, "r", encoding="utf-8") as f:
        elig_doc = json.load(f)
    eligible_ids = elig_doc.get("eligible_instrument_ids", [])

    ctx: CoreRunContext = asyncio.run(
        resolve_core_run_context(
            trade_date=date(2026, 8, 17),
            snapshot_run_id="2b7c5877-7d36-4396-84c3-7186dc911073",
            eligible_instrument_ids=eligible_ids,
            resolver=resolver,
            run_mode="after_close",
            universe_version="v1",
        )
    )

    # 落盘为 JSON（dataclass -> dict 序列化）
    cfg = ctx.config
    payload = {
        "frozen_at": "2026-08-17",
        "generated_by": "generate_expected_core_context.py (4A-1R/E)",
        "source": "production resolve_core_run_context() + LocalFrozenReleasedResolver",
        "trade_date": str(ctx.trade_date),
        "run_mode": ctx.run_mode,
        "source_cutoff": ctx.source_cutoff,
        "execution_contract_version": ctx.execution_contract_version,
        "algorithm_versions": ctx.algorithm_versions,
        "parameter_hash": ctx.parameter_hash,
        "dsa": cfg.get("dsa"),
        "smc": cfg.get("smc"),
        "bollinger": cfg.get("bollinger"),
        "momentum": cfg.get("momentum"),
        "volume_context": cfg.get("volume_context"),
        "swing_zones": cfg.get("swing_zones"),
    }

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

    print(f"WROTE {OUTPUT}")
    print(f"  trade_date={ctx.trade_date}")
    print(f"  run_mode={ctx.run_mode}")
    print(f"  parameter_hash={ctx.parameter_hash}")
    print(f"  algorithm_versions={ctx.algorithm_versions}")
    print(f"  dsa={cfg.get('dsa')}")
    print(f"  smc={cfg.get('smc')}")
    print(f"  momentum={cfg.get('momentum')}")
    print(f"  volume_context={cfg.get('volume_context')}")
    print(f"  swing_zones={cfg.get('swing_zones')}")


if __name__ == "__main__":
    main()
