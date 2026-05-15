from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..datafeed.models import Tick


def velocity_over_window(ticks: Sequence[Tick], *, window_seconds: float) -> float | None:
    """Return dollars/second using a tick at least `window_seconds` old.

    No short-window fallback: if we do not have enough history for the named
    window, return None so risk/strategy code can stay conservative.
    """

    if len(ticks) < 2:
        return None
    current = ticks[-1]
    candidates = [tick for tick in ticks[:-1] if (current.ts - tick.ts).total_seconds() >= window_seconds]
    if not candidates:
        return None
    previous = candidates[-1]
    seconds = (current.ts - previous.ts).total_seconds()
    if seconds <= 0:
        return None
    return (current.price - previous.price) / seconds


@dataclass
class SlopeTracker:
    window_seconds: float = 30.0
    retention_seconds: float = 180.0
    _ticks: deque[Tick] = field(default_factory=deque)

    def add(self, tick: Tick) -> None:
        self._ticks.append(tick)
        while self._ticks and (tick.ts - self._ticks[0].ts).total_seconds() > self.retention_seconds:
            self._ticks.popleft()

    def velocity(self) -> float | None:
        return velocity_over_window(list(self._ticks), window_seconds=self.window_seconds)
