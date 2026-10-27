"""Manifold prediction-market vendor (no key needed).

Manifold is play-money: anyone can create a market and traders bet mana, not
dollars. Its probabilities are a strictly weaker signal than real-money
books, so every report labels the source and leads with unique-bettor count
and mana volume as the depth cues. Included as the last rung of the chain —
*something* honest when both real-money books are unreachable — never as a
primary.
"""

import logging
from datetime import UTC, datetime

import requests

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

MANIFOLD_BASE = "https://api.manifold.markets"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Default number of markets to return, most liquid first.
DEFAULT_LIMIT = 6


def _request(params: dict) -> list:
    try:
        response = requests.get(f"{MANIFOLD_BASE}/v0/search-markets",
                                params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Manifold request failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise VendorUnavailableError("Manifold returned a non-JSON response") from exc
    if not isinstance(payload, list):
        raise VendorUnavailableError("Manifold returned an unparseable payload")
    return payload


def _probability(market: dict) -> float | None:
    try:
        prob = float(market.get("probability"))
    except (TypeError, ValueError):
        return None
    return prob if 0.0 <= prob <= 1.0 else None


def _volume(market: dict) -> float:
    for key in ("volume_mana", "totalVolume", "volume"):
        try:
            value = market.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _resolve_date(market: dict) -> str:
    for key in ("closeTime", "close_time", "resolutionTime"):
        value = market.get(key)
        if value:
            try:
                return datetime.fromtimestamp(int(value) / 1000, tz=UTC).strftime("%Y-%m-%d")
            except (ValueError, OSError, TypeError):
                continue
    return ""


def get_prediction_markets(topic: str, limit: int | None = None, as_of_date: str | None = None) -> str:
    """Live Manifold probabilities for binary markets matching ``topic``.

    Same report shape as the other prediction vendors, explicitly labeled
    play-money. Only binary, still-open markets are served; everything else
    would need a different probability reading.
    """
    if is_historical(as_of_date):
        return (
            f"Prediction-market odds are withheld for {as_of_date}. Manifold serves "
            f"only live odds on open markets, with no historical vintage, so serving "
            f"them would put post-decision information into a {as_of_date} analysis."
        )
    if limit is None:
        limit = DEFAULT_LIMIT

    markets = _request({"term": topic, "filter": "open", "contractType": "BINARY",
                        "sort": "liquidity", "limit": max(limit, 10)})
    header = (
        f'## Manifold prediction markets: "{topic}"\n'
        f"Live, play-money implied probabilities (mana bets, not dollars — a "
        f"weaker signal than real-money books; more unique bettors and deeper "
        f"mana volume = more reliable). A probability is the crowd's priced "
        f"odds of the event, not a forecast you should take as certain.\n\n"
    )
    rows = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        prob = _probability(m)
        if prob is None:
            continue
        rows.append(m)
    if not rows:
        return header + (
            f"No open Manifold binary markets matched '{topic}'."
        )

    lines = []
    for m in rows[:limit]:
        prob = _probability(m)
        vol = _volume(m)
        bettors = m.get("unique_bettors") or m.get("uniqueBettors")
        try:
            depth = f"{int(bettors)} bettors, " if bettors is not None else ""
        except (TypeError, ValueError):
            depth = ""
        lines.append(
            f"- **{m.get('question') or m.get('id')}** — Yes {prob:.0%} "
            f"({depth}{vol:,.0f} mana volume, resolves {_resolve_date(m)})"
        )
    return header + "\n".join(lines) + "\n"
