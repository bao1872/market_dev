"""[权限模型 V2 后端合同收口] 失败测试骨架（纯单元，PURE_UNIT_TEST=1，不连库）。

覆盖合同：
- 场景化 legacy 物化：显式邀请码新注册不物化；旧用户续期/管理员首次管理才物化。
- 统一 grant_days 授权：active 顺延、expired/revoked/空 从 now 重算。
- 统一用户行锁顺序：任何需要物化的路径必须先 SELECT ... FOR UPDATE 锁 User。
- tombstone 撤销：admin_revoke 强制 inactive；保留原 granted_by；幂等。
- 商业状态 fail-closed：缺 starts_at/expires_at/invalid_period 判 expired + 诊断。
- 同事务结构化审计 target_id="{user_id}:{capability}"，默认原因。
- Grant/Revoke 请求 reason 规范化。

说明：通过 mock AsyncSession 与 ORM 对象驱动服务层，不连接任何数据库。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.access import (
    AccessProfileResponse,
    AdminAccessProfileResponse,
    AdminAccountInfo,
    EffectiveAccessInfo,
    ExplicitCapabilityRecord,
    SubscriptionSummaryInfo,
)
from app.schemas.subscription import GrantCapabilityRequest, RevokeCapabilityRequest
from app.services.subscription_service import (
    apply_capability_grant,
    change_self_selection_quota,
    get_effective_subscription_status,
    list_subscribers,
    resolve_commercial_status,
    revoke_capability_from_user,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _user_row() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4())


def _cap_row(
    capability: str = "market_data",
    source: str = "invite_code",
    expires_at: datetime | None = None,
    granted_by: uuid.UUID | None = None,
    watchlist_limit: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        capability=capability,
        source=source,
        expires_at=expires_at or (_now() + timedelta(days=30)),
        granted_at=_now(),
        granted_by=granted_by,
        watchlist_limit=watchlist_limit,
    )


class _FakeResult:
    """模拟 AsyncSession.execute 返回的 Result（scalar_one_or_none / scalars）。"""

    def __init__(self, value=None, many=None):
        self._value = value
        self._many = many if many is not None else []

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def all(self):
        return self._many

    def scalars(self):
        _many = self._many
        _value = self._value
        class _Scalars:
            def all(self):
                return _many
            def first(self):
                return _many[0] if _many else _value
        return _Scalars()

    def first(self):
        return self._value


class _FakeSession:
    """记录 execute/with_for_update/add/flush 的 mock session。"""

    def __init__(self):
        self.execute = AsyncMock()
        self.add = MagicMock()
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.locked_user = None
        self.lock_with_for_update_used = False


def _make_lock_execute(session, user_row):
    """构造 execute side_effect：User 行锁查询（_is_user_lock=True）返回 user_row，
    其余 SELECT(UserCapability) 返回空。用于验证「先锁用户」合同。
    """
    async def _execute(stmt, *args, **kwargs):
        if getattr(stmt, "_is_user_lock", False):
            session.locked_user = user_row
            session.lock_with_for_update_used = True
            return _FakeResult(user_row)
        return _FakeResult(None)

    return _execute


# ============================================================
# 1. 场景化 legacy 物化：调用 apply_capability_grant 时的锁合同
# ============================================================


class TestLockContract:
    """任何需要物化的路径必须先锁 User 行（SELECT ... FOR UPDATE）。"""

    @pytest.mark.asyncio
    async def test_materialize_path_locks_user_row(self):
        """管理员 grant（materialize_legacy=True）必须先对 User 行加 FOR UPDATE 锁。"""
        session = _FakeSession()
        user_row = _user_row()
        session.execute.side_effect = _make_lock_execute(session, user_row)

        from sqlalchemy import select

        # 让 select(User).with_for_update() 产生的语句带 _is_user_lock 标记，
        # 以便 mock 判定锁查询命中；其余 capability 查询返回空
        real_select = select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            await apply_capability_grant(
                db=session,
                user_id=user_row.id,
                capability="market_data",
                grant_days=30,
                watchlist_limit=None,
                source="admin_grant",
                materialize_legacy=True,
                actor_user_id=uuid.uuid4(),
            )

        assert session.lock_with_for_update_used is True, (
            "materialize_legacy=True 路径必须先 SELECT ... FOR UPDATE 锁 User 行"
        )

    @pytest.mark.asyncio
    async def test_no_materialize_path_does_not_require_lock(self):
        """显式邀请码新注册（materialize_legacy=False）不物化，仍可走统一授权（锁由外层编排）。"""
        session = _FakeSession()
        user_row = _user_row()
        session.execute.side_effect = _make_lock_execute(session, user_row)

        from sqlalchemy import select

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", wraps=select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            await apply_capability_grant(
                db=session,
                user_id=user_row.id,
                capability="self_selection",
                grant_days=30,
                watchlist_limit=20,
                source="invite_code",
                materialize_legacy=False,
            )

        # 新注册路径（materialize_legacy=False）不锁用户、不物化，仍返回 mutation result
        assert session.lock_with_for_update_used is False, (
            "显式邀请码新注册不应触发用户行锁（不物化）"
        )
        assert session.add.called is True


# ============================================================
# 2. reason 规范化（Schema 层）
# ============================================================


class TestReasonNormalization:
    """Grant/Revoke 请求可选 reason：去空白、空字符串转 None、限长。"""

    def test_grant_reason_trimmed(self):
        req = GrantCapabilityRequest(
            capability="market_data", months=1, reason="  手动授予  "
        )
        assert req.reason == "手动授予"

    def test_grant_reason_empty_becomes_none(self):
        req = GrantCapabilityRequest(capability="market_data", months=1, reason="   ")
        assert req.reason is None

    def test_revoke_reason_trimmed(self):
        req = RevokeCapabilityRequest(capability="market_data", reason="  违规  ")
        assert req.reason == "违规"

    def test_revoke_reason_empty_becomes_none(self):
        req = RevokeCapabilityRequest(capability="market_data", reason="")
        assert req.reason is None

    def test_reason_max_length_rejected(self):
        with pytest.raises(ValueError):
            GrantCapabilityRequest(
                capability="market_data", months=1, reason="x" * 501
            )


# ============================================================
# 3. Access Schema 自测修复
# ============================================================


class TestAccessProfileSchemaFields:
    """旧 AccessProfileResponse 自测断言 12 字段，但模型已扩展（含 default_route 等）。"""

    def test_field_count_includes_v2_fields(self):
        """模型字段数应包含 default_route/active_capability_keys/capability_source/diagnostics。"""
        fields = set(AccessProfileResponse.model_fields.keys())
        for f in (
            "default_route",
            "active_capability_keys",
            "capability_source",
            "diagnostics",
        ):
            assert f in fields, f"AccessProfileResponse 必须含字段 {f}"

    def test_construct_with_default_route(self):
        """构造实例需提供必填 default_route（修复旧自测缺字段）。"""
        resp = AccessProfileResponse(
            user_id="test-uuid",
            account_status="active",
            roles=["member"],
            is_admin=False,
            is_member=True,
            subscription_active=True,
            default_route="/forbidden",
        )
        assert resp.default_route == "/forbidden"


# ============================================================
# 4. 商业状态解析器（fail-closed）
# ============================================================


class TestCommercialStatusParser:
    """resolve_commercial_status：受限 status + 诊断 reason，异常周期 fail-closed。"""

    def _sub(self, **kwargs):
        defaults = {
            "status": "active",
            "starts_at": _now() - timedelta(days=1),
            "expires_at": _now() + timedelta(days=30),
            "plan_code": "observe_20",
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_none_returns_none(self):
        from app.services.subscription_service import resolve_commercial_status
        r = resolve_commercial_status(None)
        assert r.status == "none"

    def test_persistent_revoked_preserved(self):
        from app.services.subscription_service import resolve_commercial_status
        r = resolve_commercial_status(self._sub(status="revoked"))
        assert r.status == "revoked"

    def test_persistent_cancelled_preserved(self):
        from app.services.subscription_service import resolve_commercial_status
        r = resolve_commercial_status(self._sub(status="cancelled"))
        assert r.status == "cancelled"

    def test_missing_starts_at_fail_closed(self):
        from app.services.subscription_service import resolve_commercial_status
        r = resolve_commercial_status(self._sub(starts_at=None))
        assert r.status == "expired"
        assert r.reason == "missing_starts_at"

    def test_missing_expires_at_fail_closed(self):
        from app.services.subscription_service import resolve_commercial_status
        r = resolve_commercial_status(self._sub(expires_at=None))
        assert r.status == "expired"
        assert r.reason == "missing_expires_at"

    def test_invalid_period_fail_closed(self):
        from app.services.subscription_service import resolve_commercial_status
        starts = _now() + timedelta(days=5)
        expires = _now() - timedelta(days=5)
        r = resolve_commercial_status(self._sub(starts_at=starts, expires_at=expires))
        assert r.status == "expired"
        assert r.reason == "invalid_period"

    def test_not_started_pending(self):
        from app.services.subscription_service import resolve_commercial_status
        starts = _now() + timedelta(days=1)
        expires = _now() + timedelta(days=30)
        r = resolve_commercial_status(self._sub(starts_at=starts, expires_at=expires))
        assert r.status == "pending"

    def test_expired(self):
        from app.services.subscription_service import resolve_commercial_status
        r = resolve_commercial_status(
            self._sub(starts_at=_now() - timedelta(days=10), expires_at=_now() - timedelta(days=1))
        )
        assert r.status == "expired"

    def test_active(self):
        from app.services.subscription_service import resolve_commercial_status
        r = resolve_commercial_status(self._sub())
        assert r.status == "active"


# ============================================================
# 5. 撤销保留 granted_by（tombstone 合同）
# ============================================================


class TestRevokePreservesGrantedBy:
    """撤销不得覆盖原 granted_by，撤销人仅进入审计快照。"""

    @pytest.mark.asyncio
    async def test_revoke_preserves_original_granted_by(self):
        """已有记录撤销：source 变 admin_revoke，但 granted_by 保留原授予人。"""
        session = _FakeSession()
        user_row = _user_row()
        original_grantor = uuid.uuid4()
        revoker = uuid.uuid4()
        existing_row = _cap_row(source="admin_grant", granted_by=original_grantor)
        session.execute.side_effect = _make_lock_execute(session, user_row)

        from sqlalchemy import select

        def _patched_select(*cols, **kwargs):
            stmt = select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        calls = {"n": 0}

        async def _execute(stmt, *args, **kwargs):
            calls["n"] += 1
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                session.lock_with_for_update_used = True
                return _FakeResult(user_row)
            # 执行顺序：
            #   n=2 ensure_explicit_capability_mode 查 UserCapability（scalars().all()）→ 空
            #   n=3 ensure_explicit_capability_mode 查 Subscription（scalars().first()）→ 无（None）
            #   n=4 revoke 查询目标 capability 行 → existing_row
            if calls["n"] == 4:
                return _FakeResult(existing_row)
            return _FakeResult(None)

        session.execute.side_effect = _execute

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            result = await revoke_capability_from_user(
                db=session,
                user_id=user_row.id,
                capability="market_data",
                revoked_by=revoker,
            )

        assert result.action == "revoke"
        # granted_by 保留原授予人（不被 revoker 覆盖）
        assert result.after["granted_by"] == str(original_grantor)
        assert result.after["granted_by"] != str(revoker)
        assert result.after["active"] is False
        assert result.after["source"] == "admin_revoke"


# ============================================================
# 6. mutation_type 精确区分（PV2-B05 审计 action 依据真实类型）
# ============================================================


class TestMutationType:
    """apply_capability_grant / revoke_capability_from_user 的 mutation_type 区分。

    覆盖（apply_capability_grant 只产生四种）：
    - grant：无既有行，新建授权。
    - extend：已有行续期（active 顺延 / expired 从 now 重算，额度不变或非 self_selection）。
    - extend_and_quota_change：已有 self_selection 且额度变化（无论此前 active/expired）。
    - regrant：tombstone（admin_revoke）重新授权。
    - revoke：撤销（mutation_type="revoke"）。
    纯 quota_change 由独立 change_self_selection_quota 产生（见 TestQuotaChange）。
    """

    async def _run_apply(
        self,
        existing_row,
        *,
        capability="market_data",
        watchlist_limit=None,
        source="admin_grant",
        materialize_legacy=True,
        actor_user_id=None,
    ):
        session = _FakeSession()
        user_row = _user_row()

        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        async def _execute(stmt, *args, **kwargs):
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                session.lock_with_for_update_used = True
                return _FakeResult(user_row)
            # ensure_explicit_capability_mode 的 UserCapability 查询用 scalars().all()，
            # 返回非空则视为已有显式记录、跳过 Subscription 物化；apply 目标行查询用
            # scalar_one_or_none() 返回 existing_row。
            return _FakeResult(existing_row, many=[existing_row] if existing_row else [])

        session.execute.side_effect = _execute

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta
            return await apply_capability_grant(
                db=session,
                user_id=user_row.id,
                capability=capability,
                grant_days=30,
                watchlist_limit=watchlist_limit,
                source=source,
                materialize_legacy=materialize_legacy,
                actor_user_id=actor_user_id,
            )

    @pytest.mark.asyncio
    async def test_new_row_grant(self):
        """无既有行（admin_grant）→ mutation_type=grant；不物化时 materialized 为空。"""
        result = await self._run_apply(
            None, materialize_legacy=False, actor_user_id=uuid.uuid4()
        )
        assert result.action == "grant"
        assert result.mutation_type == "grant"
        assert result.after["mutation_type"] == "grant"
        # materialize_legacy=False：不物化，materialized_capabilities 为空列表
        assert result.materialized_capabilities == []

    @pytest.mark.asyncio
    async def test_active_extend(self):
        """active 已有行续期（market_data，无额度变化）→ mutation_type=extend。"""
        existing = _cap_row(
            capability="market_data", source="admin_grant",
            expires_at=_now() + timedelta(days=10),
        )
        result = await self._run_apply(
            existing, capability="market_data", actor_user_id=uuid.uuid4()
        )
        assert result.mutation_type == "extend"

    @pytest.mark.asyncio
    async def test_expired_quota_change_is_extend_and_quota_change(self):
        """过期 self_selection 行且额度变化 → extend_and_quota_change（本次同时改期限与额度）。"""
        existing = _cap_row(
            capability="self_selection", source="admin_grant",
            expires_at=_now() - timedelta(days=1), watchlist_limit=20,
        )
        result = await self._run_apply(
            existing, capability="self_selection", watchlist_limit=30,
            actor_user_id=uuid.uuid4(),
        )
        assert result.mutation_type == "extend_and_quota_change"
        assert result.after["watchlist_limit"] == 30

    @pytest.mark.asyncio
    async def test_active_extend_and_quota_change(self):
        """active self_selection 行续期 + 调整额度 → extend_and_quota_change。"""
        existing = _cap_row(
            capability="self_selection", source="admin_grant",
            expires_at=_now() + timedelta(days=10), watchlist_limit=20,
        )
        result = await self._run_apply(
            existing, capability="self_selection", watchlist_limit=30,
            actor_user_id=uuid.uuid4(),
        )
        assert result.mutation_type == "extend_and_quota_change"
        assert result.after["watchlist_limit"] == 30

    @pytest.mark.asyncio
    async def test_tombstone_regrant(self):
        """tombstone（admin_revoke）重新授权 → mutation_type=regrant。"""
        existing = _cap_row(
            capability="market_data", source="admin_revoke",
            expires_at=_now() - timedelta(days=1),
        )
        result = await self._run_apply(
            existing, capability="market_data", actor_user_id=uuid.uuid4()
        )
        assert result.mutation_type == "regrant"
        # regrant 恢复真实来源
        assert result.after["source"] == "admin_grant"

    @pytest.mark.asyncio
    async def test_revoke_mutation_type(self):
        """撤销 → mutation_type=revoke，action=revoke。"""
        session = _FakeSession()
        user_row = _user_row()
        existing = _cap_row(
            capability="market_data", source="admin_grant",
            expires_at=_now() + timedelta(days=10), granted_by=uuid.uuid4(),
        )

        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        calls = {"n": 0}

        async def _execute(stmt, *args, **kwargs):
            calls["n"] += 1
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                return _FakeResult(user_row)
            # ensure_explicit_capability_mode 查询 UserCapability（scalars().all()）→ 空
            # 目标 capability 行（scalar_one_or_none）→ existing
            if calls["n"] in (2, 3):
                return _FakeResult(None)
            return _FakeResult(existing)

        session.execute.side_effect = _execute

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            result = await revoke_capability_from_user(
                db=session,
                user_id=user_row.id,
                capability="market_data",
                revoked_by=uuid.uuid4(),
                reason="违规",
            )

        assert result.action == "revoke"
        assert result.mutation_type == "revoke"
        assert result.after["mutation_type"] == "revoke"
        assert result.after["reason"] == "违规"


# ============================================================
# 7. 期限安全与撤销 tombstone 补充合同
# ============================================================


class TestExpiryAndTombstoneSafety:
    """expires_at=None 安全 / 无记录创建 tombstone / 重复撤销幂等。"""

    @pytest.mark.asyncio
    async def test_expires_at_none_safe(self):
        """expires_at=None 的行授权：从 now 计算，不报错，mutation_type=extend。"""
        session = _FakeSession()
        user_row = _user_row()

        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        existing = _cap_row(
            capability="market_data", source="admin_grant", expires_at=None,
        )

        async def _execute(stmt, *args, **kwargs):
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                return _FakeResult(user_row)
            return _FakeResult(existing, many=[existing])

        session.execute.side_effect = _execute

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            result = await apply_capability_grant(
                db=session,
                user_id=user_row.id,
                capability="market_data",
                grant_days=30,
                watchlist_limit=None,
                source="admin_grant",
                materialize_legacy=True,
                actor_user_id=uuid.uuid4(),
            )

        assert result.mutation_type == "extend"
        assert result.after["expires_at"] is not None
        assert result.after["active"] is True

    @pytest.mark.asyncio
    async def test_revoke_no_record_creates_tombstone(self):
        """无目标记录时创建撤销 tombstone（granted_by=None，active=False）。"""
        session = _FakeSession()
        user_row = _user_row()

        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        async def _execute(stmt, *args, **kwargs):
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                return _FakeResult(user_row)
            # ensure UserCapability.all()/Subscription.first() 均空 → 不物化；
            # 目标 capability 行 scalar_one_or_none → None → 创建 tombstone
            return _FakeResult(None)

        session.execute.side_effect = _execute

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            result = await revoke_capability_from_user(
                db=session,
                user_id=user_row.id,
                capability="market_data",
                revoked_by=uuid.uuid4(),
            )

        assert result.action == "revoke"
        assert result.after["active"] is False
        assert result.after["source"] == "admin_revoke"
        assert result.after["granted_by"] is None
        # tombstone 已 add 到 session
        assert session.add.called is True

    @pytest.mark.asyncio
    async def test_revoke_idempotent(self):
        """已撤销（admin_revoke）再次撤销：幂等返回，不重复插入。"""
        session = _FakeSession()
        user_row = _user_row()
        already_revoked = _cap_row(
            capability="market_data", source="admin_revoke",
            expires_at=_now() - timedelta(days=1),
        )

        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        async def _execute(stmt, *args, **kwargs):
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                return _FakeResult(user_row)
            return _FakeResult(already_revoked, many=[already_revoked])

        session.execute.side_effect = _execute

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            result = await revoke_capability_from_user(
                db=session,
                user_id=user_row.id,
                capability="market_data",
                revoked_by=uuid.uuid4(),
            )

        assert result.action == "revoke"
        assert result.after["active"] is False
        assert result.after["source"] == "admin_revoke"
        # 幂等：已是 admin_revoke，不 add 新行（不重复插入）
        assert session.add.called is False


# ============================================================
# 8. source/actor 合同（PV2-B08）
# ============================================================


class TestSourceActorContract:
    """admin_grant 必须有 actor；invite_code 必须无 actor；invite 默认 reason 非 admin_manual_grant。"""

    async def _invite_grant(self):
        """构造一个显式邀请码新注册（materialize_legacy=False, source=invite_code）mock 场景。"""
        session = _FakeSession()
        user_row = _user_row()
        session.execute.side_effect = _make_lock_execute(session, user_row)
        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta
            return await apply_capability_grant(
                db=session,
                user_id=user_row.id,
                capability="self_selection",
                grant_days=30,
                watchlist_limit=20,
                source="invite_code",
                materialize_legacy=False,
                actor_user_id=None,
            )

    @pytest.mark.asyncio
    async def test_admin_grant_requires_actor(self):
        """admin_grant + actor=None 必须抛 ValueError。"""
        session = _FakeSession()
        with pytest.raises(ValueError, match="admin_grant 必须提供 actor_user_id"):
            await apply_capability_grant(
                db=session,
                user_id=uuid.uuid4(),
                capability="market_data",
                grant_days=30,
                watchlist_limit=None,
                source="admin_grant",
                materialize_legacy=False,
                actor_user_id=None,
            )

    @pytest.mark.asyncio
    async def test_invite_code_rejects_actor(self):
        """invite_code + actor 非空必须抛 ValueError。"""
        session = _FakeSession()
        with pytest.raises(ValueError, match="invite_code 不允许提供 actor_user_id"):
            await apply_capability_grant(
                db=session,
                user_id=uuid.uuid4(),
                capability="market_data",
                grant_days=30,
                watchlist_limit=None,
                source="invite_code",
                materialize_legacy=False,
                actor_user_id=uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_invite_default_reason_not_admin_manual_grant(self):
        """邀请码授权默认 reason 不是 admin_manual_grant（应保持 None）。"""
        result = await self._invite_grant()
        assert result.after["reason"] is None
        assert result.after["reason"] != "admin_manual_grant"

    @pytest.mark.asyncio
    async def test_admin_grant_reason_default(self):
        """admin_grant 未提供 reason 时默认 admin_manual_grant。"""
        session = _FakeSession()
        user_row = _user_row()
        session.execute.side_effect = _make_lock_execute(session, user_row)
        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta
            result = await apply_capability_grant(
                db=session,
                user_id=user_row.id,
                capability="self_selection",
                grant_days=30,
                watchlist_limit=20,
                source="admin_grant",
                materialize_legacy=False,
                actor_user_id=uuid.uuid4(),
            )
        assert result.after["reason"] == "admin_manual_grant"


# ============================================================
# 9. 独立 quota change（PV2-B05）
# ============================================================


class TestQuotaChange:
    """change_self_selection_quota：纯 quota_change，不修改 expires_at；revoked 不可恢复。"""

    async def _run(self, existing_row, *, new_limit=30):
        session = _FakeSession()
        user_row = _user_row()

        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        async def _execute(stmt, *args, **kwargs):
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                return _FakeResult(user_row)
            return _FakeResult(existing_row, many=[existing_row] if existing_row else [])

        session.execute.side_effect = _execute

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta
            return session, await change_self_selection_quota(
                db=session,
                user_id=user_row.id,
                new_watchlist_limit=new_limit,
                actor_user_id=uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_quota_change_does_not_modify_expires_at(self):
        """纯 quota change 只改额度，不修改 expires_at。"""
        original_expiry = _now() + timedelta(days=50)
        existing = _cap_row(
            capability="self_selection", source="admin_grant",
            expires_at=original_expiry, watchlist_limit=20,
        )
        session, result = await self._run(existing, new_limit=30)
        assert result.action == "grant"
        assert result.mutation_type == "quota_change"
        assert result.after["watchlist_limit"] == 30
        # expires_at 保持不变（原有期限不被修改）
        assert result.after["expires_at"] == original_expiry

    @pytest.mark.asyncio
    async def test_quota_change_rejects_revoked(self):
        """revoked 状态不能通过调整额度恢复。"""
        existing = _cap_row(
            capability="self_selection", source="admin_revoke",
            expires_at=_now() - timedelta(days=1), watchlist_limit=20,
        )
        with pytest.raises(ValueError, match="revoked"):
            await self._run(existing, new_limit=30)

    @pytest.mark.asyncio
    async def test_quota_change_requires_existing_record(self):
        """无显式 self_selection 记录时调整额度失败。"""
        with pytest.raises(ValueError, match="无显式 self_selection"):
            await self._run(None, new_limit=30)


# ============================================================
# 10. 商业状态三入口复用（PV2-B06）
# ============================================================


class TestCommercialStatusReuse:
    """get_effective_subscription_status / list_subscribers / access-profile 三入口
    对相同 Subscription 返回相同商业状态（均复用 resolve_commercial_status）。"""

    def _sub(self, *, status="active", starts_at=None, expires_at=None):
        return SimpleNamespace(
            status=status,
            starts_at=starts_at or (_now() - timedelta(days=1)),
            expires_at=expires_at or (_now() + timedelta(days=30)),
            plan_code="observe_20",
        )

    @pytest.mark.asyncio
    async def test_get_effective_subscription_status_active(self):
        """get_effective_subscription_status 复用解析器返回 active。"""
        sub = self._sub()
        session = _FakeSession()
        session.execute.return_value = _FakeResult(sub)
        status, _ = await get_effective_subscription_status(session, uuid.uuid4())
        assert status == "active"

    @pytest.mark.asyncio
    async def test_get_effective_subscription_status_expired(self):
        """过期订阅复用解析器返回 expired。"""
        sub = self._sub(expires_at=_now() - timedelta(days=1))
        session = _FakeSession()
        session.execute.return_value = _FakeResult(sub)
        status, _ = await get_effective_subscription_status(session, uuid.uuid4())
        assert status == "expired"

    @pytest.mark.asyncio
    async def test_get_effective_subscription_status_revoked(self):
        """revoked 订阅复用解析器返回 revoked。"""
        sub = self._sub(status="revoked")
        session = _FakeSession()
        session.execute.return_value = _FakeResult(sub)
        status, _ = await get_effective_subscription_status(session, uuid.uuid4())
        assert status == "revoked"

    @pytest.mark.asyncio
    async def test_list_subscribers_reuses_commercial_status(self):
        """list_subscribers 的 membership_status 复用解析器（active 正常周期）。"""
        sub = self._sub()
        user = SimpleNamespace(id=uuid.uuid4(), email="u@e.com", status="active", created_at=_now())
        session = _FakeSession()

        # 第 1 次 execute：count 查询 scalar_one -> total；第 2 次：列表查询 all() -> [(user, sub)]
        calls = {"n": 0}

        async def _execute(stmt, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResult(1)
            return _FakeResult(many=[(user, sub)])

        session.execute.side_effect = _execute
        with patch(
            "app.services.subscription_service.get_renewal_count",
            AsyncMock(return_value=0),
        ):
            rows, total = await list_subscribers(session, limit=10)
        assert total == 1
        assert len(rows) == 1
        assert rows[0]["membership_status"] == "active"

    @pytest.mark.asyncio
    async def test_commercial_status_direct_consistency(self):
        """三入口与唯一解析器对相同订阅语义一致（active 场景）。"""
        sub = self._sub()
        commercial = resolve_commercial_status(sub)
        # list_subscribers / access-profile 均以 resolve_commercial_status 为唯一来源
        assert commercial.status == "active"


# ============================================================
# 11. access-profile 真正 Schema 化（PV2-B06）：受限类型与 datetime
# ============================================================


class TestAccessProfileSchema:
    """管理员 access-profile 分层 Schema：受限 Literal、datetime 序列化、非法值失败。"""

    def _profile_payload(self):
        return {
            "account": {
                "id": uuid.uuid4(),
                "email": "u@e.com",
                "account_status": "active",
                "roles": ["member"],
                "created_at": _now(),
                "last_login_at": _now(),
            },
            "effective_access": {
                "capabilities": {},
                "active_capability_keys": [],
                "has_any_access": True,
                "default_route": "/overview",
                "capability_source": "user_capabilities",
                "nearest_capability_expires_at": _now() + timedelta(days=30),
                "legacy_fallback": False,
                "diagnostics": [],
            },
            "subscription_summary": {
                "status": "active",
                "reason": "active",
                "plan_code": "observe_20",
                "plan_display_name": None,
                "starts_at": _now() - timedelta(days=1),
                "expires_at": _now() + timedelta(days=30),
                "source": "invite",
                "entitlement_snapshot": None,
            },
            "explicit_capability_records": [
                {
                    "capability": "market_data",
                    "state": "active",
                    "granted_at": _now(),
                    "expires_at": _now() + timedelta(days=30),
                    "watchlist_limit": None,
                    "source": "admin_grant",
                    "granted_by": uuid.uuid4(),
                }
            ],
        }

    def test_full_profile_validates(self):
        """完整 access-profile 响应通过 Schema 校验。"""
        resp = AdminAccessProfileResponse.model_validate(self._profile_payload())
        assert resp.subscription_summary.status == "active"
        assert len(resp.explicit_capability_records) == 1
        assert resp.explicit_capability_records[0].state == "active"

    def test_invalid_capability_state_fails(self):
        """非法 capability state（如 bogus）验证失败。"""
        from pydantic import ValidationError

        payload = self._profile_payload()
        payload["explicit_capability_records"][0]["state"] = "bogus"
        with pytest.raises(ValidationError):
            AdminAccessProfileResponse.model_validate(payload)

    def test_invalid_subscription_status_fails(self):
        """非法订阅商业状态验证失败。"""
        from pydantic import ValidationError

        payload = self._profile_payload()
        payload["subscription_summary"]["status"] = "activeish"
        with pytest.raises(ValidationError):
            AdminAccessProfileResponse.model_validate(payload)

    def test_datetime_fields_are_not_str(self):
        """access-profile 时间字段类型为 datetime，由 Pydantic 序列化为 ISO 字符串。"""
        from app.schemas.access import (
            CAPABILITY_STATE_LITERAL,
            COMMERCIAL_STATUS_LITERAL,
        )

        assert SubscriptionSummaryInfo.model_fields["starts_at"].annotation == datetime | None
        assert SubscriptionSummaryInfo.model_fields["expires_at"].annotation == datetime | None
        assert AdminAccountInfo.model_fields["created_at"].annotation == datetime | None
        assert AdminAccountInfo.model_fields["last_login_at"].annotation == datetime | None
        assert EffectiveAccessInfo.model_fields[
            "nearest_capability_expires_at"
        ].annotation == datetime | None
        assert ExplicitCapabilityRecord.model_fields["granted_at"].annotation == datetime | None
        assert ExplicitCapabilityRecord.model_fields["expires_at"].annotation == datetime | None
        assert COMMERCIAL_STATUS_LITERAL is not None
        assert CAPABILITY_STATE_LITERAL is not None

    def test_datetime_serializes_to_iso(self):
        """完整 profile 序列化为 JSON 时 datetime 字段为 ISO 字符串。"""
        resp = AdminAccessProfileResponse.model_validate(self._profile_payload())
        data = resp.model_dump(mode="json")
        assert isinstance(data["account"]["created_at"], str)
        assert isinstance(data["subscription_summary"]["expires_at"], str)
        assert isinstance(data["explicit_capability_records"][0]["expires_at"], str)
        # UUID 序列化为字符串
        assert isinstance(data["account"]["id"], str)
        assert isinstance(data["explicit_capability_records"][0]["granted_by"], str)


# ============================================================
# 12. 审计证据（PV2-B09）：request_id 传递 + 首次物化列表进入审计
# ============================================================


class TestAuditEvidence:
    """write_audit_log 传递 request_id；首次物化时 materialized_capabilities 非空。"""

    @pytest.mark.asyncio
    async def test_write_audit_log_persists_request_id(self):
        """write_audit_log 接收 request_id 并写入审计对象（不伪造随机值）。"""
        from app.services.access_audit_service import write_audit_log

        session = _FakeSession()
        log = await write_audit_log(
            db=session,
            actor_user_id=uuid.uuid4(),
            action="capability.grant",
            target_type="user_capability",
            target_id="u:market_data",
            after_data={"mutation_type": "grant"},
            request_id="req-abc-123",
        )
        assert log.request_id == "req-abc-123"
        assert session.add.called is True

    @pytest.mark.asyncio
    async def test_write_audit_log_request_id_optional(self):
        """无 request_id 时（请求链未提供）request_id 为 None，不伪造随机值。"""
        from app.services.access_audit_service import write_audit_log

        session = _FakeSession()
        log = await write_audit_log(
            db=session,
            actor_user_id=uuid.uuid4(),
            action="capability.revoke",
            target_type="user_capability",
            target_id="u:market_data",
            after_data={"mutation_type": "revoke"},
        )
        assert log.request_id is None

    @pytest.mark.asyncio
    async def test_apply_first_materialize_returns_snapshot(self):
        """管理员首次操作触发 legacy 物化时，materialized_capabilities 非空。"""
        session = _FakeSession()
        user_row = _user_row()
        sub = SimpleNamespace(
            plan_code="observe_20", status="active",
            starts_at=_now() - timedelta(days=1),
            expires_at=_now() + timedelta(days=30),
        )

        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        calls = {"n": 0}

        async def _execute(stmt, *args, **kwargs):
            calls["n"] += 1
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                return _FakeResult(user_row)
            if calls["n"] == 2:
                # ensure: UserCapability.all() 空（无显式记录）
                return _FakeResult(None)
            if calls["n"] == 3:
                # ensure: Subscription.first() -> sub（触发物化）
                return _FakeResult(sub)
            # apply 目标 capability 行 -> None（新建）
            return _FakeResult(None)

        session.execute.side_effect = _execute

        from app.services import effective_access_service as eas
        from app.services import plan_service

        plan = SimpleNamespace(monitor_limit=20)
        inferred = {
            "market_data": {"watchlist_limit": None, "expires_at": _now() + timedelta(days=30)},
            "self_selection": {"watchlist_limit": 20, "expires_at": _now() + timedelta(days=30)},
        }

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ), patch.object(plan_service, "get_plan", new=AsyncMock(return_value=plan)), patch.object(
            eas, "infer_capabilities_from_plan", return_value=inferred
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            result = await apply_capability_grant(
                db=session,
                user_id=user_row.id,
                capability="market_data",
                grant_days=30,
                watchlist_limit=None,
                source="admin_grant",
                materialize_legacy=True,
                actor_user_id=uuid.uuid4(),
            )

        # 首次物化：materialized_capabilities 含 legacy 推导出的 Capability 快照
        assert len(result.materialized_capabilities) >= 1
        assert all(
            item.get("source") == "legacy_materialized"
            for item in result.materialized_capabilities
        )

    @pytest.mark.asyncio
    async def test_no_materialize_returns_empty(self):
        """非首次（已有显式记录）不物化时 materialized_capabilities 为空。"""
        existing = _cap_row(capability="market_data", source="admin_grant")
        session = _FakeSession()
        user_row = _user_row()

        from sqlalchemy import select as real_select

        def _patched_select(*cols, **kwargs):
            stmt = real_select(*cols, **kwargs)
            for c in cols:
                from app.models.user import User as _User
                if isinstance(c, type) and issubclass(c, _User):
                    object.__setattr__(stmt, "_is_user_lock", True)
            return stmt

        async def _execute(stmt, *args, **kwargs):
            if getattr(stmt, "_is_user_lock", False):
                session.locked_user = user_row
                return _FakeResult(user_row)
            # ensure: UserCapability.all() 非空（已有显式记录）-> 不物化，返回空；
            # apply 目标行 -> existing
            return _FakeResult(existing, many=[existing])

        session.execute.side_effect = _execute

        with patch(
            "app.services.subscription_service.datetime"
        ) as mock_dt, patch(
            "app.services.subscription_service.select", side_effect=_patched_select
        ):
            mock_dt.now.return_value = _now()
            mock_dt.UTC = UTC
            mock_dt.timedelta = timedelta

            result = await apply_capability_grant(
                db=session,
                user_id=user_row.id,
                capability="market_data",
                grant_days=30,
                watchlist_limit=None,
                source="admin_grant",
                materialize_legacy=True,
                actor_user_id=uuid.uuid4(),
            )

        assert result.materialized_capabilities == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
