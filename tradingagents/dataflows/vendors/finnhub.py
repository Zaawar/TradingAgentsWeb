"""Finnhub vendor: US/crypto prices, company news and insider transactions.

Free tier: 60 calls/minute, no daily cap; stock candles cover the last year,
company news the last year (North America only), insider transactions the US,
UK, Canada, Australia, India and major EU markets. International listings are
a paid tier, so non-US symbols honestly report no coverage and the router
falls through to the next vendor.
"""

import logging
import os
from datetime import UTC, datetime

import pandas as pd
import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import (
    in_window,
    withhold_live_profile,
    withhold_undisclosed_trades,
)
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.net import get_scrubbed
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

API_BASE_URL = "https://finnhub.io/api/v1"

# Network timeout (seconds) so a stalled Finnhub request can't hang the
# CLI/agents indefinitely (same rationale as #990 for Alpha Vantage).
REQUEST_TIMEOUT = 30

# Free-tier stock history covers the last year; older windows fast-fail so a
# historical run falls through to a deeper vendor instead of spending a call.
FREE_HISTORY_DAYS = 365


class FinnhubNotConfiguredError(VendorNotConfiguredError):
    """Raised when Finnhub is selected but no API key is configured."""

    pass


def get_api_key() -> str:
    """Retrieve the API key for Finnhub from environment variables."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        raise FinnhubNotConfiguredError(
            "FINNHUB_API_KEY environment variable is not set. Get a free key at "
            "https://finnhub.io/register"
        )
    return api_key


def _request(path: str, params: dict):
    """GET ``path`` with the key attached; classify failures for the router.

    429/throttling, 403 (plan gate, e.g. candles on some keys) and network
    errors say nothing about the instrument -> VendorUnavailableError, and the
    router tries the next vendor. A resolved ``{"error": ...}`` body for a bad
    key surfaces as not-configured so it is actionable.
    """
    api_key = get_api_key()
    query = dict(params)
    query["token"] = api_key
    try:
        response = get_scrubbed(
            f"{API_BASE_URL}{path}", params=query, timeout=REQUEST_TIMEOUT, secret=api_key
        )
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Finnhub request failed: {type(exc).__name__}") from exc
    if response.status_code == 429:
        raise VendorUnavailableError("Finnhub rate limit exceeded (HTTP 429)")
    if response.status_code in (401, 403):
        # 403 doubles as Finnhub's plan gate ("You don't have access to this
        # resource"): it says nothing about the symbol, so fall through rather
        # than fail the run — except a body naming the key, which is actionable.
        body = response.text or ""
        if "api key" in body.lower() or "token" in body.lower():
            raise FinnhubNotConfiguredError(f"Finnhub API key rejected: {body[:200]}")
        raise VendorUnavailableError(f"Finnhub refused the request (HTTP {response.status_code})")
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise VendorUnavailableError(
            f"Finnhub request failed (HTTP {response.status_code})"
        ) from exc
    try:
        return response.json()
    except ValueError as exc:
        raise VendorUnavailableError("Finnhub returned a non-JSON response") from exc


def to_finnhub_symbol(symbol: str) -> tuple[str, str]:
    """(endpoint family, finnhub symbol) for a user ticker, or raise honest no-data.

    Only what the free tier demonstrably covers: US listings as-is and
    ``BASE-USD`` crypto as ``BINANCE:BASEUSDT``. Anything else (``^`` indices,
    ``=F`` futures, exchange-suffixed listings) reports no coverage so the
    router falls through instead of querying a symbol Finnhub would reject.
    """
    canonical = normalize_symbol(symbol)
    base, _, quote = canonical.partition("-")
    if quote in ("USD", "USDT", "USDC") and base.isalnum():
        return "crypto", f"BINANCE:{base.upper()}USDT"
    # Exchange-suffixed listings (".HK", ".T", ".NS", …) are outside the free
    # tier; dotted US tickers like BRK.B are not Yahoo-canonical and pass
    # through — Finnhub's own no-data answer handles whatever it can't serve.
    suffix = canonical.rsplit(".", 1)
    if len(suffix) == 2 and suffix[1] in (
        "HK", "T", "L", "NS", "BO", "TO", "AX", "SS", "SZ", "SA",
        "TW", "TWO", "KS", "KQ", "SI", "DE", "PA", "AS", "SW", "MI",
    ):
        raise NoMarketDataError(symbol, canonical, "Finnhub free tier does not cover exchange-suffixed listings")
    if not canonical or len(canonical) > 12 or canonical.startswith("^") or "=" in canonical \
            or not all(ch.isalnum() or ch in ".-" for ch in canonical):
        raise NoMarketDataError(symbol, canonical, "not covered by Finnhub free tier")
    return "stock", canonical.upper()


def _unix(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Daily OHLCV for ``symbol`` in the window, as a header + CSV string.

    Same shape as ``get_YFin_data_online`` so agents see identical input
    whichever vendor serves the call. Windows older than the free tier's
    one-year history fast-fail to the next vendor.
    """
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")
    if (datetime.now(tz=UTC).date() - datetime.strptime(end_date, "%Y-%m-%d").date()).days > FREE_HISTORY_DAYS:
        raise NoMarketDataError(
            symbol, symbol,
            f"Finnhub free tier covers the last year; {end_date} is older",
        )
    family, finnhub_symbol = to_finnhub_symbol(symbol)
    path = "/stock/candle" if family == "stock" else "/crypto/candle"
    payload = _request(path, {
        "symbol": finnhub_symbol, "resolution": "D",
        "from": _unix(start_date), "to": _unix(end_date) + 86399,
    })
    if not isinstance(payload, dict) or payload.get("s") != "ok":
        raise NoMarketDataError(symbol, finnhub_symbol, "no price rows in range")
    try:
        frame = pd.DataFrame({
            "Date": pd.to_datetime(payload["t"], unit="s", utc=True).tz_localize(None),
            "Open": pd.to_numeric(payload["o"]),
            "High": pd.to_numeric(payload["h"]),
            "Low": pd.to_numeric(payload["l"]),
            "Close": pd.to_numeric(payload["c"]),
            "Volume": pd.to_numeric(payload.get("v") or [0] * len(payload["t"])),
        })
    except (KeyError, TypeError, ValueError) as exc:
        raise NoMarketDataError(symbol, finnhub_symbol, f"unparseable candle payload: {exc}") from exc
    if frame.empty:
        raise NoMarketDataError(symbol, finnhub_symbol, f"no rows between {start_date} and {end_date}")
    frame = frame.round(2)
    label = symbol if symbol.upper() == finnhub_symbol else f"{finnhub_symbol} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(frame)}\n\n"
    return header + frame.to_csv(index=False)


def _extract_article(article: dict) -> dict:
    ts = article.get("datetime")
    pub_date = None
    if ts:
        try:
            pub_date = datetime.fromtimestamp(int(ts), tz=UTC)
        except (ValueError, OSError, TypeError):
            pub_date = None
    return {
        "title": article.get("headline") or "No title",
        "summary": article.get("summary") or "",
        "publisher": article.get("source") or "Unknown",
        "link": article.get("url") or "",
        "pub_date": pub_date,
    }


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Company news for ``ticker`` published in the window, Finnhub flavor.

    Same report shape as ``get_news_yfinance``; articles outside the window
    are dropped so nothing leaks past the analysis date. Finnhub company news
    covers North America only — anything else reports no coverage.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    family, finnhub_symbol = to_finnhub_symbol(ticker)
    if family != "stock":
        raise NoMarketDataError(ticker, finnhub_symbol, "Finnhub company news covers North American stocks")
    article_limit = get_config()["news_article_limit"]
    payload = _request("/company-news", {
        "symbol": finnhub_symbol,
        "from": start_date, "to": end_date,
    })
    if not isinstance(payload, list):
        raise NoMarketDataError(ticker, finnhub_symbol, "no news response")
    articles = [_extract_article(a) for a in payload if isinstance(a, dict)]
    in_range = [a for a in articles if in_window(a["pub_date"], start_dt, end_dt)][:article_limit]
    if in_range:
        out = f"## {ticker} News, from {start_date} to {end_date}:\n\n"
        for data in in_range:
            out += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                out += f"{data['summary']}\n"
            if data["link"]:
                out += f"Link: {data['link']}\n"
            out += "\n"
        return out
    return f"No news found for {ticker} between {start_date} and {end_date}"


def get_insider_transactions(ticker: str, as_of_date: str | None = None) -> str:
    """Insider transactions for ``ticker``; withheld for historical run dates.

    Finnhub dates a trade by when it happened with no filing date, so like the
    Yahoo and Alpha Vantage vendors it withholds for past runs
    (``withhold_undisclosed_trades``) rather than leaking undisclosed trades.
    """
    family, finnhub_symbol = to_finnhub_symbol(ticker)
    if family != "stock":
        raise NoMarketDataError(ticker, finnhub_symbol, "Finnhub insider data covers stocks")
    withheld = withhold_undisclosed_trades(as_of_date, finnhub_symbol)
    if withheld:
        return withheld
    payload = _request("/stock/insider-transactions", {"symbol": finnhub_symbol})
    rows = (payload.get("data") or []) if isinstance(payload, dict) else []
    if not rows:
        return f"No insider transactions reported for symbol '{finnhub_symbol}'"
    lines = ["name,shares,change,transactionDate,filingDate,transactionPrice"]
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        lines.append(",".join(str(row.get(k) or "") for k in
                              ("name", "share", "change", "transactionDate", "filingDate", "transactionPrice")))
    return f"# Insider Transactions data for {finnhub_symbol}\n" + "\n".join(lines)


def get_global_news(as_of_date: str, look_back_days: int | None = None,
                    limit: int | None = None) -> str:
    """Recent general-market headlines, date-window filtered.

    Same report shape as the Yahoo global-news vendor. Finnhub's general feed
    is a latest-headlines firehose rather than topic search, so this serves
    recent macro context — not the query-targeted coverage Yahoo builds from
    ``global_news_queries``. Nothing outside the window is served.
    """
    from datetime import timedelta

    from tradingagents.dataflows.date_window import coverage_gap

    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    curr_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    payload = _request("/news", {"category": "general"})
    if not isinstance(payload, list):
        raise NoMarketDataError("market", "market", "no general-news response")
    seen, kept = set(), []
    for article in payload:
        if not isinstance(article, dict) or len(kept) >= limit:
            break
        data = _extract_article(article)
        if not in_window(data["pub_date"], start_dt, curr_dt):
            continue
        if data["title"] and data["title"] not in seen:
            seen.add(data["title"])
            kept.append(data)
    if not kept:
        gap = coverage_gap((), start_date, as_of_date, "Finnhub general news", "market news")
        return gap or f"No global news found between {start_date} and {as_of_date}"
    out = f"## Global Market News, from {start_date} to {as_of_date}:\n\n"
    for data in kept:
        out += f"### {data['title']} (source: {data['publisher']})\n"
        if data["summary"]:
            out += f"{data['summary']}\n"
        if data["link"]:
            out += f"Link: {data['link']}\n"
        out += "\n"
    return out


def get_fundamentals(ticker: str, as_of_date: str | None = None):
    """Company overview from Finnhub profile, quote and metrics.

    Same ``# Company Fundamentals`` shape as the Yahoo vendor. Profile data
    is a present-day snapshot with no historical vintage, so a past
    ``as_of_date`` withholds it through the shared point-in-time guard,
    exactly like every other overview vendor.
    """
    family, finnhub_symbol = to_finnhub_symbol(ticker)
    if family != "stock":
        raise NoMarketDataError(ticker, finnhub_symbol, "Finnhub overview covers stocks")
    withheld = withhold_live_profile(as_of_date, finnhub_symbol)
    if withheld:
        return withheld

    profile = _request("/stock/profile2", {"symbol": finnhub_symbol})
    quote = _request("/quote", {"symbol": finnhub_symbol})
    metric = _request("/stock/metric", {"symbol": finnhub_symbol, "metric": "all"})
    if not isinstance(profile, dict) or not profile.get("name"):
        raise NoMarketDataError(ticker, finnhub_symbol, "no company profile")
    if not isinstance(quote, dict):
        quote = {}
    metrics = metric.get("metric", {}) if isinstance(metric, dict) else {}

    def first(*names):
        for name in names:
            value = metrics.get(name)
            if value is not None:
                return value
        return None

    market_cap = profile.get("marketCapitalization")
    fields = [
        ("Name", profile.get("name")),
        ("Exchange", profile.get("exchange")),
        ("Industry", profile.get("finnhubIndustry")),
        ("Country", profile.get("country")),
        ("Currency", profile.get("currency")),
        ("IPO Date", profile.get("ipo")),
        ("Market Cap", None if market_cap is None else f"{market_cap:,.2f} (USD millions)"),
        ("Current Price", quote.get("c")),
        ("Previous Close", quote.get("pc")),
        ("52 Week High", first("52WeekHigh")),
        ("52 Week Low", first("52WeekLow")),
        ("Beta", first("beta")),
        ("PE Ratio (TTM)", first("peBasicExclExtraTTM", "peNormalizedAnnual")),
        ("Price to Book", first("pbAnnual", "pbQuarterly")),
        ("EPS (TTM)", first("epsBasicExclExtraTTM", "epsNormalizedAnnual")),
        ("Dividend Yield", first("dividendYieldIndicatedAnnual", "currentDividendYieldTTM")),
        ("10 Day Avg Volume", first("10DayAverageTradingVolume")),
        ("3 Month Avg Volume", first("3MonthAverageTradingVolume")),
    ]
    lines = [f"{label}: {v}" for label, v in fields if v is not None]
    if not lines:
        raise NoMarketDataError(ticker, finnhub_symbol, "no usable overview fields")
    return f"# Company Fundamentals for {finnhub_symbol}\n\n" + "\n".join(lines)
