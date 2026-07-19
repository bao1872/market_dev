"""算法合同注册表架构守护测试（CHANGE-20260718-006 Section 2 + CHANGE-20260718-007 S3.2）。

验证 AlgorithmRegistry 是所有算法族合同的唯一注册真源，CanonicalComputationService
是统一计算入口：

1. 注册表完整性：
   - 所有 algorithm_id 唯一（dataclass frozen + dict key 已保证，AST 校验入口）
   - 所有 kernel_entrypoint 唯一（一个 kernel 只能服务一个算法族）
   - 所有 contract_fingerprint 唯一（指纹冲突会导致缓存键碰撞）
   - 至少 12 个算法族已注册（DSA/Swing/SMC/BB/SQZMOM/MACD/Breakout/Participation/
     Temporal/Structural/Primary-Secondary/Snapshot-Derived）
2. 注册表唯一性：
   - 只有 algorithm_registry.py 调用 AlgorithmRegistry.register（生产代码）
   - 其他模块禁止调用 AlgorithmRegistry.register（避免运行时动态注册绕过审计）
3. CanonicalComputationService 入口：
   - 注册表 + 服务模块本身不受约束
   - 生产模块应通过 CanonicalComputationService 调用算法（当前四链均未迁移，
     migration_status 字段诚实记录每个算法的接线状态；新算法必须经 compute_with_mdas
     验证后方可标 input_provider_wired）
4. S3.2 migration_status 守护：
   - 每个算法都有合法 migration_status（registered_only | input_provider_wired）
   - input_provider_wired 算法的 kernel_entrypoint callable 必须真实存在
     （防止 7-broken-entrypoints 类 bug：注册表指向不存在的函数）

覆盖范围：app/ 下全部生产 .py 文件（排除 contracts/algorithm_registry.py 自身与
services/canonical_computation_service.py）。
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_APP_DIR = Path(__file__).parent.parent / "app"

# 注册表与服务模块本身不受约束
_REGISTRY_MODULE = "contracts/algorithm_registry.py"
_CANONICAL_SERVICE_MODULE = "services/canonical_computation_service.py"
_ALLOWED_MODULES = {_REGISTRY_MODULE, _CANONICAL_SERVICE_MODULE}

# 预期算法族清单（与 algorithm_registry.py _register_builtin_algorithms 一致）
_EXPECTED_ALGORITHM_IDS = {
    "node_cluster",
    "dsa",
    "smc",
    "bollinger",
    "macd",
    "sqzmom",
    "breakout",
    "participation",
    "temporal_features",
    "structural_features",
    "primary_secondary_relation",
    "snapshot_derived_features",
}


def _iter_production_files() -> list[tuple[Path, str]]:
    """枚举 app/ 下所有生产 .py 文件（排除允许集），返回 (path, rel) 列表。"""
    result: list[tuple[Path, str]] = []
    for p in sorted(_APP_DIR.rglob("*.py")):
        rel = p.relative_to(_APP_DIR).as_posix()
        if rel in _ALLOWED_MODULES:
            continue
        if "__pycache__" in rel:
            continue
        result.append((p, rel))
    return result


def _has_call_to(tree: ast.AST, qualifier: str, method: str) -> bool:
    """检测是否存在 `<qualifier>.<method>(...)` 调用。

    qualifier 可以是类名（如 AlgorithmRegistry）或变量名。
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == qualifier
        ):
            return True
    return False


# =============================================================================
# 注册表完整性测试
# =============================================================================


class TestAlgorithmRegistryIntegrity:
    """注册表完整性测试 — 验证 12+ 算法族合同已注册且唯一。"""

    def test_all_expected_algorithms_registered(self) -> None:
        """所有预期算法族必须已注册。"""
        from app.contracts.algorithm_registry import AlgorithmRegistry

        registered = set(AlgorithmRegistry.list_ids())
        missing = _EXPECTED_ALGORITHM_IDS - registered
        assert not missing, f"未注册的算法族: {sorted(missing)}"

    def test_at_least_12_algorithms_registered(self) -> None:
        """至少 12 个算法族已注册（覆盖 DSA/Swing/SMC/BB/SQZMOM/MACD 等）。"""
        from app.contracts.algorithm_registry import AlgorithmRegistry

        ids = AlgorithmRegistry.list_ids()
        assert len(ids) >= 12, f"应至少注册 12 个算法族，实际 {len(ids)}: {ids}"

    def test_algorithm_ids_unique(self) -> None:
        """所有 algorithm_id 唯一（dict key 已保证，显式校验）。"""
        from app.contracts.algorithm_registry import AlgorithmRegistry

        ids = AlgorithmRegistry.list_ids()
        assert len(ids) == len(set(ids)), f"algorithm_id 重复: {ids}"

    def test_kernel_entrypoints_unique(self) -> None:
        """所有 kernel_entrypoint 唯一 — 一个 kernel 只能服务一个算法族。

        如果两个算法族共享同一 kernel_entrypoint，会导致缓存键碰撞和调度歧义。
        例外：允许同一 module 下不同 callable 服务不同算法族（如 indicator_service
        下的 compute_macd 和 compute_bollinger_bands）。
        """
        from app.contracts.algorithm_registry import AlgorithmRegistry

        entrypoints = [c.kernel_entrypoint for c in AlgorithmRegistry.list_all()]
        assert len(entrypoints) == len(set(entrypoints)), (
            f"kernel_entrypoint 重复: {entrypoints}"
        )

    def test_contract_fingerprints_unique(self) -> None:
        """所有 contract_fingerprint 唯一 — 指纹冲突会导致缓存键碰撞。"""
        from app.contracts.algorithm_registry import AlgorithmRegistry

        fingerprints = [
            c.contract_fingerprint for c in AlgorithmRegistry.list_all()
        ]
        assert len(fingerprints) == len(set(fingerprints)), (
            f"contract_fingerprint 重复: {fingerprints}"
        )

    def test_kernel_module_consistent_with_entrypoint(self) -> None:
        """每个合同的 kernel_module 必须与 kernel_entrypoint 的 module 部分一致。

        AlgorithmContract.__post_init__ 已校验，此处为冗余守护。
        """
        from app.contracts.algorithm_registry import AlgorithmRegistry

        for contract in AlgorithmRegistry.list_all():
            entrypoint_module = contract.kernel_entrypoint.split(":", 1)[0]
            assert entrypoint_module == contract.kernel_module, (
                f"algorithm_id={contract.algorithm_id} kernel_module="
                f"{contract.kernel_module!r} 与 entrypoint module="
                f"{entrypoint_module!r} 不一致"
            )

    def test_kernel_modules_importable(self) -> None:
        """所有 kernel_module 必须可导入（避免注册表指向不存在的模块）。

        注意：本测试只验证模块可导入，不验证 callable 是否存在（部分 callable
        可能是类名而非函数名，且实例化需要参数）。
        """
        from app.contracts.algorithm_registry import AlgorithmRegistry

        failed: list[str] = []
        for contract in AlgorithmRegistry.list_all():
            try:
                importlib.import_module(contract.kernel_module)
            except ImportError as e:
                failed.append(
                    f"algorithm_id={contract.algorithm_id} module="
                    f"{contract.kernel_module}: {e}"
                )
        assert not failed, "kernel_module 导入失败:\n" + "\n".join(failed)

    def test_node_cluster_contract_matches_semantics(self) -> None:
        """node_cluster 合同必须与 indicator_semantics 常量一致。"""
        from app.contracts.algorithm_registry import AlgorithmRegistry
        from app.contracts.indicator_semantics import (
            NODE_CLUSTER_ALGORITHM_VERSION,
            NODE_CLUSTER_CONTRACT_FINGERPRINT,
            NODE_CLUSTER_OUTPUT_SCHEMA_VERSION,
        )

        nc = AlgorithmRegistry.get("node_cluster")
        assert nc.algorithm_version == NODE_CLUSTER_ALGORITHM_VERSION, (
            f"node_cluster.algorithm_version={nc.algorithm_version!r} "
            f"!= semantics={NODE_CLUSTER_ALGORITHM_VERSION!r}"
        )
        assert nc.contract_fingerprint == NODE_CLUSTER_CONTRACT_FINGERPRINT
        assert nc.output_schema_version == NODE_CLUSTER_OUTPUT_SCHEMA_VERSION

    def test_registry_version_constant_exists(self) -> None:
        """注册表版本常量必须存在且非空。"""
        from app.contracts.algorithm_registry import ALGORITHM_REGISTRY_VERSION

        assert ALGORITHM_REGISTRY_VERSION
        assert isinstance(ALGORITHM_REGISTRY_VERSION, str)


# =============================================================================
# 注册表调用约束测试
# =============================================================================


class TestAlgorithmRegistryCallConstraint:
    """注册表调用约束测试 — 只有 algorithm_registry.py 可以调用 register。"""

    def test_only_registry_module_calls_register(self) -> None:
        """生产模块禁止调用 AlgorithmRegistry.register（避免运行时动态注册）。

        例外：contracts/algorithm_registry.py 自身（注册内置算法）。
        """
        violations: list[str] = []
        for p, rel in _iter_production_files():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            if _has_call_to(tree, "AlgorithmRegistry", "register"):
                violations.append(rel)
        assert not violations, (
            "违反注册表唯一性：以下模块调用了 AlgorithmRegistry.register（只有 "
            "contracts/algorithm_registry.py 可以注册内置算法）:\n"
            + "\n".join(violations)
        )


# =============================================================================
# CanonicalComputationService 接口测试
# =============================================================================


class TestCanonicalComputationServiceInterface:
    """CanonicalComputationService 接口测试 — 验证调度器行为。"""

    def test_list_supported_algorithms_matches_registry(self) -> None:
        """list_supported_algorithms 必须与注册表 list_ids 一致。"""
        from app.contracts.algorithm_registry import AlgorithmRegistry
        from app.services.canonical_computation_service import (
            CanonicalComputationService,
        )

        assert (
            CanonicalComputationService.list_supported_algorithms()
            == AlgorithmRegistry.list_ids()
        )

    def test_get_contract_returns_registry_contract(self) -> None:
        """get_contract 必须返回与 AlgorithmRegistry.get 相同的合同。"""
        from app.contracts.algorithm_registry import AlgorithmRegistry
        from app.services.canonical_computation_service import (
            CanonicalComputationService,
        )

        for algorithm_id in ("node_cluster", "dsa", "smc", "bollinger"):
            contract = CanonicalComputationService.get_contract(algorithm_id)
            assert contract == AlgorithmRegistry.get(algorithm_id)

    def test_get_contract_raises_for_unknown_algorithm(self) -> None:
        """get_contract 对未注册算法抛 AlgorithmNotFoundError。"""
        from app.services.canonical_computation_service import (
            AlgorithmNotFoundError,
            CanonicalComputationService,
        )

        with pytest.raises(AlgorithmNotFoundError):
            CanonicalComputationService.get_contract("definitely_not_registered_algo")

    def test_result_hash_deterministic(self) -> None:
        """相同输入必须得到相同 result_hash（缓存键 + 一致性比对基础）。"""
        from app.services.canonical_computation_service import (
            CanonicalComputationService,
        )

        kwargs = {
            "algorithm_id": "test_algo",
            "contract_fingerprint": "test-cf-v1",
            "instrument_id": "inst-001",
            "as_of": "2026-07-18",
            "source_bar_hash": "src-hash-abc",
            "adj_factor_hash": "adj-hash-def",
            "result": {"a": 1, "b": [1, 2, 3], "c": None},
        }
        h1 = CanonicalComputationService._compute_result_hash(**kwargs)
        h2 = CanonicalComputationService._compute_result_hash(**kwargs)
        assert h1 == h2, f"相同输入应得到相同 hash: {h1} != {h2}"
        assert len(h1) == 16, f"hash 应为 16 字符: {h1}"

    def test_result_hash_changes_on_different_input(self) -> None:
        """不同输入必须得到不同 result_hash（避免缓存键碰撞）。"""
        from app.services.canonical_computation_service import (
            CanonicalComputationService,
        )

        base = {
            "algorithm_id": "test_algo",
            "contract_fingerprint": "test-cf-v1",
            "instrument_id": "inst-001",
            "as_of": "2026-07-18",
            "source_bar_hash": "src-hash-abc",
            "adj_factor_hash": "adj-hash-def",
            "result": {"a": 1},
        }
        h_base = CanonicalComputationService._compute_result_hash(**base)

        # 改 algorithm_id
        diff = dict(base, algorithm_id="other_algo")
        assert CanonicalComputationService._compute_result_hash(**diff) != h_base

        # 改 result
        diff = dict(base, result={"a": 2})
        assert CanonicalComputationService._compute_result_hash(**diff) != h_base

        # 改 source_bar_hash
        diff = dict(base, source_bar_hash="other-hash")
        assert CanonicalComputationService._compute_result_hash(**diff) != h_base

    def test_serialize_result_stable_for_dict_key_order(self) -> None:
        """dict 不同 key 顺序应得到相同序列化（JSON sort_keys=True）。"""
        from app.services.canonical_computation_service import (
            CanonicalComputationService,
        )

        s1 = CanonicalComputationService._serialize_result_for_hash(
            {"z": 1, "a": 2, "m": [3, 2, 1]}
        )
        s2 = CanonicalComputationService._serialize_result_for_hash(
            {"a": 2, "m": [3, 2, 1], "z": 1}
        )
        assert s1 == s2, f"dict 不同 key 顺序应得到相同序列化: {s1} != {s2}"


# =============================================================================
# S3.2: migration_status 守护测试（CHANGE-20260718-007）
# =============================================================================


class TestMigrationStatusGuard:
    """S3.2 migration_status 守护 — 诚实记录算法接线状态 + callable 存在性校验。"""

    def test_migration_status_documented_for_all(self) -> None:
        """每个算法都有合法 migration_status（registered_only | input_provider_wired）。

        AlgorithmContract.__post_init__ 已校验合法值，此处为冗余守护 +
        打印当前迁移状态摘要用于审计。
        """
        from app.contracts.algorithm_registry import AlgorithmRegistry

        valid_statuses = {"registered_only", "input_provider_wired"}
        all_contracts = AlgorithmRegistry.list_all()
        for contract in all_contracts:
            assert contract.migration_status in valid_statuses, (
                f"algorithm_id={contract.algorithm_id} migration_status="
                f"{contract.migration_status!r} 不合法"
            )
        # 摘要：wired vs registered_only
        wired = [c.algorithm_id for c in all_contracts if c.migration_status == "input_provider_wired"]
        registered = [c.algorithm_id for c in all_contracts if c.migration_status == "registered_only"]
        # 至少 1 个 wired（macd），其余 registered_only
        assert len(wired) >= 1, f"应至少 1 个 input_provider_wired，实际 0。registered={registered}"
        assert len(wired) + len(registered) == len(all_contracts)

    def test_wired_algorithms_have_existing_callables(self) -> None:
        """input_provider_wired 算法的 kernel_entrypoint callable 必须真实存在。

        防止 CHANGE-20260718-007 S3.2 发现的 7-broken-entrypoints 类 bug：
        注册表指向不存在的函数（如 compute_smc_dto/compute_bollinger_bands 等），
        但架构测试只校验模块可导入，不校验 callable 存在 — 导致 bug 从未被发现。

        本测试对每个 input_provider_wired 算法：
        1. 导入 kernel_module（验证模块存在）
        2. getattr 检查 callable 存在（验证函数/类名正确）
        """
        from app.contracts.algorithm_registry import AlgorithmRegistry

        wired = [
            c for c in AlgorithmRegistry.list_all()
            if c.migration_status == "input_provider_wired"
        ]
        assert wired, "应至少有 1 个 input_provider_wired 算法"

        failures: list[str] = []
        for contract in wired:
            # 1. 模块可导入
            try:
                module = importlib.import_module(contract.kernel_module)
            except ImportError as e:
                failures.append(
                    f"algorithm_id={contract.algorithm_id} module="
                    f"{contract.kernel_module} 导入失败: {e}"
                )
                continue
            # 2. callable 存在
            callable_name = contract.kernel_entrypoint.split(":", 1)[1]
            kernel = getattr(module, callable_name, None)
            if kernel is None:
                failures.append(
                    f"algorithm_id={contract.algorithm_id} callable="
                    f"{contract.kernel_entrypoint} 不存在"
                    f"（module={contract.kernel_module} 中无 {callable_name}）"
                )
        assert not failures, (
            "input_provider_wired 算法 callable 不存在（违反接线契约）:\n"
            + "\n".join(failures)
        )

    def test_registered_only_algorithms_need_not_have_callables(self) -> None:
        """registered_only 算法允许 callable 不存在（诚实记录未接线状态）。

        这是设计意图：registered_only 意味着"合同已登记但未接线统一 adapter"，
        callable 可能不存在（如 compute_smc_dto）或签名未适配。
        本测试验证 migration_status 与 callable 存在性的一致性：
        - input_provider_wired → callable 必须存在（上一个测试）
        - registered_only → callable 可能存在也可能不存在（本测试只记录，不报错）
        """
        from app.contracts.algorithm_registry import AlgorithmRegistry

        registered = [
            c for c in AlgorithmRegistry.list_all()
            if c.migration_status == "registered_only"
        ]
        # 记录哪些 registered_only 的 callable 存在/不存在（用于审计，不报错）
        exists_count = 0
        missing_count = 0
        for contract in registered:
            try:
                module = importlib.import_module(contract.kernel_module)
                callable_name = contract.kernel_entrypoint.split(":", 1)[1]
                if getattr(module, callable_name, None) is not None:
                    exists_count += 1
                else:
                    missing_count += 1
            except ImportError:
                missing_count += 1
        # 至少有一些 registered_only（11 个），其中 macd 已 wired 所以 registered_only 应为 11
        assert len(registered) >= 10, (
            f"registered_only 算法数应 >= 10，实际 {len(registered)}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
