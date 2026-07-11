"""qstock_fetcher 测试（PRD §7.5）。

验证项：
1. 超时/重试/退避：_fetch_with_retry 在失败后重试，超过次数抛 QStockFetchError
2. 异常传播：任一目录/成分请求失败抛 QStockFetchError，不返回空数组
3. 标准化去重：代码去重、6 位左填充
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.qstock_fetcher import (
    FETCH_RETRY_COUNT,
    FETCH_TIMEOUT_SECONDS,
    QStockFetcher,
    QStockFetchError,
    _fetch_boards_sync,
    _fetch_memberships_sync,
    _fetch_with_retry,
)


class TestFetchWithRetry:
    """_fetch_with_retry 超时/重试/退避测试。"""

    def test_success_on_first_attempt(self) -> None:
        """首次成功不重试。"""
        qs = MagicMock()
        qs.some_method.return_value = "ok"

        result = _fetch_with_retry(qs, "some_method", label="test")
        assert result == "ok"
        assert qs.some_method.call_count == 1

    def test_raises_after_max_retries(self) -> None:
        """超过最大重试次数后抛 QStockFetchError。"""
        qs = MagicMock()
        qs.failing_method.side_effect = RuntimeError("network error")

        with patch("app.services.qstock_fetcher.time.sleep") as mock_sleep:
            with pytest.raises(QStockFetchError, match="failed after"):
                _fetch_with_retry(qs, "failing_method", label="test")

        # 调用次数 = FETCH_RETRY_COUNT + 1
        assert qs.failing_method.call_count == FETCH_RETRY_COUNT + 1
        # 退避次数 = FETCH_RETRY_COUNT
        assert mock_sleep.call_count == FETCH_RETRY_COUNT

    def test_success_after_retry(self) -> None:
        """首次失败，重试后成功。"""
        qs = MagicMock()
        qs.flaky_method.side_effect = [RuntimeError("temp"), "ok"]

        with patch("app.services.qstock_fetcher.time.sleep"):
            result = _fetch_with_retry(qs, "flaky_method", label="test")

        assert result == "ok"
        assert qs.flaky_method.call_count == 2

    def test_timeout_is_configured(self) -> None:
        """超时参数存在且合理。"""
        assert FETCH_TIMEOUT_SECONDS == 30
        assert FETCH_RETRY_COUNT == 2


class TestFetchBoardsSync:
    """_fetch_boards_sync 目录拉取测试。"""

    def test_normalizes_and_deduplicates_boards(self) -> None:
        """标准化并去重板块代码。"""
        industry_df = pd.DataFrame({
            "code": ["881001", "881001", "881002"],
            "name": ["行业1", "行业1重复", "行业2"],
        })
        concept_df = pd.DataFrame({
            "code": ["881003", "881004"],
            "name": ["概念1", "概念2"],
        })
        qs = MagicMock()

        with patch("app.services.qstock_fetcher._fetch_with_retry", side_effect=[industry_df, concept_df]):
            boards = _fetch_boards_sync(qs)

        # industry: 881001(去重), 881002 → 2 个
        # concept: 881003, 881004 → 2 个
        # 总共 4 个
        assert len(boards) == 4
        types = {b["type"] for b in boards}
        assert types == {"industry", "concept"}
        codes = {b["external_code"] for b in boards}
        assert codes == {"881001", "881002", "881003", "881004"}
        # 881001 只出现一次（去重）
        assert sum(1 for b in boards if b["external_code"] == "881001") == 1

    def test_industry_failure_raises(self) -> None:
        """行业目录拉取失败抛 QStockFetchError。"""
        qs = MagicMock()
        with patch(
            "app.services.qstock_fetcher._fetch_with_retry",
            side_effect=QStockFetchError("industry failed"),
        ):
            with pytest.raises(QStockFetchError, match="industry failed"):
                _fetch_boards_sync(qs)

    def test_concept_failure_raises(self) -> None:
        """概念目录拉取失败抛 QStockFetchError。"""
        industry_df = pd.DataFrame({"code": ["881001"], "name": ["行业1"]})
        qs = MagicMock()
        with patch(
            "app.services.qstock_fetcher._fetch_with_retry",
            side_effect=[industry_df, QStockFetchError("concept failed")],
        ):
            with pytest.raises(QStockFetchError, match="concept failed"):
                _fetch_boards_sync(qs)


class TestFetchMembershipsSync:
    """_fetch_memberships_sync 成分拉取测试。"""

    def test_normalizes_and_deduplicates_symbols(self) -> None:
        """标准化并去重股票代码（6 位左填充）。"""
        df = pd.DataFrame({
            "code": ["1", "000001", "600000", "600000"],
            "name": ["平安", "平安", "浦发", "浦发重复"],
        })
        qs = MagicMock()
        with patch(
            "app.services.qstock_fetcher._fetch_with_retry",
            return_value=df,
        ):
            symbols = _fetch_memberships_sync(qs, "881001")

        # "1" 左填充为 "000001"，与第二个 "000001" 去重
        # "600000" 去重
        assert "000001" in symbols
        assert "600000" in symbols
        assert symbols.count("000001") == 1
        assert symbols.count("600000") == 1

    def test_failure_raises(self) -> None:
        """成分拉取失败抛 QStockFetchError。"""
        qs = MagicMock()
        with patch(
            "app.services.qstock_fetcher._fetch_with_retry",
            side_effect=QStockFetchError("membership failed"),
        ):
            with pytest.raises(QStockFetchError, match="membership failed"):
                _fetch_memberships_sync(qs, "881001")

    def test_empty_dataframe_returns_empty_list(self) -> None:
        """空 DataFrame 返回空列表（合法空集合）。"""
        qs = MagicMock()
        with patch(
            "app.services.qstock_fetcher._fetch_with_retry",
            return_value=pd.DataFrame(),
        ):
            symbols = _fetch_memberships_sync(qs, "881999")
        assert symbols == []


class TestQStockFetcherAsync:
    """QStockFetcher 异步包装测试。"""

    @pytest.mark.asyncio
    async def test_fetch_boards_uses_to_thread(self) -> None:
        """fetch_boards 通过 asyncio.to_thread 执行同步调用。"""
        fetcher = QStockFetcher()

        with patch.object(fetcher, "_ensure_qstock", return_value=MagicMock()):
            with patch(
                "app.services.qstock_fetcher._fetch_boards_sync",
                return_value=[{"external_code": "881001", "name": "行业1", "type": "industry"}],
            ):
                result = await fetcher.fetch_boards()

        assert len(result) == 1
        assert result[0]["external_code"] == "881001"

    @pytest.mark.asyncio
    async def test_fetch_memberships_uses_to_thread(self) -> None:
        """fetch_memberships 通过 asyncio.to_thread 执行同步调用。"""
        fetcher = QStockFetcher()

        with patch.object(fetcher, "_ensure_qstock", return_value=MagicMock()):
            with patch(
                "app.services.qstock_fetcher._fetch_memberships_sync",
                return_value=["000001", "600000"],
            ):
                result = await fetcher.fetch_memberships("881001", "industry")

        assert result == ["000001", "600000"]

    @pytest.mark.asyncio
    async def test_import_error_raises(self) -> None:
        """qstock 未安装时抛 ImportError。"""
        fetcher = QStockFetcher()
        with patch("builtins.__import__", side_effect=ImportError("no qstock")):
            with pytest.raises(ImportError, match="qstock is not installed"):
                await fetcher.fetch_boards()
