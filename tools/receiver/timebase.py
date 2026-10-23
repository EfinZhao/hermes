"""Map one device's clock (ms since boot) to wall-clock time (Unix seconds).

Packets carry device time only. A packet can't arrive before it was sent, so
`arrival - device time` is the clock offset plus the network delay, and the smallest value seen
is the best estimate of the offset (the packet that was delayed least). The estimate may rise
only slowly (at most `max_drift_ppm`), so one slow packet can't drag it up, but a device clock
that runs a little slow is still followed. Values come out late by the network's smallest delay.

base_ms is a u32 and wraps after 49.7 days; `unwrap` turns it into a number that keeps counting.
"""
from __future__ import annotations

U32 = 1 << 32


class Timebase:
    def __init__(self, max_drift_ppm: float = 1000.0):
        self.max_drift = max_drift_ppm / 1e6
        self.reset()

    def reset(self) -> None:
        """Forget everything (the device rebooted, so its clock started again)."""
        self.offset_s: float | None = None   # wall time minus device time, seconds
        self._last_raw: int | None = None    # newest raw base_ms seen, and its unwrapped value
        self._last_ms: int | None = None
        self._obs_ms: int | None = None      # device time of the newest observation

    def unwrap(self, raw_ms: int) -> int:
        """Device time that keeps counting past the u32 wrap. Older times map to the past."""
        if self._last_raw is None:
            self._last_raw, self._last_ms = raw_ms, raw_ms
            return raw_ms
        ahead = (raw_ms - self._last_raw) % U32
        if ahead < U32 // 2:                                  # newer than anything seen
            self._last_raw, self._last_ms = raw_ms, self._last_ms + ahead
            return self._last_ms
        return self._last_ms - (U32 - ahead)                  # older: leave the reference alone

    def observe(self, device_ms: int, arrival_s: float) -> None:
        """A packet whose newest record is at `device_ms` (unwrapped) arrived at `arrival_s`."""
        candidate = arrival_s - device_ms / 1000
        if self.offset_s is None:
            self.offset_s, self._obs_ms = candidate, device_ms
        elif device_ms > self._obs_ms:
            allowed = self.offset_s + self.max_drift * (device_ms - self._obs_ms) / 1000
            self.offset_s, self._obs_ms = min(candidate, allowed), device_ms
        else:                                                 # an older packet can only lower it
            self.offset_s = min(self.offset_s, candidate)

    def to_wall(self, device_ms: int) -> float | None:
        """Wall-clock time (Unix seconds) of an unwrapped device time, or None before any packet."""
        return None if self.offset_s is None else self.offset_s + device_ms / 1000
