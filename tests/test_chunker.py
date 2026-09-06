"""
The chunker decides how quickly JARVIS starts talking, so its tests are mostly
about the first chunk being allowed out early — and about the two ways a naive
splitter embarrasses itself: cutting "3.14" in half, and cutting a number that
was split across two tokens.
"""

from __future__ import annotations

from jarvis.audio.chunker import SentenceChunker
from jarvis.config import Config


def chunker(**over: str) -> SentenceChunker:
    return SentenceChunker(Config.from_env(over).tts)


def feed_all(text: str, size: int = 0) -> list[str]:
    """Stream ``text`` in fixed-size pieces (0 = all at once), then flush."""
    c = chunker()
    pieces = [text] if size == 0 else [text[i : i + size] for i in range(0, len(text), size)]
    out: list[str] = []
    for piece in pieces:
        out += c.feed(piece)
    return out + c.flush()


def test_the_first_chunk_leaves_early_and_the_next_one_waits():
    c = chunker()
    assert c.feed("Eighty-two percent. ") == ["Eighty-two percent."]  # 19 chars, over 12
    # The second chunk needs 60, so a short follow-up sentence is held back...
    assert c.feed("And falling. ") == []
    assert c.feed("You have about three hours left before it wants a cable. ") == [
        "And falling. You have about three hours left before it wants a cable."
    ]


def test_a_very_short_opening_sentence_is_held_until_it_is_worth_speaking():
    c = chunker()
    assert c.feed("Yes. ") == []  # 4 chars: not worth a TTS round trip on its own
    assert c.feed("Spotify is playing. ") == ["Yes. Spotify is playing."]


def test_it_never_cuts_a_decimal_in_half():
    assert feed_all("The load average is 3.14 and the disk is 44% full. ") == [
        "The load average is 3.14 and the disk is 44% full."
    ]


def test_a_number_split_across_tokens_is_not_a_sentence_end():
    """The awkward case from the spec: "...nine" then " forty."."""
    c = chunker()
    assert c.feed("It finished in nine") == []
    assert c.feed(".") == []  # undecidable: could be "9.4"
    assert c.feed("4 seconds flat, which is quicker than usual. ") == [
        "It finished in nine.4 seconds flat, which is quicker than usual."
    ]


def test_the_same_text_chunks_the_same_way_however_it_arrives():
    text = "Eighty-two percent. Not charging. It should last the afternoon at this rate. "
    whole = feed_all(text)
    assert feed_all(text, size=1) == whole
    assert feed_all(text, size=7) == whole
    assert whole[0] == "Eighty-two percent."


def test_nothing_is_lost_between_feed_and_flush():
    text = "Good evening. The build finished about four minutes ago; two of the tests failed."
    joined = " ".join(feed_all(text, size=3))
    assert joined.replace("  ", " ") == text.strip()


def test_a_long_stream_with_no_punctuation_cuts_at_a_word_boundary():
    words = " ".join(["word"] * 200)
    chunks = feed_all(words)
    assert len(chunks) > 1
    assert all(not c.startswith(" ") and not c.endswith(" ") for c in chunks)
    assert all(set(c.split()) == {"word"} for c in chunks), "a word was cut in half"
    assert " ".join(chunks) == words.strip()


def test_soft_boundaries_and_newlines_are_cut_points():
    c = chunker()
    assert c.feed("Two of the tests failed; ") == ["Two of the tests failed;"]
    c2 = chunker()
    assert c2.feed("The first line\nthe second line ") == ["The first line"]


def test_the_minimum_applies_to_newlines_too():
    """A newline is a cut point, not a licence to speak two words."""
    c = chunker()
    assert c.feed("Line\n") == []  # 4 chars
    assert c.feed("and the rest of it. ") == ["Line\nand the rest of it."]


def test_flush_on_an_empty_stream_says_nothing():
    c = chunker()
    assert c.feed("") == []
    assert c.flush() == []


def test_trailing_punctuation_is_kept_with_its_sentence():
    c = chunker()
    assert c.feed('He said "no". ') == ['He said "no".']


# The comma break is off by default — the transport delivers text in two or
# three large deltas, so there is nothing to gain by cutting earlier. These
# cover the option for whoever turns it on.
COMMA_ON = {"JARVIS_TTS_FIRST_CHUNK_BREAKS_ON_COMMA": "1"}


def test_by_default_a_comma_is_not_a_cut_point():
    c = chunker()
    reply = "I can run commands and manage files on this Mac, search the web, and set timers. "
    assert c.feed(reply) == [reply.strip()]


def test_with_the_option_on_the_opening_clause_may_end_at_a_comma():
    c = chunker(**COMMA_ON)
    reply = "I can run commands and manage files on this Mac, search the web, and set timers."
    assert c.feed(reply) == ["I can run commands and manage files on this Mac,"]


def test_with_the_option_on_only_the_opening_clause_does_that():
    c = chunker(**COMMA_ON)
    assert c.feed("Eighty-two percent, and not charging. ") == ["Eighty-two percent,"]
    # Something has been spoken now, so a comma is no longer a cut point.
    assert c.feed("It should last the afternoon, at this rate, unless you play video. ") == [
        "and not charging. It should last the afternoon, at this rate, unless you play video."
    ]


def test_a_comma_inside_a_number_is_never_a_cut_point():
    c = chunker(**COMMA_ON)
    assert c.feed("There are 1,500 files in there and about nine gigabytes. ") == [
        "There are 1,500 files in there and about nine gigabytes."
    ]
