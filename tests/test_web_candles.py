"""The chart asks for a year once and slices locally; the lookback is clamped.

Zooming between 1M and 1Y must not cost a vendor call each time, so the client
fetches one series and windows it. The server still guards the lookback it is
willing to ask a vendor for, and a bare client keeps the original six months.
"""

from __future__ import annotations

import pytest

from web.server import _bars_from_vendor_csv, _candle_span, candles


@pytest.mark.unit
def test_a_bare_client_still_gets_six_months():
    assert _candle_span(None) == 195


@pytest.mark.unit
def test_the_lookback_is_clamped_to_a_range_vendors_serve():
    assert _candle_span(10) == 30
    assert _candle_span(400) == 400
    assert _candle_span(5000) == 900


@pytest.mark.unit
def test_a_non_numeric_lookback_is_reported_not_coerced():
    with pytest.raises(ValueError, match="day count"):
        _candle_span("last year")


@pytest.mark.unit
def test_a_bad_date_is_an_error_payload_before_any_vendor_call():
    assert candles("SPY", date="not-a-date") == {"error": "not a date: 'not-a-date'"}
    assert candles("SPY", days="nope") == {"error": "not a day count: 'nope'"}


@pytest.mark.unit
def test_vendor_csv_becomes_chart_bars():
    csv_text = (
        "# Stock data for SPY\n"
        "Date,Open,High,Low,Close,Volume\n"
        "2026-10-26,767.1,771.2,764.0,769.7,89100000\n"
        "2026-10-27,769.7,772.0,768.0,770.5,\n"
    )
    bars = _bars_from_vendor_csv(csv_text)

    assert [b["t"] for b in bars] == ["2026-10-26", "2026-10-27"]
    assert bars[0] == {"t": "2026-10-26", "o": 767.1, "h": 771.2, "l": 764.0, "c": 769.7, "v": 89100000}
    # an empty volume cell is a zero, not a dropped bar
    assert bars[1]["v"] == 0


@pytest.mark.unit
def test_a_vendor_sentinel_yields_no_bars():
    assert _bars_from_vendor_csv("DATA_UNAVAILABLE: get_stock_data") == []
    assert _bars_from_vendor_csv(None) == []
