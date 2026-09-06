"""
JARVIS: a voice assistant with a cinematic HUD, running on one Mac.

The package is deliberately split in two. Everything under here that touches
audio, the agent, or the machine is *the core* and must work headless; the HUD
is a view fed by :mod:`jarvis.events` and can be killed without the voice loop
noticing. Import order follows that: nothing in the core may import the HUD.
"""

from __future__ import annotations

__version__ = "0.1.0"
__phase__ = 5  # the HUD; see specs/ for what lands next

from jarvis.config import CONFIG, Config
from jarvis.events import EventBus, Subscription, get_bus

__all__ = ["CONFIG", "Config", "EventBus", "Subscription", "__version__", "get_bus"]
