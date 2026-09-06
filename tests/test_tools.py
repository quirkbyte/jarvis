"""
Memory and timers. The memory tests are mostly about the file: it is the only
thing in JARVIS that is meant to outlive the process, so losing it to a partial
write or a corrupt read is the failure that actually costs the user something.

The timer tests are about announcing, which is the whole reason timers exist
here rather than in a shell script.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jarvis.config import Config
from jarvis.tools import build_server, qualified
from jarvis.tools.memory import MemoryStore
from jarvis.tools.timers import TimerService


def store_in(tmp_path, **over) -> MemoryStore:
    return MemoryStore(Config.from_env({"JARVIS_STATE_DIR": str(tmp_path), **over}))


# -- memory ------------------------------------------------------------------


def test_a_fact_survives_a_new_store_over_the_same_file(tmp_path):
    """The restart test, without the restart."""
    store_in(tmp_path).remember("units", "metric")
    assert store_in(tmp_path).all() == {"units": "metric"}


def test_keys_are_normalised_so_the_same_fact_is_not_stored_twice(tmp_path):
    store = store_in(tmp_path)
    store.remember("Units", "imperial")
    store.remember("  units  ", "metric")
    assert store.all() == {"units": "metric"}


def test_updating_a_fact_says_what_it_replaced(tmp_path):
    store = store_in(tmp_path)
    assert "Noted" in store.remember("units", "imperial")
    assert "was imperial" in store.remember("units", "metric")


def test_forgetting_something_that_was_never_there_is_not_an_error(tmp_path):
    assert "Nothing stored" in store_in(tmp_path).forget("nonsense")


def test_forget_removes_it_from_the_file_too(tmp_path):
    store = store_in(tmp_path)
    store.remember("units", "metric")
    store.forget("units")
    assert store_in(tmp_path).all() == {}


def test_empty_keys_and_values_are_refused(tmp_path):
    store = store_in(tmp_path)
    store.remember("", "metric")
    store.remember("units", "   ")
    assert store.all() == {}


def test_the_store_is_capped_so_the_system_prompt_cannot_bloat(tmp_path):
    store = store_in(tmp_path, JARVIS_BRAIN_MEMORY_MAX_FACTS="3")
    for i in range(3):
        store.remember(f"fact {i}", "yes")
    assert "full" in store.remember("one too many", "yes")
    assert len(store.all()) == 3
    assert "Updated" in store.remember("fact 1", "still yes"), "a cap must not block updates"


def test_a_corrupt_file_does_not_stop_it_booting_and_is_kept_for_forensics(tmp_path):
    (tmp_path / "memory.json").write_text("{ this is not json")
    store = store_in(tmp_path)
    assert store.all() == {}
    assert (tmp_path / "memory.corrupt.json").exists(), "the old file was destroyed"
    store.remember("units", "metric")
    assert store_in(tmp_path).all() == {"units": "metric"}


def test_the_file_on_disk_is_readable_json(tmp_path):
    store = store_in(tmp_path)
    store.remember("units", "metric")
    assert json.loads(store.path.read_text()) == {"units": "metric"}


def test_no_temporary_files_are_left_behind(tmp_path):
    store = store_in(tmp_path)
    for i in range(5):
        store.remember(f"fact {i}", "yes")
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


# -- timers ------------------------------------------------------------------


def timers(spoken: list[str], **over) -> TimerService:
    async def announce(phrase: str) -> None:
        spoken.append(phrase)

    return TimerService(announce, Config.from_env(over))


def test_a_timer_announces_itself_when_it_fires():
    said: list[str] = []

    async def go():
        service = timers(said)
        assert "for tea" in service.set(0.05, "tea")
        await asyncio.sleep(0.3)

    asyncio.run(go())
    assert said == ["tea is up."]


def test_a_cancelled_timer_stays_quiet():
    said: list[str] = []

    async def go():
        service = timers(said)
        service.set(0.05, "tea")
        assert "Cancelled" in service.cancel("tea")
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert said == []


def test_setting_the_same_label_replaces_rather_than_doubles():
    said: list[str] = []

    async def go():
        service = timers(said)
        service.set(0.05, "tea")
        service.set(0.1, "tea")
        assert len(service.timers) == 1
        await asyncio.sleep(0.35)

    asyncio.run(go())
    assert said == ["tea is up."], "the replaced timer fired as well"


def test_listing_timers_reads_as_speech():
    async def go():
        service = timers([])
        assert "No timers" in service.describe()
        service.set(600, "laundry")
        described = service.describe()
        service.cancel_all()
        return described

    described = asyncio.run(go())
    assert "laundry" in described and "minutes left" in described
    assert "600" not in described


@pytest.mark.parametrize("seconds", [0, -5])
def test_a_timer_with_no_length_is_refused(seconds):
    async def go():
        return timers([]).set(seconds, "nonsense")

    assert "not a length of time" in asyncio.run(go())


def test_an_absurdly_long_timer_is_pushed_towards_reminders():
    async def go():
        return timers([], JARVIS_BRAIN_TIMER_MAX_S="60").set(3600, "next year")

    assert "reminder" in asyncio.run(go())


def test_cancelling_something_that_is_not_running():
    async def go():
        return timers([]).cancel("ghost")

    assert "No timer" in asyncio.run(go())


# -- the server the model actually sees --------------------------------------


def test_the_tools_are_exposed_under_the_jarvis_prefix(tmp_path):
    async def go():
        _, names = build_server(store_in(tmp_path), timers([]))
        return names

    names = asyncio.run(go())
    assert qualified("remember") in names
    assert set(names) == {
        qualified(n)
        for n in ("remember", "recall", "forget", "set_timer", "list_timers", "cancel_timer")
    }
