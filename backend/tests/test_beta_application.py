"""内测申请后端闭环测试 (Task 2, SubTask 2.7).

TDD 红灯阶段：先写失败测试，再实现业务代码。

测试内容（对应 spec 第三节"内测申请后端闭环"）：
1. 合法申请写入（返回 201 + 申请编号）
2. 微信和手机号都为空时拒绝（422）
3. "其他"无说明时拒绝（422）
4. 重复提交返回原申请（200 + 原记录）
5. IP 限流（同 IP 1h 内 >5 次返回 429）
6. 手机格式校验（非法格式 422）
7. 日志脱敏（不输出完整手机号/微信号）
8. Outbox 事件写入（beta_application_admin）

测试策略：
- service 层测试使用真实 PostgreSQL 测试库（允许 commit），测试后清理 beta_applications
- schema 层测试不需要 DB，仅验证 Pydantic 校验
- API 层测试使用 httpx ASGITransport + get_db override
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.beta_application import (
    BETA_APPLICATION_STATUSES,
    BETA_APPLICATION_STATUSES_DEFAULT,
    REASON_CODES,
)
from app.models.beta_application import BetaApplication
from app.models.outbox import Outbox
from app.schemas.beta_application import (
    BetaApplicationCreate,
)
from app.services.beta_application_service import create_application

# ============================================================
# 测试 fixtures
# ============================================================


@pytest_asyncio.fixture
async def beta_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Beta application 测试用 DB session。

    使用真实 PostgreSQL 测试库（允许 commit），测试后清理 beta_applications
    与对应的 outbox 记录，保证测试隔离。

    注意：不使用 conftest.db_session 的 nested transaction 模式，因为
    create_application 内部需要 db.commit()（先提交申请再写 Outbox）。
    """
    from tests.conftest import TestAsyncSessionLocal

    session = TestAsyncSessionLocal()
    try:
        yield session
    finally:
        # 清理：删除本测试创建的 beta_applications 和对应 outbox
        try:
            await session.rollback()
        except Exception:
            pass
        await session.execute(
            text("DELETE FROM outbox WHERE event_type = 'beta_application.admin_notification.created'")
        )
        await session.execute(text("DELETE FROM beta_applications"))
        await session.commit()
        await session.close()


@pytest_asyncio.fixture
async def beta_api_client(beta_db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """提供 httpx AsyncClient，get_db 注入为 beta_db_session。"""
    from app.db import get_db as db_get_db
    from app.main import app

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield beta_db_session

    app.dependency_overrides[db_get_db] = get_test_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _hash_ip(ip: str) -> str:
    """计算 IP 的 SHA256 哈希（与 API 层一致）。"""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def _make_payload(
    *,
    wechat: str | None = "test_wechat_123",
    phone: str | None = None,
    watch_stock_count: int = 10,
    reason_code: str = "busy",
    reason_other: str | None = None,
    privacy_agreed: bool = True,
) -> dict:
    """构造合法的请求 payload（可覆盖个别字段）。"""
    payload: dict = {
        "watch_stock_count": watch_stock_count,
        "reason_code": reason_code,
        "privacy_agreed": privacy_agreed,
    }
    if wechat is not None:
        payload["wechat"] = wechat
    if phone is not None:
        payload["phone"] = phone
    if reason_other is not None:
        payload["reason_other"] = reason_other
    return payload


# ============================================================
# SubTask 2.3: 常量枚举测试
# ============================================================


def test_reason_codes_contains_all_required_values():
    """REASON_CODES 必须包含 busy/too_many/forget/quant/other。"""
    assert set(REASON_CODES) == {"busy", "too_many", "forget", "quant", "other"}


def test_beta_application_statuses_contains_all_required_values():
    """BETA_APPLICATION_STATUSES 必须包含 new/contacted/approved/rejected/converted。"""
    assert set(BETA_APPLICATION_STATUSES) == {
        "new",
        "contacted",
        "approved",
        "rejected",
        "converted",
    }


def test_beta_application_default_status_is_new():
    """默认状态必须为 'new'。"""
    assert BETA_APPLICATION_STATUSES_DEFAULT == "new"


# ============================================================
# SubTask 2.4: Schema 校验测试（无需 DB）
# ============================================================


def test_schema_accepts_valid_payload_with_wechat():
    """合法 payload（仅微信号）通过校验。"""
    payload = _make_payload(wechat="valid_wechat", phone=None)
    obj = BetaApplicationCreate(**payload)
    assert obj.wechat == "valid_wechat"
    assert obj.phone is None
    assert obj.watch_stock_count == 10


def test_schema_accepts_valid_payload_with_phone():
    """合法 payload（仅手机号）通过校验。"""
    payload = _make_payload(wechat=None, phone="13800138000")
    obj = BetaApplicationCreate(**payload)
    assert obj.phone == "13800138000"
    assert obj.wechat is None


def test_schema_rejects_no_contact():
    """微信和手机号都为空时拒绝（422）。"""
    from pydantic import ValidationError

    payload = _make_payload(wechat=None, phone=None)
    with pytest.raises(ValidationError) as exc_info:
        BetaApplicationCreate(**payload)
    assert "至少填写一种联系方式" in str(exc_info.value)


def test_schema_rejects_other_without_reason():
    """选择 'other' 但未填写说明时拒绝（422）。"""
    from pydantic import ValidationError

    payload = _make_payload(reason_code="other", reason_other=None)
    with pytest.raises(ValidationError) as exc_info:
        BetaApplicationCreate(**payload)
    assert "补充说明" in str(exc_info.value)


def test_schema_accepts_other_with_reason():
    """选择 'other' 并填写说明时通过校验。"""
    payload = _make_payload(reason_code="other", reason_other="我的使用理由是...")
    obj = BetaApplicationCreate(**payload)
    assert obj.reason_code == "other"
    assert obj.reason_other == "我的使用理由是..."


def test_schema_rejects_invalid_phone_format():
    """手机号格式非法时拒绝（422）。"""
    from pydantic import ValidationError

    payload = _make_payload(wechat=None, phone="12345")
    with pytest.raises(ValidationError) as exc_info:
        BetaApplicationCreate(**payload)
    assert "手机号" in str(exc_info.value)


def test_schema_rejects_invalid_reason_code():
    """reason_code 不在枚举中时拒绝。"""
    from pydantic import ValidationError

    payload = _make_payload(reason_code="invalid_code")
    with pytest.raises(ValidationError):
        BetaApplicationCreate(**payload)


def test_schema_rejects_zero_watch_stock_count():
    """盯盘股票数量必须为正整数。"""
    from pydantic import ValidationError

    payload = _make_payload(watch_stock_count=0)
    with pytest.raises(ValidationError):
        BetaApplicationCreate(**payload)


def test_schema_rejects_privacy_not_agreed():
    """未勾选隐私同意时拒绝。"""
    from pydantic import ValidationError

    payload = _make_payload(privacy_agreed=False)
    with pytest.raises(ValidationError):
        BetaApplicationCreate(**payload)


# ============================================================
# SubTask 2.1: Model 字段测试
# ============================================================


def test_beta_application_model_has_all_required_fields():
    """BetaApplication 模型必须包含 spec 要求的所有字段。"""
    cols = [c.name for c in BetaApplication.__table__.columns]
    required = [
        "id",
        "wechat",
        "phone",
        "watch_stock_count",
        "reason_code",
        "reason_other",
        "status",
        "source",
        "admin_note",
        "handled_by",
        "handled_at",
        "submitted_at",
        "updated_at",
        "ip_hash",
        "feishu_delivery_status",
        "feishu_delivered_at",
        "feishu_last_error",
    ]
    for field in required:
        assert field in cols, f"BetaApplication 缺少字段: {field}"


def test_beta_application_model_has_indexes():
    """BetaApplication 必须包含 status/submitted_at/ip_hash/phone/wechat 索引。"""
    idx_names = [idx.name for idx in BetaApplication.__table__.indexes]
    # 至少有覆盖这些列的索引
    all_indexed_cols: set[str] = set()
    for idx in BetaApplication.__table__.indexes:
        for col in idx.columns:
            all_indexed_cols.add(col.name)
    for required_col in ["status", "submitted_at", "ip_hash", "phone", "wechat"]:
        assert required_col in all_indexed_cols, (
            f"BetaApplication 缺少 {required_col} 列的索引"
        )


# ============================================================
# SubTask 2.5: Service 业务逻辑测试
# ============================================================


@pytest.mark.asyncio
async def test_create_application_success(beta_db_session: AsyncSession):
    """合法申请写入成功，返回 (application, is_new=True)。"""
    payload = BetaApplicationCreate(**_make_payload(wechat="test_user_001", phone=None))
    ip_hash = _hash_ip("192.168.1.1")

    app, is_new = await create_application(
        db=beta_db_session, payload=payload, ip_hash=ip_hash, source="landing_page"
    )

    assert is_new is True
    assert app.id is not None
    assert app.wechat == "test_user_001"
    assert app.phone is None
    assert app.watch_stock_count == 10
    assert app.reason_code == "busy"
    assert app.status == "new"
    assert app.source == "landing_page"
    assert app.ip_hash == ip_hash
    assert app.submitted_at is not None


@pytest.mark.asyncio
async def test_create_application_duplicate_returns_original(beta_db_session: AsyncSession):
    """同一联系方式 24h 内重复提交返回原申请（is_new=False）。"""
    payload = BetaApplicationCreate(**_make_payload(wechat="dup_user_001", phone=None))
    ip_hash = _hash_ip("192.168.1.2")

    app1, is_new1 = await create_application(
        db=beta_db_session, payload=payload, ip_hash=ip_hash
    )
    assert is_new1 is True

    # 重复提交（同 wechat）
    app2, is_new2 = await create_application(
        db=beta_db_session, payload=payload, ip_hash=ip_hash
    )
    assert is_new2 is False
    assert app2.id == app1.id  # 返回原申请

    # 验证数据库只有一条记录
    result = await beta_db_session.execute(
        select(BetaApplication).where(BetaApplication.wechat == "dup_user_001")
    )
    apps = list(result.scalars().all())
    assert len(apps) == 1


@pytest.mark.asyncio
async def test_create_application_duplicate_by_phone_returns_original(
    beta_db_session: AsyncSession,
):
    """同一手机号 24h 内重复提交返回原申请。"""
    payload1 = BetaApplicationCreate(
        **_make_payload(wechat=None, phone="13900139000")
    )
    ip_hash = _hash_ip("192.168.1.3")

    app1, is_new1 = await create_application(
        db=beta_db_session, payload=payload1, ip_hash=ip_hash
    )
    assert is_new1 is True

    # 重复提交（同 phone，不同 wechat）
    payload2 = BetaApplicationCreate(
        **_make_payload(wechat="another_wechat", phone="13900139000")
    )
    app2, is_new2 = await create_application(
        db=beta_db_session, payload=payload2, ip_hash=ip_hash
    )
    assert is_new2 is False
    assert app2.id == app1.id


@pytest.mark.asyncio
async def test_create_application_ip_rate_limit_raises_429(beta_db_session: AsyncSession):
    """同 IP 1h 内超过 5 次提交返回 429。"""
    from fastapi import HTTPException

    ip_hash = _hash_ip("10.0.0.1")

    # 前 5 次允许（不同联系方式，避免重复检测）
    for i in range(5):
        payload = BetaApplicationCreate(
            **_make_payload(wechat=f"rate_user_{i}", phone=None)
        )
        _, is_new = await create_application(
            db=beta_db_session, payload=payload, ip_hash=ip_hash
        )
        assert is_new is True

    # 第 6 次应被限流
    payload = BetaApplicationCreate(
        **_make_payload(wechat="rate_user_5", phone=None)
    )
    with pytest.raises(HTTPException) as exc_info:
        await create_application(db=beta_db_session, payload=payload, ip_hash=ip_hash)
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_create_application_logs_masked_phone(
    beta_db_session: AsyncSession, caplog: pytest.LogCaptureFixture
):
    """日志中不得输出完整手机号（只显示后 4 位）。"""
    payload = BetaApplicationCreate(
        **_make_payload(wechat=None, phone="13800138000")
    )
    ip_hash = _hash_ip("192.168.1.4")

    with caplog.at_level(logging.INFO, logger="beta_application_service"):
        await create_application(db=beta_db_session, payload=payload, ip_hash=ip_hash)

    log_text = caplog.text
    # 完整手机号不得出现在日志中
    assert "13800138000" not in log_text, (
        f"日志中出现了完整手机号: {log_text}"
    )
    # 脱敏后的后 4 位应出现
    assert "8000" in log_text, f"日志中未出现脱敏手机号后4位: {log_text}"


@pytest.mark.asyncio
async def test_create_application_logs_masked_wechat(
    beta_db_session: AsyncSession, caplog: pytest.LogCaptureFixture
):
    """日志中不得输出完整微信号（只显示后 4 位）。"""
    payload = BetaApplicationCreate(
        **_make_payload(wechat="my_secret_wechat_id", phone=None)
    )
    ip_hash = _hash_ip("192.168.1.5")

    with caplog.at_level(logging.INFO, logger="beta_application_service"):
        await create_application(db=beta_db_session, payload=payload, ip_hash=ip_hash)

    log_text = caplog.text
    assert "my_secret_wechat_id" not in log_text, (
        f"日志中出现了完整微信号: {log_text}"
    )
    # 脱敏后 4 位应出现
    assert "t_id" in log_text or "id" in log_text, (
        f"日志中未出现脱敏微信号后4位: {log_text}"
    )


@pytest.mark.asyncio
async def test_create_application_writes_outbox_event(beta_db_session: AsyncSession):
    """成功创建申请后写入 Outbox 事件（event_type=beta_application_admin）。"""
    payload = BetaApplicationCreate(
        **_make_payload(wechat="outbox_user_001", phone=None)
    )
    ip_hash = _hash_ip("192.168.1.6")

    app, is_new = await create_application(
        db=beta_db_session, payload=payload, ip_hash=ip_hash
    )
    assert is_new is True

    # 查询 outbox 表
    result = await beta_db_session.execute(
        select(Outbox)
        .where(Outbox.event_type == "beta_application.admin_notification.created")
        .where(Outbox.aggregate_id == app.id)
    )
    outbox_records = list(result.scalars().all())
    assert len(outbox_records) >= 1, "未写入 beta_application_admin Outbox 事件"
    outbox = outbox_records[0]
    assert outbox.aggregate_type == "beta_application"
    assert outbox.status == "pending"


# ============================================================
# SubTask 2.6: API 端点测试
# ============================================================


@pytest.mark.asyncio
async def test_api_post_returns_201(beta_api_client: AsyncClient):
    """POST /public/beta-applications 合法请求返回 201 + 申请编号。"""
    response = await beta_api_client.post(
        "/public/beta-applications",
        json=_make_payload(wechat="api_user_001", phone=None),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert "id" in data
    assert "submitted_at" in data
    # 不应返回敏感字段（phone/wechat 完整值）
    assert "phone" not in data or data["phone"] is None
    assert data.get("wechat") is None or data.get("wechat") != "api_user_001"


@pytest.mark.asyncio
async def test_api_post_duplicate_returns_200(beta_api_client: AsyncClient):
    """重复提交返回 200 + 原申请。"""
    payload = _make_payload(wechat="api_dup_user", phone=None)
    resp1 = await beta_api_client.post("/public/beta-applications", json=payload)
    assert resp1.status_code == 201

    resp2 = await beta_api_client.post("/public/beta-applications", json=payload)
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["id"] == resp1.json()["id"]


@pytest.mark.asyncio
async def test_api_post_no_contact_returns_422(beta_api_client: AsyncClient):
    """微信和手机号都为空返回 422。"""
    response = await beta_api_client.post(
        "/public/beta-applications",
        json={
            "watch_stock_count": 10,
            "reason_code": "busy",
            "privacy_agreed": True,
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_api_post_other_without_reason_returns_422(beta_api_client: AsyncClient):
    """选择 'other' 但未填写说明返回 422。"""
    response = await beta_api_client.post(
        "/public/beta-applications",
        json={
            "wechat": "test_no_reason",
            "watch_stock_count": 10,
            "reason_code": "other",
            "privacy_agreed": True,
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_api_post_invalid_phone_returns_422(beta_api_client: AsyncClient):
    """手机号格式非法返回 422。"""
    response = await beta_api_client.post(
        "/public/beta-applications",
        json={
            "phone": "12345",
            "watch_stock_count": 10,
            "reason_code": "busy",
            "privacy_agreed": True,
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_api_post_rate_limit_returns_429(beta_api_client: AsyncClient):
    """同 IP 1h 内超过 5 次返回 429。"""
    # 前 5 次成功
    for i in range(5):
        resp = await beta_api_client.post(
            "/public/beta-applications",
            json=_make_payload(wechat=f"api_rate_user_{i}", phone=None),
        )
        assert resp.status_code == 201, f"第 {i+1} 次应成功: {resp.text}"

    # 第 6 次限流
    resp = await beta_api_client.post(
        "/public/beta-applications",
        json=_make_payload(wechat="api_rate_user_5", phone=None),
    )
    assert resp.status_code == 429, resp.text


@pytest.mark.asyncio
async def test_api_post_no_auth_required(beta_api_client: AsyncClient):
    """POST /public/beta-applications 无需 Authorization header。"""
    response = await beta_api_client.post(
        "/public/beta-applications",
        json=_make_payload(wechat="no_auth_user", phone=None),
    )
    # 不应返回 401/403（无需登录）
    assert response.status_code not in (401, 403), (
        f"公开端点不应要求认证: {response.status_code} {response.text}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
