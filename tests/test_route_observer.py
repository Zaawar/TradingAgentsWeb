"""Route observer: UIs hear vendor fall-through as it happens.

The observer is purely additive — with no subscriber, routing behaves
exactly as before (covered by asserting identical verdicts with and without
a sink). Events carry method + symbol so concurrent runs can be told apart.
"""

import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.router import observe_routes


def _reset_config():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


def _raises(exc):
    def impl(*a, **k):
        raise exc
    return impl


def _returns(value):
    def impl(*a, **k):
        return value
    return impl


@pytest.mark.unit
class RouteObserverTests(unittest.TestCase):
    def setUp(self):
        _reset_config()
        self.assertEqual(router._route_sinks, [])

    def tearDown(self):
        _reset_config()
        self.assertEqual(router._route_sinks, [])

    def _route(self, vendors):
        return mock.patch.dict(router.VENDOR_METHODS, {"get_stock_data": vendors}, clear=False)

    def test_first_try_success_is_silent(self):
        events = []
        set_config({"data_vendors": {"core_stock_apis": "a,b"}})
        with self._route({"a": _returns("DATA"), "b": _returns("NEVER")}), \
                observe_routes(events.append):
            result = router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "DATA")
        self.assertEqual(events, [])

    def test_fallback_then_served(self):
        events = []
        set_config({"data_vendors": {"core_stock_apis": "a,b"}})
        with self._route({"a": _raises(VendorUnavailableError("throttled")), "b": _returns("DATA")}), \
                observe_routes(events.append):
            result = router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "DATA")
        self.assertEqual([e["event"] for e in events], ["fallback", "served"])
        fallback, served = events
        self.assertEqual(fallback["vendor"], "a")
        self.assertEqual(fallback["next"], "b")
        self.assertIn("throttled", fallback["reason"])
        self.assertEqual(fallback["method"], "get_stock_data")
        self.assertEqual(fallback["symbol"], "AAPL")
        self.assertEqual(served["vendor"], "b")
        self.assertEqual(served["after"], 1)

    def test_no_data_fallthrough_reports_reason(self):
        events = []
        set_config({"data_vendors": {"core_stock_apis": "a,b"}})
        with self._route({"a": _raises(NoMarketDataError("X", "X", "no rows")),
                          "b": _returns("DATA")}), \
                observe_routes(events.append):
            router.route_to_vendor("get_stock_data", "X", "2026-01-01", "2026-01-10")
        self.assertEqual(events[0]["event"], "fallback")
        self.assertIn("no data", events[0]["reason"])

    def test_not_configured_fallthrough(self):
        events = []
        set_config({"data_vendors": {"core_stock_apis": "a,b"}})
        with self._route({"a": _raises(VendorNotConfiguredError("no key")), "b": _returns("DATA")}), \
                observe_routes(events.append):
            router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertIn("not configured", events[0]["reason"])

    def test_exhausted_chain_emits_verdict(self):
        events = []
        set_config({"data_vendors": {"core_stock_apis": "a,b"}})
        with self._route({"a": _raises(VendorUnavailableError("down")),
                          "b": _raises(VendorUnavailableError("down"))}), \
                observe_routes(events.append):
            result = router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertIn("DATA_UNAVAILABLE", result)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["fallback", "exhausted"])
        self.assertEqual(events[-1]["verdict"], "unavailable")

    def test_no_sink_changes_nothing(self):
        # Identical verdicts with and without a subscriber: observation is
        # purely additive and can never steer routing.
        set_config({"data_vendors": {"core_stock_apis": "a,b"}})
        chain = {"a": _raises(VendorUnavailableError("down")), "b": _returns("DATA")}
        with self._route(chain):
            plain = router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        events = []
        with self._route(chain), observe_routes(events.append):
            observed = router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(plain, observed)

    def test_raising_sink_does_not_break_routing(self):
        def bad_sink(ev):
            raise RuntimeError("sink boom")

        set_config({"data_vendors": {"core_stock_apis": "a,b"}})
        with self._route({"a": _raises(VendorUnavailableError("down")), "b": _returns("DATA")}), \
                observe_routes(bad_sink):
            result = router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "DATA")

    def test_nesting_restores_outer_sink(self):
        outer, inner = [], []
        set_config({"data_vendors": {"core_stock_apis": "a,b"}})
        chain = {"a": _raises(VendorUnavailableError("down")), "b": _returns("DATA")}
        with self._route(chain), observe_routes(outer.append), observe_routes(inner.append):
            router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertTrue(outer and inner)
        # After exit, neither fires.
        with self._route(chain):
            router.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(len(outer), len(inner))
