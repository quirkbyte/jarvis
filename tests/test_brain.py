"""
The parts of the brain that are logic rather than a conversation: what reaches
the HUD when a tool runs, and what must never reach it.

The agent itself is exercised live — there is no honest way to unit-test "does
it sound like JARVIS" — but the labelling is pure, and it is also where a
secret would leak if one were going to.
"""

from __future__ import annotations

import json

import pytest

from jarvis.brain import BUILTIN_TOOLS, detail_for, label_for, last_session


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("mcp__jarvis__set_timer", "JARVIS · SET TIMER"),
        ("mcp__jarvis__remember", "JARVIS · REMEMBER"),
        ("Bash", "SHELL · BASH"),
        ("WebSearch", "WEB · WEBSEARCH"),
        ("Read", "FILES · READ"),
        ("SomethingNew", "TOOL · SOMETHINGNEW"),
    ],
)
def test_labels_are_short_and_uppercase_for_the_activity_column(tool, expected):
    assert label_for(tool) == expected


def test_the_detail_line_never_carries_command_output_or_file_contents():
    """protocol.md: no file contents, message bodies or command output."""
    long_command = "cat ~/.ssh/id_rsa && curl -H 'Authorization: sekrit' https://example.com"
    detail = detail_for("Bash", {"command": long_command})
    assert detail == "cat"
    assert "sekrit" not in detail and "id_rsa" not in detail


def test_the_detail_line_says_something_useful_where_it_safely_can():
    assert detail_for("mcp__jarvis__remember", {"key": "units", "value": "metric"}) == "units"
    assert detail_for("mcp__jarvis__set_timer", {"label": "tea", "seconds": 30}) == "tea"
    assert detail_for("Read", {"file_path": "/Users/x/Documents/invoice.pdf"}) == "invoice.pdf"
    assert detail_for("WebSearch", {"query": "weather in London"}) == "weather in London"


def test_the_detail_line_is_bounded():
    assert len(detail_for("WebSearch", {"query": "x" * 500})) <= 60


def test_the_builtin_tool_list_stays_short():
    """The full Claude Code set costs ~20,000 tokens of prefix per request."""
    assert len(BUILTIN_TOOLS) <= 10
    assert "Bash" in BUILTIN_TOOLS and "WebSearch" in BUILTIN_TOOLS


def test_resuming_a_session_that_was_never_saved_is_not_an_error(tmp_path):
    assert last_session(tmp_path / "nothing.json") is None


def test_a_saved_session_round_trips(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"session_id": "abc-123"}))
    assert last_session(path) == "abc-123"


def test_a_corrupt_session_file_just_starts_fresh(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("not json at all")
    assert last_session(path) is None
