"""
The turn line is the only thing the terminal shows during a normal conversation,
so its format is a contract: BUILD.md's stages, in order, in milliseconds. If it
drifts, the latency budget stops being checkable at a glance.
"""

from __future__ import annotations

import logging
import re

from jarvis.config import Config
from jarvis.logging import TurnTimer, over_budget, setup_logging

LINE = re.compile(
    r"^turn  wake=\d+ms  vad=\d+ms  stt=\d+ms  llm_ttft=\d+ms  tts_ttfb=\d+ms  "
    r'total=\d+ms  "what\'s my battery at"$'
)


def timer_with_a_full_turn() -> TurnTimer:
    timer = TurnTimer()
    for stage, ms in (
        ("wake", 142),
        ("vad", 310),
        ("stt", 138),
        ("llm_ttft", 402),
        ("tts_ttfb", 131),
        ("total", 1123),
    ):
        timer.mark(stage, ms)
    return timer


def test_turn_line_matches_the_format_in_the_brief():
    line = timer_with_a_full_turn().format("what's my battery at")
    assert LINE.match(line), line
    assert "wake=142ms" in line and "total=1123ms" in line


def test_stages_print_in_pipeline_order_however_they_were_recorded():
    timer = TurnTimer()
    timer.mark("tts_ttfb", 131)
    timer.mark("wake", 142)
    timer.mark("stt", 138)
    stages = re.findall(r"(\w+)=", timer.format())
    assert stages == ["wake", "stt", "tts_ttfb", "total"]


def test_total_is_filled_in_from_the_clock_when_nobody_marked_it():
    timer = TurnTimer()
    with timer.stage("stt"):
        pass
    assert "stt" in timer.stages
    assert timer.total_ms > 0
    assert "total=" in timer.format()


def test_over_budget_names_the_stage_that_blew_it():
    cfg = Config.from_env({})
    timer = TurnTimer()
    timer.mark("stt", 900)
    timer.mark("llm_ttft", 100)
    assert over_budget(timer, cfg) == [f"stt=900ms>{cfg.stt.budget_ms}ms"]
    assert over_budget(timer_with_a_full_turn(), cfg) == ["llm_ttft=402ms>400ms"]


def test_setup_logging_writes_detail_to_the_state_dir(tmp_path):
    cfg = Config.from_env({"JARVIS_STATE_DIR": str(tmp_path), "JARVIS_VERBOSE": "1"})
    root = setup_logging(cfg, force=True)
    TurnTimer(logging.getLogger("jarvis.turn")).log("hello")
    for handler in root.handlers:
        handler.flush()
    assert cfg.log_path.exists()
    assert "hello" in cfg.log_path.read_text()
    assert any(h.level == logging.DEBUG for h in root.handlers)
