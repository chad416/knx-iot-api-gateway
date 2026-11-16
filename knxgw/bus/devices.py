"""Bus devices of the simulated floor and the ground-truth log.

Every device is a Station on the TP1 line with its own transmit queue, so a
multi-channel actuator sends its status telegrams one after the other, as a
real one does. `Truth` records what each physical output really did and when;
the gateway never sees it, only the study observers do.

Device parameters that are assumptions (no public source, see DOSSIER section 7):
  * actuator reaction time (frame received -> output switched): uniform 8..20 ms
  * dimming speed for absolute values: 0..100 % in 2 s
  * blind travel time: 30 s for the full range
  * status objects send after every write ("always"), not only on change
"""
from __future__ import annotations

import bisect
import random
from typing import Any, Optional

from ..simtime import Scheduler, ms, sec
from .tp1 import Frame, Station, TP1Line


class Truth:
    """What the physical outputs did: key -> sorted list of (t, value, cause)."""

    def __init__(self) -> None:
        self.h: dict[str, list[tuple[int, Any, str]]] = {}
        self.listeners: list = []
        self.applied: dict[str, tuple[int, str, Any]] = {}   # cause tag -> (t, key, value)

    def mark(self, cause: str, t: int, key: str, value: Any) -> None:
        """An actuator executed the command with this cause tag (even if nothing changed)."""
        self.applied[cause] = (t, key, value)

    def set(self, key: str, t: int, value: Any, cause: str = "") -> None:
        h = self.h.setdefault(key, [])
        if h and h[-1][1] == value:
            return
        h.append((t, value, cause))
        for fn in self.listeners:
            fn(key, t, value, cause)

    def at(self, key: str, t: int) -> Any:
        h = self.h.get(key)
        if not h:
            return None
        i = bisect.bisect_right(h, t, key=lambda e: e[0]) - 1
        return h[i][1] if i >= 0 else None

    def last(self, key: str) -> Any:
        h = self.h.get(key)
        return h[-1][1] if h else None

    def changes(self, key: str) -> list[tuple[int, Any, str]]:
        return self.h.get(key, [])


class _Device(Station):
    def __init__(self, line: TP1Line, addr: int, name: str, truth: Truth, seed: int) -> None:
        super().__init__(line, addr, name)
        self.truth = truth
        self.sched: Scheduler = line.sched
        self.rng = random.Random(seed)
        self.react_ms = (8.0, 20.0)
        self.status_mode = "always"            # or "on_change"
        self._status_ga: dict[int, tuple] = {}  # status ga -> (getter, data_bytes)

    def _delay(self) -> int:
        return ms(self.rng.uniform(*self.react_ms))

    def _status(self, ga_: int, value: Any, data_bytes: int, cause: str, changed: bool = True) -> None:
        if self.status_mode == "on_change" and not changed:
            return
        self.send(Frame(self.addr, ga_, "write", value, data_bytes, "low", origin="status:" + cause))

    def _answer_read(self, f: Frame) -> bool:
        if f.apci == "read" and f.dst in self._status_ga:
            getter, nbytes = self._status_ga[f.dst]
            self.send(Frame(self.addr, f.dst, "response", getter(), nbytes, "low",
                            origin="response:" + f.origin))
            return True
        return False


class SwitchActuator(_Device):
    """Multi-channel relay actuator: command object and status object per channel."""

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.ch: dict[int, dict] = {}

    def add_channel(self, key: str, cmd_ga: int, status_ga: int) -> None:
        c = {"key": key, "cmd": cmd_ga, "status": status_ga, "on": False}
        self.ch[cmd_ga] = c
        self.group_table.update((cmd_ga, status_ga))
        self._status_ga[status_ga] = (lambda c=c: c["on"], 0)
        self.truth.set(key, 0, False, "init")

    def on_frame(self, f: Frame) -> None:
        if self._answer_read(f):
            return
        if f.apci == "write" and f.dst in self.ch:
            self.sched.after(self._delay(), self._apply, self.ch[f.dst], bool(f.value), f.origin)

    def _apply(self, c: dict, value: bool, cause: str) -> None:
        changed = c["on"] != value
        c["on"] = value
        self.truth.mark(cause, self.sched.now, c["key"], value)
        self.truth.set(c["key"], self.sched.now, value, cause)
        self._status(c["status"], value, 0, cause, changed)


class DimActuator(_Device):
    """Dimmer: switch object jumps, value object ramps and reports at the end of the ramp."""

    def __init__(self, *a: Any, ramp_full_s: float = 2.0, **k: Any) -> None:
        super().__init__(*a, **k)
        self.ramp_full_s = ramp_full_s
        self.by_ga: dict[int, tuple[dict, str]] = {}

    def add_channel(self, key: str, sw_ga: int, sw_st: int, val_ga: int, val_st: int) -> None:
        c = {"key": key, "sw_st": sw_st, "val_st": val_st, "level": 0.0, "last": 100.0,
             "ramp": None, "from": 0.0, "to": 0.0, "t0": 0, "t1": 0}
        self.by_ga[sw_ga] = (c, "sw")
        self.by_ga[val_ga] = (c, "val")
        self.group_table.update((sw_ga, sw_st, val_ga, val_st))
        self._status_ga[sw_st] = (lambda c=c: self._level(c) > 0, 0)
        self._status_ga[val_st] = (lambda c=c: round(self._level(c), 1), 1)
        self.truth.set(key + ".sw", 0, False, "init")
        self.truth.set(key + ".val", 0, 0.0, "init")

    def _level(self, c: dict) -> float:
        if c["ramp"] is None:
            return c["level"]
        now = self.sched.now
        if c["t1"] <= c["t0"]:
            return c["to"]
        x = min(1.0, max(0.0, (now - c["t0"]) / (c["t1"] - c["t0"])))
        return c["from"] + (c["to"] - c["from"]) * x

    def on_frame(self, f: Frame) -> None:
        if self._answer_read(f):
            return
        if f.apci == "write" and f.dst in self.by_ga:
            c, obj = self.by_ga[f.dst]
            self.sched.after(self._delay(), self._apply, c, obj, f.value, f.origin)

    def _apply(self, c: dict, obj: str, value: Any, cause: str) -> None:
        now = self.sched.now
        cur = self._level(c)
        if c["ramp"] is not None:
            c["ramp"].cancel()
            c["ramp"] = None
        c["level"] = cur
        if obj == "sw":
            target = c["last"] if value else 0.0
            self.truth.mark(cause, now, c["key"] + ".sw", bool(value))
            self._arrive(c, target, cause, mark_val=False)    # jump
            return
        target = float(value)
        was_on = cur > 0
        if target > 0 and not was_on:
            self.truth.set(c["key"] + ".sw", now, True, cause)
            self._status(c["sw_st"], True, 0, cause)
        dur = sec(self.ramp_full_s * abs(target - cur) / 100.0)
        c.update({"from": cur, "to": target, "t0": now, "t1": now + dur})
        c["ramp"] = self.sched.after(dur, self._arrive, c, target, cause)

    def _arrive(self, c: dict, target: float, cause: str, mark_val: bool = True) -> None:
        now = self.sched.now
        was_on = self.truth.last(c["key"] + ".sw")
        c["ramp"] = None
        c["level"] = target
        if target > 0:
            c["last"] = target
        if mark_val:
            self.truth.mark(cause, now, c["key"] + ".val", target)
        self.truth.set(c["key"] + ".val", now, target, cause)
        self._status(c["val_st"], target, 1, cause)
        on = target > 0
        self.truth.set(c["key"] + ".sw", now, on, cause)
        self._status(c["sw_st"], on, 0, cause, changed=(on != was_on))


class BlindActuator(_Device):
    """Blind actuator: absolute position 0 % (open) .. 100 % (closed), status at the end of travel."""

    def __init__(self, *a: Any, travel_s: float = 30.0, **k: Any) -> None:
        super().__init__(*a, **k)
        self.travel_s = travel_s
        self.ch: dict[int, dict] = {}

    def add_channel(self, key: str, pos_ga: int, pos_st: int) -> None:
        c = {"key": key, "st": pos_st, "pos": 0.0, "move": None, "from": 0.0, "to": 0.0,
             "t0": 0, "t1": 0}
        self.ch[pos_ga] = c
        self.group_table.update((pos_ga, pos_st))
        self._status_ga[pos_st] = (lambda c=c: round(self._pos(c), 1), 1)
        self.truth.set(key, 0, 0.0, "init")

    def _pos(self, c: dict) -> float:
        if c["move"] is None or c["t1"] <= c["t0"]:
            return c["pos"] if c["move"] is None else c["to"]
        x = min(1.0, max(0.0, (self.sched.now - c["t0"]) / (c["t1"] - c["t0"])))
        return c["from"] + (c["to"] - c["from"]) * x

    def on_frame(self, f: Frame) -> None:
        if self._answer_read(f):
            return
        if f.apci == "write" and f.dst in self.ch:
            self.sched.after(self._delay(), self._apply, self.ch[f.dst], float(f.value), f.origin)

    def _apply(self, c: dict, target: float, cause: str) -> None:
        now = self.sched.now
        cur = self._pos(c)
        if c["move"] is not None:
            c["move"].cancel()
        c["pos"] = cur
        dur = sec(self.travel_s * abs(target - cur) / 100.0)
        c.update({"from": cur, "to": target, "t0": now, "t1": now + dur})
        c["move"] = self.sched.after(dur, self._arrive, c, target, cause)

    def _arrive(self, c: dict, target: float, cause: str) -> None:
        c["move"] = None
        c["pos"] = target
        self.truth.mark(cause, self.sched.now, c["key"], target)
        self.truth.set(c["key"], self.sched.now, target, cause)
        self._status(c["st"], target, 1, cause)


class PushButton(_Device):
    """Wall switch. Listens to the command and status addresses of its rockers (LED, toggle
    tracking), so it acknowledges frames on them."""

    def listen(self, *gas: int) -> None:
        self.group_table.update(gas)

    def press(self, cmd_ga: int, value: Any, data_bytes: int = 0, origin: str = "wall") -> Frame:
        f = Frame(self.addr, cmd_ga, "write", value, data_bytes, "low", origin=origin)
        self.send(f)
        return f


class Panel(_Device):
    """Visualisation panel / room controller: consumes status and sensor values, so it
    acknowledges them. Without such a consumer every sensor frame would be sent four times."""

    def listen(self, *gas: int) -> None:
        self.group_table.update(gas)


class PresenceDetector(_Device):
    def __init__(self, *a: Any, key: str, ga_: int, **k: Any) -> None:
        super().__init__(*a, **k)
        self.key = key
        self.ga = ga_
        self.truth.set(key, 0, False, "init")

    def trigger(self, present: bool, origin: str = "presence") -> None:
        self.truth.set(self.key, self.sched.now, present, origin)
        self.send(Frame(self.addr, self.ga, "write", present, 0, "low", origin=origin))


class CyclicSensor(_Device):
    """Sends a 2 octet float cyclically (temperature, brightness)."""

    def __init__(self, *a: Any, key: str, ga_: int, period_s: float, base: float, swing: float,
                 **k: Any) -> None:
        super().__init__(*a, **k)
        self.key, self.ga, self.period_s, self.base, self.swing = key, ga_, period_s, base, swing
        self.value = base
        self.truth.set(key, 0, base, "init")
        self.sched.after(sec(self.rng.uniform(0.1, period_s)), self._tick)

    def _tick(self) -> None:
        self.value = round(self.base + self.swing * (self.rng.random() - 0.5), 2)
        self.truth.set(self.key, self.sched.now, self.value, "cyclic")
        self.send(Frame(self.addr, self.ga, "write", self.value, 2, "low", origin="cyclic"))
        self.sched.after(sec(self.period_s * self.rng.uniform(0.98, 1.02)), self._tick)
