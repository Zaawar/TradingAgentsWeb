"""Fallback price/news vendors: Finnhub, Twelve Data, Tiingo.

Unit tests with mocked HTTP: response shapes, honest no-data gates,
point-in-time guards, key handling, and ordered fall-through in the router.
Live behavior of each vendor was verified against its real API during
development (2026-10-06); these tests pin the contract, not the network.
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
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors import finnhub, tiingo, twelvedata


def _reset_config():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


class _Resp:
    """Minimal requests.Response double for get_scrubbed/requests.get."""

    def __init__(self, status=200, payload=None, raw="", headers=None, json_fails=False):
        self.status_code = status
        self._payload = payload
        self.text = raw
        self.headers = headers or {}
        self._json_fails = json_fails

    def json(self):
        if self._json_fails:
            raise ValueError("No JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _scrubbed(resp=None, exc=None):
    def fake(*a, **k):
        if exc is not None:
            raise exc
        return resp
    return fake


@pytest.mark.unit
class FallbackVendorTests(unittest.TestCase):
    def setUp(self):
        _reset_config()

    def tearDown(self):
        _reset_config()

    # --- Finnhub ---

    def test_finnhub_stock_shape(self):
        payload = {"s": "ok",
                   "t": [1759104000, 1759190400],
                   "o": [100.0, 101.0], "h": [102.0, 103.0],
                   "l": [99.0, 100.0], "c": [101.5, 102.5], "v": [1000, 2000]}
        with mock.patch.object(finnhub, "get_scrubbed", _scrubbed(_Resp(payload=payload))), \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}):
            out = finnhub.get_stock("AAPL", "2026-09-22", "2026-09-23")
        self.assertIn("# Stock data for AAPL", out)
        self.assertIn("# Total records: 2", out)
        self.assertIn("Date,Open,High,Low,Close,Volume", out)

    def test_finnhub_stock_no_data(self):
        with mock.patch.object(finnhub, "get_scrubbed", _scrubbed(_Resp(payload={"s": "no_data"}))), \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}), \
                self.assertRaises(NoMarketDataError):
            finnhub.get_stock("AAPL", "2026-09-22", "2026-09-23")

    def test_finnhub_hk_gate_makes_no_request(self):
        with mock.patch.object(finnhub, "get_scrubbed") as scrub, \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}), \
                self.assertRaises(NoMarketDataError):
            finnhub.get_stock("0700.HK", "2026-10-01", "2026-10-06")
        scrub.assert_not_called()

    def test_finnhub_old_date_fast_fails(self):
        with mock.patch.object(finnhub, "get_scrubbed") as scrub, \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}), \
                self.assertRaises(NoMarketDataError):
            finnhub.get_stock("AAPL", "2024-01-01", "2024-02-01")
        scrub.assert_not_called()

    def test_finnhub_missing_key(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os as _os
            _os.environ.pop("FINNHUB_API_KEY", None)
            with self.assertRaises(finnhub.FinnhubNotConfiguredError):
                finnhub.get_stock("AAPL", "2026-10-01", "2026-10-06")

    def test_finnhub_429_is_unavailable(self):
        with mock.patch.object(finnhub, "get_scrubbed", _scrubbed(_Resp(status=429, raw="slow down"))), \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}), \
                self.assertRaises(VendorUnavailableError):
            finnhub.get_stock("AAPL", "2026-10-01", "2026-10-06")

    def test_finnhub_news_window(self):
        articles = [
            {"headline": "In range", "summary": "s", "source": "X",
             "url": "http://x", "datetime": 1759190400},   # 2025-09-30
            {"headline": "Too old", "summary": "s", "source": "X",
             "url": "http://x", "datetime": 1700000000},    # 2023
        ]
        with mock.patch.object(finnhub, "get_scrubbed", _scrubbed(_Resp(payload=articles))), \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}):
            out = finnhub.get_news("AAPL", "2025-09-29", "2025-10-01")
        self.assertIn("In range", out)
        self.assertNotIn("Too old", out)

    def test_finnhub_insider_withheld_when_historical(self):
        out = finnhub.get_insider_transactions("AAPL", "2026-09-01")
        self.assertIn("withheld", out)

    def test_finnhub_insider_shape(self):
        payload = {"data": [{"name": "Doe Jane", "share": 100, "change": -10,
                              "transactionDate": "2026-10-01", "filingDate": "2026-10-02",
                              "transactionPrice": 200.0}]}
        with mock.patch.object(finnhub, "get_scrubbed", _scrubbed(_Resp(payload=payload))), \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}):
            out = finnhub.get_insider_transactions("AAPL")
        self.assertIn("# Insider Transactions data for AAPL", out)
        self.assertIn("Doe Jane", out)

    def test_finnhub_global_news_window(self):
        articles = [
            {"headline": "Fresh", "summary": "s", "source": "X",
             "url": "http://x", "datetime": 1791235200},   # 2026-10-05
            {"headline": "Stale", "summary": "s", "source": "X",
             "url": "http://x", "datetime": 1700000000},    # 2023
        ]
        with mock.patch.object(finnhub, "get_scrubbed", _scrubbed(_Resp(payload=articles))), \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}):
            out = finnhub.get_global_news("2026-10-06")
        self.assertIn("## Global Market News, from 2026-09-29 to 2026-10-06:", out)
        self.assertIn("Fresh", out)
        self.assertNotIn("Stale", out)

    def test_finnhub_global_news_empty(self):
        with mock.patch.object(finnhub, "get_scrubbed", _scrubbed(_Resp(payload=[]))), \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}):
            out = finnhub.get_global_news("2026-10-06")
        # Empty feed: unavailable (a sample), never "no news".
        self.assertIn("unavailable", out)

    def test_finnhub_fundamentals_shape(self):
        def fake(path, params):
            if path == "/stock/profile2":
                return {"name": "Apple Inc", "exchange": "NASDAQ", "finnhubIndustry": "Technology",
                        "country": "US", "currency": "USD", "ipo": "1980-12-12",
                        "marketCapitalization": 1000.0}
            if path == "/quote":
                return {"c": 200.0, "pc": 199.0}
            return {"metric": {"beta": 1.1, "52WeekHigh": 210.0}}
        with mock.patch.object(finnhub, "_request", side_effect=lambda p, q: fake(p, q)), \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}):
            # A run dated today may carry the live profile; any earlier date is
            # withheld (see test_finnhub_fundamentals_withheld_when_historical).
            out = finnhub.get_fundamentals("AAPL", get_current_date())
        self.assertIn("# Company Fundamentals for AAPL", out)
        self.assertIn("Name: Apple Inc", out)
        self.assertIn("Beta: 1.1", out)

    def test_finnhub_fundamentals_withheld_when_historical(self):
        with mock.patch.object(finnhub, "_request") as req, \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}):
            out = finnhub.get_fundamentals("AAPL", "2026-09-01")
        self.assertIn("withheld", out)
        req.assert_not_called()

    def test_finnhub_fundamentals_crypto_gate(self):
        with mock.patch.object(finnhub, "_request") as req, \
                mock.patch.dict("os.environ", {"FINNHUB_API_KEY": "test"}), \
                self.assertRaises(NoMarketDataError):
            finnhub.get_fundamentals("BTC-USD", "2026-10-06")
        req.assert_not_called()

    # --- Twelve Data ---

    def _td_payload(self):
        return {"status": "ok", "values": [
            {"datetime": "2026-10-05 00:00:00", "open": "1.0", "high": "2.0",
             "low": "0.5", "close": "1.5", "volume": "100"},
            {"datetime": "2026-10-06 00:00:00", "open": "1.5", "high": "2.5",
             "low": "1.0", "close": "2.0", "volume": "200"},
        ]}

    def test_twelvedata_stock_shape(self):
        with mock.patch.object(twelvedata, "get_scrubbed", _scrubbed(_Resp(payload=self._td_payload()))), \
                mock.patch.dict("os.environ", {"TWELVEDATA_API_KEY": "test"}):
            out = twelvedata.get_stock("AAPL", "2026-10-05", "2026-10-06")
        self.assertIn("# Stock data for AAPL", out)
        self.assertIn("# Total records: 2", out)

    def test_twelvedata_hk_gate_makes_no_request(self):
        with mock.patch.object(twelvedata, "get_scrubbed") as scrub, \
                mock.patch.dict("os.environ", {"TWELVEDATA_API_KEY": "test"}), \
                self.assertRaises(NoMarketDataError):
            twelvedata.get_stock("0700.HK", "2026-10-01", "2026-10-06")
        scrub.assert_not_called()

    def test_twelvedata_error_payload(self):
        payload = {"status": "error", "message": "Unknown symbol"}
        with mock.patch.object(twelvedata, "get_scrubbed", _scrubbed(_Resp(payload=payload))), \
                mock.patch.dict("os.environ", {"TWELVEDATA_API_KEY": "test"}), \
                self.assertRaises(NoMarketDataError):
            twelvedata.get_stock("FAKE", "2026-10-01", "2026-10-06")

    def test_twelvedata_missing_key(self):
        import os as _os
        _os.environ.pop("TWELVEDATA_API_KEY", None)
        with self.assertRaises(twelvedata.TwelveDataNotConfiguredError):
            twelvedata.get_stock("AAPL", "2026-10-01", "2026-10-06")

    def test_twelvedata_indicator_shape(self):
        values = [{"datetime": f"2026-09-{d:02d} 00:00:00", "open": "100", "high": "101",
                   "low": "99", "close": str(100 + d * 0.1), "volume": "1000"}
                  for d in range(1, 30)]
        with mock.patch.object(twelvedata, "get_scrubbed", _scrubbed(_Resp(payload={"status": "ok", "values": values}))), \
                mock.patch.dict("os.environ", {"TWELVEDATA_API_KEY": "test"}):
            out = twelvedata.get_indicator("AAPL", "rsi", "2026-09-29", 1)
        self.assertIn("## rsi values from 2026-09-28 to 2026-09-29:", out)
        self.assertIn("RSI: Measures momentum", out)

    # --- Tiingo ---

    def _tiingo_eod(self):
        return [{"date": "2026-10-05T00:00:00Z", "open": 1.0, "high": 2.0, "low": 0.5,
                 "close": 1.5, "volume": 100},
                {"date": "2026-10-06T00:00:00Z", "open": 1.5, "high": 2.5, "low": 1.0,
                 "close": 2.0, "volume": 200}]

    def test_tiingo_stock_shape(self):
        with mock.patch.object(tiingo, "_get", return_value=_Resp(payload=self._tiingo_eod())), \
                mock.patch.dict("os.environ", {"TIINGO_API_KEY": "test"}):
            out = tiingo.get_stock("AAPL", "2026-10-05", "2026-10-06")
        self.assertIn("# Stock data for AAPL", out)
        self.assertIn("# Total records: 2", out)

    def test_tiingo_404_is_no_data(self):
        with mock.patch.object(tiingo, "_get", return_value=_Resp(status=404, raw="not found")), \
                mock.patch.dict("os.environ", {"TIINGO_API_KEY": "test"}), \
                self.assertRaises(NoMarketDataError):
            tiingo.get_stock("FAKE", "2026-10-05", "2026-10-06")

    def test_tiingo_missing_key(self):
        import os as _os
        _os.environ.pop("TIINGO_API_KEY", None)
        with self.assertRaises(tiingo.TiingoNotConfiguredError):
            tiingo.get_stock("AAPL", "2026-10-05", "2026-10-06")

    def test_tiingo_indicator_shape(self):
        rows = [{"date": f"2026-09-{d:02d}T00:00:00Z", "open": 100.0, "high": 101.0,
                 "low": 99.0, "close": 100.0 + d * 0.1, "volume": 1000}
                for d in range(1, 30)]
        with mock.patch.object(tiingo, "_get", return_value=_Resp(payload=rows)), \
                mock.patch.dict("os.environ", {"TIINGO_API_KEY": "test"}):
            out = tiingo.get_indicator("AAPL", "close_10_ema", "2026-09-29", 1)
        self.assertIn("## close_10_ema values from 2026-09-28 to 2026-09-29:", out)
        self.assertIn("10 EMA: A responsive short-term average.", out)

    # --- Router registration + chains ---

    def test_new_vendors_registered(self):
        self.assertIn("twelvedata", router.VENDOR_METHODS["get_stock_data"])
        self.assertIn("tiingo", router.VENDOR_METHODS["get_stock_data"])
        self.assertIn("twelvedata", router.VENDOR_METHODS["get_indicators"])
        self.assertIn("tiingo", router.VENDOR_METHODS["get_indicators"])
        self.assertIn("finnhub", router.VENDOR_METHODS["get_news"])
        self.assertIn("finnhub", router.VENDOR_METHODS["get_insider_transactions"])
        self.assertIn("finnhub", router.VENDOR_METHODS["get_fundamentals"])
        self.assertIn("finnhub", router.VENDOR_METHODS["get_global_news"])
        # Finnhub candles are unverified on some keys: not a stock vendor.
        self.assertNotIn("finnhub", router.VENDOR_METHODS["get_stock_data"])

    def test_default_chains_order(self):
        from tradingagents.default_config import DEFAULT_CONFIG as cfg

        self.assertEqual(cfg["data_vendors"]["core_stock_apis"],
                         "yfinance,twelvedata,tiingo,alpha_vantage")
        self.assertEqual(cfg["data_vendors"]["technical_indicators"],
                         "yfinance,twelvedata,tiingo,alpha_vantage")
        self.assertEqual(cfg["data_vendors"]["news_data"],
                         "yfinance,finnhub,alpha_vantage")

    def test_chain_falls_through_throttled_primary(self):
        from tradingagents.dataflows.errors import VendorUnavailableError

        def down(symbol, *a, **k):
            raise VendorUnavailableError("Yahoo 429")

        served = mock.Mock(return_value="TD_DATA")
        skipped = mock.Mock()
        set_config({"data_vendors": {"core_stock_apis": "yfinance,twelvedata,tiingo,alpha_vantage"}})
        with mock.patch.dict(router.VENDOR_METHODS,
                             {"get_stock_data": {"yfinance": down, "twelvedata": served,
                                                 "tiingo": skipped, "alpha_vantage": skipped}},
                             clear=False):
            result = router.route_to_vendor("get_stock_data", "AAPL", "2026-10-01", "2026-10-06")
        self.assertEqual(result, "TD_DATA")
        skipped.assert_not_called()  # chain stops at the first vendor that answers
