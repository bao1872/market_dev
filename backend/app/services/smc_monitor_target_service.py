"""C3B — Versioned SMC Monitor Target Contract builder（SMC realtime 前置）。

职责（严格限定）：
1. 对 SMC 实际日线输入建立稳定 ``daily_bars_hash``（仅 hash time/O/H/L/C，不含 volume）；
2. 消费 C3A ``structure_target_state``（必须以 ``emit_structure_target_state=True`` 取得）；
3. 从完整 structure state 派生 active structure targets（formed AND crossed==False）；
4. 从 core ``order_blocks`` 派生 active OB targets（mitigated_index 字段存在且为 None；缺失 fail closed）；
5. 建立稳定 target identity（target_id 不 scoped by target_set_version）；
6. 建立整个 TargetSet snapshot version（target_set_version）。

本模块不修改 SMC core、不接 Monitor、不接 snapshot、不做 realtime crossing /
BOS/CHoCH / first-touch、不删旧 retest、不 import Node target service、不写死
adjustment="qfq"、不引入 factor hash / adjustment context / m15 identity。

identity 直接复用 SMC algorithm registry（algorithm_id / algorithm_version /
contract_fingerprint / output_schema_version），不再自行定义。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.contracts.algorithm_registry import AlgorithmRegistry

# ---------------------------------------------------------------------------
# 常量（monitor target contract 自身 schema，不同于 algorithm output schema）
# ---------------------------------------------------------------------------

SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION: int = 1

_SMC_CONTRACT = AlgorithmRegistry.get("smc")

_SMC_ALGORITHM_ID = "smc"
_SMC_ALGORITHM_VERSION = _SMC_CONTRACT.algorithm_version
_SMC_CONTRACT_FINGERPRINT = _SMC_CONTRACT.contract_fingerprint
_SMC_OUTPUT_SCHEMA_VERSION = _SMC_CONTRACT.output_schema_version

_TARGET_NAMESPACE_STRUCTURE = "smc_structure_target"
_TARGET_NAMESPACE_ORDER_BLOCK = "smc_order_block_target"

_REQUIRED_OHLC = ("open", "high", "low", "close")

_STRUCTURE_SLOT_ORDER = ("swing_high", "swing_low", "internal_high", "internal_low")


class SmcTargetContractError(Exception):
    """contract 本身 malformed（fail closed，不 silent fallback）。"""


# ---------------------------------------------------------------------------
# 内部 canonicalization 工具（所有 hash payload 统一走同一 recursive serializer）
# ---------------------------------------------------------------------------


def _canonicalize(value: Any) -> Any:
    """递归 canonicalize 任意 JSON-compatible 值，供所有 hash payload 统一使用。

    规则（所有进入 hash 的 float 走同一 exact 表示，杜绝三套规则）：
    - None / bool / int / str：原样保留（bool 与 int 不混为一谈）
    - float：float(value).hex()（exact、非 round、非 :.4f）；non-finite → fail closed
    - list / tuple：元素递归 canonicalize
    - dict：按 key 排序后逐值递归 canonicalize（key 确定性）
    - 未知类型：fail closed，不 str(obj) 静默降级
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SmcTargetContractError(f"non-finite float 进入 canonical payload: {value!r}")
        return value.hex()
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonicalize(v) for k, v in sorted(value.items())}
    raise SmcTargetContractError(f"canonicalizer 不支持的类型: {type(value).__name__}")


def _sha256_json(obj: Any) -> str:
    """对 obj 递归 canonicalize + 确定性 JSON 序列化并 SHA256。

    dict key 顺序无关（canonicalize 已排序），list 顺序按语义保留，
    float exact canonicalization，non-finite fail closed，不依赖 repr(dict)。
    """
    canonical = _canonicalize(obj)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---------------------------------------------------------------------------
# 逻辑模型（frozen dataclass）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SmcStructureTarget:
    target_id: str
    lane: str  # "swing" | "internal"
    kind: str  # "high" | "low"
    level: float
    anchor_index: int
    anchor_time: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "lane": self.lane,
            "kind": self.kind,
            "level": self.level,
            "anchor_index": self.anchor_index,
            "anchor_time": self.anchor_time,
        }


@dataclass(frozen=True)
class SmcOrderBlockTarget:
    target_id: str
    internal: bool
    bias: int
    bar_low: float
    bar_high: float
    anchor_index: int
    anchor_time: str
    confirmed_index: int
    confirmed_time: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "internal": self.internal,
            "bias": self.bias,
            "bar_low": self.bar_low,
            "bar_high": self.bar_high,
            "anchor_index": self.anchor_index,
            "anchor_time": self.anchor_time,
            "confirmed_index": self.confirmed_index,
            "confirmed_time": self.confirmed_time,
        }


@dataclass(frozen=True)
class SmcMonitorTargetSet:
    contract_identity: dict[str, Any]
    input_identity: dict[str, Any]
    structure_context: dict[str, Any]
    active_structure_targets: list[SmcStructureTarget]
    active_order_block_targets: list[SmcOrderBlockTarget]
    target_set_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_identity": self.contract_identity,
            "input_identity": self.input_identity,
            "structure_context": self.structure_context,
            "active_targets": {
                "structure_targets": [t.to_dict() for t in self.active_structure_targets],
                "order_block_targets": [t.to_dict() for t in self.active_order_block_targets],
            },
            "target_set_version": self.target_set_version,
        }


# ---------------------------------------------------------------------------
# input identity：daily_bars_hash
# ---------------------------------------------------------------------------


def _extract_bars(daily_bars: pd.DataFrame) -> tuple[list[str], list[float], list[float], list[float], list[float]]:
    """从 DataFrame 提取 time/open/high/low/close 列表（SMC 实际消费的 5 列）。

    时间使用与 SMC core 实际时间输入路径完全一致的 ``ts.isoformat()``
    （compute_smc_adapter 给 core 的时间就是 idx.isoformat()，不能自行发明 str(ts)）。
    """
    missing = [c for c in _REQUIRED_OHLC if c not in daily_bars.columns]
    if missing:
        raise SmcTargetContractError(f"daily_bars 缺少必需列: {missing}")
    if len(daily_bars.index) == 0:
        raise SmcTargetContractError("daily_bars 为空（禁止静默空输入）")
    try:
        times = [ts.isoformat() for ts in daily_bars.index]
    except AttributeError as exc:  # noqa: BLE001
        raise SmcTargetContractError(f"daily_bars index 不支持稳定 isoformat(): {exc}") from exc
    opens = [float(x) for x in daily_bars["open"]]
    highs = [float(x) for x in daily_bars["high"]]
    lows = [float(x) for x in daily_bars["low"]]
    closes = [float(x) for x in daily_bars["close"]]
    return times, opens, highs, lows, closes


def compute_daily_bars_hash(daily_bars: pd.DataFrame) -> str:
    """对 SMC 实际消费的 time/O/H/L/C 做 canonical hash（不含 volume）。

    选择：non-finite OHLC → fail closed（real SMC 输入不应含 NaN/inf，
    不隐式用 allow_nan=True）。时间使用与 core 一致的 ``ts.isoformat()``；
    所有 float 走统一 canonical serializer（float.hex()）。
    """
    times, opens, highs, lows, closes = _extract_bars(daily_bars)
    payload: list[list[Any]] = []
    for t, o, h, low, c in zip(times, opens, highs, lows, closes, strict=True):
        for name, v in (("open", o), ("high", h), ("low", low), ("close", c)):
            if not _is_finite(v):
                raise SmcTargetContractError(f"daily_bars 含非有限 {name}={v}")
        payload.append([t, o, h, low, c])
    return _sha256_json({"bars": payload})


# ---------------------------------------------------------------------------
# structure target_id / OB target_id
# ---------------------------------------------------------------------------


def _structure_target_id(
    params_hash: str,
    lane: str,
    kind: str,
    level: float,
    anchor_index: int,
    anchor_time: str,
) -> str:
    payload = {
        "namespace": _TARGET_NAMESPACE_STRUCTURE,
        # monitor target contract schema（与 algorithm output_schema_version 是两个维度，二者都保留）
        "target_contract_schema_version": SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION,
        "algorithm_id": _SMC_ALGORITHM_ID,
        "algorithm_version": _SMC_ALGORITHM_VERSION,
        "contract_fingerprint": _SMC_CONTRACT_FINGERPRINT,
        "output_schema_version": _SMC_OUTPUT_SCHEMA_VERSION,
        "params_hash": params_hash,
        "lane": lane,
        "kind": kind,
        "level": level,
        "anchor_index": anchor_index,
        "anchor_time": anchor_time,
    }
    return _sha256_json(payload)


def _ob_target_id(
    params_hash: str,
    internal: bool,
    bias: int,
    bar_low: float,
    bar_high: float,
    anchor_index: int,
    anchor_time: str,
    confirmed_index: int,
    confirmed_time: str,
) -> str:
    payload = {
        "namespace": _TARGET_NAMESPACE_ORDER_BLOCK,
        # monitor target contract schema（与 algorithm output_schema_version 是两个维度，二者都保留）
        "target_contract_schema_version": SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION,
        "algorithm_id": _SMC_ALGORITHM_ID,
        "algorithm_version": _SMC_ALGORITHM_VERSION,
        "contract_fingerprint": _SMC_CONTRACT_FINGERPRINT,
        "output_schema_version": _SMC_OUTPUT_SCHEMA_VERSION,
        "params_hash": params_hash,
        "internal": internal,
        "bias": bias,
        "bar_low": bar_low,
        "bar_high": bar_high,
        "anchor_index": anchor_index,
        "anchor_time": anchor_time,
        "confirmed_index": confirmed_index,
        "confirmed_time": confirmed_time,
    }
    return _sha256_json(payload)


# ---------------------------------------------------------------------------
# structure state 解析 / formed 判定
# ---------------------------------------------------------------------------


def _slot_formed(slot: dict[str, Any]) -> bool:
    """formed = level / anchor_index / anchor_time 三者均非 None。

    partial-formed（部分非 None）→ fail closed。
    """
    level = slot.get("level")
    anchor_index = slot.get("anchor_index")
    anchor_time = slot.get("anchor_time")
    present = [x is not None for x in (level, anchor_index, anchor_time)]
    if any(present) and not all(present):
        raise SmcTargetContractError(f"partial-formed structure slot: {slot}")
    if not all(present):
        return False
    if not _is_finite(level):
        raise SmcTargetContractError(f"formed structure slot 含非有限 level: {slot}")
    return True


def _parse_structure_context(smc_result: dict[str, Any]) -> dict[str, Any]:
    state = smc_result.get("structure_target_state")
    if not isinstance(state, dict):
        raise SmcTargetContractError("smc_result 缺少 structure_target_state（需 emit_structure_target_state=True）")
    if "swing_bias" not in state or "internal_bias" not in state:
        raise SmcTargetContractError("structure_target_state 缺少 swing_bias/internal_bias")
    if not isinstance(state["swing_bias"], int) or not isinstance(state["internal_bias"], int):
        raise SmcTargetContractError("structure_target_state bias 非法（非 int）")
    slots = state.get("slots")
    if not isinstance(slots, dict):
        raise SmcTargetContractError("structure_target_state.slots 缺失或非 dict")
    if set(slots.keys()) != set(_STRUCTURE_SLOT_ORDER):
        raise SmcTargetContractError(
            f"structure_target_state.slots 必须是固定四槽位 {_STRUCTURE_SLOT_ORDER}，实际: {sorted(slots.keys())}"
        )
    # 深拷贝，避免与 smc_result 共享引用
    return json.loads(json.dumps(state))


# ---------------------------------------------------------------------------
# 主 builder
# ---------------------------------------------------------------------------


def build_smc_monitor_target_set(
    daily_bars: pd.DataFrame,
    smc_result: dict[str, Any],
) -> SmcMonitorTargetSet:
    """从实际 SMC 日线输入 + smc_result 构建确定性、可版本化 TargetSet。

    Args:
        daily_bars: SMC 实际消费的日线（DatetimeIndex + open/high/low/close 列）。
        smc_result: compute_smc_pine(..., emit_structure_target_state=True) 的结果。

    Returns:
        SmcMonitorTargetSet（frozen dataclass）。

    Raises:
        SmcTargetContractError: contract malformed（fail closed）。
    """
    # 1. input identity
    daily_bars_hash = compute_daily_bars_hash(daily_bars)
    times, _, _, _, _ = _extract_bars(daily_bars)
    bar_count = len(times)
    updated_through = times[-1]  # diagnostic metadata，不单独进入 version

    # 2. effective params identity（取 core 实际返回的 params，而非 DEFAULT_PARAMS）
    params = smc_result.get("params")
    if not isinstance(params, dict):
        raise SmcTargetContractError("smc_result 缺少 effective params")
    params_hash = _sha256_json(params)

    # 3. structure_context（完整保留 C3A 状态，含 crossed=True 与未形成 slot）
    structure_context = _parse_structure_context(smc_result)
    slots = structure_context["slots"]

    # 4. active structure targets：formed AND crossed == False
    structure_targets: list[SmcStructureTarget] = []
    for name in _STRUCTURE_SLOT_ORDER:
        slot = slots[name]
        if not _slot_formed(slot):
            continue
        if slot.get("crossed") is not False:
            # formed 但已 crossed → 不是 active target，仅保留在 structure_context
            continue
        lane = "swing" if name.startswith("swing") else "internal"
        kind = "high" if name.endswith("high") else "low"
        target_id = _structure_target_id(
            params_hash,
            lane,
            kind,
            float(slot["level"]),
            int(slot["anchor_index"]),
            str(slot["anchor_time"]),
        )
        structure_targets.append(
            SmcStructureTarget(
                target_id=target_id,
                lane=lane,
                kind=kind,
                level=float(slot["level"]),
                anchor_index=int(slot["anchor_index"]),
                anchor_time=str(slot["anchor_time"]),
            )
        )

    # 5. active OB targets：仅 mitigated_index 字段存在且为 None
    order_blocks = smc_result.get("order_blocks")
    if not isinstance(order_blocks, list):
        raise SmcTargetContractError("smc_result 缺少 order_blocks")
    ob_targets: list[SmcOrderBlockTarget] = []
    for ob in order_blocks:
        # mitigated_index 三态：缺失 → malformed（fail closed）；None → active；非 None → inactive
        if "mitigated_index" not in ob:
            raise SmcTargetContractError(f"active OB 缺少 mitigated_index 字段（不得 silent 当 active）: {ob}")
        if ob["mitigated_index"] is not None:
            continue  # 已 mitigated 不参与 realtime target
        # 必要字段校验（entered / OB_ENTERED / enter_* 不影响）
        for field in (
            "internal", "bias", "bar_low", "bar_high",
            "anchor_index", "anchor_time", "confirmed_index", "confirmed_time",
            "mitigated_index",
        ):
            if field not in ob:
                raise SmcTargetContractError(f"active OB 缺少字段 {field}: {ob}")
        bar_low = float(ob["bar_low"])
        bar_high = float(ob["bar_high"])
        if not _is_finite(bar_low) or not _is_finite(bar_high):
            raise SmcTargetContractError(f"active OB zone 含非有限值: {ob}")
        if bar_low > bar_high:
            raise SmcTargetContractError(f"active OB bar_low > bar_high: {ob}")
        target_id = _ob_target_id(
            params_hash,
            bool(ob["internal"]),
            int(ob["bias"]),
            bar_low,
            bar_high,
            int(ob["anchor_index"]),
            str(ob["anchor_time"]),
            int(ob["confirmed_index"]),
            str(ob["confirmed_time"]),
        )
        ob_targets.append(
            SmcOrderBlockTarget(
                target_id=target_id,
                internal=bool(ob["internal"]),
                bias=int(ob["bias"]),
                bar_low=bar_low,
                bar_high=bar_high,
                anchor_index=int(ob["anchor_index"]),
                anchor_time=str(ob["anchor_time"]),
                confirmed_index=int(ob["confirmed_index"]),
                confirmed_time=str(ob["confirmed_time"]),
            )
        )

    # OB targets 确定性排序（不依赖 order_blocks 原顺序；float 直接比较即可，canonical 由 serializer 处理）
    ob_targets.sort(
        key=lambda t: (
            t.internal, t.bias, t.anchor_index, t.anchor_time,
            t.confirmed_index, t.confirmed_time,
            t.bar_low, t.bar_high,
        )
    )

    # 6. contract identity / input identity / target_set_version
    contract_identity = {
        "schema_version": SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION,
        "algorithm_id": _SMC_ALGORITHM_ID,
        "algorithm_version": _SMC_ALGORITHM_VERSION,
        "output_schema_version": _SMC_OUTPUT_SCHEMA_VERSION,
        "contract_fingerprint": _SMC_CONTRACT_FINGERPRINT,
        "params_hash": params_hash,
    }
    input_identity = {
        "daily_bars_hash": daily_bars_hash,
        "bar_count": bar_count,
        "updated_through": updated_through,  # diagnostic，不单独进入 version
    }

    version_payload = {
        "monitor_schema_version": SMC_MONITOR_TARGET_CONTRACT_SCHEMA_VERSION,
        "algorithm_id": _SMC_ALGORITHM_ID,
        "algorithm_version": _SMC_ALGORITHM_VERSION,
        "contract_fingerprint": _SMC_CONTRACT_FINGERPRINT,
        "output_schema_version": _SMC_OUTPUT_SCHEMA_VERSION,
        "params_hash": params_hash,
        "daily_bars_hash": daily_bars_hash,
        "bar_count": bar_count,
        "structure_context": structure_context,
        "active_structure_targets": [t.to_dict() for t in structure_targets],
        "active_order_block_targets": [t.to_dict() for t in ob_targets],
    }
    target_set_version = _sha256_json(version_payload)

    return SmcMonitorTargetSet(
        contract_identity=contract_identity,
        input_identity=input_identity,
        structure_context=structure_context,
        active_structure_targets=structure_targets,
        active_order_block_targets=ob_targets,
        target_set_version=target_set_version,
    )
