"""
Every tunable number in JARVIS lives here, in one frozen tree that is built once
at import. The reason it is centralised is not tidiness: it is that the audio
path has a latency budget, and a budget you cannot see is a budget you cannot
defend. When a threshold turns out to be wrong at three in the morning it should
be changeable with an environment variable and a restart, not a code edit — so
every field is overridable as ``JARVIS_<SECTION>_<FIELD>``, and the two secrets
also answer to their conventional vendor names.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis._env import REPO_ROOT, ConfigError, bind, describe, load_dotenv

ENV_PREFIX = "JARVIS_"

VALID_STATES = ("boot", "idle", "listening", "thinking", "speaking", "error")

__all__ = ["CONFIG", "VALID_STATES", "Config", "ConfigError", "load_dotenv"]


def _secret(env: str) -> dict[str, Any]:
    return {"env": env, "secret": True}


@dataclass(frozen=True)
class AudioConfig:
    """One always-open capture, fanned out to every consumer."""

    sample_rate: int = 16000
    channels: int = 1
    block_ms: int = 80  # openWakeWord needs multiples of 80ms at 16kHz
    input_device: str | None = None
    output_device: str | None = None
    output_sample_rate: int = 24000
    output_block_ms: int = 20
    # How much synthesised audio may sit ahead of the speakers. Bigger is
    # safer against network stalls; smaller is less to throw away on barge-in.
    output_buffer_s: float = 0.5
    level_hz: float = 30.0  # protocol.md: level events while listening/speaking
    level_smoothing: float = 0.35  # EWMA alpha on rms, so the HUD gets it smoothed
    level_bands: int = 8
    # Speech sits around -30 dBFS, so a raw 0..1 amplitude renders as a flat
    # line. Map decibels onto the range instead: this is what makes the
    # waveform look alive without the HUD doing signal processing.
    level_floor_db: float = -60.0
    level_ceil_db: float = -12.0
    mic_queue_max: int = 32  # blocks buffered per consumer before we drop audio
    probe_ms: int = 500  # how long `make doctor` listens for a signal
    probe_warmup_ms: int = 150  # a bluetooth mic is silent while HFP negotiates
    probe_min_peak: int = 4  # int16 amplitude below which there is no real signal

    @property
    def block_samples(self) -> int:
        return self.sample_rate * self.block_ms // 1000


@dataclass(frozen=True)
class WakeConfig:
    model: str = "hey_jarvis_v0.1"
    model_dir: Path | None = None
    inference_framework: str = "onnx"  # tflite is Linux-only in practice
    threshold: float = 0.5  # below ~0.4 it triggers on television
    barge_in_threshold: float = 0.9  # stricter while our own speakers are running
    debounce_s: float = 1.0
    enabled: bool = True
    # The detector fires at the end of the phrase, but the recorder's pre-roll
    # reaches back before that, so "Hey JARVIS" lands at the front of the
    # transcript and gets answered as though it were the question. Strip it.
    # Spelled loosely on purpose: speech-to-text hears Jervis, Javis, Jarvis's.
    transcript_prefix: str = r"^\W*(?:hey|hi|ok|okay|a)?\W*j[aeu]r?v[iy]s+(?:'s)?\b\W*"


@dataclass(frozen=True)
class VadConfig:
    # 3, not the 2 the spec suggests. WebRTC's detector adapts to the loud
    # speech it just heard and then calls the room tone afterwards "speech";
    # measured on this machine, 0-2 never declare an endpoint unless the room is
    # below -70 dBFS, and 3 works from -55 down. See tests/test_vad.py.
    aggressiveness: int = 3  # webrtcvad 0 permissive .. 3 aggressive
    # Belt and braces for the same problem: a frame only counts as speech if the
    # detector says so *and* it is loud enough to be. The floor rises with the
    # loudest speech in the utterance, so it works in a quiet room and a noisy one.
    silence_floor_db: float = -50.0
    silence_range_db: float = 35.0
    frame_ms: int = 20  # webrtcvad accepts 10/20/30 only
    silence_ms: int = 300  # the endpoint window; below ~250 it clips people
    min_speech_ms: int = 250  # less actual speech than this was a cough
    pre_roll_ms: int = 300  # audio kept from before speech is detected
    # ...but only this much straight after a wake word, because the rest of that
    # window still contains "JARVIS". Measured against run-on phrases: at 320ms
    # the recogniser hears "Discount slowly from one to twenty", at 0ms it clips
    # the first word, and 80-160ms is clean. See scripts/bench.py --pre-roll.
    wake_pre_roll_ms: int = 160
    max_utterance_s: float = 20.0
    start_timeout_s: float = 6.0  # silence after the wake word before giving up
    follow_up_s: float = 8.0  # keep listening this long after a reply, no wake word


@dataclass(frozen=True)
class SttConfig:
    backend: str = "parakeet"  # parakeet | whisper
    model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    whisper_model: str = "small.en"
    whisper_compute_type: str = "int8"
    language: str = "en"
    beam_size: int = 1
    warmup: bool = True  # first inference is slow; pay for it at boot
    budget_ms: int = 400  # BUILD.md: past this the model is the wrong one


@dataclass(frozen=True)
class TtsConfig:
    backend: str = "elevenlabs"  # elevenlabs | say | mute
    # `mute` runs the whole path — chunking, timing, level events, barge-in —
    # and produces no sound. It is how the loop gets exercised without
    # talking to whoever is in the room.
    api_key: str | None = field(default=None, metadata=_secret("ELEVENLABS_API_KEY"))
    voice_id: str | None = field(default=None, metadata={"env": "ELEVENLABS_VOICE_ID"})
    voice_name: str | None = field(default=None, metadata={"env": "ELEVENLABS_VOICE_NAME"})
    model: str = "eleven_flash_v2_5"
    output_format: str = "pcm_24000"
    sample_rate: int = 24000
    request_timeout_s: float = 20.0
    fallback_voice: str = "Daniel"  # say -v Daniel, British RP, offline
    fallback_rate_wpm: int = 175
    # The first chunk may be much shorter than the rest: getting *something* out
    # of the speakers is what removes the dead air. Later chunks wait for more
    # text so the prosody does not come out chopped.
    first_chunk_min_chars: int = 12
    chunk_min_chars: int = 60
    chunk_max_chars: int = 220  # hard cut, at a word boundary, with no punctuation
    # The first chunk may also break at a comma, to start speaking sooner.
    # Off by default because it was measured and it does not work: the SDK
    # delivers text in two or three large deltas rather than token by token, so
    # the comma and the full stop usually arrive together and there is nothing
    # to gain. What it did produce was "It's two minutes to eleven," followed by
    # "sir." as a separate utterance. Left here because a future transport that
    # streams properly would make it worth having again.
    first_chunk_breaks_on_comma: bool = False
    budget_ms: int = 150
    # Time from the model's first token to a chunk worth speaking. Invisible
    # until measured, and on a short reply it is the largest stage after the
    # model itself, because a sentence has to exist before it can be said.
    chunk_budget_ms: int = 100


@dataclass(frozen=True)
class BrainConfig:
    model: str = "claude-sonnet-5"
    heavy_model: str = "claude-opus-5"
    api_key: str | None = field(default=None, metadata=_secret("ANTHROPIC_API_KEY"))
    permission_mode: str = "acceptEdits"  # never bypassPermissions; guard.py decides
    persona_path: Path = REPO_ROOT / "reference" / "persona.md"
    user_name: str = "Eliran"
    address: str = "sir"
    # The system prompt is the largest cacheable prefix in every request, so it
    # is built once and only rebuilt when the clock in it would be wrong.
    persona_refresh_s: float = 600.0
    memory_max_facts: int = 60  # past this the prompt prefix starts to cost real latency
    timer_max_s: float = 6 * 3600  # longer than this wants a reminder, not a timer
    timer_announcement: str = "{label} is up."
    # How long a spoken "yes" stays good for. Long enough to hear the question
    # and answer it; short enough that it cannot be reused later by accident.
    confirm_window_s: float = 60.0
    approval_pattern: str = (
        r"\b(yes|yeah|yep|yup|go ahead|do it|send it|confirm|please do|ok|okay|fine)\b"
    )
    refusal_pattern: str = r"\b(no|nope|don'?t|do not|stop|cancel|forget it|leave it|never mind)\b"
    memory_file: str = "memory.json"
    session_file: str = "session.json"
    max_turns: int | None = None
    max_budget_usd: float | None = None
    history_turns: int = 20
    ttft_budget_ms: int = 400
    max_reply_sentences: int = 3  # the persona is brief; this is the hard stop


@dataclass(frozen=True)
class HudConfig:
    enabled: bool = True
    host: str = "127.0.0.1"  # never 0.0.0.0 — this carries the conversation
    port: int = 8765
    ws_path: str = "/ws"
    protocol_version: int = 1
    client_queue_max: int = 200  # past this, drop level/telemetry for that client
    telemetry_hz: float = 1.0
    reconnect_ms: int = 1000
    open_browser: bool = False  # JARVIS_HUD_OPEN_BROWSER=1 to launch it on boot


@dataclass(frozen=True)
class MacosConfig:
    osascript_timeout_s: float = 15.0  # AppleScript against a hung app waits forever
    shell_timeout_s: float = 20.0
    confirm_before_send: bool = True  # messages, mail, anything irreversible
    volume_step: int = 10  # what "turn it down a bit" means
    brightness_step: float = 0.1
    file_search_days: int = 14
    file_search_limit: int = 20


@dataclass(frozen=True)
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    hud: HudConfig = field(default_factory=HudConfig)
    macos: MacosConfig = field(default_factory=MacosConfig)

    # ~/.jarvis and not the repo, so a git clean cannot destroy the memory file.
    state_dir: Path = Path("~/.jarvis")
    log_file: str = "jarvis.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backups: int = 3
    event_queue_max: int = 256
    verbose: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser())
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_macos(self) -> bool:
        return platform.system() == "Darwin"

    @property
    def is_apple_silicon(self) -> bool:
        return self.is_macos and platform.machine() == "arm64"

    @property
    def repo_root(self) -> Path:
        return REPO_ROOT

    @property
    def log_path(self) -> Path:
        return self.state_dir / self.log_file

    @property
    def memory_path(self) -> Path:
        return self.state_dir / self.brain.memory_file

    @property
    def session_path(self) -> Path:
        return self.state_dir / self.brain.session_file

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        return bind(cls, ENV_PREFIX, os.environ if env is None else env)

    def describe(self) -> dict[str, Any]:
        """The resolved tree, with secrets reduced to whether they are set."""
        return describe(self) | {
            "is_macos": self.is_macos,
            "is_apple_silicon": self.is_apple_silicon,
            "log_path": str(self.log_path),
        }


load_dotenv()
CONFIG = Config.from_env()
