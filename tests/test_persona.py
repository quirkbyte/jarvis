"""
The persona is the character, so these tests are mostly about register: the
context handed to the model must be in the same voice the model is asked to
reply in, because it writes what it reads. Feed it "Darwin arm64" and it starts
saying things like that back.

The other half is prompt-cache stability. Rebuilding the system prompt with a
fresh timestamp every turn throws away the largest cacheable prefix in the
request, which costs latency on the one path that matters.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jarvis import spoken
from jarvis.config import Config
from jarvis.persona import NO_MEMORY, Persona, memory_block

EVENING = datetime(2026, 8, 29, 21, 20)


def persona(**over: str) -> Persona:
    return Persona(Config.from_env(over))


def test_the_prompt_comes_from_the_file_and_is_not_paraphrased_here():
    text = persona().build(EVENING)
    for phrase in [
        "You are JARVIS",
        "One or two sentences",
        "British English throughout",
        "Before anything irreversible",
        "return to standby",
    ]:
        assert phrase in text, phrase


def test_every_placeholder_is_filled():
    text = persona().build(EVENING)
    assert "{" not in text and "}" not in text, "an unfilled placeholder reached the model"


def test_the_context_is_in_spoken_form_not_machine_form():
    text = persona().build(EVENING)
    assert "twenty past nine in the evening" in context_line(text)
    assert "the twenty-ninth of August" in text
    assert "2026-08-29" not in text and "21:20" not in text
    assert "Darwin" not in text and "arm64" not in text


def test_the_user_and_the_form_of_address_are_configurable():
    text = persona(JARVIS_BRAIN_USER_NAME="Ada", JARVIS_BRAIN_ADDRESS="ma'am").build(EVENING)
    assert "Ada's assistant" in text
    assert "Address them as ma'am" in text


def test_memory_appears_in_the_prompt_or_says_there_is_none():
    p = persona()
    assert NO_MEMORY in p.build(EVENING)
    p.set_memory({"units": "metric", "wake time": "half six"})
    text = p.build(EVENING)
    assert "units: metric" in text and "wake time: half six" in text
    assert NO_MEMORY not in text


def test_memory_block_is_stable_in_order():
    """An unstable prefix is an uncached prefix."""
    facts = {"b": "two", "a": "one"}
    assert memory_block(facts) == memory_block(dict(reversed(list(facts.items()))))


def test_the_prompt_is_built_once_and_reused():
    p = persona()
    first = p.prompt(EVENING)
    assert p.prompt(EVENING + timedelta(minutes=9)) is first, "rebuilt inside the window"
    assert p.rebuilt_at == EVENING


def context_line(prompt: str) -> str:
    """The CONTEXT sentence, not the VOICE section that quotes similar phrases."""
    return next(line for line in prompt.splitlines() if line.startswith("It is "))


def test_it_refreshes_only_when_the_clock_has_really_moved():
    p = persona(JARVIS_BRAIN_PERSONA_REFRESH_S="600")
    p.prompt(EVENING)
    assert "twenty past nine" in context_line(p.prompt(EVENING + timedelta(minutes=5)))
    later = context_line(p.prompt(EVENING + timedelta(minutes=10)))
    assert "twenty past nine" not in later
    assert "half past nine" in later


def test_new_memory_invalidates_the_cached_prompt():
    p = persona()
    p.prompt(EVENING)
    p.set_memory({"units": "metric"})
    assert "units: metric" in p.prompt(EVENING)


def test_setting_the_same_memory_again_does_not_invalidate_it():
    p = persona()
    p.set_memory({"units": "metric"})
    first = p.prompt(EVENING)
    p.set_memory({"units": "metric"})
    assert p.prompt(EVENING) is first


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 8, 29, 21, 20), "twenty past nine in the evening"),
        (datetime(2026, 8, 29, 9, 0), "nine o'clock in the morning"),
        (datetime(2026, 8, 29, 13, 45), "quarter to two in the afternoon"),
        (datetime(2026, 8, 29, 0, 30), "half past twelve in the small hours"),
        (datetime(2026, 8, 29, 23, 55), "five to twelve at night"),
    ],
)
def test_the_clock_reads_as_a_person_would_say_it(when, expected):
    assert spoken.clock(when) == expected


def test_numbers_and_quantities_read_aloud():
    assert spoken.number(93) == "ninety-three"
    assert spoken.number(200) == "two hundred"
    assert spoken.number(142) == "one hundred and forty-two"
    assert spoken.ordinal(29) == "twenty-ninth"
    assert spoken.ordinal(1) == "first"
    assert spoken.ordinal(12) == "twelfth"
    assert spoken.quantity(3.2, "gigabytes") == "three gigabytes"
    assert spoken.duration(5400) == "an hour and a half"
