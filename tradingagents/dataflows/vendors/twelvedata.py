"""Twelve Data vendor: daily OHLCV for US listings and crypto.

Free tier: 8 credits/minute, 800 credits/day; ``time_series`` costs one
credit per symbol, so a sweep paces smoothly where hourly-capped vendors
stall. Only 3 markets are included free (US equities, forex, crypto), so
exchange-suffixed listings honestly report no coverage and the router falls
through. Server-side indicators are deliberately not used: local
``stockstats`` computation stays the single calculation path.
"""

import logging
import os
from datetime import datetime

import pandas as pd
import requests

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.net import get_scrubbed
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.twelvedata.com"

# Network timeout (seconds) so a stalled Twelve Data request can't hang the
# CLI/agents indefinitely (same rationale as #990 for Alpha Vantage).
REQUEST_TIMEOUT = 30


class TwelveDataNotConfiguredError(VendorNotConfiguredError):
    """Raised when Twelve Data is selected but no API key is configured."""

    pass


def get_api_key() -> str:
    """Retrieve the API key for Twelve Data from environment variables."""
    api_key = os.getenv("TWELVEDATA_API_KEY")
    if not api_key:
        raise TwelveDataNotConfiguredError(
            "TWELVEDATA_API_KEY environment variable is not set. Get a free key at "
            "https://twelvedata.com/register"
        )
    return api_key


def to_twelvedata_symbol(symbol: str) -> str:
    """Twelve Data symbol for a user ticker, or raise honest no-data.

    US listings pass through; ``BASE-USD`` crypto becomes ``BASE/USD``. The
    free tier's 3 markets exclude exchange-suffixed listings, indices and
    futures, which report no coverage so the router falls through.
    """
    canonical = normalize_symbol(symbol)
    base, _, quote = canonical.partition("-")
    if quote in ("USD", "USDT", "USDC") and base.isalnum():
        return f"{base.upper()}/USD"
    if "." in canonical or canonical.startswith("^") or "=" in canonical:
        raise NoMarketDataError(symbol, canonical, "Twelve Data free tier covers US listings, forex and crypto")
    if not canonical or len(canonical) > 12 or not all(ch.isalnum() or ch in ".-" for ch in canonical):
        raise NoMarketDataError(symbol, canonical, "not covered by Twelve Data free tier")
    return canonical.upper()


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV for ``symbol`` in the window, as a header + CSV string.

    Same shape as ``get_YFin_data_online`` so agents see identical input
    whichever vendor serves the call.
    """
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")
    twelve_symbol = to_twelvedata_symbol(symbol)
    api_key = get_api_key()
    try:
        response = get_scrubbed(
            f"{API_BASE_URL}/time_series",
            params={"symbol": twelve_symbol, "interval": "1day",
                    "start_date": start_date, "end_date": end_date, "apikey": api_key},
            timeout=REQUEST_TIMEOUT, secret=api_key,
        )
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Twelve Data request failed: {type(exc).__name__}") from exc
    if response.status_code == 429:
        raise VendorUnavailableError("Twelve Data rate limit exceeded (HTTP 429)")
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise VendorUnavailableError(
            f"Twelve Data request failed (HTTP {response.status_code})"
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise VendorUnavailableError("Twelve Data returned a non-JSON response") from exc
    if not isinstance(payload, dict) or payload.get("status") == "error":
        message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
        low = message.lower()
        if "api key" in low or "apikey" in low or "credentials" in low:
            raise TwelveDataNotConfiguredError(f"Twelve Data API key rejected: {message[:200]}")
        if "rate limit" in low or "credits" in low or "too many" in low:
            raise VendorUnavailableError(f"Twelve Data rate limit exceeded: {message[:200]}")
        raise NoMarketDataError(symbol, twelve_symbol, message[:200] or "no price rows in range")
    rows = payload.get("values") or []
    if not rows:
        raise NoMarketDataError(symbol, twelve_symbol, f"no rows between {start_date} and {end_date}")
    frame = _frame_from_rows(rows, symbol, twelve_symbol)
    if frame.empty:
        raise NoMarketDataError(symbol, twelve_symbol, f"no rows between {start_date} and {end_date}")
    frame = frame.sort_values("Date").round(2)
    label = symbol if symbol.upper() == twelve_symbol else f"{twelve_symbol} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(frame)}\n\n"
    return header + frame.to_csv(index=False)


def _frame_from_rows(rows: list, symbol: str, twelve_symbol: str) -> pd.DataFrame:
    try:
        return pd.DataFrame([{
            "Date": r["datetime"][:10],
            "Open": float(r["open"]), "High": float(r["high"]),
            "Low": float(r["low"]), "Close": float(r["close"]),
            "Volume": int(float(r.get("volume") or 0)),
        } for r in rows if isinstance(r, dict)])
    except (KeyError, TypeError, ValueError) as exc:
        raise NoMarketDataError(symbol, twelve_symbol, f"unparseable time_series payload: {exc}") from exc


def get_indicator(symbol: str, indicator: str, as_of_date: str, look_back_days: int) -> str:
    """Indicator values over the lookback window, computed locally via stockstats.

    Same output shape as the Yahoo vendor: a wide OHLC window is fetched and
    the shared formatter renders it, so agents see identical input whichever
    vendor serves the call.
    """
    from datetime import timedelta

    from tradingagents.dataflows.vendors.yahoo.market import (
        compute_indicator_values,
        format_indicator_window,
    )

    as_of = datetime.strptime(as_of_date, "%Y-%m-%d")
    start = (as_of - timedelta(days=500)).strftime("%Y-%m-%d")
    end = (as_of + timedelta(days=1)).strftime("%Y-%m-%d")
    twelve_symbol = to_twelvedata_symbol(symbol)
    api_key = get_api_key()
    try:
        response = get_scrubbed(
            f"{API_BASE_URL}/time_series",
            params={"symbol": twelve_symbol, "interval": "1day",
                    "start_date": start, "end_date": end, "apikey": api_key},
            timeout=REQUEST_TIMEOUT, secret=api_key,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Twelve Data request failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise VendorUnavailableError("Twelve Data returned a non-JSON response") from exc
    if not isinstance(payload, dict) or payload.get("status") == "error" or not payload.get("values"):
        raise NoMarketDataError(symbol, twelve_symbol, "no price rows for indicator window")
    frame = _frame_from_rows(payload["values"], symbol, twelve_symbol)
    if frame.empty:
        raise NoMarketDataError(symbol, twelve_symbol, "no price rows for indicator window")
    values = compute_indicator_values(frame, indicator)
    return format_indicator_window(values, indicator, as_of_date, look_back_days)
