"""Tiingo vendor: deep-history daily OHLCV for US listings and crypto.

Free tier: 50 requests/hour, 1000/day, 30+ years of EOD history — the depth
specialist for cells older than Finnhub's one-year free window. The hourly
cap is honored with backoff on 429 so a paced sweep degrades instead of
burning quota. Exchange-suffixed listings honestly report no coverage.
"""

import logging
import os
import time
from datetime import datetime

import pandas as pd
import requests

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.tiingo.com"

# Network timeout (seconds) so a stalled Tiingo request can't hang the
# CLI/agents indefinitely (same rationale as #990 for Alpha Vantage).
REQUEST_TIMEOUT = 30

# Free tier allows 50 requests/hour; back off (honoring Retry-After) so a
# paced caller degrades instead of failing the chain on the first 429.
_MAX_RETRIES = 3


class TiingoNotConfiguredError(VendorNotConfiguredError):
    """Raised when Tiingo is selected but no API key is configured."""

    pass


def get_api_key() -> str:
    """Retrieve the API key for Tiingo from environment variables."""
    api_key = os.getenv("TIINGO_API_KEY")
    if not api_key:
        raise TiingoNotConfiguredError(
            "TIINGO_API_KEY environment variable is not set. Get a free key at "
            "https://www.tiingo.com/account/api/token"
        )
    return api_key


def to_tiingo_symbol(symbol: str) -> tuple[str, str]:
    """(family, tiingo symbol) for a user ticker, or raise honest no-data.

    US listings pass through (case-insensitive on Tiingo's side);
    ``BASE-USD`` crypto maps to the crypto endpoint's ``baseusd`` form.
    Exchange-suffixed listings, indices and futures are outside the free
    tier's practical coverage and report no coverage.
    """
    canonical = normalize_symbol(symbol)
    base, _, quote = canonical.partition("-")
    if quote in ("USD", "USDT", "USDC") and base.isalnum():
        return "crypto", f"{base.lower()}usd"
    suffix = canonical.rsplit(".", 1)
    if len(suffix) == 2 and suffix[1] in (
        "HK", "T", "L", "NS", "BO", "TO", "AX", "SS", "SZ", "SA",
        "TW", "TWO", "KS", "KQ", "SI", "DE", "PA", "AS", "SW", "MI",
    ):
        raise NoMarketDataError(symbol, canonical, "Tiingo free tier does not cover exchange-suffixed listings")
    if not canonical or len(canonical) > 12 or canonical.startswith("^") or "=" in canonical \
            or not all(ch.isalnum() or ch in ".-" for ch in canonical):
        raise NoMarketDataError(symbol, canonical, "not covered by Tiingo free tier")
    return "stock", canonical.upper()


def _get(path: str, params: dict):
    """GET with key header, retries on 429 honoring Retry-After.

    A 429 that outlasts retries, or any network failure, says nothing about
    the instrument -> VendorUnavailableError. Unknown tickers (404/empty)
    are left for the caller to classify as no-data.
    """
    api_key = get_api_key()
    headers = {"Content-Type": "application/json", "Authorization": f"Token {api_key}"}
    url = f"{API_BASE_URL}{path}"
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise VendorUnavailableError(f"Tiingo request failed: {type(exc).__name__}") from exc
        if response.status_code == 429 and attempt < _MAX_RETRIES:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 120.0) if retry_after else 2.0 * (2 ** attempt)
            except ValueError:
                delay = 2.0 * (2 ** attempt)
            logger.warning("Tiingo rate limited, retrying in %.0fs (attempt %d/%d)",
                           delay, attempt + 1, _MAX_RETRIES)
            time.sleep(delay)
            continue
        if response.status_code == 429:
            raise VendorUnavailableError("Tiingo rate limit exceeded after retries (HTTP 429)")
        if response.status_code in (401, 403):
            raise TiingoNotConfiguredError(f"Tiingo API key rejected (HTTP {response.status_code})")
        return response
    raise VendorUnavailableError(f"Tiingo request failed: {last_exc}")


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV for ``symbol`` in the window, as a header + CSV string.

    Same shape as ``get_YFin_data_online`` so agents see identical input
    whichever vendor serves the call.
    """
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")
    family, tiingo_symbol = to_tiingo_symbol(symbol)
    if family == "crypto":
        response = _get("/tiingo/crypto/prices", {
            "tickers": tiingo_symbol, "startDate": start_date,
            "endDate": end_date, "resampleFreq": "1day",
        })
        if response.status_code == 404:
            raise NoMarketDataError(symbol, tiingo_symbol, "unknown ticker")
        try:
            response.raise_for_status()
            payload = response.json()
            rows = (payload[0].get("priceData") or []) if payload else []
        except (requests.RequestException, ValueError, IndexError, AttributeError) as exc:
            raise VendorUnavailableError(f"Tiingo crypto response unusable: {type(exc).__name__}") from exc
        frame = _frame_from_crypto(rows)
    else:
        response = _get(f"/tiingo/daily/{tiingo_symbol}/prices",
                        {"startDate": start_date, "endDate": end_date})
        if response.status_code == 404:
            raise NoMarketDataError(symbol, tiingo_symbol, "unknown ticker")
        try:
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise VendorUnavailableError(f"Tiingo request failed (HTTP {response.status_code})") from exc
        except ValueError as exc:
            raise VendorUnavailableError("Tiingo returned a non-JSON response") from exc
        if not isinstance(payload, list) or not payload:
            raise NoMarketDataError(symbol, tiingo_symbol, f"no rows between {start_date} and {end_date}")
        frame = _frame_from_eod(payload)
    if frame is None or frame.empty:
        raise NoMarketDataError(symbol, tiingo_symbol, f"no rows between {start_date} and {end_date}")
    frame = frame.round(2)
    label = symbol if symbol.upper() == tiingo_symbol.upper() else f"{tiingo_symbol} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(frame)}\n\n"
    return header + frame.to_csv(index=False)


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
    family, tiingo_symbol = to_tiingo_symbol(symbol)
    if family == "crypto":
        response = _get("/tiingo/crypto/prices", {
            "tickers": tiingo_symbol, "startDate": start,
            "endDate": end, "resampleFreq": "1day",
        })
        try:
            response.raise_for_status()
            payload = response.json()
            rows = (payload[0].get("priceData") or []) if payload else []
        except (requests.RequestException, ValueError, IndexError, AttributeError) as exc:
            raise VendorUnavailableError(f"Tiingo crypto response unusable: {type(exc).__name__}") from exc
        frame = _frame_from_crypto(rows)
    else:
        response = _get(f"/tiingo/daily/{tiingo_symbol}/prices",
                        {"startDate": start, "endDate": end})
        try:
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise VendorUnavailableError(f"Tiingo request failed (HTTP {response.status_code})") from exc
        except ValueError as exc:
            raise VendorUnavailableError("Tiingo returned a non-JSON response") from exc
        if not isinstance(payload, list) or not payload:
            raise NoMarketDataError(symbol, tiingo_symbol, "no price rows for indicator window")
        frame = _frame_from_eod(payload)
    if frame is None or frame.empty:
        raise NoMarketDataError(symbol, tiingo_symbol, "no price rows for indicator window")
    values = compute_indicator_values(frame, indicator)
    return format_indicator_window(values, indicator, as_of_date, look_back_days)


def _frame_from_eod(payload: list) -> pd.DataFrame | None:
    try:
        frame = pd.DataFrame([{
            "Date": r["date"][:10],
            "Open": float(r["open"]), "High": float(r["high"]),
            "Low": float(r["low"]), "Close": float(r["close"]),
            "Volume": int(r.get("volume") or 0),
        } for r in payload if isinstance(r, dict)])
    except (KeyError, TypeError, ValueError):
        return None
    return frame.sort_values("Date") if not frame.empty else frame


def _frame_from_crypto(rows: list) -> pd.DataFrame | None:
    try:
        frame = pd.DataFrame([{
            "Date": r["date"][:10],
            "Open": float(r["open"]), "High": float(r["high"]),
            "Low": float(r["low"]), "Close": float(r["close"]),
            "Volume": int(float(r.get("volume") or 0)),
        } for r in rows if isinstance(r, dict)])
    except (KeyError, TypeError, ValueError):
        return None
    return frame.sort_values("Date") if not frame.empty else frame
