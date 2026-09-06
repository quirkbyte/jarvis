"""
Config's job is to be boring and correct: the defaults are the ones in BUILD.md,
every field answers to an environment variable, and a typo in that variable
fails loudly at startup rather than quietly halving the silence window.
"""

from __future__ import annotations

import platform
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path

import pytest

from jarvis.config import CONFIG, Config, ConfigError, load_dotenv

SECTIONS = ("audio", "wake", "vad", "stt", "tts", "brain", "hud", "macos")


def build(**env) -> Config:
    env.setdefault("JARVIS_STATE_DIR", str(Path(CONFIG.state_dir)))
    return Config.from_env(env)


def test_every_section_from_the_spec_exists_and_is_a_frozen_dataclass():
    for section in SECTIONS:
        sub = getattr(CONFIG, section)
        assert is_dataclass(sub), section
        with pytest.raises(FrozenInstanceError):
            setattr(sub, fields(sub)[0].name, None)


def test_defaults_match_the_build_brief():
    cfg = build()
    assert cfg.audio.sample_rate == 16000
    assert cfg.audio.block_ms == 80  # openWakeWord wants multiples of 80ms
    assert cfg.audio.block_samples == 1280
    assert cfg.wake.model == "hey_jarvis_v0.1"
    assert cfg.wake.inference_framework == "onnx"  # tflite is Linux-only
    assert cfg.vad.frame_ms in (10, 20, 30)  # webrtcvad accepts nothing else
    assert cfg.vad.silence_ms >= 250  # below this it clips people
    assert cfg.tts.model == "eleven_flash_v2_5"
    assert cfg.tts.sample_rate == 24000
    assert cfg.brain.model == "claude-sonnet-5"
    assert cfg.brain.permission_mode != "bypassPermissions"
    assert cfg.hud.host == "127.0.0.1"  # never 0.0.0.0


def test_env_overrides_reach_every_type():
    cfg = build(
        JARVIS_VAD_SILENCE_MS="450",
        JARVIS_WAKE_THRESHOLD="0.62",
        JARVIS_WAKE_ENABLED="no",
        JARVIS_STT_BACKEND="whisper",
        JARVIS_VERBOSE="1",
        JARVIS_AUDIO_INPUT_DEVICE="MacBook Pro Microphone",
        JARVIS_BRAIN_PERSONA_PATH="~/persona.md",
    )
    assert cfg.vad.silence_ms == 450
    assert cfg.wake.threshold == pytest.approx(0.62)
    assert cfg.wake.enabled is False
    assert cfg.stt.backend == "whisper"
    assert cfg.verbose is True
    assert cfg.audio.input_device == "MacBook Pro Microphone"
    assert cfg.brain.persona_path == Path.home() / "persona.md"


def test_optional_fields_accept_none_by_name_or_emptiness():
    assert build(JARVIS_BRAIN_MAX_BUDGET_USD="2.50").brain.max_budget_usd == 2.5
    assert build(JARVIS_BRAIN_MAX_BUDGET_USD="none").brain.max_budget_usd is None
    assert build(ELEVENLABS_API_KEY="").tts.api_key is None


def test_secrets_come_from_their_vendor_names_and_prefer_the_jarvis_one():
    assert build(ELEVENLABS_API_KEY="sk-eleven").tts.api_key == "sk-eleven"
    assert build(ANTHROPIC_API_KEY="sk-ant").brain.api_key == "sk-ant"
    assert build(ELEVENLABS_VOICE_ID="v_123").tts.voice_id == "v_123"
    both = build(ELEVENLABS_API_KEY="vendor", JARVIS_TTS_API_KEY="explicit")
    assert both.tts.api_key == "explicit"


def test_a_malformed_override_fails_loudly():
    for bad in (
        {"JARVIS_VAD_SILENCE_MS": "soon"},
        {"JARVIS_WAKE_ENABLED": "maybe"},
        {"JARVIS_WAKE_THRESHOLD": "loud"},
    ):
        with pytest.raises(ConfigError):
            build(**bad)


def _probe(f) -> str:
    """A value guaranteed to differ from this field's default."""
    if f.type == "bool":
        return "0" if f.default else "1"
    if f.type.startswith("int"):
        return str((f.default or 0) + 7)
    if f.type.startswith("float"):
        return str((f.default or 0.0) + 7.5)
    return "changed-by-the-environment"


def test_every_field_is_reachable_by_an_env_var():
    """No knob may be tunable only by editing source."""
    for section in SECTIONS:
        sub = getattr(CONFIG, section)
        for f in fields(sub):
            var = f"JARVIS_{section.upper()}_{f.name.upper()}"
            cfg = build(**{var: _probe(f)})
            assert getattr(getattr(cfg, section), f.name) != f.default, var


def test_state_dir_is_outside_the_repo_and_creates_itself(tmp_path):
    target = tmp_path / "nested" / "state"
    cfg = Config.from_env({"JARVIS_STATE_DIR": str(target)})
    assert target.is_dir()
    assert cfg.log_path == target / "jarvis.log"
    assert cfg.memory_path == target / "memory.json"
    assert not str(CONFIG.state_dir).startswith(str(CONFIG.repo_root)), (
        "a git clean must not be able to destroy the memory file"
    )


def test_platform_flags_reflect_this_machine():
    assert CONFIG.is_macos is (platform.system() == "Darwin")
    assert CONFIG.is_apple_silicon is (CONFIG.is_macos and platform.machine() == "arm64")


def test_describe_redacts_secrets():
    described = build(ELEVENLABS_API_KEY="sk-secret", ANTHROPIC_API_KEY="sk-also").describe()
    assert described["tts"]["api_key"] == "<set>"
    assert described["brain"]["api_key"] == "<set>"
    assert "sk-secret" not in str(described)
    assert described["is_macos"] in (True, False)


def test_dotenv_never_overrides_a_real_environment_variable(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ELEVENLABS_API_KEY=from-file\nexport OTHER=fine\n# comment\nbad line\n")
    env = {"ELEVENLABS_API_KEY": "from-shell"}
    assert load_dotenv(env_file, env) == 1
    assert env["ELEVENLABS_API_KEY"] == "from-shell"
    assert env["OTHER"] == "fine"
