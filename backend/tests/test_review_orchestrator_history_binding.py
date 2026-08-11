"""[Phase4C §4-§6] Formal Current Review history binding (P0-B) 单元测试。

验证：
- _build_scope_history 将 canonical history lineage 过滤条件
  (required_history_contract_version / required_taxonomy_compatibility_key /
  required_source_history_run_id) 透传给 load_metric_history；
- source run id 由 canonical readiness contract 动态解析，禁止硬编码生产 run id；
- market scope 的 taxonomy_compatibility_key 可为 None（canonical market series）。

不连接真实数据库（mock load_metric_history），属于 modified-scope unit test。
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.services.review_orchestrator_service import (
    ScopeDefinition,
    _bind_or_reuse_canonical_history_source,
    _build_scope_history,
    _compute_scope_metrics_phase,
    _resolve_canonical_history_source,
)


class TestHistoryBindingForwarding:
    async def test_build_scope_history_forwards_required_lineage(self):
        scope = ScopeDefinition(
            scope_type="market",
            scope_key="ALL_A_SHARE",
            scope_name="全市场",
            taxonomy_version="canonical-market-v1",
            taxonomy_compatibility_key=None,
            membership_version=None,
        )
        load = AsyncMock(return_value=(None, None, None))
        with patch(
            "app.services.review_metric_observation_service.load_metric_history",
            load,
        ):
            await _build_scope_history(
                AsyncMock(),
                scope=scope,
                trade_date=date(2026, 8, 7),
                algorithm_version="review-v1",
                baseline_window=120,
                required_history_contract_version="review-history-v2",
                required_taxonomy_compatibility_key=None,
                required_source_history_run_id=uuid.uuid4(),
            )
        assert load.await_count == 1
        kwargs = load.call_args.kwargs
        assert kwargs["required_history_contract_version"] == "review-history-v2"
        assert kwargs["required_taxonomy_compatibility_key"] is None
        assert kwargs["required_source_history_run_id"] is not None
        assert kwargs["scope_type"] == "market"
        assert kwargs["trade_date"] == date(2026, 8, 7)

    async def test_compute_scope_phase_passes_taxonomy_key(self):
        scope = ScopeDefinition(
            scope_type="market",
            scope_key="ALL_A_SHARE",
            scope_name="全市场",
            taxonomy_version="canonical-market-v1",
            taxonomy_compatibility_key=None,
            membership_version=None,
        )
        # _compute_scope_metrics_phase 会调用很多下游服务；这里只断言它把
        # scope.taxonomy_compatibility_key 透传给了 _build_scope_history。
        captured = {}

        async def fake_build(*args, **kwargs):
            captured.update(kwargs)
            return (None, None, None)

        run = type(
            "R",
            (),
            {
                "id": uuid.uuid4(),
                "trade_date": date(2026, 8, 7),
                "algorithm_version": "review-v1",
                "baseline_window": 120,
                "source_core_run_id": uuid.uuid4(),
            },
        )()

        class _Snap:
            pass

        with patch(
            "app.services.review_orchestrator_service._build_scope_history",
            fake_build,
        ), patch(
            "app.services.review_orchestrator_service._resolve_all_discovery_scopes",
            AsyncMock(return_value=[scope]),
        ), patch(
            "app.services.review_orchestrator_service.resolve_scope_members",
            AsyncMock(return_value=([uuid.uuid4()], "全市场")),
        ), patch(
            "app.services.review_orchestrator_service.fetch_member_flat_list",
            AsyncMock(return_value=[]),
        ), patch(
            "app.services.review_orchestrator_service.compute_scope_metrics",
            AsyncMock(return_value=_Snap()),
        ), patch(
            "app.services.review_orchestrator_service.apply_cross_section_percentiles",
            AsyncMock(),
        ), patch(
            "app.services.review_orchestrator_service._compute_scope_signal_pipeline",
            AsyncMock(return_value=0),
        ), patch(
            "app.services.review_orchestrator_service._upsert_run_item",
            AsyncMock(),
        ):
            await _compute_scope_metrics_phase(
                AsyncMock(),
                run,
                scope,
                required_history_contract_version="review-history-v2",
                required_source_history_run_id=uuid.uuid4(),
            )
        assert captured["required_taxonomy_compatibility_key"] is None
        assert captured["required_history_contract_version"] == "review-history-v2"


class TestCanonicalSourceResolver:
    async def test_resolver_returns_none_when_no_canonical_run(self):
        """无就绪 canonical run → source run id = None，contract 仍为算法版本。"""
        session = AsyncMock()
        fake_result = type("FR", (), {"scalars": lambda self: []})()
        session.execute = AsyncMock(return_value=fake_result)
        run_id, contract = await _resolve_canonical_history_source(session)
        assert run_id is None
        # contract 来自算法常量（版本字符串），不是具体 run id
        assert contract == "review-history-v2"

    async def test_resolver_does_not_hardcode_production_run_id(self):
        """resolver 通过 readiness contract 从运行数据解析 run id，不硬编码 be56dcd2...。"""
        # 用一个伪造的 ready run 验证 resolver 返回该 run 的 id（取自数据，非常量）
        ready_run = type("Run", (), {"id": uuid.uuid4()})()

        async def fake_execute(stmt):
            return type("R", (), {"scalars": lambda self: [ready_run]})()

        async def fake_validate(session, run_id, contract, required_trade_date=None):
            return {"status": "ok", "run_id": str(run_id)}

        session = AsyncMock()
        session.execute = fake_execute
        with patch(
            "app.services.review_orchestrator_service.validate_canonical_history_run_readiness",
            fake_validate,
        ):
            run_id, contract = await _resolve_canonical_history_source(session)
        assert run_id == ready_run.id
        assert contract == "review-history-v2"


class TestRunBoundHistoryLifecycle:
    """[Phase4C P0-B B1~B5] MarketReviewRun ↔ canonical HistoryRun 绑定生命周期。

    验证：同一 ReviewRun 从开始到完成必须使用同一个 history source（禁止 A→B 漂移）；
    新 ReviewRun 可解析当时最新合法 source；bound source 缺失/contract 不符 → fail closed。
    """

    def _make_run(self, metadata=None, trade_date=date(2026, 8, 10)):
        return type(
            "R",
            (),
            {
                "id": uuid.uuid4(),
                "trade_date": trade_date,
                "metadata_json": metadata if metadata is not None else {},
            },
        )()

    async def test_new_run_resolves_and_binds_canonical_a(self):
        """B1: 新 run 无 binding → 解析 canonical A → 写入 metadata。"""
        session = AsyncMock()
        session.flush = AsyncMock()
        run_a = uuid.uuid4()
        with patch(
            "app.services.review_orchestrator_service._resolve_canonical_history_source",
            AsyncMock(return_value=(run_a, "review-history-v2")),
        ), patch(
            "app.services.review_orchestrator_service.validate_canonical_history_run_readiness",
            AsyncMock(return_value={"status": "ok", "run_id": str(run_a)}),
        ):
            src_id, contract = await _bind_or_reuse_canonical_history_source(session, self._make_run())
        assert src_id == run_a
        assert contract == "review-history-v2"
        # metadata 必须回写绑定（用新 run 对象验证写入路径）
        run = self._make_run()
        session2 = AsyncMock()
        session2.flush = AsyncMock()
        with patch(
            "app.services.review_orchestrator_service._resolve_canonical_history_source",
            AsyncMock(return_value=(run_a, "review-history-v2")),
        ), patch(
            "app.services.review_orchestrator_service.validate_canonical_history_run_readiness",
            AsyncMock(return_value={"status": "ok", "run_id": str(run_a)}),
        ):
            await _bind_or_reuse_canonical_history_source(session2, run)
        assert run.metadata_json.get("canonical_history_source_run_id") == str(run_a)
        assert run.metadata_json.get("canonical_history_contract_version") == "review-history-v2"

    async def test_resume_reuses_bound_source_not_latest(self):
        """B2: resume 同一 run（即使数据库出现新 canonical B）→ 仍使用 A。"""
        run_a = uuid.uuid4()
        run_b = uuid.uuid4()
        run = self._make_run(
            {
                "canonical_history_source_run_id": str(run_a),
                "canonical_history_contract_version": "review-history-v2",
            }
        )
        session = AsyncMock()
        session.flush = AsyncMock()
        # 即使 _resolve_canonical_history_source 会返回新 B，resume 也不应调用它
        resolve_mock = AsyncMock(return_value=(run_b, "review-history-v2"))
        with patch(
            "app.services.review_orchestrator_service._resolve_canonical_history_source",
            resolve_mock,
        ), patch(
            "app.services.review_orchestrator_service.validate_canonical_history_run_readiness",
            AsyncMock(return_value={"status": "ok", "run_id": str(run_a)}),
        ):
            src_id, _ = await _bind_or_reuse_canonical_history_source(session, run)
        assert src_id == run_a  # 复用 A，不漂移
        assert resolve_mock.await_count == 0  # resume 不重新 resolve

    async def test_new_run_can_resolve_different_source(self):
        """B3: 新 run 无 binding → 可解析当时最新合法 source（允许 A→B 跨 run，但同 run 不漂移）。"""
        run_b = uuid.uuid4()
        run = self._make_run()  # 无 binding
        session = AsyncMock()
        session.flush = AsyncMock()
        with patch(
            "app.services.review_orchestrator_service._resolve_canonical_history_source",
            AsyncMock(return_value=(run_b, "review-history-v2")),
        ), patch(
            "app.services.review_orchestrator_service.validate_canonical_history_run_readiness",
            AsyncMock(return_value={"status": "ok", "run_id": str(run_b)}),
        ):
            src_id, _ = await _bind_or_reuse_canonical_history_source(session, run)
        assert src_id == run_b
        assert run.metadata_json.get("canonical_history_source_run_id") == str(run_b)

    async def test_bound_source_contract_mismatch_fail_closed(self):
        """B4: bound source contract 不再 canonical-compatible → fail closed（不切新 source）。"""
        run_a = uuid.uuid4()
        run = self._make_run(
            {
                "canonical_history_source_run_id": str(run_a),
                "canonical_history_contract_version": "review-history-v2",
            }
        )
        session = AsyncMock()
        session.flush = AsyncMock()
        with patch(
            "app.services.review_orchestrator_service.validate_canonical_history_run_readiness",
            AsyncMock(return_value={"status": "rejected", "reason": "contract mismatch"}),
        ):
            with pytest.raises(Exception):  # ReviewOrchestratorError
                await _bind_or_reuse_canonical_history_source(session, run)

    async def test_bound_source_missing_fail_closed(self):
        """B5: bound source 不再存在 → fail closed。"""
        run_a = uuid.uuid4()
        run = self._make_run(
            {
                "canonical_history_source_run_id": str(run_a),
                "canonical_history_contract_version": "review-history-v2",
            }
        )
        session = AsyncMock()
        session.flush = AsyncMock()
        with patch(
            "app.services.review_orchestrator_service.validate_canonical_history_run_readiness",
            AsyncMock(return_value={"status": "missing", "reason": "run not found"}),
        ):
            with pytest.raises(Exception):
                await _bind_or_reuse_canonical_history_source(session, run)
