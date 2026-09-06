"""
The audio path: microphone in, wake word, endpointing, transcription, speech
out. Everything here is blocking by nature — PortAudio, ONNX, MLX and HTTP
streaming all are — so the rule is that these modules stay synchronous and
honest about it, and `main.py` is the only thing that knows about asyncio.
"""

from __future__ import annotations
