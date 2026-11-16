"""Per-user Gmail query-cost budget.

Each API call has a query cost from the Gmail usage table. This budget
allows 6,000 of those units in any 60 seconds, which is the per-user
Total Query Cost limit. There is no separate requests-per-second cap.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

_WINDOW = tuple[float, int, int]
_MINUTE = 60.0


class SyncStopped(Exception):
    """A quota wait was abandoned because the mailbox is closing."""


class Quota:
    def __init__(
        self,
        per_minute: float = 6000.0,
        sleeper: Callable[[float], None] | None = None,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("quota per minute must be positive")
        self.per_minute = float(per_minute)
        self.units = 0
        self.waits: list[float] = []
        self._now = now if now is not None else time.monotonic
        self._window: list[_WINDOW] = []
        self._reserved: list[_WINDOW] = []
        self._seq = 0
        self._next_share = 0.0
        self._lock = threading.Lock()
        self._sleeper = sleeper if sleeper is not None else time.sleep
        # Quit sets this so a minute-long budget sleep ends immediately.
        self._cancel = threading.Event()

    def abort(self) -> None:
        """Stop waiting. The next budget sleep raises ``SyncStopped``."""
        self._cancel.set()

    def stopped(self) -> bool:
        return self._cancel.is_set()

    def acquire(self, cost: int) -> None:
        if cost < 0:
            raise ValueError("quota cost must be non-negative")
        if cost > self.per_minute:
            raise ValueError("single call exceeds the minute budget")
        while True:
            with self._lock:
                self._trim()
                if self._used() + cost <= self.per_minute:
                    self.units += cost
                    self._window.append(self._entry(cost))
                    return
                wait = self._wait_for()
                self.waits.append(wait)
                reserved = self._entry(cost)
                self._reserved.append(reserved)
            started = self._now()
            try:
                self._sleep(wait)
            except SyncStopped:
                with self._lock:
                    self._reserved = [entry for entry in self._reserved if entry is not reserved]
                raise
            with self._lock:
                self._reserved = [entry for entry in self._reserved if entry is not reserved]
                if self._now() - started < wait * 0.5:
                    self._window.clear()
                    self.units += cost
                    self._window.append(self._entry(cost))
                    return

    def available(self, unit_cost: int, limit: int) -> int:
        """How many calls of ``unit_cost`` fit in this minute, waiting until one does."""
        if unit_cost <= 0 or limit <= 0:
            raise ValueError("batch size must be positive")
        if unit_cost > self.per_minute:
            raise ValueError("single call exceeds the minute budget")
        while True:
            with self._lock:
                self._trim()
                count = min(limit, int((self.per_minute - self._used()) // unit_cost))
                if count >= 1:
                    return count
                wait = self._wait_for()
                self.waits.append(wait)
                reserved = self._entry(unit_cost)
                self._reserved.append(reserved)
            started = self._now()
            try:
                self._sleep(wait)
            except SyncStopped:
                with self._lock:
                    self._reserved = [entry for entry in self._reserved if entry is not reserved]
                raise
            with self._lock:
                self._reserved = [entry for entry in self._reserved if entry is not reserved]
                if self._now() - started < wait * 0.5:
                    return 1

    def spread(self, cost: int) -> None:
        """Wait out this call's share of the minute.

        The minute budget is unchanged. A fast connection would otherwise
        spend it in a few seconds and pile that work into one CPU burst.
        """
        if cost <= 0:
            return
        slot = _MINUTE * cost / self.per_minute
        now = self._now()
        remain = self._next_share - now
        self._next_share = (now if remain <= 0 else self._next_share) + slot
        if remain <= 0:
            return
        with self._lock:
            self.waits.append(remain)
        self._sleep(remain)

    def backoff(self, attempt: int) -> None:
        wait = 0.1 * (2 ** max(attempt, 1))
        with self._lock:
            self.waits.append(wait)
        self._sleep(wait)

    def hold(self, seconds: float | None = None) -> None:
        """The server already rejected this minute. Wait for that window to pass."""
        seconds = _MINUTE if seconds is None else seconds
        with self._lock:
            self._window = [self._entry(int(self.per_minute))]
            self._reserved.clear()
            self.waits.append(seconds)
        started = self._now()
        self._sleep(seconds)
        with self._lock:
            if self._now() - started < seconds * 0.5:
                self._window.clear()

    def _sleep(self, seconds: float) -> None:
        if self._cancel.is_set():
            raise SyncStopped()
        if seconds <= 0:
            return
        # The default sleeper can block for the rest of a Gmail minute.
        # Waiting on the event lets quit wake that sleep.
        if self._sleeper is time.sleep:
            if self._cancel.wait(timeout=seconds):
                raise SyncStopped()
            return
        self._sleeper(seconds)
        if self._cancel.is_set():
            raise SyncStopped()

    def _wait_for(self) -> float:
        if self._window:
            return max(0.05, _MINUTE - (self._now() - self._window[0][0]))
        return 0.05

    def _entry(self, cost: int) -> _WINDOW:
        self._seq += 1
        return (self._now(), cost, self._seq)

    def _used(self) -> int:
        return sum(cost for _, cost, _seq in self._window) + sum(
            cost for _, cost, _seq in self._reserved
        )

    def _trim(self) -> None:
        cutoff = self._now() - _MINUTE
        self._window = [entry for entry in self._window if entry[0] > cutoff]
