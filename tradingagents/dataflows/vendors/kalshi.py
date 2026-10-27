"""Kalshi prediction-market vendor (no key needed for reads).

Kalshi is a US-regulated, real-money exchange, so its prices carry the same
weight class as Polymarket's. Only the public market-data endpoints are used
(``GET /series``, ``GET /events``, ``GET /markets`` need no authentication);
nothing here trades. There is no full-text search endpoint, so topics are
matched by keyword against open-market titles, ranked by keyword overlap
then traded volume.
"""

import logging
import re

import requests

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Default number of markets to return, ranked by keyword overlap then volume.
DEFAULT_LIMIT = 6

# One bulk pull per call (the API has no text search); matches are filtered
# client-side, so this bounds the response without deciding relevance.
_SEARCH_LIMIT = 100


def _request(path: str, params: dict) -> dict:
    try:
        response = requests.get(f"{KALSHI_BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Kalshi request failed: {type(exc).__name__}") from exc


def _topic_tokens(topic: str) -> list[str]:
    return [w for w in re.split(r"\W+", (topic or "").lower()) if len(w) > 2]


def _match_score(title: str, tokens: list[str]) -> int:
    lowered = (title or "").lower()
    return sum(1 for t in tokens if t in lowered)


def _probability(market: dict) -> float | None:
    """Midpoint of the yes bid/ask in cents (0-100) as a fraction, or None."""
    try:
        bid, ask = market.get("yes_bid"), market.get("yes_ask")
        if bid is None and ask is None:
            return None
        if bid is None:
            return float(ask) / 100
        if ask is None:
            return float(bid) / 100
        return (float(bid) + float(ask)) / 200
    except (TypeError, ValueError):
        return None


def _resolve_date(market: dict) -> str:
    for key in ("close_time", "expiration_time", "expiration", "closeTime"):
        value = market.get(key)
        if value:
            return str(value)[:10]
    return ""


def get_prediction_markets(topic: str, limit: int | None = None, as_of_date: str | None = None) -> str:
    """Live Kalshi probabilities for markets matching ``topic``.

    Same report shape as the Polymarket vendor, labeled as Kalshi real-money
    prices. Kalshi coverage is US-centric (elections, Fed, econ); crypto and
    geopolitics are thinner, which reads as fewer matches, not as failure.
    """
    if is_historical(as_of_date):
        return (
            f"Prediction-market odds are withheld for {as_of_date}. Kalshi serves "
            f"only live odds on open markets, with no historical vintage, so serving "
            f"them would put post-decision information into a {as_of_date} analysis."
        )
    if limit is None:
        limit = DEFAULT_LIMIT

    data = _request("markets", {"status": "open", "limit": _SEARCH_LIMIT})
    markets = data.get("markets") if isinstance(data, dict) else None
    if not isinstance(markets, list):
        raise VendorUnavailableError("Kalshi returned an unparseable markets payload")

    tokens = _topic_tokens(topic)
    scored = []
    for m in markets:
        if not isinstance(m, dict) or (m.get("status") or "open") not in ("open",):
            continue
        score = _match_score(m.get("title") or m.get("ticker") or "", tokens) if tokens else 1
        if score <= 0:
            continue
        prob = _probability(m)
        if prob is None:
            continue
        try:
            volume = int(m.get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0
        scored.append((score, volume, m, prob))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

    header = (
        f'## Kalshi prediction markets: "{topic}"\n'
        f"Live, real-money implied probabilities from the US-regulated Kalshi "
        f"exchange (yes bid/ask midpoint; higher traded volume = deeper, "
        f"more reliable). A probability is the crowd's priced odds of the event, "
        f"not a forecast you should take as certain.\n\n"
    )
    if not scored:
        return header + (
            f"No open Kalshi markets matched '{topic}'. Kalshi coverage is "
            f"concentrated in US elections, Fed decisions, and economic "
            f"indicators; crypto and geopolitics are thinner here than on "
            f"Polymarket."
        )

    lines = []
    for _score, _volume, m, prob in scored[:limit]:
        volume = m.get("volume") or 0
        try:
            vol_str = f"${int(volume):,} volume"
        except (TypeError, ValueError):
            vol_str = "volume n/a"
        end_date = _resolve_date(m)
        lines.append(
            f"- **{m.get('title') or m.get('ticker')}** — Yes {prob:.0%} "
            f"({vol_str}, resolves {end_date})"
        )
    return header + "\n".join(lines) + "\n"
