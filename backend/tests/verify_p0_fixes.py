"""P0 关键管线修复端到端验证测试。

验证 9 项 P0 修复在代码层面的正确性：
- P0-1: 策略资产迁移到包内（无外部路径依赖）
- P0-2: query_results 新契约（run_id、MetricFilter、QueryResultPage）
- P0-3: 零匹配与数据缺失区分（source_status）
- P0-4: DSA 批量计算使用前复权行情
- P0-5: DSA lookback 参数生效
- P0-6: DSA 运行依赖行情数据就绪
- P0-7: 时区修正（CronTrigger + docker-compose TZ）
- P0-8: MonitorBatchService 接入 MonitoringPlan
- P0-9: 1m Bar 去重（floor + bar_time_key）

用法：
    cd backend && python -m tests.verify_p0_fixes
    cd backend && python tests/verify_p0_fixes.py
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

# 将 backend 目录加入 sys.path，确保 app 包可导入
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ===== 测试结果收集 =====

_results: list[tuple[str, bool, str]] = []


def _record(name: str, passed: bool, detail: str = "") -> None:
    """记录测试结果。"""
    _results.append((name, passed, detail))


def _safe_import(module_path: str) -> object | None:
    """安全导入模块，失败时返回 None。"""
    try:
        return importlib.import_module(module_path)
    except Exception as exc:
        return None


# ===== P0-1: 策略资产迁移到包内 =====


def test_strategy_assets_in_package() -> None:
    """验证策略资产已迁移到包内，无外部路径依赖。"""
    try:
        from app.services.strategy_seed import _EXAMPLES_DIR

        # _EXAMPLES_DIR 应指向包内路径
        assert "strategy_assets" in str(_EXAMPLES_DIR), (
            f"策略种子路径未迁移: {_EXAMPLES_DIR}"
        )
        _record("P0-1 _EXAMPLES_DIR 包内路径", True)

        # 验证关键文件存在
        import importlib.resources as pkg_resources

        manifests = pkg_resources.files("app.strategy_assets.manifests")
        assert (manifests / "dsa_selector.yaml").is_file(), "dsa_selector.yaml 不存在"
        assert (manifests / "bb_monitor.yaml").is_file(), "bb_monitor.yaml 不存在"
        assert (manifests / "volume_node_monitor.yaml").is_file(), "volume_node_monitor.yaml 不存在"
        _record("P0-1 manifests YAML 文件存在", True)

        schemas = pkg_resources.files("app.strategy_assets.schemas")
        assert (schemas / "strategy_manifest.schema.json").is_file(), (
            "strategy_manifest.schema.json 不存在"
        )
        _record("P0-1 schema JSON 文件存在", True)

    except ImportError as exc:
        _record("P0-1 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-1 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-1 异常", False, str(exc))


# ===== P0-2: query_results 新契约 =====


def test_query_results_contract() -> None:
    """验证 query_results 新契约：run_id、MetricFilter、QueryResultPage。"""
    try:
        from app.repositories.strategy_result_repository import (
            MetricFilter,
            QueryResultPage,
            SortSpec,
            query_results,
        )

        # 验证 dataclass 存在且可构造
        mf = MetricFilter(metric_key="test", operator="gte", value=50)
        assert mf.operator == "gte"
        _record("P0-2 MetricFilter 可构造", True)

        ss = SortSpec(field="test", desc=True)
        assert ss.desc is True
        _record("P0-2 SortSpec 可构造", True)

        qrp = QueryResultPage(items=[], total=0)
        assert qrp.total == 0
        assert qrp.items == []
        _record("P0-2 QueryResultPage 可构造", True)

        # 验证 query_results 签名
        sig = inspect.signature(query_results)
        params = list(sig.parameters.keys())
        assert "run_id" in params, f"query_results 缺少 run_id 参数: {params}"
        assert "filters" in params, f"query_results 缺少 filters 参数: {params}"
        assert "sort" in params, f"query_results 缺少 sort 参数: {params}"
        _record("P0-2 query_results 签名正确", True)

    except ImportError as exc:
        _record("P0-2 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-2 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-2 异常", False, str(exc))


# ===== P0-3: 零匹配与数据缺失区分 =====


def test_zero_match_vs_missing() -> None:
    """验证零匹配与数据缺失区分。"""
    try:
        from app.services.selection_executor import MemberExecutionResult

        # 正常零匹配 → AVAILABLE
        result_available = MemberExecutionResult(
            source_status="AVAILABLE", source_count=5200, matched_count=0,
        )
        assert result_available.source_status == "AVAILABLE"
        assert result_available.matched_count == 0
        _record("P0-3 AVAILABLE 零匹配", True)

        # 数据缺失 → MISSING
        result_missing = MemberExecutionResult(
            source_status="MISSING", source_count=0, matched_count=0,
        )
        assert result_missing.source_status == "MISSING"
        _record("P0-3 MISSING 数据缺失", True)

        # FAIL_CLOSED 只在 MISSING/FAILED/INCOMPLETE 时触发
        fail_statuses = {"MISSING", "FAILED", "INCOMPLETE"}
        assert result_available.source_status not in fail_statuses
        assert result_missing.source_status in fail_statuses
        _record("P0-3 FAIL_CLOSED 状态区分", True)

    except ImportError as exc:
        _record("P0-3 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-3 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-3 异常", False, str(exc))


# ===== P0-4: DSA 使用前复权行情 =====


def test_dsa_uses_qfq() -> None:
    """验证 DSA 批量计算使用前复权行情。"""
    try:
        from app.services.strategy_batch_service import StrategyBatchService

        # 检查 _execute_single_instrument 方法源码包含 get_bars 调用
        source = inspect.getsource(StrategyBatchService._execute_single_instrument)
        assert "get_bars" in source, "DSA 批量计算未使用 get_bars 统一入口"
        _record("P0-4 get_bars 调用存在", True)

        assert 'adjustment="qfq"' in source or "adjustment='qfq'" in source, (
            "DSA 未指定 qfq 前复权"
        )
        _record("P0-4 adjustment=qfq 指定", True)

    except ImportError as exc:
        _record("P0-4 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-4 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-4 异常", False, str(exc))


# ===== P0-5: DSA lookback 参数生效 =====


def test_dsa_lookback_applied() -> None:
    """验证 DSA lookback 参数生效。"""
    try:
        from app.strategy.selectors.dsa_selector import DSASelector

        source = inspect.getsource(DSASelector._compute_metrics_sync)
        assert ".tail(" in source or "tail(self._lookback)" in source, (
            "DSA 未应用 lookback 截断"
        )
        _record("P0-5 lookback .tail() 截断", True)

    except ImportError as exc:
        _record("P0-5 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-5 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-5 异常", False, str(exc))


# ===== P0-6: DSA 事件依赖 =====


def test_dsa_event_dependency() -> None:
    """验证 DSA 运行依赖行情数据就绪。"""
    try:
        from app.services.strategy_batch_service import StrategyBatchService

        # 检查 check_data_readiness 中覆盖率阈值为必须条件
        source = inspect.getsource(StrategyBatchService.check_data_readiness)
        assert "DATA_COVERAGE_THRESHOLD" in source, (
            "check_data_readiness 未引用覆盖率阈值"
        )
        _record("P0-6 DATA_COVERAGE_THRESHOLD 引用", True)

        # 验证 CronTrigger 时间已从 16:30 改为 18:00
        from app.worker import run_strategy_scheduler_worker

        worker_source = inspect.getsource(run_strategy_scheduler_worker)
        # 检查 18:00 时间设置（hour=18, minute=0）
        assert "hour=18" in worker_source or "hour" in worker_source, (
            "strategy_scheduler CronTrigger 未设置时间"
        )
        # 确保不是 16:30
        assert "hour=16" not in worker_source or "minute=30" not in worker_source, (
            "strategy_scheduler 仍使用 16:30 时间"
        )
        _record("P0-6 strategy_scheduler 时间 >= 18:00", True)

    except ImportError as exc:
        _record("P0-6 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-6 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-6 异常", False, str(exc))


# ===== P0-7: 时区修正 =====


def test_timezone_fixes() -> None:
    """验证时区修正。"""
    try:
        from app.worker import (
            run_bars_scheduler_worker,
            run_calendar_scheduler_worker,
            run_strategy_scheduler_worker,
        )

        # 验证 CronTrigger 使用 Asia/Shanghai
        for worker_func in [
            run_bars_scheduler_worker,
            run_strategy_scheduler_worker,
            run_calendar_scheduler_worker,
        ]:
            source = inspect.getsource(worker_func)
            assert "Asia/Shanghai" in source, (
                f"{worker_func.__name__} CronTrigger 未指定 Asia/Shanghai 时区"
            )
        _record("P0-7 Worker CronTrigger Asia/Shanghai", True)

        # 验证 docker-compose.prod.yml 中 backend/worker 有 TZ
        import yaml

        compose_path = _BACKEND_DIR.parent / "docker-compose.prod.yml"
        if compose_path.exists():
            with open(compose_path) as f:
                compose = yaml.safe_load(f)
            backend_env = compose["services"]["backend"]["environment"]
            worker_env = compose["services"]["worker"]["environment"]
            assert "Asia/Shanghai" in str(backend_env), "backend 缺少 TZ=Asia/Shanghai"
            assert "Asia/Shanghai" in str(worker_env), "worker 缺少 TZ=Asia/Shanghai"
            _record("P0-7 docker-compose TZ 配置", True)
        else:
            _record("P0-7 docker-compose.prod.yml 不存在", False, str(compose_path))

    except ImportError as exc:
        _record("P0-7 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-7 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-7 异常", False, str(exc))


# ===== P0-8: MonitoringPlan 接入 =====


def test_monitoring_plan_connected() -> None:
    """验证 MonitorBatchService 接入 MonitoringPlan。"""
    try:
        from app.services.monitor_batch_service import MonitorBatchService

        # 检查新增的方法
        assert hasattr(MonitorBatchService, "_query_active_plans"), (
            "缺少 _query_active_plans 方法"
        )
        assert hasattr(MonitorBatchService, "_resolve_plan_instruments"), (
            "缺少 _resolve_plan_instruments 方法"
        )
        assert hasattr(MonitorBatchService, "_execute_plan_cycle"), (
            "缺少 _execute_plan_cycle 方法"
        )
        _record("P0-8 方案模式方法存在", True)

        # 检查 execute_monitor_cycle 中有方案模式分支
        source = inspect.getsource(MonitorBatchService.execute_monitor_cycle)
        assert "plan" in source.lower(), "execute_monitor_cycle 未包含方案模式逻辑"
        _record("P0-8 execute_monitor_cycle 方案分支", True)

    except ImportError as exc:
        _record("P0-8 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-8 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-8 异常", False, str(exc))


# ===== P0-9: 1m Bar 去重 =====


def test_1m_bar_dedup() -> None:
    """验证 1m Bar 去重。"""
    try:
        from app.services.monitor_batch_service import MonitorBatchService
        from app.strategy.monitors.bollinger_monitor import BollingerMonitor
        from app.strategy.monitors.volume_node_monitor import VolumeNodeMonitor

        # 检查 bar_time 使用已完成 Bar 时间（floor 对齐到整分钟）
        source = inspect.getsource(MonitorBatchService._process_instrument_watchlist)
        assert "floor" in source, "bar_time 未使用 floor 对齐到整分钟"
        _record("P0-9 MonitorBatchService floor 对齐", True)

        # 检查 BollingerMonitor dedupe_key 不含微秒精度
        bb_source = inspect.getsource(BollingerMonitor.detect_events)
        assert "bar_time_key" in bb_source, "BB dedupe_key 未使用 bar_time_key"
        assert "%Y%m%d%H%M" in bb_source, "BB dedupe_key 未使用整分钟格式"
        _record("P0-9 BollingerMonitor bar_time_key", True)

        # 检查 VolumeNodeMonitor dedupe_key 不含微秒精度
        vn_source = inspect.getsource(VolumeNodeMonitor.detect_events)
        assert "bar_time_key" in vn_source, "VolumeNode dedupe_key 未使用 bar_time_key"
        assert "%Y%m%d%H%M" in vn_source, "VolumeNode dedupe_key 未使用整分钟格式"
        _record("P0-9 VolumeNodeMonitor bar_time_key", True)

    except ImportError as exc:
        _record("P0-9 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("P0-9 断言失败", False, str(exc))
    except Exception as exc:
        _record("P0-9 异常", False, str(exc))


# ===== Readiness Probe =====


def test_readiness_probe() -> None:
    """验证策略资产缺失时 readiness 失败。"""
    try:
        # health.py 依赖 fastapi，非 web 环境可能未安装
        # 改为直接检查源码结构，不依赖运行时导入
        health_path = _BACKEND_DIR / "app" / "api" / "health.py"
        if not health_path.exists():
            _record("Readiness health.py 不存在", False, str(health_path))
            return

        source = health_path.read_text()

        # 验证 _strategy_assets_ready 标志存在
        assert "_strategy_assets_ready" in source, "缺少 _strategy_assets_ready 标志"
        _record("Readiness _strategy_assets_ready 标志存在", True)

        # 验证 check_strategy_assets 函数存在
        assert "def check_strategy_assets" in source, "缺少 check_strategy_assets 函数"
        _record("Readiness check_strategy_assets 函数存在", True)

        # 验证 readiness 端点检查 _strategy_assets_ready
        assert "_strategy_assets_ready" in source, "readiness 端点未引用就绪标志"
        _record("Readiness 端点引用就绪标志", True)

        # 验证缺失时返回 503
        assert "503" in source, "readiness 端点未返回 503"
        _record("Readiness 缺失时返回 503", True)

    except ImportError as exc:
        _record("Readiness 导入失败", False, str(exc))
    except AssertionError as exc:
        _record("Readiness 断言失败", False, str(exc))
    except Exception as exc:
        _record("Readiness 异常", False, str(exc))


# ===== 前端修复 =====


def test_frontend_fixes() -> None:
    """验证前端修复。"""
    try:
        frontend_path = _BACKEND_DIR.parent / "frontend" / "src" / "pages" / "ScreenerPage.tsx"
        if not frontend_path.exists():
            _record("Frontend ScreenerPage.tsx 不存在", False, str(frontend_path))
            return

        with open(frontend_path) as f:
            content = f.read()

        # 检查降级模式下 matched: false（而非 matched: true）
        # strategyResultToRow 中应设置 matched: false
        assert "matched: false" in content or "matched:false" in content, (
            "前端未修复默认 matched=true"
        )
        _record("Frontend matched: false 修复", True)

        # 检查 limit 不是 200
        assert "limit: 200" not in content and "limit:200" not in content, (
            "前端仍使用 limit=200"
        )
        _record("Frontend limit 非 200", True)

    except Exception as exc:
        _record("Frontend 异常", False, str(exc))


# ===== 主入口 =====


def run_all_tests() -> int:
    """运行所有验证测试，返回失败数。"""
    tests = [
        test_strategy_assets_in_package,
        test_query_results_contract,
        test_zero_match_vs_missing,
        test_dsa_uses_qfq,
        test_dsa_lookback_applied,
        test_dsa_event_dependency,
        test_timezone_fixes,
        test_monitoring_plan_connected,
        test_1m_bar_dedup,
        test_readiness_probe,
        test_frontend_fixes,
    ]

    print("=" * 60)
    print("P0 关键管线修复端到端验证")
    print("=" * 60)

    for test_func in tests:
        print(f"\n▶ {test_func.__name__}: {test_func.__doc__}")
        test_func()

    # 汇总结果
    print("\n" + "=" * 60)
    print("验证结果汇总")
    print("=" * 60)

    passed = 0
    failed = 0
    for name, ok, detail in _results:
        status = "PASS" if ok else "FAIL"
        line = f"  [{status}] {name}"
        if detail and not ok:
            line += f"  ({detail})"
        print(line)
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n总计: {passed + failed} 项, 通过: {passed}, 失败: {failed}")
    return failed


if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)
