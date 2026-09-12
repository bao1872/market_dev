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

_SMC_ALGORITHM_ID = _SMC_CONTRACT.algorithm_id
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
        if not all(isinstance(k, str) for k in value):
            raise SmcTargetContractError("canonical payload dict key 必须全部为 str")
        return {k: _canonicalize(value[k]) for k in sorted(value)}
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


_STRUCTURE_SLOT_FIELDS = {
    "level",
    "anchor_index",
    "anchor_time",
    "crossed",
}


def _validate_lane_bias(value: Any, field: str) -> int:
    """lane bias 必须是 -1/0/1 的精确 int（bool 是 int 子类，必须排除）。"""
    if type(value) is not int or value not in (-1, 0, 1):
        raise SmcTargetContractError(
            f"{field} 必须是 -1/0/1 的 int，实际={value!r}"
        )
    return value


def _validate_structure_slot(name: str, slot: Any) -> dict[str, Any]:
    """严格校验单个 structure slot；不替上游静默修数据。

    - 必须是 dict，且字段集合恰好为 level/anchor_index/anchor_time/crossed；
    - crossed 必须是精确 bool；
    - level/anchor_index/anchor_time 三者同进同出（partial-formed → error）；
    - 未形成 slot 不允许 crossed=True；
    - 形成 slot：level 有限、anchor_index 非负 int、anchor_time 非空 str。
    """
    if not isinstance(slot, dict):
        raise SmcTargetContractError(f"structure slot {name} 必须是 dict")
    if set(slot.keys()) != _STRUCTURE_SLOT_FIELDS:
        raise SmcTargetContractError(
            f"structure slot {name} 字段非法: {sorted(slot.keys())}"
        )
    crossed = slot["crossed"]
    if type(crossed) is not bool:
        raise SmcTargetContractError(f"{name}.crossed 必须是 bool")
    level = slot["level"]
    anchor_index = slot["anchor_index"]
    anchor_time = slot["anchor_time"]
    present = (
        level is not None,
        anchor_index is not None,
        anchor_time is not None,
    )
    if any(present) and not all(present):
        raise SmcTargetContractError(f"partial-formed structure slot {name}: {slot}")
    if not any(present):
        if crossed is not False:
            raise SmcTargetContractError(f"unformed slot {name} 不允许 crossed=True")
        return dict(slot)
    if not _is_finite(level):
        raise SmcTargetContractError(f"{name}.level 必须是 finite number")
    if type(anchor_index) is not int or anchor_index < 0:
        raise SmcTargetContractError(f"{name}.anchor_index 必须是非负 int")
    if not isinstance(anchor_time, str) or not anchor_time:
        raise SmcTargetContractError(f"{name}.anchor_time 必须是非空 str")
    return dict(slot)


def _parse_structure_context(smc_result: dict[str, Any]) -> dict[str, Any]:
    state = smc_result.get("structure_target_state")
    if not isinstance(state, dict):
        raise SmcTargetContractError("smc_result 缺少 structure_target_state（需 emit_structure_target_state=True）")
    swing_bias = _validate_lane_bias(state.get("swing_bias"), "swing_bias")
    internal_bias = _validate_lane_bias(state.get("internal_bias"), "internal_bias")
    slots_raw = state.get("slots")
    if not isinstance(slots_raw, dict):
        raise SmcTargetContractError("structure_target_state.slots 缺失或非 dict")
    if set(slots_raw.keys()) != set(_STRUCTURE_SLOT_ORDER):
        raise SmcTargetContractError(
            f"structure_target_state.slots 必须是固定四槽位 {_STRUCTURE_SLOT_ORDER}，实际: {sorted(slots_raw.keys())}"
        )
    validated_slots = {
        name: _validate_structure_slot(name, slots_raw[name])
        for name in _STRUCTURE_SLOT_ORDER
    }
    return {
        "swing_bias": swing_bias,
        "internal_bias": internal_bias,
        "slots": validated_slots,
    }


# ---------------------------------------------------------------------------
# OB / structure 严格类型校验 helpers（禁止 bool()/int()/str() 隐式转换）
# ---------------------------------------------------------------------------


def _require_int(value: Any, field: str) -> int:
    """非负精确 int（排除 bool）。"""
    if type(value) is not int or value < 0:
        raise SmcTargetContractError(f"{field} 必须是非负 int，实际={value!r}")
    return value


def _require_bias(value: Any, field: str) -> int:
    """OB bias 必须是 -1/1 的精确 int（排除 bool）。"""
    if type(value) is not int or value not in (-1, 1):
        raise SmcTargetContractError(f"{field} 必须是 -1/1，实际={value!r}")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise SmcTargetContractError(f"{field} 必须是 bool，实际={value!r}")
    return value


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SmcTargetContractError(f"{field} 必须是非空 str，实际={value!r}")
    return value


def _structure_version_content(target: SmcStructureTarget) -> dict[str, Any]:
    """TargetSet version 仅依赖底层 canonical target content（不含派生 target_id）。"""
    return {
        "lane": target.lane,
        "kind": target.kind,
        "level": target.level,
        "anchor_index": target.anchor_index,
        "anchor_time": target.anchor_time,
    }


def _ob_version_content(target: SmcOrderBlockTarget) -> dict[str, Any]:
    """TargetSet version 仅依赖底层 canonical target content（不含派生 target_id）。"""
    return {
        "internal": target.internal,
        "bias": target.bias,
        "bar_low": target.bar_low,
        "bar_high": target.bar_high,
        "anchor_index": target.anchor_index,
        "anchor_time": target.anchor_time,
        "confirmed_index": target.confirmed_index,
        "confirmed_time": target.confirmed_time,
    }


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
    #    formed = level / anchor_index / anchor_time 三者均非 None（已严格校验）
    structure_targets: list[SmcStructureTarget] = []
    for name in _STRUCTURE_SLOT_ORDER:
        slot = slots[name]
        level = slot["level"]
        anchor_index = slot["anchor_index"]
        anchor_time = slot["anchor_time"]
        crossed = slot["crossed"]
        formed = level is not None and anchor_index is not None and anchor_time is not None
        if not formed:
            continue
        if crossed is not False:
            # formed 但已 crossed → 不是 active target（pending breakout candidate 已被突破）
            continue
        lane = "swing" if name.startswith("swing") else "internal"
        kind = "high" if name.endswith("high") else "low"
        target_id = _structure_target_id(
            params_hash,
            lane,
            kind,
            float(level),
            int(anchor_index),
            str(anchor_time),
        )
        structure_targets.append(
            SmcStructureTarget(
                target_id=target_id,
                lane=lane,
                kind=kind,
                level=float(level),
                anchor_index=int(anchor_index),
                anchor_time=str(anchor_time),
            )
        )

    # 5. active OB targets：mitigated_index 缺失→error；None→active；非负 int→inactive
    order_blocks = smc_result.get("order_blocks")
    if not isinstance(order_blocks, list):
        raise SmcTargetContractError("smc_result 缺少 order_blocks")
    ob_targets: list[SmcOrderBlockTarget] = []
    for ob in order_blocks:
        if not isinstance(ob, dict):
            raise SmcTargetContractError(f"order_blocks 元素必须是 dict: {ob!r}")
        # mitigated_index 三态严格判定
        if "mitigated_index" not in ob:
            raise SmcTargetContractError(f"active OB 缺少 mitigated_index 字段（不得 silent 当 active）: {ob}")
        mi = ob["mitigated_index"]
        if mi is not None:
            if type(mi) is not int or mi < 0:
                raise SmcTargetContractError(f"OB mitigated_index 必须是 None 或非负 int: {ob}")
            continue  # 已 mitigated 不参与 realtime target
        # active OB：严格校验 target-only 字段，禁止隐式类型转换
        for field in (
            "internal", "bias", "bar_low", "bar_high",
            "anchor_index", "anchor_time", "confirmed_index", "confirmed_time",
            "mitigated_index",
        ):
            if field not in ob:
                raise SmcTargetContractError(f"active OB 缺少字段 {field}: {ob}")
        internal = _require_bool(ob["internal"], "internal")
        bias = _require_bias(ob["bias"], "bias")
        if not _is_finite(ob["bar_low"]):
            raise SmcTargetContractError(f"active OB bar_low 必须是 finite number（bool 禁止）: {ob}")
        if not _is_finite(ob["bar_high"]):
            raise SmcTargetContractError(f"active OB bar_high 必须是 finite number（bool 禁止）: {ob}")
        bar_low = float(ob["bar_low"])
        bar_high = float(ob["bar_high"])
        if bar_low > bar_high:
            raise SmcTargetContractError(f"active OB bar_low > bar_high: {ob}")
        anchor_index = _require_int(ob["anchor_index"], "anchor_index")
        anchor_time = _require_nonempty_str(ob["anchor_time"], "anchor_time")
        confirmed_index = _require_int(ob["confirmed_index"], "confirmed_index")
        confirmed_time = _require_nonempty_str(ob["confirmed_time"], "confirmed_time")
        target_id = _ob_target_id(
            params_hash,
            internal,
            bias,
            bar_low,
            bar_high,
            anchor_index,
            anchor_time,
            confirmed_index,
            confirmed_time,
        )
        ob_targets.append(
            SmcOrderBlockTarget(
                target_id=target_id,
                internal=internal,
                bias=bias,
                bar_low=bar_low,
                bar_high=bar_high,
                anchor_index=anchor_index,
                anchor_time=anchor_time,
                confirmed_index=confirmed_index,
                confirmed_time=confirmed_time,
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
        "active_structure_targets": [_structure_version_content(t) for t in structure_targets],
        "active_order_block_targets": [_ob_version_content(t) for t in ob_targets],
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
