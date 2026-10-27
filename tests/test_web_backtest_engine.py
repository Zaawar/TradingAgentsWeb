"""A sweep runs on the engine chosen in Run setup, not the process default.

The backtest builds the same graph as an analysis, so the provider, endpoint,
models and depth from the Run setup panel have to reach ``run_backtest``.
Otherwise the same choices would apply to one tab and silently not the other.

Cells are cached per (ticker, date) without the engine in the key, so a resumed
run id has to say when its old cells were computed with something else.
"""

from __future__ import annotations

import json

import pytest

from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from web.server import (
    BacktestRequest,
    _backtest_config,
    _engine_changes,
    _engine_note,
    _run_record,
    _stamp_run,
)


def _request(**overrides) -> BacktestRequest:
    return BacktestRequest(**overrides)


@pytest.mark.unit
def test_run_setup_fields_override_the_process_defaults():
    config, engine = _backtest_config(_request(
        llm_provider="openai_compatible",
        backend_url="http://10.18.252.56:8000/v1",
        quick_think_llm="llm-local",
        deep_think_llm="llm-local",
        research_depth=3,
    ))

    assert config["llm_provider"] == "openai_compatible"
    assert config["backend_url"] == "http://10.18.252.56:8000/v1"
    assert config["quick_think_llm"] == "llm-local"
    assert config["deep_think_llm"] == "llm-local"
    assert config["max_debate_rounds"] == 3
    assert config["max_risk_discuss_rounds"] == 3
    assert engine == {
        "llm_provider": "openai_compatible",
        "backend_url": "http://10.18.252.56:8000/v1",
        "quick_think_llm": "llm-local",
        "deep_think_llm": "llm-local",
        "max_debate_rounds": 3,
        "max_risk_discuss_rounds": 3,
    }


@pytest.mark.unit
def test_omitted_fields_keep_the_process_defaults():
    """A bare API client still gets today's behaviour."""
    config, engine = _backtest_config(_request())

    assert config["llm_provider"] == DEFAULT_CONFIG["llm_provider"].lower()
    assert config["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]
    assert config["deep_think_llm"] == DEFAULT_CONFIG["deep_think_llm"]
    assert config["max_debate_rounds"] == DEFAULT_CONFIG["max_debate_rounds"]
    assert engine["deep_think_llm"] == DEFAULT_CONFIG["deep_think_llm"]


@pytest.mark.unit
def test_effort_is_only_overridden_when_the_form_sends_it():
    """An empty effort box must not wipe a thinking level set in the environment."""
    config, _ = _backtest_config(_request(llm_provider="google"))
    assert config["google_thinking_level"] == DEFAULT_CONFIG["google_thinking_level"]

    config, _ = _backtest_config(_request(llm_provider="google", google_thinking_level="high"))
    assert config["google_thinking_level"] == "high"


@pytest.mark.unit
def test_openai_compatible_without_a_backend_url_is_rejected():
    with pytest.raises(ValueError, match="backend URL"):
        _backtest_config(_request(llm_provider="openai_compatible"))


@pytest.mark.unit
def test_engine_note_names_the_engine_and_depth():
    _, engine = _backtest_config(_request(
        llm_provider="openai_compatible",
        backend_url="http://127.0.0.1:8000/v1",
        deep_think_llm="llm-local",
        research_depth=1,
    ))
    assert _engine_note(engine) == "openai_compatible · llm-local · 1 round"


@pytest.mark.unit
def test_engine_changes_lists_only_what_moved():
    _, engine = _backtest_config(_request(
        llm_provider="openai_compatible", backend_url="http://127.0.0.1:8000/v1",
        quick_think_llm="llm-local", deep_think_llm="llm-local", research_depth=1,
    ))
    assert _engine_changes(dict(engine), engine) == []
    assert _engine_changes({**engine, "deep_think_llm": "gpt-6-sol", "max_debate_rounds": 3}, engine) \
        == ["deep_think_llm", "max_debate_rounds"]


@pytest.mark.unit
def test_a_finished_sweep_records_its_engine_for_the_next_resume(tmp_path):
    config = {**DEFAULT_CONFIG, "results_dir": str(tmp_path)}
    _, engine = _backtest_config(_request(llm_provider="google", research_depth=1))
    log_path = tmp_path / "backtest" / "sweep1" / "trading_memory.md"
    log_path.parent.mkdir(parents=True)
    log_path.touch()

    _stamp_run(log_path, {**engine, "tickers": ["SPY"]})

    record = _run_record("sweep1", config)
    assert record is not None
    assert record["llm_provider"] == "google"
    assert record["tickers"] == ["SPY"]


@pytest.mark.unit
def test_an_unknown_or_unrecorded_run_has_no_engine(tmp_path):
    config = {**DEFAULT_CONFIG, "results_dir": str(tmp_path)}
    assert _run_record("never-ran", config) is None

    # A pre-existing sweep from before the stamp existed reads as "unknown"
    # rather than as a false mismatch.
    (tmp_path / "backtest" / safe_ticker_component("old-run")).mkdir(parents=True)
    assert _run_record("old-run", config) is None


@pytest.mark.unit
def test_a_corrupt_stamp_does_not_break_a_resume(tmp_path):
    config = {**DEFAULT_CONFIG, "results_dir": str(tmp_path)}
    run_dir = tmp_path / "backtest" / "broken"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{not json", encoding="utf-8")

    assert _run_record("broken", config) is None


@pytest.mark.unit
def test_the_stamp_is_plain_json(tmp_path):
    log_path = tmp_path / "backtest" / "sweep2" / "trading_memory.md"
    log_path.parent.mkdir(parents=True)
    log_path.touch()

    _stamp_run(log_path, {"llm_provider": "google", "max_debate_rounds": 1})

    written = json.loads((log_path.parent / "run.json").read_text(encoding="utf-8"))
    assert written == {"llm_provider": "google", "max_debate_rounds": 1}
