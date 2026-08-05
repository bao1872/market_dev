"""chip_consensus 正式发布指针单元测试（Commit D 2026-08-05）。

[Commit D] 补齐 chip 的 publication / pointer / lineage 合同：
- PUBLICATION_KIND_CHIP_CONSENSUS 此前只被 product_readiness 读取，从未被写入。
- publish_chip_consensus 在 chip run 达到可发布终态（succeeded/partial）后原子写入
  FactorPublication 发布指针，并强制 lineage：chip 必须基于当日已发布的 stock_core run。

本测试为纯单元测试（PURE_UNIT_TEST），不连接数据库，使用 AsyncMock 模拟 session。
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.factor_publication import (
    PUBLICATION_KIND_CHIP_CONSENSUS,
    PUBLICATION_KIND_STOCK_CORE,
)
from app.services import factor_publication_service as fps
from app.services.factor_publication_service import publish_chip_consensus

_TRADE_DATE = date(2026, 8, 4)


def _make_chip_run(
    *,
    status: str = "succeeded",
    coverage: float = 0.95,
    source_core_run_id: uuid.UUID | None = None,
    trade_date: date = _TRADE_DATE,
) -> Mock:
    run = Mock()
    run.id = uuid.uuid4()
    run.trade_date = trade_date
    run.status = status
    run.source_core_run_id = source_core_run_id or uuid.uuid4()
    run.coverage_ratio = coverage
    return run


def _make_pub() -> Mock:
    pub = Mock()
    pub.id = uuid.uuid4()
    pub.data_run_id = uuid.uuid4()
    return pub


@pytest.mark.asyncio
async def test_publish_chip_consensus_success_succeeded() -> None:
    """chip_run succeeded 且 core pointer 匹配 → 写入发布指针并返回。"""
    chip_run = _make_chip_run(status="succeeded")
    pub = _make_pub()
    session = AsyncMock()
    session.get.return_value = chip_run

    fps.get_published_snapshot_run_id = AsyncMock(return_value=chip_run.source_core_run_id)
    fps.get_publication = AsyncMock(return_value=pub)

    result = await publish_chip_consensus(
        session, _TRADE_DATE, chip_run.id, "chip-v1",
    )

    assert result is pub
    session.get.assert_awaited_once()
    fps.get_published_snapshot_run_id.assert_awaited_once()
    # 写入 upsert
    session.execute.assert_awaited_once()
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_chip_consensus_success_partial() -> None:
    """chip_run partial 也是可发布终态（部分成功，coverage 仍写入）。"""
    chip_run = _make_chip_run(status="partial", coverage=0.6)
    pub = _make_pub()
    session = AsyncMock()
    session.get.return_value = chip_run

    fps.get_published_snapshot_run_id = AsyncMock(return_value=chip_run.source_core_run_id)
    fps.get_publication = AsyncMock(return_value=pub)

    result = await publish_chip_consensus(
        session, _TRADE_DATE, chip_run.id, "chip-v1",
    )

    assert result is pub


@pytest.mark.asyncio
async def test_publish_chip_consensus_run_not_found() -> None:
    """chip_run 不存在 → ValueError。"""
    session = AsyncMock()
    session.get.return_value = None

    with pytest.raises(ValueError, match="不存在"):
        await publish_chip_consensus(session, _TRADE_DATE, uuid.uuid4(), "chip-v1")


@pytest.mark.asyncio
async def test_publish_chip_consensus_non_terminal_status() -> None:
    """chip_run 非可发布终态（failed）→ ValueError。"""
    chip_run = _make_chip_run(status="failed")
    session = AsyncMock()
    session.get.return_value = chip_run

    with pytest.raises(ValueError, match="非可发布终态"):
        await publish_chip_consensus(session, _TRADE_DATE, chip_run.id, "chip-v1")


@pytest.mark.asyncio
async def test_publish_chip_consensus_trade_date_mismatch() -> None:
    """chip_run.trade_date 与调用方不一致 → ValueError。"""
    chip_run = _make_chip_run(trade_date=date(2026, 8, 3))
    session = AsyncMock()
    session.get.return_value = chip_run

    with pytest.raises(ValueError, match="trade_date"):
        await publish_chip_consensus(session, _TRADE_DATE, chip_run.id, "chip-v1")


@pytest.mark.asyncio
async def test_publish_chip_consensus_no_published_core() -> None:
    """当日无已发布 stock_core pointer → ValueError（chip 必须先于 core 发布）。"""
    chip_run = _make_chip_run()
    session = AsyncMock()
    session.get.return_value = chip_run

    fps.get_published_snapshot_run_id = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="无已发布 stock_core"):
        await publish_chip_consensus(session, _TRADE_DATE, chip_run.id, "chip-v1")


@pytest.mark.asyncio
async def test_publish_chip_consensus_core_lineage_mismatch() -> None:
    """chip_run.source_core_run_id 与已发布 stock_core pointer 不一致 → ValueError。"""
    chip_run = _make_chip_run()
    session = AsyncMock()
    session.get.return_value = chip_run

    other_core = uuid.uuid4()
    fps.get_published_snapshot_run_id = AsyncMock(return_value=other_core)

    with pytest.raises(ValueError, match="不匹配"):
        await publish_chip_consensus(session, _TRADE_DATE, chip_run.id, "chip-v1")


@pytest.mark.asyncio
async def test_publish_chip_consensus_uses_chip_publication_kind() -> None:
    """发布指针必须使用 chip_consensus publication_kind。"""
    chip_run = _make_chip_run()
    pub = _make_pub()
    session = AsyncMock()
    session.get.return_value = chip_run

    fps.get_published_snapshot_run_id = AsyncMock(return_value=chip_run.source_core_run_id)
    fps.get_publication = AsyncMock(return_value=pub)

    await publish_chip_consensus(session, _TRADE_DATE, chip_run.id, "chip-v1")

    # 读取发布指针时必须以 chip_consensus kind 查询（而非 stock_core）
    _, kwargs = fps.get_publication.call_args
    assert kwargs["publication_kind"] == PUBLICATION_KIND_CHIP_CONSENSUS
    assert kwargs["publication_kind"] != PUBLICATION_KIND_STOCK_CORE