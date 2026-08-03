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

from app.schemas.access import AccessProfileResponse
from app.schemas.subscription import GrantCapabilityRequest, RevokeCapabilityRequest
from app.services.subscription_service import (
    apply_capability_grant,
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
                granted_by=uuid.uuid4(),
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

    覆盖：
    - grant：无既有行，新建授权。
    - extend：已有行续期（active 顺延 / expired 从 now 重算，额度不变或非 self_selection）。
    - quota_change：已有行过期且仅调整 self_selection 额度。
    - extend_and_quota_change：已有行 active 且续期 + 调整额度。
    - regrant：tombstone（admin_revoke）重新授权。
    - revoke：撤销（mutation_type="revoke"）。
    """

    async def _run_apply(
        self,
        existing_row,
        *,
        capability="market_data",
        watchlist_limit=None,
        source="admin_grant",
        materialize_legacy=True,
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
                granted_by=uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_new_row_grant(self):
        """无既有行 → mutation_type=grant。"""
        result = await self._run_apply(None, materialize_legacy=False)
        assert result.action == "grant"
        assert result.mutation_type == "grant"
        assert result.after["mutation_type"] == "grant"

    @pytest.mark.asyncio
    async def test_active_extend(self):
        """active 已有行续期（market_data，无额度变化）→ mutation_type=extend。"""
        existing = _cap_row(
            capability="market_data", source="admin_grant",
            expires_at=_now() + timedelta(days=10),
        )
        result = await self._run_apply(existing, capability="market_data")
        assert result.mutation_type == "extend"

    @pytest.mark.asyncio
    async def test_expired_quota_change(self):
        """过期 self_selection 行仅调整额度 → mutation_type=quota_change。"""
        existing = _cap_row(
            capability="self_selection", source="admin_grant",
            expires_at=_now() - timedelta(days=1), watchlist_limit=20,
        )
        result = await self._run_apply(
            existing, capability="self_selection", watchlist_limit=30
        )
        assert result.mutation_type == "quota_change"
        assert result.after["watchlist_limit"] == 30

    @pytest.mark.asyncio
    async def test_active_extend_and_quota_change(self):
        """active self_selection 行续期 + 调整额度 → extend_and_quota_change。"""
        existing = _cap_row(
            capability="self_selection", source="admin_grant",
            expires_at=_now() + timedelta(days=10), watchlist_limit=20,
        )
        result = await self._run_apply(
            existing, capability="self_selection", watchlist_limit=30
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
        result = await self._run_apply(existing, capability="market_data")
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
                granted_by=uuid.uuid4(),
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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
