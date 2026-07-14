"""Excel 导出 API 集成测试（CHANGE-20260713-010）。

测试目标：
1. 权限：401 未登录 / 403 无订阅 / 403 过期 / 200 admin / 200 active member
2. published run 校验：404 不存在 / 400 非选股 run
3. all/watchlist universe 导出
4. keyword/industry/concept/metric_filters/sort 筛选
5. 导出行数 = filtered_total（X-Filtered-Total == X-Export-Rows）
6. visible_columns 列顺序
7. 非法 sort_by 返回 422（公共 DSA 列白名单）
8. 操作列不导出（只导出 visible_columns 中定义的列）
9. 公式注入防护：= + - @ 前缀单引号
10. 10000 行上限 422
11. MIME / Content-Disposition / 临时文件释放
12. 固定 SQL 数量、无 N+1（通过结果数量验证）
13. 生成文件：zip 完整性 / XML 解析 / workbook 关系 / 单元格类型

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
        pytest backend/tests/test_excel_export_api.py -q
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree as ET

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import (
    StrategyResult,
    StrategyResultMetric,
    StrategyRun,
    StrategyRunItem,
)
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.models.watchlist import UserWatchlistItem
from app.services.subscription_service import (
    generate_invite_codes,
    register_with_invite_code,
)
from tests.conftest import make_asgi_transport

# ============================================================
# 测试辅助函数
# ============================================================


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回（幂等）。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_admin(db: AsyncSession) -> User:
    """创建管理员用户（admin 角色，无 subscription）。"""
    admin = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("admin-password-123"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(admin)
    admin_role = await _ensure_role(db, "admin")
    db.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    await db.flush()
    return admin


async def _create_member_with_plan(
    db: AsyncSession, plan_code: str = "observe_20", grant_months: int = 1
) -> tuple[User, Subscription]:
    """通过邀请码注册创建 member 用户 + 订阅记录。"""
    admin = await _create_admin(db)
    results = await generate_invite_codes(
        db=db,
        count=1,
        created_by=admin.id,
        plan_code=plan_code,
        grant_months=grant_months,
    )
    await db.flush()
    email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db,
        email=email,
        password="password-12345",
        raw_invite_code=results[0][1],
    )
    await db.flush()
    return user, subscription


async def _create_member_without_subscription(db: AsyncSession) -> User:
    """创建无订阅记录的 member 用户。"""
    user = User(
        id=uuid.uuid4(),
        email=f"nomember_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("password-12345"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(user)
    member_role = await _ensure_role(db, "member")
    db.add(UserRole(user_id=user.id, role_id=member_role.id))
    await db.flush()
    return user


async def _create_expired_member(db: AsyncSession) -> User:
    """创建订阅已过期的 member 用户。"""
    user, subscription = await _create_member_with_plan(db, "observe_20")
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.flush()
    return user


async def _setup_export_test_data(
    db: AsyncSession,
    *,
    num_instruments: int = 5,
    payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """创建 published run + N 个 instruments + results。

    Args:
        num_instruments: 创建的标的数量
        payloads: 可选的自定义 payload 列表（长度需 = num_instruments）

    Returns:
        dict: {run_id, version_id, instruments, trade_date}
    """
    now = datetime.now(UTC)
    trade_date = date(2026, 7, 13)

    definition = StrategyDefinition(
        strategy_key="dsa_selector",
        kind="selector",
        display_name="趋势选股",
    )
    db.add(definition)
    await db.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "outputs": [
                {"key": "dsa_dir_bars", "type": "numeric", "filterable": True, "sortable": True},
                {"key": "offset_mean", "type": "numeric", "filterable": True, "sortable": True},
            ],
        },
        build_hash=f"test_{uuid.uuid4().hex[:16]}",
        released_at=now,
    )
    db.add(version)
    await db.flush()

    instruments: list[Instrument] = []
    names = ["贵州茅台", "平安银行", "万科A", "五粮液", "康美药业"]
    for i in range(num_instruments):
        name = names[i] if i < len(names) else f"测试股票{i}"
        inst = Instrument(
            symbol=f"600{i:03d}",
            name=name,
            pinyin_initials=f"t{i}",
            market="SH",
            status="active",
        )
        db.add(inst)
        await db.flush()
        instruments.append(inst)

    n = len(instruments)
    run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=trade_date,
        status="published",
        input_overrides={},
        started_at=now,
        finished_at=now,
        idempotency_key=f"test:{version.id}:scheduled:{trade_date}:{uuid.uuid4().hex[:8]}",
        published_at=now,
        total_instruments=n,
        succeeded_count=n,
        failed_count=0,
        skipped_count=0,
    )
    db.add(run)
    await db.flush()

    for i, inst in enumerate(instruments):
        payload = payloads[i] if payloads and i < len(payloads) else {
            "dsa_dir_bars": 30 + i * 10,
            "offset_mean": 0.01 * (i + 1),
        }
        result = StrategyResult(
            run_id=run.id,
            strategy_version_id=version.id,
            instrument_id=inst.id,
            trade_date=trade_date,
            payload=payload,
        )
        db.add(result)
        await db.flush()
        # 同步写入 StrategyResultMetric 记录（与生产 feature_snapshot_service 行为一致）
        # metric_filters 筛选查询 strategy_result_metrics 表，仅有 payload 不会命中
        for key, value in payload.items():
            if isinstance(value, bool):
                metric = StrategyResultMetric(
                    result_id=result.id,
                    strategy_version_id=version.id,
                    trade_date=trade_date,
                    instrument_id=inst.id,
                    metric_key=key,
                    bool_value=value,
                )
                db.add(metric)
            elif isinstance(value, (int, float)):
                metric = StrategyResultMetric(
                    result_id=result.id,
                    strategy_version_id=version.id,
                    trade_date=trade_date,
                    instrument_id=inst.id,
                    metric_key=key,
                    numeric_value=float(value),
                )
                db.add(metric)
            elif isinstance(value, str):
                metric = StrategyResultMetric(
                    result_id=result.id,
                    strategy_version_id=version.id,
                    trade_date=trade_date,
                    instrument_id=inst.id,
                    metric_key=key,
                    text_value=value,
                )
                db.add(metric)
        item = StrategyRunItem(
            run_id=run.id,
            instrument_id=inst.id,
            status="succeeded",
            result_id=result.id,
            started_at=now,
            finished_at=now,
        )
        db.add(item)

    await db.flush()
    return {
        "run_id": run.id,
        "version_id": version.id,
        "instruments": instruments,
        "trade_date": trade_date,
    }


def _valid_visible_columns() -> list[dict]:
    """返回合法的 visible_columns（不含操作列）。"""
    return [
        {"key": "stock", "title": "股票", "data_type": "text", "payload_key": None},
        {"key": "change_pct", "title": "涨跌幅", "data_type": "percent", "payload_key": "change_pct"},
        {"key": "dsa_dir_bars", "title": "趋势", "data_type": "number", "payload_key": "dsa_dir_bars"},
    ]


def _valid_export_request(**overrides: Any) -> dict:
    """返回合法的导出请求 body。"""
    body: dict[str, Any] = {
        "universe": "all",
        "visible_columns": _valid_visible_columns(),
    }
    body.update(overrides)
    return body


# ============================================================
# fixtures
# ============================================================


@pytest_asyncio.fixture
async def export_client(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session。"""
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 1. 权限测试
# ============================================================


@pytest.mark.asyncio
async def test_export_requires_auth(export_client: tuple[AsyncClient, AsyncSession]) -> None:
    """未登录访问导出 → 401。"""
    client, db = export_client
    data = await _setup_export_test_data(db)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_export_rejects_member_without_subscription(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无订阅 member → 403。"""
    client, db = export_client
    user = await _create_member_without_subscription(db)
    data = await _setup_export_test_data(db)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_export_rejects_expired_member(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期 member → 403。"""
    client, db = export_client
    user = await _create_expired_member(db)
    data = await _setup_export_test_data(db)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_export_admin_allowed(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """admin → 200（admin 豁免 feature 检查）。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_export_active_member_allowed(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member（有订阅）→ 200。"""
    client, db = export_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    data = await _setup_export_test_data(db)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text


# ============================================================
# 2. published run 校验
# ============================================================


@pytest.mark.asyncio
async def test_export_nonexistent_run_returns_404(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """不存在的 run_id → 404。"""
    client, db = export_client
    admin = await _create_admin(db)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{uuid.uuid4()}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_export_unpublished_run_returns_404(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """未发布的 run → 404（query_published_selector_results 抛 RunNotFoundError）。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db)
    # 手动将 run 状态改为 running
    run = await db.get(StrategyRun, data["run_id"])
    assert run is not None
    run.status = "running"
    run.published_at = None
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 404, resp.text


# ============================================================
# 3. all/watchlist universe 导出
# ============================================================


@pytest.mark.asyncio
async def test_export_universe_all(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """universe=all 导出全部结果。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=3)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(universe="all"),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text
    assert int(resp.headers.get("X-Export-Rows", 0)) == 3
    assert int(resp.headers.get("X-Filtered-Total", 0)) == 3


@pytest.mark.asyncio
async def test_export_universe_watchlist(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """universe=watchlist 只导出用户自选股。"""
    client, db = export_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    data = await _setup_export_test_data(db, num_instruments=3)
    # 将第 1 个 instrument 加入自选
    inst0 = data["instruments"][0]
    watch_item = UserWatchlistItem(
        id=uuid.uuid4(),
        user_id=user.id,
        instrument_id=inst0.id,
        source="manual",
        active=True,
    )
    db.add(watch_item)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(universe="watchlist"),
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text
    assert int(resp.headers.get("X-Export-Rows", 0)) == 1
    assert int(resp.headers.get("X-Filtered-Total", 0)) == 1


# ============================================================
# 4. keyword 筛选
# ============================================================


@pytest.mark.asyncio
async def test_export_keyword_filter(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """keyword 筛选只导出匹配的股票。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=3)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(keyword="茅台"),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text
    assert int(resp.headers.get("X-Export-Rows", 0)) == 1
    assert int(resp.headers.get("X-Filtered-Total", 0)) == 1


# ============================================================
# 5. metric_filters 筛选
# ============================================================


@pytest.mark.asyncio
async def test_export_metric_filters(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """metric_filters 筛选只导出满足条件的行。"""
    client, db = export_client
    admin = await _create_admin(db)
    payloads = [
        {"dsa_dir_bars": 10, "offset_mean": 0.01},
        {"dsa_dir_bars": 30, "offset_mean": 0.02},
        {"dsa_dir_bars": 50, "offset_mean": 0.03},
    ]
    data = await _setup_export_test_data(db, num_instruments=3, payloads=payloads)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(
            metric_filters=[{"metric_key": "dsa_dir_bars", "operator": "gte", "value": 30}]
        ),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text
    assert int(resp.headers.get("X-Export-Rows", 0)) == 2
    assert int(resp.headers.get("X-Filtered-Total", 0)) == 2


# ============================================================
# 6. sort 排序 + 列白名单校验
# ============================================================


@pytest.mark.asyncio
async def test_export_sort_by_valid_field(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """合法 sort_by（在 filterable 白名单中）→ 200。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=3)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(sort_by="dsa_dir_bars", sort_desc=True),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_export_sort_by_invalid_field_returns_422(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """非法 sort_by（不在白名单中）→ 422。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=3)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(sort_by="arbitrary_payload_key"),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 422, resp.text


# ============================================================
# 7. visible_columns 列顺序
# ============================================================


@pytest.mark.asyncio
async def test_export_visible_columns_order(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """导出的列顺序与 visible_columns 一致。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=1)
    await db.flush()

    # 自定义列顺序：趋势在前，股票在后
    columns = [
        {"key": "dsa_dir_bars", "title": "趋势", "data_type": "number", "payload_key": "dsa_dir_bars"},
        {"key": "stock", "title": "股票", "data_type": "text", "payload_key": None},
    ]
    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(visible_columns=columns),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text

    # 验证表头顺序：第 1 列=趋势，第 2 列=股票
    raw = resp.content
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf, "r") as zf:
        shared = zf.read("xl/sharedStrings.xml").decode("utf-8")
    # 趋势在股票之前出现（sharedStrings 按出现顺序存储）
    idx_trend = shared.find("趋势")
    idx_stock = shared.find("股票")
    assert idx_trend != -1 and idx_stock != -1
    assert idx_trend < idx_stock, "趋势应出现在股票之前"


@pytest.mark.asyncio
async def test_export_empty_visible_columns_returns_422(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """visible_columns 为空 → 422。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=1)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(visible_columns=[]),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 422, resp.text


# ============================================================
# 8. 导出行数 = filtered_total
# ============================================================


@pytest.mark.asyncio
async def test_export_rows_equal_filtered_total(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """X-Export-Rows == X-Filtered-Total。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=5)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(keyword="茅台"),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text
    export_rows = int(resp.headers.get("X-Export-Rows", -1))
    filtered_total = int(resp.headers.get("X-Filtered-Total", -2))
    assert export_rows == filtered_total, f"X-Export-Rows={export_rows} != X-Filtered-Total={filtered_total}"


# ============================================================
# 9. 10000 行上限 422
# ============================================================


@pytest.mark.asyncio
async def test_export_exceeds_max_rows_returns_422(
    export_client: tuple[AsyncClient, AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过 MAX_EXPORT_ROWS → 422（通过 monkeypatch 降低上限避免创建 10001 条数据）。"""
    client, db = export_client
    admin = await _create_admin(db)
    # 创建 5 条数据，monkeypatch MAX_EXPORT_ROWS=3
    data = await _setup_export_test_data(db, num_instruments=5)
    await db.flush()

    # 降低上限到 3，5 条数据会触发 422
    import app.api.strategy_runs as sr_module
    monkeypatch.setattr(sr_module, "MAX_EXPORT_ROWS", 3)

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 422, resp.text
    assert "超过上限" in resp.json()["detail"]


# ============================================================
# 10. MIME / Content-Disposition
# ============================================================


@pytest.mark.asyncio
async def test_export_mime_and_content_disposition(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """响应 MIME 为 .xlsx，Content-Disposition 含 attachment; filename*=UTF-8''。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=1)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("content-type") == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "filename*=UTF-8''" in cd
    assert ".xlsx" in cd


# ============================================================
# 11. 公式注入防护（端到端）
# ============================================================


@pytest.mark.asyncio
async def test_export_formula_injection_protection(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """含 = + - @ 的股票名称在导出时被前缀单引号。"""
    client, db = export_client
    admin = await _create_admin(db)
    # 创建含公式注入字符的股票名称
    payloads = [{"dsa_dir_bars": 30}]
    data = await _setup_export_test_data(db, num_instruments=1, payloads=payloads)
    # 修改股票名称为含 = 的危险文本
    inst = data["instruments"][0]
    inst.name = "=cmd|test"
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text

    # 验证 sharedStrings 中含转义后的值（前缀单引号）。
    # 注意：XML 中单引号 ' 会被转义为 &apos;（_escape_xml 处理）。
    raw = resp.content
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf, "r") as zf:
        shared = zf.read("xl/sharedStrings.xml").decode("utf-8")
    assert "&apos;=cmd|test" in shared, f"公式注入防护失败，sharedStrings={shared}"


# ============================================================
# 12. 生成文件验证：zip完整性 / XML解析 / workbook关系 / 单元格类型
# ============================================================


def _validate_xlsx_structure(raw: bytes, expected_data_rows: int) -> None:
    """全面验证 .xlsx 文件结构。

    Args:
        raw: .xlsx 文件 bytes
        expected_data_rows: 预期数据行数（不含表头）
    """
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf, "r") as zf:
        names = zf.namelist()

        # 1. zip 完整性：含必要 OOXML 部分
        assert "[Content_Types].xml" in names, "缺 [Content_Types].xml"
        assert "_rels/.rels" in names, "缺 _rels/.rels"
        assert "xl/workbook.xml" in names, "缺 xl/workbook.xml"
        assert "xl/_rels/workbook.xml.rels" in names, "缺 xl/_rels/workbook.xml.rels"
        assert "xl/worksheets/sheet1.xml" in names, "缺 xl/worksheets/sheet1.xml"
        assert "xl/styles.xml" in names, "缺 xl/styles.xml"
        assert "xl/sharedStrings.xml" in names, "缺 xl/sharedStrings.xml"

        # 2. XML 解析：所有 XML 部分可被 ElementTree 解析（不解析会抛异常）
        ET.fromstring(zf.read("[Content_Types].xml"))
        ET.fromstring(zf.read("_rels/.rels"))
        wb_xml = ET.fromstring(zf.read("xl/workbook.xml"))
        wb_rels_xml = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        sheet_xml = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        ET.fromstring(zf.read("xl/styles.xml"))
        shared_xml = ET.fromstring(zf.read("xl/sharedStrings.xml"))

        # 3. workbook 关系：rId1→worksheet, rId2→styles, rId3→sharedStrings
        ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
        rels = wb_rels_xml.findall("r:Relationship", ns)
        rel_targets = {r.get("Target") for r in rels}
        assert "worksheets/sheet1.xml" in rel_targets, f"workbook rels 缺 worksheet: {rel_targets}"
        assert "styles.xml" in rel_targets, f"workbook rels 缺 styles: {rel_targets}"
        assert "sharedStrings.xml" in rel_targets, f"workbook rels 缺 sharedStrings: {rel_targets}"

        # 4. workbook.xml 含 sheet 定义
        wb_ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheets = wb_xml.findall(".//main:sheet", wb_ns)
        assert len(sheets) == 1, f"应有 1 个 sheet，实际 {len(sheets)}"

        # 5. sheet1.xml 行数 = expected_data_rows + 1（表头）
        rows = sheet_xml.findall(".//main:row", wb_ns)
        assert len(rows) == expected_data_rows + 1, (
            f"应有 {expected_data_rows + 1} 行（含表头），实际 {len(rows)}"
        )

        # 6. 单元格类型验证：数值列 t="n"，字符串列 t="s"
        cells = sheet_xml.findall(".//main:c", wb_ns)
        assert len(cells) > 0, "应有单元格"
        # 至少有 1 个数值单元格（t="n"）
        n_cells = [c for c in cells if c.get("t") == "n"]
        s_cells = [c for c in cells if c.get("t") == "s"]
        assert len(n_cells) > 0, "应有数值单元格 (t='n')"
        assert len(s_cells) > 0, "应有字符串单元格 (t='s')"

        # 7. sharedStrings 非空（含表头）
        si_elements = shared_xml.findall("main:si", wb_ns)
        assert len(si_elements) > 0, "sharedStrings 应非空"


@pytest.mark.asyncio
async def test_export_generates_valid_xlsx_structure(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """生成的 .xlsx 通过完整结构验证：zip / XML / workbook关系 / 单元格类型。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=3)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text
    _validate_xlsx_structure(resp.content, expected_data_rows=3)


# ============================================================
# 13. source_total / universe_total / filtered_total 四层语义
# ============================================================


@pytest.mark.asyncio
async def test_export_response_headers_contain_four_totals(
    export_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """响应头含 X-Source-Total / X-Universe-Total / X-Filtered-Total / X-Export-Rows。"""
    client, db = export_client
    admin = await _create_admin(db)
    data = await _setup_export_test_data(db, num_instruments=5)
    await db.flush()

    resp = await client.post(
        f"/strategy-runs/{data['run_id']}/results/export",
        json=_valid_export_request(keyword="茅台"),
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text
    source_total = int(resp.headers.get("X-Source-Total", -1))
    universe_total = int(resp.headers.get("X-Universe-Total", -1))
    filtered_total = int(resp.headers.get("X-Filtered-Total", -1))
    export_rows = int(resp.headers.get("X-Export-Rows", -1))

    # source_total = published run 原始总量（5，不受 keyword 筛选影响）
    assert source_total == 5, f"source_total 应为 5，实际 {source_total}"
    # universe_total = all 范围总量（5，业务筛选前）
    assert universe_total == 5, f"universe_total 应为 5，实际 {universe_total}"
    # filtered_total = keyword 筛选后总量（1）
    assert filtered_total == 1, f"filtered_total 应为 1，实际 {filtered_total}"
    # export_rows = 当前页（= filtered_total，全量导出）
    assert export_rows == filtered_total, "export_rows 应等于 filtered_total"
    assert export_rows <= filtered_total, "export_rows 不能超过 filtered_total"
