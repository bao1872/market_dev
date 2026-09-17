"""Side-effect-free exchange interface and frequency contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

import pandas as pd

FREQUENCY_MAP: dict[str, int] = {
    "1d": 8,
    "15m": 5,
    "1h": 4,
    "1w": -1,
    "1mo": -2,
}


class Exchange(ABC):
    """Read-only market-data source interface."""

    @abstractmethod
    def get_daily_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Return daily bars for an inclusive date range."""

    @abstractmethod
    def get_weekly_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """Return weekly bars."""

    @abstractmethod
    def get_monthly_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """Return monthly bars."""

    @abstractmethod
    def get_15min_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """Return 15-minute bars."""

    @abstractmethod
    def get_60min_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """Return 60-minute bars."""

    @abstractmethod
    def get_minute_bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Return one-minute bars for an inclusive time range."""

    @abstractmethod
    def get_xdxr_info(self, symbol: str) -> pd.DataFrame:
        """Return corporate-action rows used by adjustment calculation."""

    @abstractmethod
    async def klines(
        self,
        symbol: str,
        frequency: str,
        start_date: date | None = None,
        end_date: date | None = None,
        count: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame | None:
        """Return normalized bars for a supported project frequency."""

    @abstractmethod
    def get_stock_list(self, market: str | None = None) -> pd.DataFrame:
        """Return instruments for a market, or all instruments when omitted."""
