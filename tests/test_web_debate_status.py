"""Agent pills in the web UI must follow the debate turn by turn.

The research debate is sequential - Bull, Bear, Bull, Bear - and streaming
emits each step's state only after it ran. So exactly one researcher is live at
a time, the Research Manager stays queued until the last turn, and the replayed
debate state on later steps (trader, risk team) must not reopen anyone.
"""

from __future__ import annotations

import pytest

from cli.display import MessageBuffer
from web.server import _apply_debate_status

BULL, BEAR, MANAGER = "Bull Researcher", "Bear Researcher", "Research Manager"


def _buffer():
    buffer = MessageBuffer()
    buffer.init_for_analysis(["market", "social", "news", "fundamentals"])
    return buffer


def _statuses(buffer):
    return {agent: buffer.agent_status[agent] for agent in (BULL, BEAR, MANAGER)}


def _turn(buffer, bull, bear, *, speaker="", over=False, judge=""):
    _apply_debate_status(buffer, bull, bear, judge, last_speaker=speaker, debate_over=over)


@pytest.mark.unit
def test_each_turn_hands_the_live_pill_to_the_next_speaker():
    buffer = _buffer()
    assert set(_statuses(buffer).values()) == {"pending"}

    _turn(buffer, "bull opening", "", speaker="Bull Analyst: ...")
    assert _statuses(buffer) == {BULL: "completed", BEAR: "in_progress", MANAGER: "pending"}

    _turn(buffer, "bull opening", "bear reply", speaker="Bear Analyst: ...")
    assert _statuses(buffer) == {BULL: "in_progress", BEAR: "completed", MANAGER: "pending"}


@pytest.mark.unit
def test_manager_only_starts_once_the_last_turn_is_spent():
    buffer = _buffer()
    _turn(buffer, "bull opening", "bear reply", speaker="Bull Analyst: ...")
    _turn(buffer, "bull opening", "bear reply", speaker="Bear Analyst: ...", over=True)
    assert _statuses(buffer) == {BULL: "completed", BEAR: "completed", MANAGER: "in_progress"}

    _turn(buffer, "bull opening", "bear reply", judge="the plan", speaker="Bear Analyst: ...", over=True)
    assert set(_statuses(buffer).values()) == {"completed"}


@pytest.mark.unit
def test_replayed_debate_state_does_not_reopen_finished_agents():
    buffer = _buffer()
    _turn(buffer, "bull case", "bear case", judge="the plan", speaker="Bear Analyst: ...", over=True)
    done = _statuses(buffer)

    # Trader, risk team and portfolio manager steps carry the same state.
    for _ in range(3):
        _turn(buffer, "bull case", "bear case", judge="the plan", speaker="Bear Analyst: ...", over=True)
        assert _statuses(buffer) == done
