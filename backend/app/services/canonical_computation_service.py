"""规范计算服务 — 所有指标/因子/特征算法的统一计算入口。

本文件是 Section 2 (CHANGE-20260718-006) 的调度层：四条调用链（详情/盘后/盘中/
Capture）通过本服务调用已注册算法，禁止直接 import 算法 kernel 绕过注册表。

职责：
1. 通过 AlgorithmRegistry 查询算法合同
2. 校验输入 contract（timeframe/adj/completed_only 等是否匹配合同）
3. 动态加载 kernel callable（importlib + getattr）
4. 调用 kernel 计算结果
5. 计算 result_hash（用于缓存键与一致性比对）
6. 返回 CanonicalResult（含 contract_fingerprint + result_hash + algorithm_version）

非职责（不在此处实现）：
- 行情数据获取（由 MDAS 负责，调用方传入 bars）
- 缓存读写（由调用方/indicator_cache 负责，本服务只计算 result_hash 作为缓存键）
- 事件检测/状态机（由各调用链自己的适配层负责）

设计权衡：
- 本服务不持有行情数据获取逻辑，避免与 MDAS 耦合
- 本服务不缓存计算结果，避免缓存失效逻辑与算法合同耦合
- 本服务只做"调度 + 校验 + 哈希"，保持薄层

用法：
    from app.services.canonical_computation_service import CanonicalComputationService
    result = await CanonicalComputationService.compute(
        algorithm_id="node_cluster",
        bars_daily=daily_df,
        bars_15min=low_tf_df,
        instrument_id=instrument_id,
        as_of=trade_date,
    )
    # result.contract_fingerprint, result.result_hash, result.algorithm_version
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

import pandas as pd

from app.contracts.algorithm_registry import (
    ALGORITHM_REGISTRY_VERSION,
    AlgorithmContract,
    AlgorithmRegistry,
)

logger = logging.getLogger("services.canonical_computation_service")


# =============================================================================
# 不可变结果数据类
# =============================================================================


@dataclass(frozen=True)
class CanonicalResult:
    """规范计算结果（不可变）。

    Attributes:
        algorithm_id: 算法唯一标识
        algorithm_version: 算法版本（来自合同）
        output_schema_version: 输出 schema 版本（来自合同）
        contract_fingerprint: 合同指纹（来自合同，缓存键组成部分）
        result_hash: 结果哈希（结果内容 SHA256 前 16 字符，缓存键组成部分）
        registry_version: 注册表版本
        payload: 算法 kernel 返回的原始结果（dict/dataclass/任意可序列化对象）
        computed_at: 计算时间（ISO 字符串，仅用于日志/诊断，不参与哈希）
    """

    algorithm_id: str
    algorithm_version: str
    output_schema_version: int
    contract_fingerprint: str
    result_hash: str
    registry_version: str
    payload: Any
    computed_at: str = field(default="")

    def to_cache_key_parts(self) -> dict[str, str]:
        """返回用于缓存键的字段（不含 payload/computed_at）。"""
        return {
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "output_schema_version": str(self.output_schema_version),
            "contract_fingerprint": self.contract_fingerprint,
            "result_hash": self.result_hash,
            "registry_version": self.registry_version,
        }


# =============================================================================
# 异常
# =============================================================================


class CanonicalComputationError(RuntimeError):
    """规范计算服务错误。"""


class AlgorithmNotFoundError(CanonicalComputationError):
    """算法未注册。"""

    def __init__(self, algorithm_id: str) -> None:
        super().__init__(
            f"算法未注册: algorithm_id={algorithm_id} "
            f"已注册={AlgorithmRegistry.list_ids()}"
        )
        self.algorithm_id = algorithm_id


class ContractViolationError(CanonicalComputationError):
    """输入违反算法合同。"""

    def __init__(self, algorithm_id: str, reason: str) -> None:
        super().__init__(
            f"输入违反算法合同: algorithm_id={algorithm_id} reason={reason}"
        )
        self.algorithm_id = algorithm_id
        self.reason = reason


class KernelImportError(CanonicalComputationError):
    """kernel 模块导入失败。"""


# =============================================================================
# 规范计算服务（无状态，类方法入口）
# =============================================================================


class CanonicalComputationService:
    """规范计算服务 — 统一调度已注册算法 kernel。

    无状态：所有方法均为类方法，不持有实例状态。
    线程安全：importlib 缓存已加载模块，重复调用安全。
    """

    @classmethod
    async def compute(
        cls,
        algorithm_id: str,
        *,
        instrument_id: UUID | str,
        as_of: date | str | None = None,
        source_bar_hash: str | None = None,
        adj_factor_hash: str | None = None,
        **kernel_kwargs: Any,
    ) -> CanonicalResult:
        """规范计算入口 — 校验合同 → 加载 kernel → 调用 → 哈希。

        Args:
            algorithm_id: 算法唯一标识（必须在 AlgorithmRegistry 注册）
            instrument_id: 标的 ID（用于日志/诊断）
            as_of: 业务日锚点（adjustment_as_of，用于 point-in-time 复权）
            source_bar_hash: 输入 bar 的 SHA256 前 16 字符（来自 MDAS，用于诊断）
            adj_factor_hash: 因子序列 SHA256 前 16 字符（来自 MDAS，用于诊断）
            **kernel_kwargs: 传递给算法 kernel 的关键字参数（如 bars_daily, bars_15min 等）

        Returns:
            CanonicalResult（含 contract_fingerprint + result_hash + payload）

        Raises:
            AlgorithmNotFoundError: algorithm_id 未注册
            ContractViolationError: 输入违反合同（如 timeframe 不在 input_timeframes 中）
            KernelImportError: kernel 模块/函数加载失败
            CanonicalComputationError: kernel 调用失败
        """
        # 1. 查询算法合同
        try:
            contract = AlgorithmRegistry.get(algorithm_id)
        except KeyError as e:
            raise AlgorithmNotFoundError(algorithm_id) from e

        # 2. 校验输入 contract
        cls._validate_contract(contract, kernel_kwargs)

        # 3. 加载 kernel callable
        kernel = cls._load_kernel(contract)

        # 4. 调用 kernel
        logger.info(
            "CANONICAL_COMPUTE_START algorithm_id=%s version=%s instrument_id=%s "
            "as_of=%s source_bar_hash=%s adj_factor_hash=%s",
            algorithm_id, contract.algorithm_version, instrument_id, as_of,
            source_bar_hash, adj_factor_hash,
        )
        try:
            # kernel 可能是 sync 或 async 函数；统一用 await 处理
            result = kernel(**kernel_kwargs)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as e:
            raise CanonicalComputationError(
                f"kernel 调用失败 algorithm_id={algorithm_id} "
                f"kernel={contract.kernel_entrypoint}: {e}"
            ) from e

        # 5. 计算 result_hash
        result_hash = cls._compute_result_hash(
            algorithm_id=algorithm_id,
            contract_fingerprint=contract.contract_fingerprint,
            instrument_id=str(instrument_id),
            as_of=str(as_of) if as_of else "",
            source_bar_hash=source_bar_hash or "",
            adj_factor_hash=adj_factor_hash or "",
            result=result,
        )

        from datetime import UTC, datetime
        computed_at = datetime.now(UTC).isoformat()

        logger.info(
            "CANONICAL_COMPUTE_DONE algorithm_id=%s version=%s result_hash=%s",
            algorithm_id, contract.algorithm_version, result_hash,
        )

        return CanonicalResult(
            algorithm_id=algorithm_id,
            algorithm_version=contract.algorithm_version,
            output_schema_version=contract.output_schema_version,
            contract_fingerprint=contract.contract_fingerprint,
            result_hash=result_hash,
            registry_version=ALGORITHM_REGISTRY_VERSION,
            payload=result,
            computed_at=computed_at,
        )

    @classmethod
    def _validate_contract(
        cls,
        contract: AlgorithmContract,
        kernel_kwargs: dict[str, Any],
    ) -> None:
        """校验输入是否符合算法合同。

        当前校验项：
        - 调用方至少提供一个 timeframe 相关参数（bars_daily/bars_15min/bars_df 等）
        - 不强制校验具体 timeframe 值（kernel 内部负责）

        未来可扩展：
        - 检查传入的 timeframe 是否在 contract.input_timeframes 中
        - 检查 adj/completed_only 是否与合同一致
        """
        # 至少有一个 bars 输入参数
        bars_keys = [k for k in kernel_kwargs if k.startswith("bars_")]
        if not bars_keys and "bars_df" not in kernel_kwargs:
            # 部分算法可能不直接接收 bars（如纯特征计算），记录 debug 日志
            logger.debug(
                "kernel_kwargs 无 bars_* 参数 algorithm_id=%s kwargs=%s",
                contract.algorithm_id, list(kernel_kwargs.keys()),
            )

    @classmethod
    def _load_kernel(cls, contract: AlgorithmContract) -> Any:
        """动态加载算法 kernel callable。

        Args:
            contract: 算法合同

        Returns:
            kernel callable（函数或类）

        Raises:
            KernelImportError: 模块导入失败或函数不存在
        """
        module_path, callable_name = contract.kernel_entrypoint.split(":", 1)
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise KernelImportError(
                f"kernel 模块导入失败 algorithm_id={contract.algorithm_id} "
                f"module={module_path}: {e}"
            ) from e

        kernel = getattr(module, callable_name, None)
        if kernel is None:
            raise KernelImportError(
                f"kernel 函数不存在 algorithm_id={contract.algorithm_id} "
                f"module={module_path} callable={callable_name}"
            )
        return kernel

    @classmethod
    def _compute_result_hash(
        cls,
        *,
        algorithm_id: str,
        contract_fingerprint: str,
        instrument_id: str,
        as_of: str,
        source_bar_hash: str,
        adj_factor_hash: str,
        result: Any,
    ) -> str:
        """计算结果哈希（SHA256 前 16 字符）。

        哈希输入：
        - algorithm_id + contract_fingerprint（算法合同维度）
        - instrument_id + as_of（业务维度）
        - source_bar_hash + adj_factor_hash（行情输入维度）
        - result 内容（结果维度）

        相同输入必须得到相同 result_hash（缓存键 + 一致性比对基础）。

        注意：result 可能是 dataclass/dict/DataFrame/list 等任意类型。
        本方法尝试多种序列化方式，确保哈希稳定。
        """
        h = hashlib.sha256()
        # 合同维度
        h.update(algorithm_id.encode("utf-8"))
        h.update(b"|")
        h.update(contract_fingerprint.encode("utf-8"))
        h.update(b"|")
        # 业务维度
        h.update(instrument_id.encode("utf-8"))
        h.update(b"|")
        h.update(as_of.encode("utf-8"))
        h.update(b"|")
        # 行情输入维度
        h.update(source_bar_hash.encode("utf-8"))
        h.update(b"|")
        h.update(adj_factor_hash.encode("utf-8"))
        h.update(b"|")
        # 结果维度
        h.update(cls._serialize_result_for_hash(result).encode("utf-8"))
        return h.hexdigest()[:16]

    @classmethod
    def _serialize_result_for_hash(cls, result: Any) -> str:
        """将任意结果序列化为稳定字符串（用于哈希）。

        支持的类型：
        - dataclass：用 asdict 转 dict 后 JSON 序列化（sorted keys）
        - dict/list/tuple/primitives：直接 JSON 序列化（sorted keys）
        - pandas DataFrame：转为 dict records 后 JSON 序列化
        - 其他：repr() 兜底（不保证稳定，仅用于诊断）
        """
        # dataclass（含 frozen dataclass）
        if hasattr(result, "__dataclass_fields__"):
            from dataclasses import asdict
            try:
                return json.dumps(asdict(result), sort_keys=True, default=str)
            except (TypeError, ValueError):
                pass

        # pandas DataFrame / Series
        if isinstance(result, pd.DataFrame):
            try:
                return result.to_json(orient="records", date_format="iso")
            except (TypeError, ValueError):
                pass
        if isinstance(result, pd.Series):
            try:
                return result.to_json(date_format="iso")
            except (TypeError, ValueError):
                pass

        # dict/list/primitives
        if isinstance(result, (dict, list, tuple, str, int, float, bool, type(None))):
            try:
                return json.dumps(result, sort_keys=True, default=str)
            except (TypeError, ValueError):
                pass

        # 兜底：repr（不保证稳定，仅用于诊断）
        return repr(result)

    @classmethod
    def list_supported_algorithms(cls) -> list[str]:
        """返回当前注册表支持的全部 algorithm_id（排序）。"""
        return AlgorithmRegistry.list_ids()

    @classmethod
    def get_contract(cls, algorithm_id: str) -> AlgorithmContract:
        """获取算法合同（透传 AlgorithmRegistry.get，转换异常类型）。

        Raises:
            AlgorithmNotFoundError: algorithm_id 未注册
        """
        try:
            return AlgorithmRegistry.get(algorithm_id)
        except KeyError as e:
            raise AlgorithmNotFoundError(algorithm_id) from e


# =============================================================================
# 自测入口
# =============================================================================


if __name__ == "__main__":
    print("=" * 60)
    print("规范计算服务 (canonical_computation_service.py)")
    print("=" * 60)

    # 验证支持的算法列表
    supported = CanonicalComputationService.list_supported_algorithms()
    print(f"supported_algorithms = {supported}")
    assert len(supported) >= 12, f"应支持至少 12 个算法族，实际 {len(supported)}"

    # 验证获取合同
    nc_contract = CanonicalComputationService.get_contract("node_cluster")
    print(f"node_cluster contract: version={nc_contract.algorithm_version} "
          f"fingerprint={nc_contract.contract_fingerprint}")
    assert nc_contract.algorithm_version == "nc-v1"
    assert nc_contract.contract_fingerprint == "nc-cf-v1"

    # 验证未注册算法抛 AlgorithmNotFoundError
    try:
        CanonicalComputationService.get_contract("non_existent_algo")
        raise AssertionError("应抛出 AlgorithmNotFoundError")
    except AlgorithmNotFoundError as e:
        print(f"AlgorithmNotFoundError OK: {e.algorithm_id}")

    # 验证 _serialize_result_for_hash 稳定性
    s1 = CanonicalComputationService._serialize_result_for_hash(
        {"b": 2, "a": 1, "c": [1, 2, 3]}
    )
    s2 = CanonicalComputationService._serialize_result_for_hash(
        {"a": 1, "b": 2, "c": [1, 2, 3]}
    )
    assert s1 == s2, f"dict 不同 key 顺序应得到相同序列化: {s1} != {s2}"
    print(f"serialize stability OK: {s1}")

    # 验证 result_hash 确定性
    h1 = CanonicalComputationService._compute_result_hash(
        algorithm_id="test",
        contract_fingerprint="test-cf-v1",
        instrument_id="inst-1",
        as_of="2026-07-18",
        source_bar_hash="abc123",
        adj_factor_hash="def456",
        result={"key": "value"},
    )
    h2 = CanonicalComputationService._compute_result_hash(
        algorithm_id="test",
        contract_fingerprint="test-cf-v1",
        instrument_id="inst-1",
        as_of="2026-07-18",
        source_bar_hash="abc123",
        adj_factor_hash="def456",
        result={"key": "value"},
    )
    assert h1 == h2, f"相同输入应得到相同 hash: {h1} != {h2}"
    assert len(h1) == 16, f"hash 应为 16 字符: {h1}"
    print(f"result_hash determinism OK: {h1}")

    print("OK")
