"""Discrete-event scheduler with integer nanosecond time.

Everything in the project (bus, devices, gateway core, clients) runs on this
clock. The studies and scenario tests run it as fast as possible, so they are
deterministic. The live server pumps it against the wall clock.
"""
from __future__ import annotations

import heapq
from typing import Any, Callable

NS = 1
US = 1_000
MS = 1_000_000
S = 1_000_000_000


def ms(x: float) -> int:
    return int(round(x * MS))


def sec(x: float) -> int:
    return int(round(x * S))


class Event:
    __slots__ = ("fn", "args", "t")

    def __init__(self, t: int, fn: Callable[..., Any], args: tuple):
        self.t = t
        self.fn = fn
        self.args = args

    def cancel(self) -> None:
        self.fn = None  # type: ignore[assignment]


class Scheduler:
    def __init__(self) -> None:
        self.now: int = 0
        self._q: list[tuple[int, int, Event]] = []
        self._seq = 0

    def at(self, t: int, fn: Callable[..., Any], *args: Any) -> Event:
        if t < self.now:
            t = self.now
        ev = Event(t, fn, args)
        heapq.heappush(self._q, (t, self._seq, ev))
        self._seq += 1
        return ev

    def after(self, dt: int, fn: Callable[..., Any], *args: Any) -> Event:
        return self.at(self.now + max(0, dt), fn, *args)

    def run_until(self, t_end: int) -> None:
        q = self._q
        while q and q[0][0] <= t_end:
            t, _, ev = heapq.heappop(q)
            if ev.fn is None:
                continue
            self.now = t
            ev.fn(*ev.args)
        if t_end > self.now:
            self.now = t_end

    def run(self) -> None:
        q = self._q
        while q:
            t, _, ev = heapq.heappop(q)
            if ev.fn is None:
                continue
            self.now = t
            ev.fn(*ev.args)

    def pending(self) -> int:
        return sum(1 for _, _, e in self._q if e.fn is not None)
