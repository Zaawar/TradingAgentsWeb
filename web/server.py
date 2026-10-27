"""TradingAgents Tailwind web UI — FastAPI backend.

Serves the Tailwind frontend in ``web/static/`` and streams runs as
server-sent events, mirroring everything the CLI shows
(``cli/selections.py``, ``cli/display.py``, ``cli/run.py``).

Run with::

    uvicorn web.server:app --port 8000

Then open http://localhost:8000 in a browser.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel

from cli import updates as cli_updates
from cli.display import (
    ANALYST_ORDER,
    AnalystWallTimeTracker,
    MessageBuffer,
    classify_message_type,
)
from cli.models import AnalystType, AssetType
from cli.prompts import (
    _llm_provider_table,
    detect_asset_type,
    parse_analysis_date,
    parse_analysts,
    parse_ticker,
    provider_default_url,
    resolve_backend_url,
)
from cli.prefs import load_last_run, save_last_run
from cli.stats_handler import StatsCallbackHandler
from tradingagents import __version__
from tradingagents.agents.rating import is_review, run_rating
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.model_catalog import get_model_options
from tradingagents.portfolio import PortfolioContext

_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "static"

LANGUAGES = [
    "English", "Chinese", "Japanese", "Korean", "Hindi", "Spanish",
    "Portuguese", "French", "German", "Arabic", "Russian",
]
DEPTHS = {"Shallow (1 round)": 1, "Medium (3 rounds)": 3, "Deep (5 rounds)": 5}
ANALYST_KEYS = ["market", "social", "news", "fundamentals"]

_md = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")


def render_md(text: str) -> str:
    """Agent markdown → safe HTML (raw HTML/images off, like report_html.py)."""
    return _md.render(text or "")


app = FastAPI(title="TradingAgents Web")
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(_STATIC / "index.html"))


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    ticker: str = "SPY"
    analysis_date: str | None = None
    output_language: str = "English"
    analysts: list[str] = ANALYST_KEYS
    research_depth: int = 1
    llm_provider: str = "openai"
    backend_url: str | None = None
    quick_think_llm: str = "gpt-6-luna"
    deep_think_llm: str = "gpt-6-sol"
    google_thinking_level: str | None = None
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None
    checkpoint: bool = False
    save_report: bool = True
    make_html: bool = True
    portfolio: dict | None = None


class BacktestRequest(BaseModel):
    tickers: str = "NVDA,AAPL"
    start: str = ""
    end: str = ""
    every: int = 7
    asset_type: str = "stock"
    analysts: list[str] = ANALYST_KEYS
    run_id: str | None = None
    # A sweep runs the same graph as an analysis, so the Run setup panel's
    # engine applies here too. Omitted fields keep the process defaults, which
    # is what a bare API client gets.
    llm_provider: str | None = None
    backend_url: str | None = None
    quick_think_llm: str | None = None
    deep_think_llm: str | None = None
    research_depth: int | None = None
    google_thinking_level: str | None = None
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None


# ---------------------------------------------------------------------------
# Meta (populates the setup form)
# ---------------------------------------------------------------------------


@app.get("/api/meta")
def meta():
    providers = [
        {"key": key, "label": label, "default_url": url} for label, key, url in _llm_provider_table()
    ]
    models: dict[str, dict[str, list[dict[str, str]]]] = {}
    for label, key, _url in _llm_provider_table():
        for mode in ("quick", "deep"):
            try:
                opts = get_model_options(key, mode)
            except KeyError:
                opts = [("Custom model ID", "custom")]
            models.setdefault(key, {})[mode] = [{"label": d, "value": v} for d, v in opts]
    return {
        "providers": providers,
        "models": models,
        "languages": LANGUAGES,
        "depths": [{"label": k, "value": v} for k, v in DEPTHS.items()],
        "analysts": ANALYST_KEYS,
        "prefs": load_last_run(),
        "env": {
            "provider": os.environ.get("TRADINGAGENTS_LLM_PROVIDER"),
            "quick": os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM"),
            "deep": os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM"),
            "backend_url": os.environ.get("TRADINGAGENTS_LLM_BACKEND_URL"),
            "temperature": os.environ.get("TRADINGAGENTS_TEMPERATURE"),
        },
        "announcements": _update_notices(),
        "today": datetime.date.today().isoformat(),
    }


def _update_notices() -> list[str]:
    """Newer release and notices for the header pill.

    v0.7 replaced the announcement feed with the CLI's cached update check, so
    the web UI reads that cache instead of calling the announcements API on
    every page load. The check itself runs in a background thread, at most
    daily, and is skipped when it has been turned off.
    """
    if os.environ.get(cli_updates.OPT_OUT):
        return []
    try:
        cli_updates.start_background_refresh()
        return cli_updates.messages(cli_updates.read_cache(), __version__)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Analysis streaming (mirrors cli/run.py)
# ---------------------------------------------------------------------------


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _snapshot(buffer: MessageBuffer, stats_handler, start_time) -> dict:
    stats = stats_handler.get_stats()
    return {
        "agents": dict(buffer.agent_status),
        "completed_reports": buffer.get_completed_reports_count(),
        "total_reports": len(buffer.report_sections),
        "llm_calls": stats["llm_calls"],
        "tool_calls": stats["tool_calls"],
        "tokens_in": stats["tokens_in"],
        "tokens_out": stats["tokens_out"],
        "elapsed": round(time.time() - start_time, 1),
    }


def _apply_debate_status(
    buffer: MessageBuffer,
    bull: str,
    bear: str,
    judge: str,
    last_speaker: str = "",
    debate_over: bool = False,
) -> None:
    """Move the research-team pills one debate turn at a time.

    Steps stream their state *after* they run, so the researcher that just
    spoke is done and the next speaker is the live one; the Research Manager
    stays queued until every turn has been spent. Streaming replays this state
    on every later step (the trader and risk team still carry it), so a
    finished agent is never pushed back to "in_progress".
    """
    if judge:
        for agent, hist in (("Bull Researcher", bull), ("Bear Researcher", bear)):
            if hist and buffer.agent_status.get(agent) != "completed":
                buffer.update_agent_status(agent, "completed")
        if buffer.agent_status.get("Research Manager") != "completed":
            buffer.update_agent_status("Research Manager", "completed")
        return

    if last_speaker.startswith("Bull"):
        spoke, next_up = "Bull Researcher", "Bear Researcher"
    elif last_speaker.startswith("Bear"):
        spoke, next_up = "Bear Researcher", "Bull Researcher"
    else:  # nothing has been argued yet
        return

    if buffer.agent_status.get(spoke) != "completed":
        buffer.update_agent_status(spoke, "completed")

    if debate_over:
        if buffer.agent_status.get("Research Manager") == "pending":
            buffer.update_agent_status("Research Manager", "in_progress")
    elif buffer.agent_status.get(next_up) != "in_progress":
        buffer.update_agent_status(next_up, "in_progress")


def stream_analysis(req: AnalyzeRequest):
    """Yield SSE dicts for a full analysis run."""
    try:
        ticker = parse_ticker(req.ticker or "SPY")
        analysis_date = parse_analysis_date(req.analysis_date or datetime.date.today().isoformat())
        asset = detect_asset_type(ticker)
        analysts = parse_analysts(",".join(req.analysts or ANALYST_KEYS), asset)
    except ValueError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    provider = (req.llm_provider or "openai").lower()
    backend_url = (req.backend_url or "").strip() or None
    if provider == "openai_compatible" and not (backend_url or "").startswith(("http://", "https://")):
        yield {"type": "error", "message": "OpenAI-compatible provider needs a backend URL starting with http:// or https://"}
        return

    portfolio = None
    if req.portfolio:
        try:
            portfolio = PortfolioContext.model_validate(req.portfolio)
        except Exception as exc:
            yield {"type": "error", "message": f"Portfolio JSON is not usable: {exc}"}
            return

    selected_keys = [a.value for a in analysts]
    selections = {
        "ticker": ticker, "asset_type": asset.value, "analysis_date": analysis_date,
        "analysts": analysts, "research_depth": req.research_depth,
        "llm_provider": provider, "backend_url": resolve_backend_url(provider, backend_url, env_url=DEFAULT_CONFIG["backend_url"]),
        "quick_think_llm": req.quick_think_llm, "deep_think_llm": req.deep_think_llm,
        "google_thinking_level": req.google_thinking_level,
        "openai_reasoning_effort": req.openai_reasoning_effort,
        "anthropic_effort": req.anthropic_effort,
        "output_language": req.output_language or "English",
    }
    save_last_run({**selections, "analysts": selected_keys,
                   "research_depth": req.research_depth, "llm_provider": provider})

    config = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = req.research_depth
    config["max_risk_discuss_rounds"] = req.research_depth
    config.update({"quick_think_llm": req.quick_think_llm, "deep_think_llm": req.deep_think_llm,
                   "backend_url": selections["backend_url"], "llm_provider": provider,
                   "google_thinking_level": req.google_thinking_level,
                   "openai_reasoning_effort": req.openai_reasoning_effort,
                   "anthropic_effort": req.anthropic_effort,
                   "output_language": selections["output_language"],
                   "checkpoint_enabled": req.checkpoint})

    yield {"type": "hello", "ticker": ticker, "analysis_date": analysis_date,
           "asset_type": asset.value, "analysts": selected_keys}

    try:
        stats_handler = StatsCallbackHandler()
        plan = build_analyst_execution_plan([a for a in ANALYST_ORDER if a in set(selected_keys)])
        tracker = AnalystWallTimeTracker(plan)
        graph = TradingAgentsGraph([a for a in ANALYST_ORDER if a in set(selected_keys)],
                                   config=config, debug=True, callbacks=[stats_handler])
        buffer = MessageBuffer()
        buffer.init_for_analysis(selected_keys)
        start_time = time.time()

        results_dir = Path(config["results_dir"]) / safe_ticker_component(ticker) / analysis_date
        results_dir.mkdir(parents=True, exist_ok=True)
        log_file = results_dir / "message_tool.log"
        log_file.touch(exist_ok=True)

        def emit_status(note: str | None = None):
            payload = {"type": "status", "snapshot": _snapshot(buffer, stats_handler, start_time)}
            if note:
                payload["note"] = note
            return payload

        def log_line(text: str):
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")

        now = datetime.datetime.now().strftime("%H:%M:%S")
        buffer.add_message("System", f"Selected ticker: {ticker}")
        log_line(f"{now} [System] Selected ticker: {ticker}")
        if asset.value != "stock":
            buffer.add_message("System", f"Detected asset type: {asset.value}")
        buffer.add_message("System", f"Analysis date: {analysis_date}")
        buffer.add_message("System", f"Selected analysts: {', '.join(selected_keys)}")
        yield emit_status("Starting analysis…")

        for spec in plan.specs:
            buffer.update_agent_status(spec.agent_node, "in_progress")
            tracker.mark_started(spec.key)
        yield emit_status(f"Analyzing {ticker} on {analysis_date}…")

        init_state = graph.create_run_state(ticker, analysis_date, asset.value, portfolio)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])
        tid = graph.begin_checkpoint(ticker, analysis_date, asset.value, portfolio)
        if tid is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid
        buffer.add_message("System", "Resuming the saved run" if getattr(graph, "_resuming", False)
                           else f"Starting fresh for {ticker} on {analysis_date}")
        yield emit_status()

        trace: list[dict] = []
        route_notes: list[dict] = []
        from tradingagents.dataflows.router import subscribe_routes, unsubscribe_routes

        def _route_sink(ev):
            sym = ev.get("symbol")
            if sym is not None and ev.get("method") in _TICKER_FIRST_METHODS \
                    and sym.upper() != ticker.upper():
                return  # another concurrent run's notice
            route_notes.append(ev)

        def _route_text(ev):
            if ev["event"] == "fallback":
                return (f"Data: {ev['vendor']} failed on {ev['method']} "
                        f"({ev['reason']}) — trying {ev['next']}")
            if ev["event"] == "served":
                return f"Data: {ev['method']} served by {ev['vendor']}"
            return f"Data: {ev['method']} exhausted all vendors ({ev['verdict']})"

        def _drain_routes():
            while route_notes:
                text = _route_text(route_notes.pop(0))
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                log_line(f"{ts} [Route] {text}")
                yield {"type": "route", "timestamp": ts, "text": text}

        subscribe_routes(_route_sink)
        try:
            for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
                for message in messages:
                    msg_id = getattr(message, "id", None)
                    if msg_id is not None:
                        if msg_id in buffer._processed_message_ids:
                            continue
                        buffer._processed_message_ids.add(msg_id)
                    msg_type, content = classify_message_type(message)
                    if content and content.strip():
                        ts = datetime.datetime.now().strftime("%H:%M:%S")
                        buffer.add_message(msg_type, content)
                        log_line(f"{ts} [{msg_type}] {str(content).replace(chr(10), ' ')}")
                        yield {"type": "message", "timestamp": ts, "kind": msg_type,
                               "content": str(content)[:2000],
                               "snapshot": _snapshot(buffer, stats_handler, start_time)}
                    if hasattr(message, "tool_calls") and message.tool_calls:
                        for tc in message.tool_calls:
                            name, tc_args = (tc["name"], tc["args"]) if isinstance(tc, dict) else (tc.name, tc.args)
                            ts = datetime.datetime.now().strftime("%H:%M:%S")
                            buffer.add_tool_call(name, tc_args)
                            arg_str = ", ".join(f"{k}={v}" for k, v in (tc_args or {}).items())
                            log_line(f"{ts} [Tool Call] {name}({arg_str})")
                            yield {"type": "tool", "timestamp": ts, "name": name,
                                   "args": {k: str(v)[:300] for k, v in (tc_args or {}).items()},
                                   "snapshot": _snapshot(buffer, stats_handler, start_time)}
                if chunk is None:
                    yield emit_status()
                    continue

                # Analyst reports (mirrors update_analyst_statuses, but emits events)
                from cli.display import ANALYST_AGENT_NAMES, ANALYST_REPORT_MAP

                for key in ANALYST_ORDER:
                    if key not in selected_keys:
                        continue
                    if chunk.get(ANALYST_REPORT_MAP[key]):
                        buffer.update_report_section(ANALYST_REPORT_MAP[key], chunk[ANALYST_REPORT_MAP[key]])
                        yield {"type": "report", "section": ANALYST_REPORT_MAP[key],
                               "title": ANALYST_AGENT_NAMES[key],
                               "markdown": chunk[ANALYST_REPORT_MAP[key]],
                               "html": render_md(chunk[ANALYST_REPORT_MAP[key]])}
                    if buffer.report_sections.get(ANALYST_REPORT_MAP[key]):
                        buffer.update_agent_status(ANALYST_AGENT_NAMES[key], "completed")
                    else:
                        buffer.update_agent_status(ANALYST_AGENT_NAMES[key], "in_progress")
                from cli.display import sync_analyst_tracker_from_chunk

                sync_analyst_tracker_from_chunk(tracker, chunk)

                if chunk.get("investment_debate_state"):
                    debate = chunk["investment_debate_state"]
                    bull = (debate.get("bull_history") or "").strip()
                    bear = (debate.get("bear_history") or "").strip()
                    judge = (chunk.get("investment_plan") or "").strip()
                    # The debate is over once every bull/bear turn is spent, the
                    # same rule the graph's router uses; the manager is queued
                    # behind it rather than running alongside the researchers.
                    _apply_debate_status(
                        buffer, bull, bear, judge,
                        last_speaker=(debate.get("current_response") or ""),
                        debate_over=(debate.get("count") or 0) >= 2 * config["max_debate_rounds"],
                    )
                    for title, hist, section in (("Bull Researcher", bull, "bull_history"),
                                                 ("Bear Researcher", bear, "bear_history")):
                        if hist:
                            text = f"### {title} Analysis\n{hist}"
                            buffer.update_report_section("investment_plan", text)
                            yield {"type": "report", "section": section, "title": title,
                                   "markdown": text, "html": render_md(text)}
                    if judge:
                        text = f"### Research Manager Decision\n{judge}"
                        buffer.update_report_section("investment_plan", text)
                        buffer.update_agent_status("Trader", "in_progress")
                        yield {"type": "report", "section": "investment_plan", "title": "Research Manager",
                               "markdown": text, "html": render_md(text)}

                if chunk.get("trader_investment_plan"):
                    buffer.update_report_section("trader_investment_plan", chunk["trader_investment_plan"])
                    if buffer.agent_status.get("Trader") != "completed":
                        buffer.update_agent_status("Trader", "completed")
                        buffer.update_agent_status("Aggressive Analyst", "in_progress")
                    yield {"type": "report", "section": "trader_investment_plan", "title": "Trader",
                           "markdown": chunk["trader_investment_plan"],
                           "html": render_md(chunk["trader_investment_plan"])}

                if chunk.get("risk_debate_state"):
                    risk = chunk["risk_debate_state"]
                    for key, agent, title, section in (
                        ("aggressive_history", "Aggressive Analyst", "Aggressive", "aggressive_history"),
                        ("conservative_history", "Conservative Analyst", "Conservative", "conservative_history"),
                        ("neutral_history", "Neutral Analyst", "Neutral", "neutral_history"),
                    ):
                        hist = (risk.get(key) or "").strip()
                        if hist:
                            if buffer.agent_status.get(agent) != "completed":
                                buffer.update_agent_status(agent, "in_progress")
                            text = f"### {title} Analyst Analysis\n{hist}"
                            buffer.update_report_section("final_trade_decision", text)
                            yield {"type": "report", "section": section, "title": f"{title} Analyst",
                                   "markdown": text, "html": render_md(text)}
                    judge = (chunk.get("final_trade_decision") or "").strip()
                    if judge and buffer.agent_status.get("Portfolio Manager") != "completed":
                        buffer.update_agent_status("Portfolio Manager", "in_progress")
                        text = f"### Portfolio Manager Decision\n{judge}"
                        buffer.update_report_section("final_trade_decision", text)
                        for agent in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
                                      "Portfolio Manager"):
                            buffer.update_agent_status(agent, "completed")
                        yield {"type": "report", "section": "final_trade_decision",
                               "title": "Portfolio Manager", "markdown": text, "html": render_md(text)}

                yield emit_status()
                yield from _drain_routes()
                trace.append(chunk)

            final_state: dict = {}
            for chunk in trace:
                final_state.update(chunk)
            graph.record_decision(ticker, analysis_date, final_state)
            graph.clear_checkpoint_on_success(ticker, analysis_date, asset.value, portfolio)
            yield from _drain_routes()
        finally:
            unsubscribe_routes(_route_sink)
            graph.end_checkpoint()

        for agent in buffer.agent_status:
            buffer.update_agent_status(agent, "completed")
        buffer.add_message("System", f"Completed analysis for {analysis_date}")
        buffer.add_message("System", tracker.format_summary())
        for section in buffer.report_sections:
            if section in final_state:
                buffer.update_report_section(section, final_state[section])
        yield emit_status("Analysis complete.")

        rating = run_rating(final_state)
        sections = []
        for key, title in (("market_report", "Market Analyst"), ("sentiment_report", "Sentiment Analyst"),
                           ("news_report", "News Analyst"), ("fundamentals_report", "Fundamentals Analyst"),
                           ("investment_plan", "Research Team Decision"),
                           ("trader_investment_plan", "Trading Team Plan"),
                           ("final_trade_decision", "Portfolio Manager Decision")):
            if final_state.get(key):
                sections.append({"key": key, "title": title,
                                 "markdown": final_state[key], "html": render_md(final_state[key])})
        # Debate histories live outside the section keys (same content the
        # saved bull.md / bear.md / risk files carry), so the web report
        # shows every researcher, not just the two managers.
        debate = final_state.get("investment_debate_state") or {}
        for key, title in (("bull_history", "Bull Researcher"), ("bear_history", "Bear Researcher")):
            if debate.get(key):
                text = debate[key]
                sections.append({"key": key, "title": title,
                                 "markdown": text, "html": render_md(text)})
        risk = final_state.get("risk_debate_state") or {}
        for key, title in (("aggressive_history", "Aggressive Analyst"),
                           ("conservative_history", "Conservative Analyst"),
                           ("neutral_history", "Neutral Analyst")):
            if risk.get(key):
                text = risk[key]
                sections.append({"key": key, "title": title,
                                 "markdown": text, "html": render_md(text)})

        saved_md, saved_html, save_dir = None, None, None
        if req.save_report:
            try:
                saver = TradingAgentsGraph(selected_keys, config=config)
                save_path = saver.default_report_path(ticker)
                saver.save_reports(final_state, ticker, save_path, html=req.make_html)
                save_dir = str(save_path.resolve())
                saved_md = (save_path / "complete_report.md").read_text(encoding="utf-8")
                if req.make_html:
                    saved_html = (save_path / "complete_report.html").read_text(encoding="utf-8")
            except Exception as exc:
                yield {"type": "warning", "message": f"Error saving report: {exc}"}

        yield {"type": "done", "rating": rating, "is_review": is_review(rating),
               "wall_time": tracker.format_summary(),
               "snapshot": _snapshot(buffer, stats_handler, start_time),
               "sections": sections, "save_dir": save_dir,
               "complete_md": saved_md, "complete_html": saved_html}
    except Exception as exc:
        yield from _drain_routes()
        yield {"type": "error", "message": f"Analysis failed: {exc}"}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    return StreamingResponse((_event(p) for p in stream_analysis(req)),
                             media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Backtest streaming (mirrors `tradingagents backtest`)
# ---------------------------------------------------------------------------


def _backtest_config(req: BacktestRequest) -> tuple[dict, dict]:
    """The Run setup panel's provider, models and depth, applied to a sweep.

    A sweep builds the same graph as an analysis, so it has to run on the same
    engine — otherwise the Run setup choices would silently apply to one tab
    and not the other.
    """
    provider = (req.llm_provider or DEFAULT_CONFIG["llm_provider"] or "openai").lower()
    backend_url = (req.backend_url or "").strip() or None
    if provider == "openai_compatible" and not (backend_url or "").startswith(("http://", "https://")):
        raise ValueError(
            "OpenAI-compatible provider needs a backend URL starting with http:// or https://"
        )
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = provider
    config["backend_url"] = resolve_backend_url(provider, backend_url, env_url=DEFAULT_CONFIG["backend_url"])
    if req.quick_think_llm:
        config["quick_think_llm"] = req.quick_think_llm
    if req.deep_think_llm:
        config["deep_think_llm"] = req.deep_think_llm
    if req.research_depth:
        config["max_debate_rounds"] = req.research_depth
        config["max_risk_discuss_rounds"] = req.research_depth
    for key, value in (("google_thinking_level", req.google_thinking_level),
                       ("openai_reasoning_effort", req.openai_reasoning_effort),
                       ("anthropic_effort", req.anthropic_effort)):
        if value is not None:
            config[key] = value
    return config, _engine_of(config)


# The keys that decide what a cell's decision was computed with. They are
# recorded next to the sweep so a resumed run on a different engine is visible
# instead of silently mixing models.
_ENGINE_KEYS = ("llm_provider", "backend_url", "quick_think_llm", "deep_think_llm",
                "max_debate_rounds", "max_risk_discuss_rounds")


def _engine_of(config: dict) -> dict:
    return {key: config.get(key) for key in _ENGINE_KEYS}


def _engine_note(engine: dict) -> str:
    depth = engine.get("max_debate_rounds")
    note = f"{engine['llm_provider']} · {engine['deep_think_llm']}"
    return note + (f" · {depth} round{'s' if depth != 1 else ''}" if depth else "")


def _engine_changes(previous: dict, engine: dict) -> list[str]:
    """The engine keys a resumed run id would mix, old cells against new ones."""
    return [key for key in _ENGINE_KEYS if previous.get(key) != engine.get(key)]


def _run_record(run_id: str, config: dict) -> dict | None:
    """The engine a previous sweep in this run id was computed with, if recorded."""
    try:
        path = Path(config["results_dir"]) / "backtest" / safe_ticker_component(run_id) / "run.json"
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _stamp_run(log_path, record: dict) -> None:
    """Record the engine beside a finished sweep; a cache miss for old runs is fine."""
    with contextlib.suppress(OSError):
        (Path(log_path).parent / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def stream_backtest(req: BacktestRequest):
    try:
        dates = iter_grid(req.start, req.end, req.every)
        kind = AssetType(req.asset_type.strip().lower())
        keys = req.analysts or ANALYST_KEYS
        from cli.prompts import filter_analysts_for_asset_type

        available = filter_analysts_for_asset_type(list(AnalystType), kind)
        chosen = [a for a in available if a.value in [k if k != "sentiment" else "social" for k in keys]]
        if not chosen:
            yield {"type": "error", "message": "name at least one analyst"}
            return
        config, engine = _backtest_config(req)
    except ValueError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    names = [t.strip() for t in req.tickers.split(",") if t.strip()]
    if not names:
        yield {"type": "error", "message": "No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL"}
        return

    yield {"type": "hello", "engine": engine, "note": _engine_note(engine)}

    # Cells are cached per (ticker, date) in the run's memory log, without the
    # engine in the key: resuming on a different engine reuses the old answers.
    if req.run_id:
        previous = _run_record(req.run_id, config)
        if previous:
            changed = _engine_changes(previous, engine)
            if changed:
                yield {"type": "warning",
                       "message": f"Run {req.run_id} was computed on {_engine_note(previous)}; "
                                  f"switching to {_engine_note(engine)} — cells already in its log "
                                  f"keep the old engine ({', '.join(changed)} changed)."}

    events: queue.Queue = queue.Queue()
    holder: dict = {}

    def progress(done, total, ticker, date):
        events.put({"type": "progress", "done": done, "total": total, "ticker": ticker, "date": date})

    def worker():
        from tradingagents.dataflows.router import subscribe_routes, unsubscribe_routes

        def _route_sink(ev):
            if ev["event"] == "fallback":
                text = (f"Data: {ev['vendor']} failed on {ev['method']} "
                        f"({ev['reason']}) — trying {ev['next']}")
            elif ev["event"] == "served":
                text = f"Data: {ev['method']} served by {ev['vendor']}"
            else:
                text = f"Data: {ev['method']} exhausted all vendors ({ev['verdict']})"
            events.put({"type": "route", "ticker": ev.get("symbol"), "text": text})

        subscribe_routes(_route_sink)
        try:
            holder["result"] = run_backtest(names, dates, config, asset_type=kind.value,
                                            run_id=req.run_id or None, progress=progress,
                                            selected_analysts=[a.value for a in chosen])
        except Exception as exc:
            holder["error"] = str(exc)
        finally:
            unsubscribe_routes(_route_sink)
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()
    for item in iter(events.get, None):
        yield item

    if "error" in holder:
        yield {"type": "error", "message": holder["error"]}
        return
    result = holder["result"]
    _stamp_run(result.log_path, {**engine, "tickers": names, "dates": dates,
                                 "analysts": [a.value for a in chosen],
                                 "asset_type": kind.value,
                                 "finished": datetime.datetime.now().isoformat(timespec="seconds")})
    yield {"type": "done",
           "summary": summarize(result).render(),
           "cells_run": result.cells_run, "skipped": result.skipped,
           "log_path": str(result.log_path), "run_id": result.run_id,
           "engine": engine, "note": _engine_note(engine),
           "failures": [{"ticker": t, "date": d, "reason": r} for t, d, r in result.failures],
           "settlement_failures": [{"ticker": t, "reason": r} for t, r in result.settlement_failures]}


@app.post("/api/backtest")
def backtest(req: BacktestRequest):
    return StreamingResponse((_event(p) for p in stream_backtest(req)),
                             media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Report history — browse saved runs under results_dir/reports
# ---------------------------------------------------------------------------


def _reports_root() -> Path:
    root = Path(DEFAULT_CONFIG["results_dir"]) / "reports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _history_entry(path: Path) -> dict:
    """One saved run: ticker/date/rating parsed from complete_report.md header."""
    md_file = path / "complete_report.md"
    ticker, date, rating, generated = path.name, "", "", ""
    try:
        head = md_file.read_text(encoding="utf-8")[:1500].splitlines()
        for line in head:
            s = line.strip()
            if s.startswith("# Trading Analysis Report:"):
                ticker = s.split(":", 1)[1].strip() or ticker
            elif s.startswith("- Analysis date:"):
                date = s.split(":", 1)[1].strip()
            elif s.startswith("- Rating:"):
                rating = s.split(":", 1)[1].strip()
            elif s.startswith("- Generated:"):
                generated = s.split(":", 1)[1].strip()
    except OSError:
        pass
    stat = path.stat()
    return {"name": path.name, "ticker": ticker, "analysis_date": date, "rating": rating,
            "generated": generated, "has_html": (path / "complete_report.html").is_file(),
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")}


def _history_path(name: str) -> Path:
    """Resolve a report dir name, contained in the reports root (no traversal)."""
    candidate = (_reports_root() / name).resolve()
    if candidate.parent != _reports_root().resolve() or not candidate.is_dir():
        raise ValueError(f"unknown report: {name!r}")
    if not (candidate / "complete_report.md").is_file():
        raise ValueError(f"report has no complete_report.md: {name!r}")
    return candidate


@app.get("/api/history")
def history_list():
    entries = [e for p in _reports_root().iterdir() if p.is_dir()
               for e in [_history_entry(p)] if (p / "complete_report.md").is_file()]
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return {"reports": entries, "root": str(_reports_root())}


@app.get("/api/history/{name}")
def history_view(name: str):
    try:
        path = _history_path(name)
    except ValueError as exc:
        return {"error": str(exc)}
    html_file = path / "complete_report.html"
    return {"name": name,
            "md": (path / "complete_report.md").read_text(encoding="utf-8"),
            "html": html_file.read_text(encoding="utf-8") if html_file.is_file() else None}


@app.delete("/api/history/{name}")
def history_delete(name: str):
    import shutil

    try:
        path = _history_path(name)
    except ValueError as exc:
        return {"deleted": False, "error": str(exc)}
    shutil.rmtree(path)
    return {"deleted": True, "name": name}


# ---------------------------------------------------------------------------
# Market visuals — live quotes tape + candlesticks (yfinance, cached)
# ---------------------------------------------------------------------------

_QUOTES_CACHE: dict = {"at": 0.0, "data": []}
_QUOTES_TTL = 300.0  # 5-minute tape: prices don't need sub-minute freshness,
# and every refresh walks each symbol through the vendor chain.
_TAPE = ["SPY", "QQQ", "DIA", "IWM", "NVDA", "AAPL", "BTC-USD", "ETH-USD"]
_CANDLE_CACHE: dict = {}

# Router methods whose first positional arg is the analyzed ticker: route
# notices for these are filtered to the requesting run's own ticker, so two
# concurrent runs don't narrate into each other's feeds. Other methods
# (global news, macro, prediction topics) always pass through.
_TICKER_FIRST_METHODS = frozenset({
    "get_stock_data", "get_indicators", "get_fundamentals",
    "get_balance_sheet", "get_cashflow", "get_income_statement",
    "get_news", "get_insider_transactions",
})


@app.get("/api/quotes")
def quotes():
    """Last price + day change for the tape. Cached 60s; degraded quietly.

    Served through the dataflows vendor chain (``core_stock_apis``) per
    symbol, so the tape falls back across yfinance/twelvedata/tiingo/
    alpha_vantage exactly like the agents do. When a fresh fetch yields
    nothing, the last good snapshot is served instead of an empty tape.
    """
    import datetime as _dt

    now = time.time()
    if now - _QUOTES_CACHE["at"] < _QUOTES_TTL and _QUOTES_CACHE["data"]:
        return {"quotes": _QUOTES_CACHE["data"], "cached": True}
    from tradingagents.dataflows.router import route_to_vendor

    end = _dt.date.today() + _dt.timedelta(days=1)
    start = end - _dt.timedelta(days=8)
    out = []
    for sym in _TAPE:
        try:
            result = route_to_vendor("get_stock_data", sym,
                                     start.isoformat(), end.isoformat())
            bars = _bars_from_vendor_csv(result)
            if not bars:
                continue
            closes = [b["c"] for b in bars[-2:]]
            last = closes[-1]
            prev = closes[-2] if len(closes) > 1 else last
            chg = (last - prev) / prev * 100 if prev else 0.0
            out.append({"symbol": sym, "price": round(last, 2), "chg": round(chg, 2)})
        except Exception:
            continue
    if out:
        _QUOTES_CACHE.update(at=now, data=out)
    return {"quotes": out or _QUOTES_CACHE["data"], "cached": not out}


_HEALTH_CACHE: dict = {"at": 0.0, "vendors": []}
_HEALTH_TTL = 120.0


@app.get("/api/health")
def health():
    """Vendor reachability at a glance, for deciding whether to launch a run.

    One minimal probe per source, never auto-polled by the UI (each probe on
    a metered vendor spends real quota). Results are cached briefly; Alpha
    Vantage is deliberately never probed (25/day is too precious) and reports
    configured/unprobed instead.
    """
    import datetime as _dt

    import requests

    now = time.time()
    if now - _HEALTH_CACHE["at"] < _HEALTH_TTL and _HEALTH_CACHE["vendors"]:
        return {"vendors": _HEALTH_CACHE["vendors"], "cached": True,
                "checked_at": _dt.datetime.fromtimestamp(_HEALTH_CACHE["at"]).strftime("%H:%M:%S")}

    vendors = []

    def probe(name, fn):
        try:
            detail = fn()
            vendors.append({"name": name, "status": "ok", "detail": detail})
        except Exception as exc:
            vendors.append({"name": name, "status": "down", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"})

    def probe_yfinance():
        import yfinance as yf

        df = yf.download("SPY", period="2d", progress=False)
        if df is None or df.empty:
            raise RuntimeError("empty result")
        return "SPY reachable"

    def probe_finnhub():
        key = os.environ.get("FINNHUB_API_KEY")
        if not key:
            raise RuntimeError("no key configured")
        r = requests.get("https://finnhub.io/api/v1/quote",
                         params={"symbol": "AAPL", "token": key}, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "quote ok"

    def probe_twelvedata():
        key = os.environ.get("TWELVEDATA_API_KEY")
        if not key:
            raise RuntimeError("no key configured")
        r = requests.get("https://api.twelvedata.com/price",
                         params={"symbol": "AAPL", "apikey": key}, timeout=15)
        j = r.json()
        if not isinstance(j, dict) or "price" not in j:
            raise RuntimeError(str(j)[:100])
        return "price ok"

    def probe_tiingo():
        key = os.environ.get("TIINGO_API_KEY")
        if not key:
            raise RuntimeError("no key configured")
        r = requests.get("https://api.tiingo.com/tiingo/daily/AAPL",
                         headers={"Authorization": f"Token {key}"}, timeout=15)
        if r.status_code == 429:
            raise RuntimeError("hourly quota exhausted (HTTP 429)")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "meta ok"

    def probe_fred():
        key = os.environ.get("FRED_API_KEY")
        if not key:
            raise RuntimeError("no key configured")
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "GDP", "api_key": key, "file_type": "json",
                    "observation_start": "2024-01-01", "observation_end": "2024-01-31",
                    "sort_order": "desc", "limit": 1},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "series ok"

    def probe_sec_edgar():
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": os.getenv("SEC_EDGAR_USER_AGENT") or "TradingAgents (contact@example.com)"},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "tickers ok"

    def probe_polymarket():
        r = requests.get(
            "https://gamma-api.polymarket.com/public-search",
            params={"q": "Fed", "limit_per_type": 1},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "search ok"

    def probe_kalshi():
        r = requests.get(
            "https://external-api.kalshi.com/trade-api/v2/markets",
            params={"status": "open", "limit": 1},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "markets ok"

    def probe_manifold():
        r = requests.get(
            "https://api.manifold.markets/v0/search-markets",
            params={"term": "Fed", "limit": 1},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "search ok"

    def probe_stocktwits():
        r = requests.get(
            "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json",
            headers={"User-Agent": "tradingagents/0.2"},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "stream ok"

    def probe_reddit():
        r = requests.get(
            "https://www.reddit.com/r/stocks/search.rss",
            params={"q": "AAPL", "sort": "new", "t": "week", "limit": 1},
            headers={"User-Agent": "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return "feed ok"

    def probe_alpha_vantage():
        key = os.environ.get("ALPHA_VANTAGE_API_KEY")
        if not key:
            raise RuntimeError("no key configured")
        r = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "GLOBAL_QUOTE", "symbol": "SPY", "apikey": key},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        j = r.json()
        if "Global Quote" not in j or not j["Global Quote"]:
            raise RuntimeError(str(j)[:100])
        return "quote ok"

    probe("yfinance", probe_yfinance)
    probe("finnhub", probe_finnhub)
    probe("twelvedata", probe_twelvedata)
    probe("tiingo", probe_tiingo)
    probe("fred", probe_fred)
    probe("sec_edgar", probe_sec_edgar)
    probe("polymarket", probe_polymarket)
    probe("kalshi", probe_kalshi)
    probe("manifold", probe_manifold)
    probe("stocktwits", probe_stocktwits)
    probe("reddit", probe_reddit)
    probe("alpha_vantage", probe_alpha_vantage)

    _HEALTH_CACHE.update(at=now, vendors=vendors)
    return {"vendors": vendors, "cached": False,
            "checked_at": _dt.datetime.fromtimestamp(now).strftime("%H:%M:%S")}


def _bars_from_vendor_csv(result) -> list[dict]:
    """Vendor ``get_stock_data`` output (header + CSV) -> chart bars, or [].

    Sentinel strings (DATA_UNAVAILABLE / NO_DATA_AVAILABLE) and unparseable
    rows yield no bars rather than raising, so callers degrade quietly.
    """
    import csv
    import io

    if not isinstance(result, str) or not result.startswith("# Stock data"):
        return []
    bars = []
    body = "\n".join(ln for ln in result.splitlines() if ln and not ln.startswith("#"))
    for row in csv.DictReader(io.StringIO(body)):
        try:
            vol = row.get("Volume") or 0
            vol = int(float(vol)) if float(vol) == float(vol) else 0  # NaN -> 0
        except (TypeError, ValueError):
            vol = 0
        try:
            bars.append({"t": (row.get("Date") or "")[:10],
                         "o": round(float(row["Open"]), 2), "h": round(float(row["High"]), 2),
                         "l": round(float(row["Low"]), 2), "c": round(float(row["Close"]), 2),
                         "v": vol})
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def _candle_span(days) -> int:
    """Calendar lookback for the chart, clamped to what vendors will serve."""
    try:
        return max(30, min(int(days or 195), 900))
    except (TypeError, ValueError):
        raise ValueError(f"not a day count: {days!r}") from None


@app.get("/api/candles")
def candles(ticker: str, date: str | None = None, days: int | None = None):
    """Daily OHLC ending at ``date`` (or today) for the chart.

    Served through the dataflows vendor chain (``core_stock_apis``), so the
    chart falls back across yfinance/twelvedata/tiingo/alpha_vantage exactly
    like the agents do, instead of depending on Yahoo alone.

    ``days`` is the calendar lookback. The web chart asks for a year once and
    slices locally for its 1M/3M/6M/1Y chips, so zooming costs no vendor calls;
    a bare client still gets the original six months.
    """
    import datetime as _dt

    try:
        end = _dt.date.fromisoformat(date) + _dt.timedelta(days=1) if date else _dt.date.today()
    except ValueError:
        return {"error": f"not a date: {date!r}"}
    try:
        span = _candle_span(days)
    except ValueError as exc:
        return {"error": str(exc)}
    start = end - _dt.timedelta(days=span)
    key = (ticker.upper(), str(start), str(end))
    if key in _CANDLE_CACHE:
        return _CANDLE_CACHE[key]
    try:
        from tradingagents.dataflows.router import route_to_vendor

        result = route_to_vendor("get_stock_data", ticker,
                                 start.isoformat(), end.isoformat())
        bars = _bars_from_vendor_csv(result)
        if not bars:
            first = (result.splitlines() or ["no price data"])[0][:200] \
                if isinstance(result, str) else "no price data"
            return {"error": f"no price data for {ticker}: {first}"}
        payload = {"ticker": ticker.upper(), "bars": bars}
    except Exception as exc:
        return {"error": f"candle fetch failed: {exc}"}
    _CANDLE_CACHE[key] = payload
    return payload


# ---------------------------------------------------------------------------
# Ticker autocomplete — SEC EDGAR ticker map (dynamic, no hardcode)
# ---------------------------------------------------------------------------

_SYMBOLS_TTL = 24 * 60 * 60   # refresh the SEC ticker map once per day
_SYMBOLS_CACHE: dict = {"at": 0.0, "rows": []}


def _load_sec_symbols() -> list[tuple[str, str]]:
    """Fetch the SEC EDGAR company ticker map and return (symbol, name) rows."""
    import time as _time
    import requests

    now = _time.time()
    if now - _SYMBOLS_CACHE["at"] < _SYMBOLS_TTL and _SYMBOLS_CACHE["rows"]:
        return _SYMBOLS_CACHE["rows"]

    r = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": os.getenv("SEC_EDGAR_USER_AGENT") or "TradingAgents (contact@example.com)"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    rows: list[tuple[str, str]] = []
    for entry in data.values():
        sym = entry.get("ticker", "").strip().upper()
        name = entry.get("title", "").strip()
        if sym:
            rows.append((sym, name))

    _SYMBOLS_CACHE.update(at=now, rows=rows)
    return rows


@app.get("/api/symbols")
def symbols(q: str = "", limit: int = 8):
    """Prefix/substring match on symbol or company name, symbol-prefix first.
    Rows are loaded from SEC EDGAR's company_tickers.json (no hardcoded list).
    """
    needle = (q or "").strip().upper()
    if not needle or len(needle) < 1:
        return {"results": []}
    scored = []
    for sym, name in _load_sec_symbols():
        s, n = sym.upper(), name.upper()
        if s.startswith(needle):
            scored.append((0, sym, name))
        elif needle in s:
            scored.append((1, sym, name))
        elif needle in n:
            scored.append((2, sym, name))
    seen, out = set(), []
    for _, sym, name in sorted(scored, key=lambda t: (t[0], t[1])):
        if sym not in seen:
            seen.add(sym)
            out.append({"symbol": sym, "name": name})
        if len(out) >= max(1, min(limit, 12)):
            break
    return {"results": out}
