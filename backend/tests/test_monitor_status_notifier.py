"""Tests for the Monitor status notifier extraction (PANJI-GOV-W4B2).

These lock the six W4B2 audit questions without a real Redis / Postgres / Feishu:

* ``_notify_monitor_status`` fully left ``app.worker`` (now ``monitor_status_notifier``).
* ``_monitor_start_notified`` migrated with the notifier (in-process fallback owner).
* The runtime only *calls* the notifier; the notification logic is not duplicated there.
* Redis SET NX EX(7d) -> in-process fallback -> DB query -> channel filter -> DTO ->
  per-channel adapter delivery ordering is preserved.
* Startup = admin-only; error = all active channels; error bypasses Redis idempotency.
* A notifier-internal failure never propagates out of ``notify_monitor_status``.

Heavy deps (sqlalchemy models, redis client, channel adapter) stay function-local lazy
imports in the notifier, so the tests monkeypatch the module attributes they resolve at
call time.  The session factory and logger are injected by the caller (the runtime).
"""

import asyncio
import inspect
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services import monitor_status_notifier as notifier
from app.services.monitor_status_notifier import notify_monitor_status

_FIXED_NOW = datetime(2026, 1, 1, 9, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def _clear_notified() -> None:
    notifier._monitor_start_notified.clear()
    yield


class _FakeRedis:
    def __init__(self, set_result=True, raise_exc=None):  # noqa: ANN001
        self._set_result = set_result
        self._raise = raise_exc
        self.set_called = 0
        self.set_calls: list[tuple[str, bool, int]] = []

    async def set(self, key, value, *, nx, ex):  # noqa: ANN001
        self.set_called += 1
        self.set_calls.append((key, nx, ex))
        if self._raise is not None:
            raise self._raise
        return self._set_result


class _FakeScalars:
    def __init__(self, channels):  # noqa: ANN001
        self._channels = channels

    def all(self):
        return self._channels


class _FakeResult:
    def __init__(self, channels):  # noqa: ANN001
        self._channels = channels

    def scalars(self):
        return _FakeScalars(self._channels)


class _FakeSession:
    def __init__(self, channels, raise_on_execute=None):  # noqa: ANN001
        self._channels = channels
        self._raise = raise_on_execute
        self.execute_called = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):  # noqa: ANN001
        self.execute_called += 1
        if self._raise is not None:
            raise self._raise
        return _FakeResult(self._channels)


def _make_session_factory(channels, *, raise_on_execute=None, calls=None):  # noqa: ANN001
    calls = calls if calls is not None else []

    def _factory():
        calls.append(1)
        return _FakeSession(channels, raise_on_execute=raise_on_execute)

    return _factory


class _Chan:
    def __init__(self, uid, adapter_type="feishu_platform_app", target_config=None):  # noqa: ANN001
        self.user_id = uid
        self.adapter_type = adapter_type
        self.target_config = target_config or {}


class _FakeDTO:
    def __init__(self, *, title, message_type, template_key, template_version, summary, data_time, resource_refs):  # noqa: ANN001
        self.title = title
        self.message_type = message_type
        self.template_key = template_key
        self.template_version = template_version
        self.summary = summary
        self.data_time = data_time
        self.resource_refs = resource_refs


class _FakeDelivery:
    def __init__(self, success=True, error_message=None):  # noqa: ANN001
        self.success = success
        self.error_message = error_message


class _FakeAdapter:
    def __init__(self, fail_for_target_config=False):  # noqa: ANN001
        self._fail = fail_for_target_config
        self.sent: list[tuple[_FakeDTO, dict]] = []

    async def send(self, dto, target_config):  # noqa: ANN001
        self.sent.append((dto, target_config))
        if self._fail and target_config.get("fail"):
            raise RuntimeError("adapter boom")
        return _FakeDelivery(success=True)


# --------------------------------------------------------------------------- #
# Behavioral tests
# --------------------------------------------------------------------------- #
def test_startup_redis_duplicate_skips_db(monkeypatch) -> None:  # noqa: ANN001
    redis = _FakeRedis(set_result=False)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: redis)
    adapter = _FakeAdapter()
    monkeypatch.setattr("app.services.channel_adapter.get_adapter", lambda at: adapter)  # noqa: ANN001
    monkeypatch.setattr("app.schemas.notification.NotificationMessageDTO", _FakeDTO)
    monkeypatch.setattr("app.core.time.now_shanghai", lambda: _FIXED_NOW)
    monkeypatch.setenv("GIT_SHA", "abc123")

    calls: list[int] = []
    sf = _make_session_factory([_Chan("u1")], calls=calls)
    logger = logging.getLogger("test-notifier")
    asyncio.run(notify_monitor_status("监控服务已启动", "x", session_factory=sf, logger=logger))

    # exact idempotency key + NX + 7-day TTL
    assert redis.set_called == 1
    key, nx, ex = redis.set_calls[0]
    assert key == "monitor-start:abc123"
    assert nx is True
    assert ex == 7 * 86400
    # duplicate -> returned before any DB query / channel delivery
    assert calls == []
    assert adapter.sent == []


def test_redis_failure_fallback_in_process_set(monkeypatch) -> None:  # noqa: ANN001
    redis = _FakeRedis(raise_exc=RuntimeError("redis down"))
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: redis)
    adapter = _FakeAdapter()
    monkeypatch.setattr("app.services.channel_adapter.get_adapter", lambda at: adapter)  # noqa: ANN001
    monkeypatch.setattr("app.schemas.notification.NotificationMessageDTO", _FakeDTO)
    monkeypatch.setattr("app.core.time.now_shanghai", lambda: _FIXED_NOW)
    monkeypatch.setenv("GIT_SHA", "sha-fallback")

    calls: list[int] = []
    sf = _make_session_factory([], calls=calls)
    logger = logging.getLogger("test-notifier")

    # first call: redis raises -> add sha to in-process set -> continue to DB
    asyncio.run(notify_monitor_status("监控服务已启动", "x", session_factory=sf, logger=logger))
    assert "sha-fallback" in notifier._monitor_start_notified
    assert len(calls) == 1

    # second call (same sha): redis raises again -> in-process set short-circuits (no DB)
    asyncio.run(notify_monitor_status("监控服务已启动", "x", session_factory=sf, logger=logger))
    assert len(calls) == 1


def test_error_notification_bypasses_redis(monkeypatch) -> None:  # noqa: ANN001
    redis = _FakeRedis(set_result=True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: redis)
    adapter = _FakeAdapter()
    monkeypatch.setattr("app.services.channel_adapter.get_adapter", lambda at: adapter)  # noqa: ANN001
    monkeypatch.setattr("app.schemas.notification.NotificationMessageDTO", _FakeDTO)
    monkeypatch.setattr("app.core.time.now_shanghai", lambda: _FIXED_NOW)

    calls: list[int] = []
    sf = _make_session_factory([_Chan("u1")], calls=calls)
    logger = logging.getLogger("test-notifier")
    asyncio.run(notify_monitor_status("监控服务异常", "boom", is_error=True, session_factory=sf, logger=logger))

    # Redis must NOT be touched for error notifications
    assert redis.set_called == 0
    # DB queried + adapter used
    assert len(calls) == 1
    assert len(adapter.sent) == 1


def test_no_channels_safe_return(monkeypatch) -> None:  # noqa: ANN001
    redis = _FakeRedis(set_result=True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: redis)
    adapter = _FakeAdapter()
    monkeypatch.setattr("app.services.channel_adapter.get_adapter", lambda at: adapter)  # noqa: ANN001
    monkeypatch.setattr("app.schemas.notification.NotificationMessageDTO", _FakeDTO)
    monkeypatch.setattr("app.core.time.now_shanghai", lambda: _FIXED_NOW)
    monkeypatch.setenv("GIT_SHA", "nocl")

    calls: list[int] = []
    sf = _make_session_factory([], calls=calls)
    logger = logging.getLogger("test-notifier")
    # must not raise
    asyncio.run(notify_monitor_status("监控服务已启动", "x", session_factory=sf, logger=logger))
    # DB queried, but no delivery (no channels)
    assert len(calls) == 1
    assert adapter.sent == []


def test_notifier_db_failure_swallowed(monkeypatch) -> None:  # noqa: ANN001
    redis = _FakeRedis(set_result=True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: redis)
    adapter = _FakeAdapter()
    monkeypatch.setattr("app.services.channel_adapter.get_adapter", lambda at: adapter)  # noqa: ANN001
    monkeypatch.setattr("app.schemas.notification.NotificationMessageDTO", _FakeDTO)
    monkeypatch.setattr("app.core.time.now_shanghai", lambda: _FIXED_NOW)
    monkeypatch.setenv("GIT_SHA", "dbboom")

    sf = _make_session_factory([], raise_on_execute=RuntimeError("db boom"))
    logger = logging.getLogger("test-notifier")
    # must not propagate
    asyncio.run(notify_monitor_status("监控服务已启动", "x", session_factory=sf, logger=logger))


def test_notifier_dto_contract(monkeypatch) -> None:  # noqa: ANN001
    redis = _FakeRedis(set_result=True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: redis)
    adapter = _FakeAdapter()
    monkeypatch.setattr("app.services.channel_adapter.get_adapter", lambda at: adapter)  # noqa: ANN001
    captured: dict = {}
    monkeypatch.setattr(
        "app.schemas.notification.NotificationMessageDTO",
        lambda **kw: captured.update(kw) or _FakeDTO(**kw),
    )
    monkeypatch.setattr("app.core.time.now_shanghai", lambda: _FIXED_NOW)
    monkeypatch.setenv("GIT_SHA", "dtoc")

    content = "x" * 500  # longer than 200 to test truncation
    sf = _make_session_factory([_Chan("u1")])
    logger = logging.getLogger("test-notifier")
    asyncio.run(notify_monitor_status("监控服务已启动", content, session_factory=sf, logger=logger))

    assert captured["message_type"] == "SYSTEM_ALERT"
    assert captured["template_key"] == "system_alert"
    assert captured["template_version"] == "1.1.0"
    assert captured["summary"] == content[:200]
    assert captured["resource_refs"] == {}
    assert captured["data_time"] == _FIXED_NOW.isoformat()
    assert captured["title"] == "✅ 监控服务已启动"

    key, nx, ex = redis.set_calls[0]
    assert key == "monitor-start:dtoc"
    assert nx is True
    assert ex == 7 * 86400


def test_error_notification_emoji(monkeypatch) -> None:  # noqa: ANN001
    redis = _FakeRedis(set_result=True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: redis)
    adapter = _FakeAdapter()
    monkeypatch.setattr("app.services.channel_adapter.get_adapter", lambda at: adapter)  # noqa: ANN001
    captured: dict = {}
    monkeypatch.setattr(
        "app.schemas.notification.NotificationMessageDTO",
        lambda **kw: captured.update(kw) or _FakeDTO(**kw),
    )
    monkeypatch.setattr("app.core.time.now_shanghai", lambda: _FIXED_NOW)

    sf = _make_session_factory([_Chan("u1")])
    logger = logging.getLogger("test-notifier")
    asyncio.run(notify_monitor_status("监控服务异常", "boom", is_error=True, session_factory=sf, logger=logger))
    assert captured["title"] == "❌ 监控服务异常"


def test_per_channel_isolation(monkeypatch) -> None:  # noqa: ANN001
    redis = _FakeRedis(set_result=True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: redis)
    adapter = _FakeAdapter(fail_for_target_config=True)
    monkeypatch.setattr("app.services.channel_adapter.get_adapter", lambda at: adapter)  # noqa: ANN001
    monkeypatch.setattr("app.schemas.notification.NotificationMessageDTO", _FakeDTO)
    monkeypatch.setattr("app.core.time.now_shanghai", lambda: _FIXED_NOW)
    monkeypatch.setenv("GIT_SHA", "iso")

    ch1 = _Chan("fail-user", target_config={"fail": True})
    ch2 = _Chan("ok-user", target_config={})
    sf = _make_session_factory([ch1, ch2])
    logger = logging.getLogger("test-notifier")
    # must not raise despite ch1 adapter failure
    asyncio.run(notify_monitor_status("监控服务已启动", "x", session_factory=sf, logger=logger))
    # both channels attempted; ch2 still delivered
    assert len(adapter.sent) == 2


# --------------------------------------------------------------------------- #
# Source contract (locks the SQL filters / DTO schema / TODO, no drift)
# --------------------------------------------------------------------------- #
def test_notifier_source_contract() -> None:
    src = inspect.getsource(notify_monitor_status)
    # idempotency key + redis flags
    assert "monitor-start:{git_sha}" in src
    assert "nx=True" in src
    assert "ex=7 * 86400" in src
    # channel base filter
    assert 'adapter_type == "feishu_platform_app"' in src
    assert 'status == "active"' in src
    # startup admin-only filter
    assert 'Role.name == "admin"' in src
    # DTO schema
    assert 'message_type="SYSTEM_ALERT"' in src
    assert 'template_key="system_alert"' in src
    assert 'template_version="1.1.0"' in src
    assert "content[:200]" in src
    assert "resource_refs={}" in src
    # error path never touches Redis idempotency / in-process set; Outbox note kept
    assert "TODO: [monitor_scheduler]" in src
    # notifier uses the injected session factory, not a hardcoded one
    assert "async with session_factory() as db:" in src
    # notifier owns the in-process fallback set (moved from worker)
    assert "_monitor_start_notified" in src
