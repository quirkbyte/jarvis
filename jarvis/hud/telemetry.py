"""
The numbers behind the left-hand panel, published once a second.

Everything here is a ratio between zero and one, because the HUD should not be
doing arithmetic — it has sixty frames a second to draw and no business working
out what percentage of a disk is full. The one exception is the network, which
is bytes per second, because a ratio of what would be meaningless.

Nothing in this module is allowed to matter. It runs on a timer, it publishes to
the bus, and if it throws, the voice loop must not notice — so it catches
everything and keeps its own counter of how often it has failed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import psutil

from jarvis.config import CONFIG, Config
from jarvis.events import EventBus

log = logging.getLogger("jarvis.telemetry")


@dataclass
class Sample:
    cpu: float
    mem: float
    disk: float
    battery: dict[str, float | bool] | None
    net: dict[str, float | str | None]
    uptime_s: float


class Telemetry:
    """Samples the machine on a timer and publishes to the bus."""

    def __init__(self, bus: EventBus, config: Config = CONFIG) -> None:
        self._bus = bus
        self._config = config
        self._task: asyncio.Task[None] | None = None
        self._last_net: tuple[float, int, int] | None = None
        self.failures = 0
        self.latest: Sample | None = None
        psutil.cpu_percent(interval=None)  # prime the counter; the first read is 0

    @property
    def interval(self) -> float:
        return 1.0 / max(self._config.hud.telemetry_hz, 0.01)

    def sample(self) -> Sample:
        boot = psutil.boot_time()
        sample = Sample(
            cpu=round(psutil.cpu_percent(interval=None) / 100, 4),
            mem=round(psutil.virtual_memory().percent / 100, 4),
            disk=round(psutil.disk_usage("/").percent / 100, 4),
            battery=self._battery(),
            net=self._network(),
            uptime_s=round(time.time() - boot, 1),
        )
        self.latest = sample
        return sample

    def _battery(self) -> dict[str, float | bool] | None:
        try:
            power = psutil.sensors_battery()
        except Exception:  # noqa: BLE001 - absent on plenty of Macs, including minis
            return None
        if power is None:
            return None
        return {"pct": round(power.percent / 100, 4), "charging": bool(power.power_plugged)}

    def _network(self) -> dict[str, float | str | None]:
        counters = psutil.net_io_counters()
        now = time.monotonic()
        up = down = 0.0
        if self._last_net is not None:
            then, sent, received = self._last_net
            elapsed = max(now - then, 1e-6)
            up = max(0.0, (counters.bytes_sent - sent) / elapsed)
            down = max(0.0, (counters.bytes_recv - received) / elapsed)
        self._last_net = (now, counters.bytes_sent, counters.bytes_recv)
        return {"up": round(up), "down": round(down), "ssid": None}

    def publish_once(self) -> None:
        sample = self.sample()
        self._bus.publish_telemetry(
            cpu=sample.cpu,
            mem=sample.mem,
            disk=sample.disk,
            battery=sample.battery,
            net=sample.net,
            uptime_s=sample.uptime_s,
        )

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.publish_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a panel is never worth a voice turn
                self.failures += 1
                if self.failures in (1, 10, 100):
                    log.warning("telemetry sample failed (%d so far)", self.failures)
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="jarvis-telemetry")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
