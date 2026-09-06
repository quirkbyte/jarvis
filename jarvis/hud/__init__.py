"""
The HUD: a view of the event bus, and nothing else.

Nothing in this package may be imported by the core. The voice loop must run
identically whether the server is up, the browser is open, the browser is slow,
or the browser was killed mid-sentence — so the dependency arrow points one way
only, and the only thing crossing it is a subscription.
"""

from __future__ import annotations
