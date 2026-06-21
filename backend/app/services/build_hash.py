"""构建哈希计算 - 用于策略版本不可变性校验与幂等发布。

compute_build_hash(manifest, schema, entrypoint):
- 计算 manifest + schema + entrypoint 的 SHA256 哈希
- 相同内容产生相同 hash，用于：
  1. 版本不可变性：released 版本的 build_hash 不变，校验未被篡改
  2. 幂等发布：相同 manifest+schema+entrypoint 的 build_hash 相同，避免重复发布

哈希计算方式：
- 将 manifest、schema、entrypoint 规范化 JSON（sort_keys=True）后拼接
- 使用 SHA256 计算哈希，返回十六进制字符串
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical_json(obj: Any) -> str:
    """规范化 JSON 序列化（sort_keys + ensure_ascii=False），保证哈希稳定。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_build_hash(
    manifest: dict[str, Any],
    schema: dict[str, Any] | None = None,
    entrypoint: str | None = None,
) -> str:
    """计算策略构建哈希（SHA256）。

    Args:
        manifest: 策略 Manifest 字典
        schema: 策略参数/输出 schema 字典（可选，用于区分相同 manifest 但 schema 不同的版本）
        entrypoint: 策略入口点字符串（可选，如 'strategies.selectors.dsa:DSASelector'）
            若未提供则从 manifest['entrypoint'] 读取

    Returns:
        64 字符十六进制 SHA256 哈希字符串

    说明：
        - manifest 必须可 JSON 序列化
        - schema 为 None 时按空字典处理
        - entrypoint 为 None 时从 manifest 读取，仍为 None 则按空串处理
    """
    if entrypoint is None:
        entrypoint = manifest.get("entrypoint", "") or ""
    if schema is None:
        schema = {}

    # 规范化拼接：manifest + schema + entrypoint
    parts = [
        _canonical_json(manifest),
        _canonical_json(schema),
        _canonical_json(entrypoint),
    ]
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    # 自测入口：验证哈希计算的稳定性与幂等性
    manifest_a = {
        "strategy_id": "dsa_selector",
        "kind": "selector",
        "version": "1.1.0",
        "entrypoint": "strategies.selectors.dsa:DSASelector",
        "input": {"bar_frequency": "1d", "min_bars": 360},
        "outputs": [{"key": "dsa_dir_bars", "type": "integer"}],
        "parameters": [{"key": "algorithm.lookback", "type": "integer", "default": 360, "allowed_scopes": ["strategy"]}],
        "capabilities": {"composable": True},
    }
    manifest_b = dict(manifest_a)  # 相同内容

    hash_a = compute_build_hash(manifest_a)
    hash_b = compute_build_hash(manifest_b)
    print(f"hash_a={hash_a[:16]}...")
    print(f"hash_b={hash_b[:16]}...")
    assert hash_a == hash_b, "相同内容应产生相同哈希"
    print("幂等性: PASS")

    # 不同内容应产生不同哈希
    manifest_c = dict(manifest_a)
    manifest_c["version"] = "1.2.0"
    hash_c = compute_build_hash(manifest_c)
    assert hash_a != hash_c, "不同内容应产生不同哈希"
    print("差异性: PASS")

    # 显式 entrypoint 与 manifest 内 entrypoint 一致时应产生相同哈希
    hash_d = compute_build_hash(manifest_a, entrypoint="strategies.selectors.dsa:DSASelector")
    assert hash_a == hash_d, "显式 entrypoint 与 manifest 内 entrypoint 一致时应相同"
    print("entrypoint 一致性: PASS")

    # schema 影响哈希
    hash_e = compute_build_hash(manifest_a, schema={"extra": True})
    assert hash_a != hash_e, "不同 schema 应产生不同哈希"
    print("schema 影响: PASS")

    print(f"hash 长度: {len(hash_a)}")
    assert len(hash_a) == 64
    print("OK")
