"""
Turns a stream of text into chunks that are safe to speak, with a much lower
minimum for the first one so that something reaches the speakers early.

A caveat worth knowing before tuning this. The intent was that "Eighty-two
percent." would be playing while the model wrote the sentence after it — but the
Agent SDK does not stream token by token. Measured here, a reply arrives as two
or three large deltas: one character, then most of a sentence, then the rest. So
for a short answer the chunker cannot start the speech any earlier than the
transport allows, and the first-chunk minimum only earns its keep on long
replies, where later sentences genuinely do stream while earlier ones play.

The rest is about not sounding stupid. A cut mid-word or mid-number is worse
than a slow reply, so a boundary is only believed once the following character
confirms it — which means a chunk ending the stream waits for `flush()`.
"""

from __future__ import annotations

from jarvis.config import CONFIG, TtsConfig

STRONG_BOUNDARIES = ".!?…"  # end of a thought
SOFT_BOUNDARIES = ":;"  # a place a reader would breathe
OPENING_BOUNDARY = ","  # first chunk only: better a clipped clause than silence
BOUNDARIES = STRONG_BOUNDARIES + SOFT_BOUNDARIES


class SentenceChunker:
    """Accumulates streamed text and yields chunks that are safe to speak."""

    def __init__(self, config: TtsConfig = CONFIG.tts) -> None:
        self._config = config
        self._buffer = ""
        self._spoken_any = False

    @property
    def pending(self) -> str:
        return self._buffer

    def _min_chars(self) -> int:
        cfg = self._config
        return cfg.chunk_min_chars if self._spoken_any else cfg.first_chunk_min_chars

    def _boundaries(self) -> str:
        """The opening clause may end at a comma; nothing after it may."""
        if self._spoken_any or not self._config.first_chunk_breaks_on_comma:
            return BOUNDARIES
        return BOUNDARIES + OPENING_BOUNDARY

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return whatever became speakable because of it."""
        self._buffer += text
        chunks: list[str] = []
        while (chunk := self._take()) is not None:
            chunks.append(chunk)
        return chunks

    def flush(self) -> list[str]:
        """End of stream: whatever is left is speakable, boundary or not."""
        remainder = self._buffer.strip()
        self._buffer = ""
        if not remainder:
            return []
        self._spoken_any = True
        return [remainder]

    def _take(self) -> str | None:
        cut = self._find_cut()
        if cut is None:
            return None
        chunk = self._buffer[:cut].strip()
        if not chunk:  # leading whitespace only; drop it and look again
            self._buffer = self._buffer[cut:]
            return None
        self._buffer = self._buffer[cut:].lstrip()
        self._spoken_any = True
        return chunk

    def _find_cut(self) -> int | None:
        minimum, maximum = self._min_chars(), self._config.chunk_max_chars
        for i, char in enumerate(self._buffer):
            if char == "\n" and i + 1 >= minimum:
                return i + 1
            if char in self._boundaries() and self._is_boundary(i) and i + 1 >= minimum:
                return i + 1
        if len(self._buffer) >= maximum:
            return self._word_cut(maximum)
        return None

    def _is_boundary(self, i: int) -> bool:
        """True if the punctuation at ``i`` really ends a thought.

        Two things masquerade as sentence ends: the decimal point in "1.5", and
        a full stop that is simply the last character we have been given so far
        — the next token may turn it into "3.14". Both wait.
        """
        following = self._buffer[i + 1 : i + 2]
        if not following:
            return False  # undecidable until more arrives, or flush()
        if self._buffer[i] == "." and self._buffer[i - 1 : i].isdigit() and following.isdigit():
            return False
        return following.isspace() or following in BOUNDARIES + "\"')]"

    def _word_cut(self, limit: int) -> int:
        """No punctuation in sight: cut at the last space before the limit."""
        space = self._buffer.rfind(" ", 0, limit + 1)
        return space + 1 if space > 0 else limit
