"""
The entry point's one piece of real logic: refusing to start without a way to
reach Claude. Everything else in jarvis/__main__.py is argument-parsing glue,
covered well enough by using it; this gate is worth pinning down because its
whole point is to fail loudly instead of the console's normal quiet.
"""

from __future__ import annotations

import scripts.doctor
from jarvis.__main__ import main
from scripts._report import Check


def test_refuses_to_start_without_anthropic_auth(monkeypatch, capsys):
    monkeypatch.setattr(
        scripts.doctor,
        "check_credentials",
        lambda: [Check("credentials", "anthropic auth", "fail", "no key, no login", fix="log in or set a key")],
    )

    exit_code = main(["--text"])

    assert exit_code == 1
    assert "can't reach Claude" in capsys.readouterr().err


def test_proceeds_past_the_gate_with_anthropic_auth(monkeypatch):
    monkeypatch.setattr(
        scripts.doctor,
        "check_credentials",
        lambda: [Check("credentials", "anthropic auth", "ok", "ANTHROPIC_API_KEY is set")],
    )
    started = {}

    async def fake_run_text(config, resume=None):
        started["ran"] = True
        return 0

    monkeypatch.setattr("jarvis.session.run_text", fake_run_text)

    exit_code = main(["--text"])

    assert exit_code == 0
    assert started.get("ran") is True
