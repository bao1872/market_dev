"""指标参数基线一致性测试。

验证代码、manifest 中的参数与 app.constants.indicator_contract 基线一致。
任何不一致均导致测试失败。
"""
import ast
import inspect
from pathlib import Path

import yaml

from app.constants import indicator_contract

# manifest 文件目录（backend/app/strategy_assets/manifests/）
_MANIFESTS_DIR = Path(__file__).parent.parent / "app" / "strategy_assets" / "manifests"

# backend/app/ 生产代码根目录（AST 扫描范围）
_APP_DIR = Path(__file__).parent.parent / "app"

# 受控参数清单：禁止在 indicator_contract.py 之外出现字面量 250/4000/600 作为参数赋值
# 字面量 → 对应受控参数名映射（用于错误信息定位）
_CONTROLLED_PARAMS = {
    "DAILY_HISTORY_BARS": 250,
    "NODE_CLUSTER_LOW_BARS": 4000,
    "NODE_CLUSTER_EVENT_TTL_SECONDS": 600,
}


# ===== Volume Profile 参数 =====


def test_unified_volume_profile_vp_lookback_matches_baseline():
    """VP_LOOKBACK 应与基线 NODE_CLUSTER_PRIMARY_BARS 一致。"""
    from app.strategy_assets.algorithms.features.unified_volume_profile import (
        VP_LOOKBACK,
    )

    assert VP_LOOKBACK == indicator_contract.NODE_CLUSTER_PRIMARY_BARS


# ===== monitor_batch_service 行情回看参数（旧值应已迁移至基线）=====


def test_monitor_batch_service_no_daily_fetch_days_370():
    """旧值 _DAILY_FETCH_DAYS=370 应已删除，改用 MarketDataAggregationService 统一入口。"""
    from app.services import monitor_batch_service

    source = inspect.getsource(monitor_batch_service)
    # 旧自然日估算常量不得残留
    assert "_DAILY_FETCH_DAYS = 370" not in source
    assert "_DAILY_FETCH_DAYS" not in source
    # 新模式：统一走 MarketDataAggregationService 并按根数 tail(N)
    assert "MarketDataAggregationService" in source
    assert "_fetch_md_bars" in source


def test_monitor_batch_service_no_15min_fetch_days_200():
    """旧值 _15MIN_FETCH_DAYS=200 应已删除，改用 MarketDataAggregationService 统一入口。"""
    from app.services import monitor_batch_service

    source = inspect.getsource(monitor_batch_service)
    # 旧自然日估算常量不得残留
    assert "_15MIN_FETCH_DAYS = 200" not in source
    assert "_15MIN_FETCH_DAYS" not in source
    # 新模式：统一走 MarketDataAggregationService 并按根数 tail(N)
    assert "MarketDataAggregationService" in source
    assert "_fetch_md_bars" in source


# ===== watchlist_monitor.yaml 事件 TTL =====


def test_watchlist_monitor_no_state_ttl_seconds_in_yaml():
    """watchlist_monitor.yaml 中 event_types 不得残留 state_ttl_seconds 字面量。

    SubTask 1.6 已删除 4 处 state_ttl_seconds: 600 字面量；
    TTL 由 VolumeNodeMonitor 运行时从 indicator_contract.NODE_CLUSTER_EVENT_TTL_SECONDS 注入。
    """
    yaml_path = _MANIFESTS_DIR / "watchlist_monitor.yaml"
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    event_types = data.get("event_types", [])
    for evt in event_types:
        assert "state_ttl_seconds" not in evt, (
            f"event_type {evt.get('key')} 仍残留 state_ttl_seconds 字面量，应已删除"
        )


def test_volume_node_monitor_injects_ttl_from_indicator_contract():
    """VolumeNodeMonitor 运行时从 indicator_contract.NODE_CLUSTER_EVENT_TTL_SECONDS 注入 TTL。"""
    from app.strategy.monitors import volume_node_monitor

    source = inspect.getsource(volume_node_monitor)
    # 必须从 indicator_contract 导入 NODE_CLUSTER_EVENT_TTL_SECONDS
    assert "from app.constants.indicator_contract import" in source
    assert "NODE_CLUSTER_EVENT_TTL_SECONDS" in source
    # 运行时使用此常量作为 EVENT_STATE_TTL_SECONDS
    assert "EVENT_STATE_TTL_SECONDS = NODE_CLUSTER_EVENT_TTL_SECONDS" in source


# ===== dsa_selector.yaml lookback =====


def test_dsa_selector_yaml_lookback_matches_baseline():
    """dsa_selector.yaml 中 algorithm.lookback 默认值应与基线 DSA_LOOKBACK 一致。"""
    yaml_path = _MANIFESTS_DIR / "dsa_selector.yaml"
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    params = {p["key"]: p for p in data["parameters"]}
    assert params["algorithm.lookback"]["default"] == indicator_contract.DSA_LOOKBACK


# ===== indicator_service 各周期根数 =====


def test_indicator_service_daily_bars_matches_baseline():
    """INDICATOR_BARS['1d'] 应与基线 indicator_contract.INDICATOR_BARS['1d'] 一致。"""
    from app.services.indicator_service import INDICATOR_BARS

    assert INDICATOR_BARS["1d"] == indicator_contract.INDICATOR_BARS["1d"]


def test_indicator_service_15min_bars_matches_baseline():
    """INDICATOR_BARS['15m'] 应与基线 indicator_contract.INDICATOR_BARS['15m'] 一致。"""
    from app.services.indicator_service import INDICATOR_BARS

    assert INDICATOR_BARS["15m"] == indicator_contract.INDICATOR_BARS["15m"]


# ===== dsa_selector.py 旧常量清理 =====


def test_dsa_selector_no_default_lookback_360():
    """旧值 DEFAULT_LOOKBACK=360 应已迁移至基线，源码中不得残留。"""
    from app.strategy.selectors import dsa_selector

    source = inspect.getsource(dsa_selector)
    assert "DEFAULT_LOOKBACK = 360" not in source


# ===== budget.py 旧 manifest 引用清理 =====


def test_budget_no_volume_node_monitor_yaml_reference():
    """budget.py 不得引用已废弃的 volume_node_monitor.yaml。"""
    from app.strategy import budget

    source = inspect.getsource(budget)
    assert "volume_node_monitor.yaml" not in source


# ===== advice.md v6 第4条：Node Cluster 参数固化 =====
# 目标：所有 Node Cluster 参数由 indicator_contract 唯一真源控制，
# 禁止从 manifest 覆盖、禁止第二套硬编码定义。


def test_volume_node_monitor_no_manifest_lookback():
    """VolumeNodeMonitor 源码不得从 manifest 读取 algorithm.lookback。

    lookback 应由 indicator_contract.VP_LOOKBACK（经 unified_volume_profile）控制，
    initialize() 不再从 manifest parameters 提取该参数。
    """
    from app.strategy.monitors import volume_node_monitor

    source = inspect.getsource(volume_node_monitor)
    assert "algorithm.lookback" not in source


def test_volume_node_monitor_ttl_from_indicator_contract():
    """EVENT_STATE_TTL_SECONDS 应从 indicator_contract 导入，不再硬编码 600。"""
    from app.strategy.monitors import volume_node_monitor

    source = inspect.getsource(volume_node_monitor)
    assert "from app.constants.indicator_contract import" in source
    assert "NODE_CLUSTER_EVENT_TTL_SECONDS" in source
    # 硬编码 600 必须移除
    assert "EVENT_STATE_TTL_SECONDS = 600" not in source


def test_watchlist_monitor_yaml_no_algorithm_lookback():
    """watchlist_monitor.yaml 不得包含 algorithm.lookback 参数入口。

    Node Cluster lookback 由 indicator_contract 唯一控制，
    manifest 不再暴露该可编辑入口（BB 参数入口保留）。
    """
    yaml_path = _MANIFESTS_DIR / "watchlist_monitor.yaml"
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    params = data.get("parameters", [])
    keys = [p.get("key") for p in params]
    assert "algorithm.lookback" not in keys


def test_luxalgo_dataclass_defaults_from_indicator_contract():
    """luxalgo VolumeProfileConfig 默认值应从 indicator_contract 导入。

    dataclass 默认值不得硬编码 360（与基线 250 不一致），
    应引用 NODE_CLUSTER_PRIMARY_BARS / VP_LOOKBACK 等 indicator_contract 常量。
    """
    luxalgo_path = (
        Path(__file__).parent.parent
        / "app" / "strategy_assets" / "algorithms" / "features"
        / "luxalgo_volume_profile_pytdx_15m_aligned.py"
    )
    source = luxalgo_path.read_text(encoding="utf-8")
    # 源码应引用 indicator_contract 常量
    assert (
        "from app.constants.indicator_contract import" in source
        or "NODE_CLUSTER_PRIMARY_BARS" in source
        or "VP_LOOKBACK" in source
    )
    # dataclass 默认值不再硬编码 360
    assert "profile_lookback_length: int = 360" not in source


def test_verify_monitor_alignment_uses_indicator_contract():
    """verify_monitor_alignment.py 应从 indicator_contract 导入参数。

    VPConfig 构造不得硬编码，应使用 indicator_contract 唯一真源。
    """
    verify_path = (
        Path(__file__).parent.parent / "scripts" / "verify_monitor_alignment.py"
    )
    source = verify_path.read_text(encoding="utf-8")
    assert (
        "from app.constants.indicator_contract import" in source
        or "import indicator_contract" in source
    )


def test_pavp_tv_marked_as_independent_tool():
    """pavp_tv_fixed_params_factors.py 应标注为独立工具。

    该文件参数与 indicator_contract 不一致属预期（用于 TradingView 截图复现），
    必须在文件头注释中明确标注，避免误用为生产链路。

    文件已移至 tools/research/ 目录（SubTask 1.7），不在 backend/app/ 生产代码中。
    """
    # 先检查移动后的位置（tools/research/）
    pavp_path_moved = (
        Path(__file__).parent.parent.parent / "tools" / "research"
        / "pavp_tv_fixed_params_factors.py"
    )
    # 兼容旧位置（移动前的状态，便于回滚验证）
    pavp_path_old = (
        Path(__file__).parent.parent
        / "app" / "strategy_assets" / "algorithms" / "features"
        / "pavp_tv_fixed_params_factors.py"
    )
    pavp_path = pavp_path_moved if pavp_path_moved.exists() else pavp_path_old
    assert pavp_path.exists(), (
        f"pavp_tv_fixed_params_factors.py 不存在于 {pavp_path_moved} 或 {pavp_path_old}"
    )
    source = pavp_path.read_text(encoding="utf-8")
    # 接受 "独立工具" 或 "独立研究工具" 两种措辞（均表明非生产链路）
    assert "独立" in source or "independent tool" in source.lower()


# ===== advice.md v6 第4条：受控参数禁止散落硬编码（AST 一致性守门测试）=====


# 已知例外白名单：以下 (相对路径, 变量名, 字面量) 三元组属于"语义不同的同值用法"，
# 不属于"受控参数第二套定义"。每条必须给出明确的语义差异说明。
# 维护规则：新增条目必须在 PR 中说明"为何不属于受控参数同语义"。
_KNOWN_SEMANTIC_DIFFERENCES: set[tuple[str, str, int]] = {
    # 60min bar 新鲜度 SLA 秒数（3600 秒 = 1 小时），与 NODE_CLUSTER_LOW_BARS=4000（15m bar 根数）语义不同
    ("app/services/freshness_sla.py", "BAR_60MIN_SLA_SECONDS", 3600),
    # 事件基类默认 state_ttl_seconds（3600 秒），用于 stage/sr 等通用事件，与 Node Cluster bar 根数语义不同
    ("app/strategy/events/base.py", "state_ttl_seconds", 3600),
    # 事件注册表默认 TTL（3600 秒），同上
    ("app/strategy/events/registry.py", "DEFAULT_STATE_TTL_SECONDS", 3600),
    # 截图缓存 TTL（600 秒），与 NODE_CLUSTER_EVENT_TTL_SECONDS=600 语义不同（截图缓存 vs 事件状态过期）
    ("app/services/stock_capture_service.py", "_CACHE_TTL_SECONDS", 600),
    # 事件写入去重冷却（600 秒）：同一 instrument+event_type+boundary 在窗口内不重复写入，
    # 与 NODE_CLUSTER_EVENT_TTL_SECONDS=600（状态过期）业务规则不同（去重 vs 过期）
    ("app/services/monitor_batch_service.py", "_EVENT_COOLDOWN_SECONDS", 600),
    # BB 通知冷却（600 秒）：Bollinger 通知去重窗口，与 Node Cluster 事件 TTL 业务规则不同
    ("app/strategy/monitors/bollinger_monitor.py", "NOTIFY_COOLDOWN_SECONDS", 600),
    # 60min bar 回补/对账数量上限（4000 条，覆盖 2023-01-01 至今约 3500 条），
    # 与 NODE_CLUSTER_LOW_BARS=4000（15m bar 根数）语义不同（60min 回补数量 vs 15m 输入根数）
    ("app/services/reconcile_bars.py", "_60MIN_COUNT_LIMIT", 4000),
}

# 字典字面量已知例外：以下 (相对路径, 字面量值) 二元组属于"语义不同的同值用法"，
# 不属于"受控参数第二套定义"。每条必须给出明确的语义差异说明。
_KNOWN_DICT_LITERAL_EXCEPTIONS: set[tuple[str, int]] = {
    # bars_scheduler_service.py BACKFILL_COUNTS["60m"]=4000：60min bar 回补数量上限，
    # 与 NODE_CLUSTER_LOW_BARS=4000（15m bar 根数）语义不同（60min 回补 vs 15m Node 输入）
    ("app/services/bars_scheduler_service.py", 4000),
}


def test_no_duplicate_controlled_params():
    """扫描 backend/app/ 所有 .py 文件，禁止第二套受控参数定义。

    受控参数清单（_CONTROLLED_PARAMS）：
        - DAILY_HISTORY_BARS (=250)
        - NODE_CLUSTER_LOW_BARS (=4000)
        - NODE_CLUSTER_EVENT_TTL_SECONDS (=600)

    规则：
        - 允许在 indicator_contract.py 中定义（唯一真源）
        - 允许从 indicator_contract 导入后引用（如 indicator_contract.NODE_CLUSTER_LOW_BARS）
        - 禁止其它文件用字面量 250/3600/600 作为"参数赋值"形成第二套定义
        - 例外：注释、字符串、非参数赋值（如数组索引/比较/算术）不报错
        - 例外：_KNOWN_SEMANTIC_DIFFERENCES 白名单（语义不同的同值用法）

    判定"参数赋值"的标准（AST 视角）：
        - ast.Assign 或 ast.AnnAssign 中，target 为 Name 节点且 value 为常量
          且字面量 ∈ {250, 3600, 600}
        - 字典字面量 ast.Dict 中 value 为常量且字面量 ∈ {250, 3600, 600}
          （避免 INDICATOR_BARS = {"1d": 250} 这种第二套定义）
    """
    # indicator_contract.py 文件名（允许定义受控参数的唯一真源）
    contract_filename = "indicator_contract.py"
    # 受控字面量集合
    controlled_literals = set(_CONTROLLED_PARAMS.values())

    violations: list[str] = []

    for py_file in sorted(_APP_DIR.rglob("*.py")):
        rel_path = py_file.relative_to(_APP_DIR.parent)
        # 唯一真源文件允许定义
        if py_file.name == contract_filename and "constants" in str(rel_path):
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError as exc:
            violations.append(f"{rel_path}: 语法错误无法解析: {exc}")
            continue

        for node in ast.walk(tree):
            # 1) 普通赋值: x = 250 / x: int = 250
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and isinstance(node.value, ast.Constant):
                        if isinstance(node.value.value, int) and node.value.value in controlled_literals:
                            if (str(rel_path), tgt.id, node.value.value) in _KNOWN_SEMANTIC_DIFFERENCES:
                                continue
                            violations.append(
                                f"{rel_path}:{node.lineno} 受控字面量 {node.value.value} "
                                f"赋值给变量 '{tgt.id}'（应从 indicator_contract 导入）"
                            )
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, int)
                    and node.value.value in controlled_literals
                ):
                    if (str(rel_path), node.target.id, node.value.value) in _KNOWN_SEMANTIC_DIFFERENCES:
                        continue
                    violations.append(
                        f"{rel_path}:{node.lineno} 受控字面量 {node.value.value} "
                        f"赋值给变量 '{node.target.id}'（应从 indicator_contract 导入）"
                    )
            # 2) 字典字面量: {"1d": 250} 这种第二套定义也禁止
            elif isinstance(node, ast.Dict):
                for val in node.values:
                    if (
                        isinstance(val, ast.Constant)
                        and isinstance(val.value, int)
                        and val.value in controlled_literals
                    ):
                        if (str(rel_path), val.value) in _KNOWN_DICT_LITERAL_EXCEPTIONS:
                            continue
                        violations.append(
                            f"{rel_path}:{node.lineno} 字典字面量中含受控字面量 {val.value} "
                            f"（应从 indicator_contract 导入）"
                        )

    assert not violations, (
        "发现第二套受控参数定义（应从 indicator_contract 唯一真源导入）：\n  - "
        + "\n  - ".join(violations)
    )
