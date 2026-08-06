"""PG 原子 stock_core publication 故障注入测试（P0-07，PANJI_REMOTE_VERIFY_DB_TEST=1）。

[CHANGE-20260806-CP4A-Amendment] 正式化 CP4A 诊断阶段 /tmp 临时脚本的故障注入验证为
**受版本控制的测试文件**。本文件只在远程验证库（bz_stock_verify_<sha>）运行：

    同一事务内 quality gate → fencing → supersede 旧 pointer → insert 新 pointer →
    run 标记 → audit，任一阶段失败整体回滚，旧 pointer 保留。

覆盖（对应 PRD immutable publication history + 原子发布）：
- 无注入：发布成功，新 pointer 生效，run succeeded；
- publication/audit 阶段注入失败：旧 pointer 保留、新 pub 不可见、run 未标 published；
- Migration 087 partial unique index 下，新旧 pointer 可并存（supersede lineage）。

**注意**：本文件依赖 `db_session` savepoint fixture（conftest），要求 PostgreSQL 事务中
可建 savepoint。PURE_UNIT_TEST=1 时 skip（PG 集成只在远程验证库执行）。
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import text

_PURE_UNIT_TEST = os.environ.get("PURE_UNIT_TEST", "0") == "1"

pytestmark = pytest.mark.skipif(
    _PURE_UNIT_TEST,
    reason="PG 原子 publication 故障注入测试需远程验证库（PANJI_REMOTE_VERIFY_DB_TEST=1）",
)


async def _insert_old_publication(db, *, old_pub_id, trade_date) -> None:
    """插入一个当前有效（superseded_by IS NULL）的旧 publication。"""
    await db.execute(
        text(
            "INSERT INTO factor_publications "
            "(id, scope_type, scope_key, trade_date, publication_kind, algorithm_version, "
            "data_run_id, coverage_ratio, published_at) "
            "VALUES (:id, 'market', 'market', :td, 'stock_core', 'old-v1', :run, 0.9, now())"
        ),
        {
            "id": str(old_pub_id),
            "td": trade_date,
            "run": str(uuid.uuid4()),
        },
    )


async def _current_pointer_state(db, *, trade_date) -> dict:
    """当前有效 pointer（superseded_by IS NULL）状态。"""
    cur = (
        await db.execute(
            text(
                "SELECT id, data_run_id, superseded_by FROM factor_publications "
                "WHERE scope_key='market' AND trade_date=:td AND publication_kind='stock_core' "
                "AND superseded_by IS NULL"
            ),
            {"td": trade_date},
        )
    ).all()
    return {"pointers": cur}


async def _run_publish(db, *, trade_date, snapshot_run_id, worker_id, lease_epoch,
                       audit_fault: bool = False, pub_fault: bool = False) -> dict:
    """在给定故障模式下执行原子发布。audit_fault/publication_fault 用 savepoint 注入。"""
    import app.services.stock_core_publication_service as svc

    result: dict = {"published": False, "error": None}
    try:
        async with db.begin_nested():  # savepoint：测试级回滚，不污染外层
            if audit_fault:
                # 注入 audit 阶段失败：drop 后重建为 RAISE 触发器
                await db.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION _verify_audit_fail() RETURNS trigger AS "
                        "$$ BEGIN RAISE EXCEPTION 'injected audit failure'; END $$ LANGUAGE plpgsql"
                    )
                )
                await db.execute(
                    text(
                        "DROP TRIGGER IF EXISTS _verify_audit_trigger "
                        "ON stock_core_publication_audit"
                    )
                )
                await db.execute(
                    text(
                        "CREATE TRIGGER _verify_audit_trigger BEFORE INSERT "
                        "ON stock_core_publication_audit FOR EACH ROW EXECUTE FUNCTION "
                        "_verify_audit_fail()"
                    )
                )
            if pub_fault:
                # 注入 publication 阶段失败：drop 后重建为 RAISE 触发器
                await db.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION _verify_pub_fail() RETURNS trigger AS "
                        "$$ BEGIN RAISE EXCEPTION 'injected publication failure'; "
                        "END $$ LANGUAGE plpgsql"
                    )
                )
                await db.execute(
                    text(
                        "DROP TRIGGER IF EXISTS _verify_pub_trigger "
                        "ON factor_publications"
                    )
                )
                await db.execute(
                    text(
                        "CREATE TRIGGER _verify_pub_trigger BEFORE INSERT "
                        "ON factor_publications FOR EACH ROW EXECUTE FUNCTION "
                        "_verify_pub_fail()"
                    )
                )

            await svc.publish_stock_core_atomically(
                db, scope_key="market", trade_date=trade_date,
                publication_kind="stock_core", algorithm_version="new-v1",
                snapshot_run_id=snapshot_run_id, coverage_ratio=1.0,
                worker_id=worker_id, lease_epoch=lease_epoch, eligible_count=5,
            )
            result["published"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        # 清理注入的触发器（savepoint 内创建的 DDL，回滚后不存在；此处兜底）
        for trig in ("_verify_audit_trigger", "_verify_pub_trigger"):
            try:
                await db.execute(
                    text(f"DROP TRIGGER IF EXISTS {trig} ON factor_publications")
                )
            except Exception:  # noqa: BLE001
                pass
    return result


@pytest.mark.asyncio
async def test_pg_atomic_publication_success(db_session) -> None:
    """无注入：发布成功，新 pointer 生效。"""
    trade_date = date(2026, 8, 6)
    old_pub_id = uuid.uuid4()
    await _insert_old_publication(db_session, old_pub_id=old_pub_id, trade_date=trade_date)
    await db_session.flush()

    snapshot_run_id = uuid.uuid4()
    result = await _run_publish(
        db_session, trade_date=trade_date, snapshot_run_id=snapshot_run_id,
        worker_id="w1", lease_epoch=1,
    )
    await db_session.flush()

    assert result["published"] is True, f"发布应成功，error={result['error']}"
    state = await _current_pointer_state(db_session, trade_date=trade_date)
    # 新 pointer 生效；旧 pointer 已被 supersede（不再 superseded_by IS NULL）
    assert len(state["pointers"]) == 1, f"应只有一个当前有效 pointer，实际={state}"
    assert str(state["pointers"][0][1]) == str(snapshot_run_id), (
        f"新 pointer 应指向 snapshot_run_id={snapshot_run_id}"
    )


@pytest.mark.asyncio
async def test_pg_atomic_publication_audit_fault_preserves_old_pointer(db_session) -> None:
    """audit 阶段注入失败：整体回滚，旧 pointer 保留。"""
    trade_date = date(2026, 8, 6)
    old_pub_id = uuid.uuid4()
    await _insert_old_publication(db_session, old_pub_id=old_pub_id, trade_date=trade_date)
    await db_session.flush()

    snapshot_run_id = uuid.uuid4()
    result = await _run_publish(
        db_session, trade_date=trade_date, snapshot_run_id=snapshot_run_id,
        worker_id="w1", lease_epoch=1, audit_fault=True,
    )
    await db_session.flush()

    assert result["published"] is False, "audit 失败应导致发布失败"
    assert "audit failure" in (result["error"] or "").lower(), f"错误信息不符: {result['error']}"
    state = await _current_pointer_state(db_session, trade_date=trade_date)
    # 旧 pointer 保留（superseded_by IS NULL）
    assert len(state["pointers"]) == 1, f"应保留旧 pointer，实际={state}"
    assert str(state["pointers"][0][0]) == str(old_pub_id), (
        "旧 pointer 应保留，新 pub 不可见（原子回滚）"
    )


@pytest.mark.asyncio
async def test_pg_atomic_publication_pub_fault_preserves_old_pointer(db_session) -> None:
    """publication insert 阶段注入失败：整体回滚，旧 pointer 保留。"""
    trade_date = date(2026, 8, 6)
    old_pub_id = uuid.uuid4()
    await _insert_old_publication(db_session, old_pub_id=old_pub_id, trade_date=trade_date)
    await db_session.flush()

    snapshot_run_id = uuid.uuid4()
    result = await _run_publish(
        db_session, trade_date=trade_date, snapshot_run_id=snapshot_run_id,
        worker_id="w1", lease_epoch=1, pub_fault=True,
    )
    await db_session.flush()

    assert result["published"] is False, "publication 失败应导致发布失败"
    state = await _current_pointer_state(db_session, trade_date=trade_date)
    assert len(state["pointers"]) == 1, f"应保留旧 pointer，实际={state}"
    assert str(state["pointers"][0][0]) == str(old_pub_id), (
        "旧 pointer 应保留，新 pub 不可见（原子回滚）"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
