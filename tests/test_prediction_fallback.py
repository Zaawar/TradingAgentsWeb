"""Prediction-market fallbacks: Kalshi (real-money) and Manifold (play-money).

Unit tests with mocked HTTP: shapes, keyword matching, historical
withholding, failure typing, and ordered fall-through. Live shapes were taken
from each vendor's public docs (Kalshi Quick Start, Manifold API reference);
neither needs a key, but neither was reachable from this network at build
time (TLS handshake failures), so live verification is deferred to a clear
network.
"""

import copy
import unittest
from unittest import mock

import pytest
import requests

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import kalshi, manifold


def _reset_config():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


_KALSHI_MARKETS = {"markets": [
    {"ticker": "FED-SEP-CUT", "title": "Fed cut rates in September?",
     "status": "open", "yes_bid": 68, "yes_ask": 70, "volume": 41250310,
     "close_time": "2026-09-30T00:00:00Z"},
    {"ticker": "CPI-HOT", "title": "CPI comes in hot?",
     "status": "open", "yes_bid": 20, "yes_ask": 24, "volume": 1000,
     "close_time": "2026-10-15T00:00:00Z"},
    {"ticker": "OLD", "title": "Fed did something?",
     "status": "closed", "yes_bid": 100, "yes_ask": 100, "volume": 99999999,
     "close_time": "2020-01-01T00:00:00Z"},
]}

_MANIFOLD_MARKETS = [
    {"id": "aaa", "question": "Will the Fed cut rates in September?",
     "probability": 0.69, "volume_mana": 588148.33, "unique_bettors": 361,
     "closeTime": 1791004800000, "url": "https://manifold.markets/x/y",
     "outcome_type": "BINARY"},
    {"id": "bbb", "question": "Vague future?",
     "probability": "not-a-number", "volume_mana": 5,
     "closeTime": 1791004800000, "outcome_type": "BINARY"},
]


@pytest.mark.unit
class KalshiTests(unittest.TestCase):
    def _get(self, payload):
        return mock.patch("requests.get", return_value=_Resp(payload=payload))

    def test_shape_and_midpoint(self):
        with self._get(_KALSHI_MARKETS):
            out = kalshi.get_prediction_markets("Fed rate cut")
        self.assertIn("## Kalshi prediction markets", out)
        self.assertIn("real-money", out)
        self.assertIn("Fed cut rates in September?", out)
        self.assertIn("Yes 69%", out)  # (68+70)/2/100
        self.assertIn("$41,250,310 volume", out)
        self.assertIn("resolves 2026-09-30", out)

    def test_keyword_filter_and_closed_excluded(self):
        with self._get(_KALSHI_MARKETS):
            out = kalshi.get_prediction_markets("Fed")
        self.assertIn("Fed cut rates", out)
        self.assertNotIn("CPI comes in hot?", out)  # no keyword overlap
        self.assertNotIn("Fed did something?", out)  # closed

    def test_no_matches_reports_clearly(self):
        with self._get({"markets": []}):
            out = kalshi.get_prediction_markets("obscure xyzzy")
        self.assertIn("No open Kalshi markets", out)

    def test_withheld_when_historical(self):
        with mock.patch("requests.get") as get:
            out = kalshi.get_prediction_markets("Fed", as_of_date="2026-09-01")
        self.assertIn("withheld for 2026-09-01", out)
        get.assert_not_called()

    def test_network_failure_raises(self):
        with mock.patch("requests.get", side_effect=requests.ConnectionError("down")), \
                self.assertRaises(VendorUnavailableError):
            kalshi.get_prediction_markets("Fed")


@pytest.mark.unit
class ManifoldTests(unittest.TestCase):
    def _get(self, payload):
        return mock.patch("requests.get", return_value=_Resp(payload=payload))

    def test_shape_and_labels(self):
        with self._get(_MANIFOLD_MARKETS):
            out = manifold.get_prediction_markets("Fed rate cut")
        self.assertIn("## Manifold prediction markets", out)
        self.assertIn("play-money", out)
        self.assertIn("Will the Fed cut rates in September?", out)
        self.assertIn("Yes 69%", out)
        self.assertIn("361 bettors", out)
        self.assertNotIn("Vague future?", out)  # unparseable probability

    def test_no_matches_reports_clearly(self):
        with self._get([]):
            out = manifold.get_prediction_markets("obscure xyzzy")
        self.assertIn("No open Manifold binary markets", out)

    def test_withheld_when_historical(self):
        with mock.patch("requests.get") as get:
            out = manifold.get_prediction_markets("Fed", as_of_date="2026-09-01")
        self.assertIn("withheld for 2026-09-01", out)
        get.assert_not_called()

    def test_network_failure_raises(self):
        with mock.patch("requests.get", side_effect=requests.ConnectionError("down")), \
                self.assertRaises(VendorUnavailableError):
            manifold.get_prediction_markets("Fed")


@pytest.mark.unit
class PredictionChainTests(unittest.TestCase):
    def setUp(self):
        _reset_config()

    def tearDown(self):
        _reset_config()

    def test_new_vendors_registered_and_default_chain(self):
        self.assertIn("kalshi", router.VENDOR_METHODS["get_prediction_markets"])
        self.assertIn("manifold", router.VENDOR_METHODS["get_prediction_markets"])
        self.assertEqual(
            default_config.DEFAULT_CONFIG["data_vendors"]["prediction_markets"],
            "polymarket,kalshi,manifold",
        )

    def test_chain_falls_through_to_kalshi(self):
        def down(*a, **k):
            raise VendorUnavailableError("Gamma down")

        set_config({"data_vendors": {"prediction_markets": "polymarket,kalshi,manifold"}})
        chain = {"polymarket": down, "kalshi": lambda *a, **k: "KALSHI_OK",
                 "manifold": mock.Mock()}
        with mock.patch.dict(router.VENDOR_METHODS, {"get_prediction_markets": chain}, clear=False):
            result = router.route_to_vendor("get_prediction_markets", "Fed", 5)
        self.assertEqual(result, "KALSHI_OK")
        chain["manifold"].assert_not_called()

    def test_chain_falls_through_to_manifold(self):
        def down(*a, **k):
            raise VendorUnavailableError("down")

        set_config({"data_vendors": {"prediction_markets": "polymarket,kalshi,manifold"}})
        chain = {"polymarket": down, "kalshi": down, "manifold": lambda *a, **k: "MANI_OK"}
        with mock.patch.dict(router.VENDOR_METHODS, {"get_prediction_markets": chain}, clear=False):
            result = router.route_to_vendor("get_prediction_markets", "Fed", 5)
        self.assertEqual(result, "MANI_OK")

    def test_all_down_degrades_to_sentinel(self):
        # prediction_markets is an optional category: total failure degrades,
        # it must not raise into the analyst.
        def down(*a, **k):
            raise VendorUnavailableError("down")

        set_config({"data_vendors": {"prediction_markets": "polymarket,kalshi,manifold"}})
        chain = {"polymarket": down, "kalshi": down, "manifold": down}
        with mock.patch.dict(router.VENDOR_METHODS, {"get_prediction_markets": chain}, clear=False):
            result = router.route_to_vendor("get_prediction_markets", "Fed", 5)
        self.assertIn("DATA_UNAVAILABLE", result)
