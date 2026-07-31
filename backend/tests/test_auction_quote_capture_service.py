# PURE_UNIT_TEST=1
"""auction_quote_capture_service 单元测试 — 纯单元测试，不连接 DB。

运行方式：
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_auction_quote_capture_service.py -v

测试范围：
1. _parse_source_time 解析 pytdx servertime
2. _safe_decimal / _safe_int 安全转换
3. _is_lease_expired 租约过期判定
4. _build_capture_summary 构造返回结果
5. 常量校验
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from app.services.auction_quote_capture_service import (
    CAPTURE_RUN_LEASE_SECONDS,
    DEFAULT_SOURCE,
    PRODUCTION_NAMESPACE,
    AuctionCaptureConflictError,
    _build_capture_summary,
    _is_lease_expired,
    _parse_source_time,
    _safe_decimal,
    _safe_int,
)
from app.services.auction_quote_provider import (
    AuctionFinalQuoteProvider,
    AuctionQuoteResult,
    MootdxAuctionQuoteProvider,
    _classify_quality,
)


class TestParseSourceTime:
    """_parse_source_time 解析测试。"""

    def test_parse_normal_time(self) -> None:
        """正常 servertime 解析。"""
        dt = _parse_source_time("9:25:5", date(2026, 7, 31))
        assert dt is not None
        assert dt.hour == 9
        assert dt.minute == 25
        assert dt.second == 5

    def test_parse_zero_padded_time(self) -> None:
        """零填充 servertime 解析。"""
        dt = _parse_source_time("09:25:05", date(2026, 7, 31))
        assert dt is not None
        assert dt.hour == 9
        assert dt.minute == 25

    def test_parse_none_returns_none(self) -> None:
        """None 输入返回 None。"""
        assert _parse_source_time(None, date(2026, 7, 31)) is None

    def test_parse_invalid_returns_none(self) -> None:
        """无效字符串返回 None。"""
        assert _parse_source_time("invalid", date(2026, 7, 31)) is None
        assert _parse_source_time("25:70:99", date(2026, 7, 31)) is None

    def test_parse_float_seconds(self) -> None:
        """带小数的秒数解析。"""
        dt = _parse_source_time("9:25:5.123", date(2026, 7, 31))
        assert dt is not None
        assert dt.second == 5


class TestSafeConversions:
    """_safe_decimal / _safe_int 测试。"""

    def test_safe_decimal_none(self) -> None:
        assert _safe_decimal(None) is None

    def test_safe_decimal_valid(self) -> None:
        assert _safe_decimal("10.5") == Decimal("10.5")
        assert _safe_decimal(10.5) == Decimal("10.5")

    def test_safe_decimal_invalid(self) -> None:
        assert _safe_decimal("invalid") is None
        assert _safe_decimal(float("nan")) is None

    def test_safe_int_none(self) -> None:
        assert _safe_int(None) is None

    def test_safe_int_valid(self) -> None:
        assert _safe_int(100.0) == 100
        assert _safe_int("100") == 100

    def test_safe_int_invalid(self) -> None:
        assert _safe_int("invalid") is None


class TestIsLeaseExpired:
    """_is_lease_expired 测试。"""

    def test_expired_when_no_heartbeat(self) -> None:
        """无 heartbeat 视为过期。"""
        now = datetime(2026, 7, 31, 2, 0, 0, tzinfo=UTC)
        assert _is_lease_expired(None, now=now) is True

    def test_not_expired_recent_heartbeat(self) -> None:
        """5 分钟前心跳未过期。"""
        now = datetime(2026, 7, 31, 2, 0, 0, tzinfo=UTC)
        hb = datetime(2026, 7, 31, 1, 55, 0, tzinfo=UTC)
        assert _is_lease_expired(hb, now=now, expired_seconds=600) is False

    def test_expired_old_heartbeat(self) -> None:
        """11 分钟前心跳已过期。"""
        now = datetime(2026, 7, 31, 2, 0, 0, tzinfo=UTC)
        hb = datetime(2026, 7, 31, 1, 49, 0, tzinfo=UTC)
        assert _is_lease_expired(hb, now=now, expired_seconds=600) is True

    def test_naive_datetime_treated_as_utc(self) -> None:
        """无时区时间视为 UTC。"""
        now = datetime(2026, 7, 31, 2, 0, 0, tzinfo=UTC)
        hb = datetime(2026, 7, 31, 1, 55, 0)  # 无时区
        assert _is_lease_expired(hb, now=now, expired_seconds=600) is False


class TestBuildCaptureSummary:
    """_build_capture_summary 测试。"""

    def test_summary_with_succeeded_run(self) -> None:
        """succeeded run 的 summary 包含 idempotent=True。"""
        class MockRun:
            id = uuid.uuid4()
            status = "succeeded"
            expected_count = 100
            received_count = 98
            valid_count = 95
            coverage = 0.95
            reason_codes = ["price_missing:3"]

        summary = _build_capture_summary(MockRun())
        assert summary["status"] == "succeeded"
        assert summary["expected_count"] == 100
        assert summary["valid_count"] == 95
        assert summary["coverage"] == 0.95
        assert summary["idempotent"] is True

    def test_summary_with_running_run(self) -> None:
        """running run 的 summary 包含 idempotent=False。"""
        class MockRun:
            id = uuid.uuid4()
            status = "running"
            expected_count = 0
            received_count = 0
            valid_count = 0
            coverage = 0.0
            reason_codes = []

        summary = _build_capture_summary(MockRun())
        assert summary["status"] == "running"
        assert summary["idempotent"] is False


class TestProviderProtocol:
    """AuctionFinalQuoteProvider 协议测试。"""

    def test_mootdx_provider_implements_protocol(self) -> None:
        """MootdxAuctionQuoteProvider 实现 AuctionFinalQuoteProvider 协议。"""
        provider = MootdxAuctionQuoteProvider()
        assert isinstance(provider, AuctionFinalQuoteProvider)

    def test_provider_close_without_connect(self) -> None:
        """未连接时 close 不抛异常。"""
        provider = MootdxAuctionQuoteProvider()
        provider.close()  # 应该是 no-op

    def test_context_manager(self) -> None:
        """上下文管理器正常工作（不连接时）。"""
        with MootdxAuctionQuoteProvider() as provider:
            assert provider is not None
        # 退出后应已 close
        assert provider._connected is False


class TestClassifyQuality:
    """_classify_quality 质量分类测试。"""

    def test_ok(self) -> None:
        status, reasons = _classify_quality(10.5, 10.0, 100, 5.0)
        assert status == "ok"
        assert reasons == []

    def test_missing_field(self) -> None:
        status, _ = _classify_quality(None, 10.0, 100, None)
        assert status == "missing_field"

    def test_zero_volume(self) -> None:
        status, _ = _classify_quality(11.0, 10.0, 0, 10.0)
        assert status == "zero_volume"

    def test_limit_up(self) -> None:
        status, reasons = _classify_quality(11.05, 10.0, 100, 10.5)
        assert status == "limit_up"
        assert len(reasons) == 1

    def test_limit_down(self) -> None:
        status, reasons = _classify_quality(8.95, 10.0, 100, -10.5)
        assert status == "limit_down"
        assert len(reasons) == 1


class TestAuctionQuoteResult:
    """AuctionQuoteResult dataclass 测试。"""

    def test_is_valid_ok(self) -> None:
        """quality_status=ok 时 is_valid=True。"""
        result = AuctionQuoteResult(
            symbol="600519", market="SH",
            price=10.5, last_close=10.0, open=None,
            high=None, low=None, volume=100, amount=1000.0,
            servertime="9:25:5",
            quality_status="ok",
        )
        assert result.is_valid is True

    def test_is_valid_not_ok(self) -> None:
        """quality_status!=ok 时 is_valid=False。"""
        result = AuctionQuoteResult(
            symbol="600519", market="SH",
            price=None, last_close=None, open=None,
            high=None, low=None, volume=None, amount=None,
            servertime=None,
            quality_status="missing_field",
        )
        assert result.is_valid is False


class TestConstants:
    """常量校验。"""

    def test_default_source(self) -> None:
        assert DEFAULT_SOURCE == "mootdx"

    def test_production_namespace(self) -> None:
        assert PRODUCTION_NAMESPACE == "production"

    def test_lease_seconds(self) -> None:
        assert CAPTURE_RUN_LEASE_SECONDS == 600


class TestConflictError:
    """异常类测试。"""

    def test_conflict_error_is_value_error(self) -> None:
        assert issubclass(AuctionCaptureConflictError, ValueError)

    def test_conflict_error_message(self) -> None:
        err = AuctionCaptureConflictError("test message")
        assert "test message" in str(err)
