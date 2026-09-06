"""
The candidate-filtering logic in `scripts/voices.py`, which is worth pinning
because the bug it replaced was invisible until a real account with a real
cloned voice hit it: filtering on `accent` alone silently hid any voice that
did not carry that label, which is every voice a person clones themselves.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.voices import _is_relevant, candidates, describe


def voice(name, voice_id="v_1", category="premade", **labels):
    return SimpleNamespace(name=name, voice_id=voice_id, category=category, labels=labels)


class FakeSearch:
    def __init__(self, voices: list) -> None:
        self._voices = voices

    def search(self, **_kwargs):
        return SimpleNamespace(voices=self._voices)


class FakeApi:
    def __init__(self, voices: list) -> None:
        self.voices = FakeSearch(voices)


GEORGE = voice("George", "v_george", accent="british", gender="male", language="en")
ADAM = voice("Adam", "v_adam", accent="american", gender="male", language="en")
CLONE_EN = voice("Eliran Voice", "v_clone_en", category="cloned", language="en")
CLONE_HE = voice("Eliran Voice HE", "v_clone_he", category="cloned", language="he")
CLONE_NO_LANGUAGE = voice("Untagged Clone", "v_clone_bare", category="cloned")


def test_a_cloned_voice_with_no_accent_label_is_not_hidden():
    """The exact bug: a clone carries no `accent` label at all."""
    assert _is_relevant(CLONE_EN)


def test_a_clone_in_another_language_is_excluded():
    assert not _is_relevant(CLONE_HE)


def test_a_clone_with_no_language_label_defaults_to_included():
    """Most premade voices default to English; a bare clone should too."""
    assert _is_relevant(CLONE_NO_LANGUAGE)


def test_a_premade_non_british_voice_is_excluded():
    assert not _is_relevant(ADAM)


def test_a_premade_british_voice_is_included():
    assert _is_relevant(GEORGE)


def test_candidates_never_needs_the_accent_search_to_come_back_empty():
    """The old bug's other half: the fallback only ran when the filtered
    search was empty, which never happens once any British voice exists."""
    api = FakeApi([GEORGE, ADAM, CLONE_EN, CLONE_HE])
    found = {v.name for v in candidates(api)}
    assert found == {"George", "Eliran Voice"}


def test_a_personal_clone_sorts_ahead_of_any_premade_voice():
    api = FakeApi([GEORGE, CLONE_EN])
    ordered = candidates(api)
    assert [v.name for v in ordered] == ["Eliran Voice", "George"]


def test_describe_flags_a_clone_and_never_shows_empty_brackets():
    assert describe(CLONE_EN) == "Eliran Voice  [cloned]"


def test_describe_shows_no_brackets_at_all_with_nothing_to_say():
    bare = voice("Mystery Voice", category="cloned")
    bare.category = ""  # no category, no labels: nothing to report
    assert describe(bare) == "Mystery Voice"


def test_describe_still_reads_normally_for_a_premade_voice():
    """category="premade" is not itself worth printing; the labels are."""
    assert describe(GEORGE) == "George  [male]"
