"""PytdxAdapter 连接诊断与 managed reconnect 测试（P1-P3）。

覆盖 PytdxAdapter 已实现的行为：
- P1 connect() 幂等：已连接后重复调用 NO-OP，successful_connect_count == 1（首次连接成功）。
- P2 healthy path：同一 adapter 连续 N 次 get_history_transaction_page（mock 返回），
  无任何重连，successful_connect_count == 1，reconnect_count == 0。
- P3 失败路径：get_history_transaction_data 第一次抛异常、第二次成功，
  触发 managed disconnect → reconnect → retry（max_retries=3 内完成），
  成功返回且 reconnect_count 增加。

全部 mock 底层连接与 pytdx API（monkeypatch TdxHq_API），不连接真实服务器。
用法：PURE_UNIT_TEST=1 pytest tests/test_pytdx_adapter_v3.py -q
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core import pytdx_adapter as module

# 固定交易日（仅用于 get_history_transaction_page 的参数传递，mock 不依赖其值）
TRADE_DATE = date(2026, 8, 14)

# 固定的 mock transaction 记录（list[dict]，healthy path 断言返回值）
FIXED_ROWS: list[dict[str, Any]] = [
    {"time": "09:30:00", "price": 10.01, "vol": 100, "buyorsell": "B"},
    {"time": "09:30:01", "price": 10.02, "vol": 200, "buyorsell": "S"},
]


def _install_fake_tdx_api(
    monkeypatch: pytest.MonkeyPatch,
    get_history_transaction_data: Any | None = None,
) -> dict[str, int]:
    """将模块级 TdxHq_API 替换为 fake 工厂（connect 返回 True），避免真实建连。

    Args:
        monkeypatch: pytest monkeypatch fixture。
        get_history_transaction_data: 可选，若提供则挂到每个新建 api 实例上
            （reconnect 后会创建新实例，必须让每个实例都带同一 mock 函数）。

    Returns:
        {"created": int}：实际创建的 api 实例数，用于验证幂等 NO-OP。
    """
    state = {"created": 0}

    def _fake_api_factory(*args: Any, **kwargs: Any) -> MagicMock:
        state["created"] += 1
        api = MagicMock()
        api.connect.return_value = True
        if get_history_transaction_data is not None:
            api.get_history_transaction_data = get_history_transaction_data
        return api

    monkeypatch.setattr(module, "TdxHq_API", _fake_api_factory)
    return state


# ============================================================
# P1: connect() 幂等
# ============================================================


def test_connect_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """[P1] 已连接后重复调用 connect() 必须 NO-OP。

    验证：
    - 首次连接成功：successful_connect_count == 1
    - 后续 connect() 不再新建 TdxHq_API / 不重新扫描 server list / 不新建 socket
    - reconnect_count 不增加（无 source failure）
    """
    state = _install_fake_tdx_api(monkeypatch)
    adapter = module.PytdxAdapter(max_retries=3, retry_delay=0)

    adapter.connect()
    adapter.connect()
    adapter.connect()

    assert adapter._api is not None, "首次 connect 后应已连接"
    assert adapter.successful_connect_count == 1, (
        f"首次连接成功计数应为 1，实际 {adapter.successful_connect_count}"
    )
    assert adapter.reconnect_count == 0, (
        f"幂等 connect 不应触发重连，reconnect_count 应为 0，实际 {adapter.reconnect_count}"
    )
    assert state["created"] == 1, (
        f"已连接后 connect() 必须 NO-OP，不得再次新建 TdxHq_API，实际创建 {state['created']} 个"
    )


# ============================================================
# P2: healthy path 诊断计数
# ============================================================


def test_healthy_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    """[P2] 同一 adapter 连续 N 个 get_history_transaction_page（mock 返回）。

    healthy path 下：
    - 只首次 connect 一次：successful_connect_count == 1
    - 全程无失败 / 无重连：reconnect_count == 0
    - 每次页面返回固定 list[dict]
    """
    _install_fake_tdx_api(monkeypatch)
    adapter = module.PytdxAdapter(max_retries=3, retry_delay=0)
    adapter.connect()
    assert adapter.successful_connect_count == 1

    # 替换底层 api.get_history_transaction_data 为返回固定 list[dict] 的 mock 函数
    monkeypatch.setattr(
        adapter.api,
        "get_history_transaction_data",
        lambda *args, **kwargs: list(FIXED_ROWS),
    )

    n_pages = 5
    for offset in range(n_pages):
        rows = adapter.get_history_transaction_page(
            "000001", TRADE_DATE, offset=offset, count=10
        )
        assert rows == FIXED_ROWS, f"offset={offset} 应返回固定 mock 记录"

    assert adapter.successful_connect_count == 1, (
        f"healthy path 只应首次连接一次，实际 {adapter.successful_connect_count}"
    )
    assert adapter.reconnect_count == 0, (
        f"healthy path 不应重连，reconnect_count 应为 0，实际 {adapter.reconnect_count}"
    )


# ============================================================
# P3: 页面失败 → managed reconnect → retry
# ============================================================


def test_page_failure_reconnect_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """[P3] get_history_transaction_data 第一次抛异常、第二次成功。

    验证 get_history_transaction_page 内部 retry（max_retries=3）：
    - attempt 1 失败 → disconnect（计入 managed reconnect）
    - attempt 2 重新 connect 成功并拉取成功 → 返回正确结果
    - reconnect_count 增加（首次连接后发生一次真实 source failure）
    """
    call_state = {"calls": 0}

    def _flaky_get_history_transaction_data(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        call_state["calls"] += 1
        if call_state["calls"] == 1:
            raise RuntimeError("mock socket failure")
        return list(FIXED_ROWS)

    # flaky mock 挂到每个新建 api 实例：reconnect 后新实例同样命中同一函数
    _install_fake_tdx_api(
        monkeypatch, get_history_transaction_data=_flaky_get_history_transaction_data
    )
    adapter = module.PytdxAdapter(max_retries=3, retry_delay=0)
    adapter.connect()
    assert adapter.successful_connect_count == 1
    assert adapter.reconnect_count == 0

    rows = adapter.get_history_transaction_page(
        "000001", TRADE_DATE, offset=0, count=10
    )

    assert rows == FIXED_ROWS, "第一次失败、第二次成功时应成功返回固定 mock 记录"
    assert call_state["calls"] == 2, (
        f"应恰好调用 2 次（1 次失败 + 1 次成功），实际 {call_state['calls']}"
    )
    # 首次 connect + 重连成功 → successful_connect_count == 2
    assert adapter.successful_connect_count == 2, (
        f"重连成功应累计 successful_connect_count=2，实际 {adapter.successful_connect_count}"
    )
    # 一次真实 source failure → reconnect_count 增加
    assert adapter.reconnect_count == 1, (
        f"一次页面失败应触发 reconnect_count=1，实际 {adapter.reconnect_count}"
    )
