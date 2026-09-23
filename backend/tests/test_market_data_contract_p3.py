"""P3 行情采集生产合同证据（纯单测部分）。

这些合同不依赖真实 PG / 网络，用生产函数 + mock provider 断言 call count。
需要真实 DB 的契约（DB-complete / one-day-missing / 两 worker 并发 / 权限 resolver）
放在 tests/pg/ 下，由 scripts/ops/panji-verify 在注册运行时执行。

覆盖：
- P3.4 retry/resume：_resolve_execution_completed_steps 跳过已完成 stage；
- P3.1 TDX server rotation：A 失败 → B 成功（pytdx 内部环形轮转）；
- P3.5/provider 隔离：正常 SH/SZ 行情只走 pytdx，绝不走 Eastmoney/THS。
"""

from __future__ import annotations

import pytest
from pytdx.errors import TdxFunctionCallError

from app.core.pytdx_adapter import PytdxAdapter
from app.services.after_close_orchestrator import _resolve_execution_completed_steps
from app.services.realtime_market_fact_service import RealtimeMarketFactService


# ---------------------------------------------------------------------------
# P3.4 retry/resume：已完成 stage 不会被重跑
# ---------------------------------------------------------------------------

def test_resolve_execution_completed_steps_resume_skips_completed() -> None:
    """A=refreshing_daily、B=syncing_boards 已完成；C=computing_features 失败。
    resume → completed 含 A、B；computing_features 不在 → C 重跑（C>=1, A=0, B=0）。"""
    completed = _resolve_execution_completed_steps("syncing_boards", None)
    assert "refreshing_daily" in completed
    assert "syncing_boards" in completed
    assert "computing_features" not in completed  # C 需重跑

    # 全新 initial run（无 checkpoint）→ 空集合，全链重跑
    assert _resolve_execution_completed_steps(None, None) == set()

    # restart 正式起点 mainchain_stage=computing_features：其之前所有 pre-stage 算完成
    restarted = _resolve_execution_completed_steps(None, "computing_features")
    assert "refreshing_daily" in restarted
    assert "syncing_boards" in restarted
    assert "computing_features" not in restarted  # 自身仍需执行


def test_resolve_execution_completed_steps_invalid_stage_fail_closed() -> None:
    """corrupt/typo mainchain_stage 不得静默退化成 full run，必须显式抛 ValueError。"""
    with pytest.raises(ValueError):
        _resolve_execution_completed_steps(None, "not_a_real_stage")


# ---------------------------------------------------------------------------
# P3.1 TDX server rotation：A 失败 → B 成功
# ---------------------------------------------------------------------------

def test_pytdx_call_with_reconnect_rotates_a_to_b(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytdx 主源 A 报 source failure → 候选迭代至 B 成功返回；
    正常路径不涉及 Eastmoney / THS（各自 provider 模块不被调用）。"""
    from app.services import eod_market_snapshot_provider as _em
    from app.services import ths_raw_daily_provider as _ths

    server_a = ("10.0.0.1", 7709)
    server_b = ("10.0.0.2", 7709)
    adapter = PytdxAdapter(servers=[server_a, server_b], max_retries=3)

    attempted: list[tuple[str, int] | None] = []

    class FakeApi:
        def __init__(self, server: tuple[str, int]) -> None:
            self.server = server

        def disconnect(self) -> None:  # pragma: no cover - 仅满足真实 disconnect 调用
            pass

    def _fake_connect_server(server=None, *_args: object, **_kwargs: object) -> None:
        # [parity C] 新连接原语：按候选 server 注入对应 FakeApi（不再环形扫描）
        attempted.append(server)
        adapter.connected_server = server
        adapter._api = FakeApi(server)  # type: ignore[assignment]

    # 替换真实连接逻辑，但保留真实 _call_with_reconnect 的候选迭代
    monkeypatch.setattr(adapter, "_connect_server", _fake_connect_server)

    em_calls: list[int] = []
    ths_calls: list[int] = []
    monkeypatch.setattr(_em, "fetch_full_a_share_snapshot", lambda *a, **k: em_calls.append(1))
    monkeypatch.setattr(_ths, "fetch_ths_raw_daily", lambda *a, **k: ths_calls.append(1))

    def _call(api: FakeApi) -> list[dict]:
        if api.server == server_a:
            raise TdxFunctionCallError("PYTDX_SOURCE_FAILURE on A")
        return [{"symbol": "600519", "close": 1.0}]

    result = adapter._call_with_reconnect("get_daily_bars", _call)
    assert result == [{"symbol": "600519", "close": 1.0}]
    assert attempted == [server_a, server_b]  # A 失败 → B 成功（各尝试一次）
    assert em_calls == []  # 正常轮转成功，不触发 Eastmoney 备用源
    assert ths_calls == []  # 不触发 THS


# ---------------------------------------------------------------------------
# P3.5 / provider 隔离：正常 SH/SZ 行情只走 pytdx
# ---------------------------------------------------------------------------

async def test_realtime_quote_sh_sz_uses_pytdx_not_eastmoney(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常 SH/SZ 批量行情只经 pytdx 主源（一次批量调用），不逐个标的重抓，也不走 Eastmoney。"""
    from app.services import realtime_market_snapshot_provider as _snap

    svc = RealtimeMarketFactService()
    pytdx_batches: list[list[str]] = []

    class FakeAdapter:
        def get_security_quotes_with_provenance(self, chunk):
            pytdx_batches.append(list(chunk))
            return [
                {
                    "code": s,
                    "price": 10.0,
                    "last_close": 9.0,
                    "open": 9.0,
                    "high": 10.0,
                    "low": 9.0,
                    "vol": 100.0,
                    "amount": 1000.0,
                }
                for s in chunk
            ], None

    em_calls: list[int] = []
    monkeypatch.setattr(_snap, "fetch_realtime_a_share_snapshot", lambda *a, **k: em_calls.append(1))

    quotes = await svc.fetch_quotes(["600519", "000001"], adapter=FakeAdapter())
    assert set(quotes.keys()) == {"600519", "000001"}
    # 一次批量 pytdx 调用覆盖全部 SH/SZ 标的（非逐股重抓）
    assert len(pytdx_batches) == 1
    assert em_calls == []  # 正常路径不走 Eastmoney 备用源
