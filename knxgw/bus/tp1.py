"""Telegram-level model of one KNX TP1 line.

Timing (see docs/SOURCES.md, S2 and S3):
  * 9600 bit/s, so one bit time is 104.17 us.
  * One character is 8 data bits sent with start, parity and stop bit and a
    pause of 2 bit times: 13 bit times per character.
  * After a frame there is a break of one character, then one character for the
    acknowledgement (IACK / INACK / BUSY).
  * After the acknowledgement the bus must be idle for 50 bit times. Frames of
    priority "system" and "urgent" and repeated frames may start then. Frames
    of priority "normal" and "low" wait 3 more bit times.
  * Total per telegram: t = t1 + t3 + (n + 2) * 13 bit times, n = 8..23
    characters, which is the 20..40 ms quoted in the KNX training material.
  * Access is CSMA/CA: stations that start in the same bit arbitrate bit by
    bit, a 0 overwrites a 1. Bits go out least significant first.
  * A frame that gets INACK, a damaged acknowledgement or none is repeated,
    3 times by default (S4), with the repeat flag set.

What is not modelled: BUSY flow control, line couplers and their filter
tables, extended frames, bit-level signal shape, collisions between stations
that start within the same bit but not at the same instant.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..simtime import Scheduler

BIT_RATE = 9600
BIT_NS = round(1e9 / BIT_RATE)          # 104167 ns
CHAR_BITS = 13                          # 11 bit character + 2 bit pause
IDLE_BITS = 50                          # t3, bus idle time after the acknowledgement
LOW_PRIO_EXTRA_BITS = 3                 # t1 for priority normal / low
ACK_SLOT_CHARS = 2                      # break of one character + acknowledgement character
NAK_RETRY = 3                           # repetitions after the first attempt

PRIORITIES = ("system", "urgent", "normal", "low")
# Control field is 1 0 R 1 P1 P0 0 0 (D7..D0). On the wire D0 goes first, so P0 (D2)
# is compared before P1 (D3). Coding P1 P0: 00 system, 10 urgent, 01 normal, 11 low.
_PRIO_WIRE = {"system": (0, 0), "urgent": (0, 1), "normal": (1, 0), "low": (1, 1)}


def ga(main: int, middle: int, sub: int) -> int:
    """Three-level group address main/middle/sub as 16 bit number."""
    return (main << 11) | (middle << 8) | sub


def ga_str(g: int) -> str:
    return f"{g >> 11}/{(g >> 8) & 7}/{g & 0xFF}"


def ia(area: int, line: int, dev: int) -> int:
    """Individual address area.line.device as 16 bit number."""
    return (area << 12) | (line << 8) | dev


def ia_str(a: int) -> str:
    return f"{a >> 12}.{(a >> 8) & 15}.{a & 0xFF}"


def frame_chars(data_bytes: int) -> int:
    """Characters of a standard L_Data frame carrying a group value.

    control, source (2), destination (2), routing/length, TPCI/APCI, APCI/data,
    checksum = 9 characters for values of up to 6 bits; one more per value octet.
    """
    return 9 + data_bytes


def telegram_bits(n_chars: int, priority: str = "low", repeated: bool = False) -> int:
    """Bit times from 'bus became idle' to 'bus is idle again' for one telegram."""
    t1 = 0 if (repeated or priority in ("system", "urgent")) else LOW_PRIO_EXTRA_BITS
    return t1 + IDLE_BITS + (n_chars + ACK_SLOT_CHARS) * CHAR_BITS


def telegram_ms(n_chars: int, priority: str = "low", repeated: bool = False) -> float:
    return telegram_bits(n_chars, priority, repeated) * BIT_NS / 1e6


def max_telegrams_per_s(n_chars: int, priority: str = "low") -> float:
    return 1e9 / (telegram_bits(n_chars, priority) * BIT_NS)


_uid = 0


@dataclass
class Frame:
    src: int
    dst: int                       # group address
    apci: str                      # "write" | "read" | "response"
    value: Any = None              # raw (bus side) value
    data_bytes: int = 0            # value octets beyond the 6 bit short form
    priority: str = "low"
    origin: str = ""               # trace tag, e.g. "api:17", "wall", "status"
    repeated: bool = False
    attempts: int = 0
    enqueued_at: int = 0
    uid: int = 0
    extra_wait_bits: int = 0       # a station may wait longer than the minimum before it starts
    seen_by: set = field(default_factory=set)

    def __post_init__(self) -> None:
        global _uid
        _uid += 1
        self.uid = _uid

    @property
    def n_chars(self) -> int:
        return frame_chars(self.data_bytes)

    def wait_bits(self) -> int:
        if self.repeated:
            return IDLE_BITS
        if self.priority in ("system", "urgent"):
            return IDLE_BITS + self.extra_wait_bits
        return IDLE_BITS + LOW_PRIO_EXTRA_BITS + self.extra_wait_bits

    def wire_key(self) -> tuple:
        """Bits in the order they appear on the wire; the smaller tuple wins arbitration."""
        p0, p1 = _PRIO_WIRE[self.priority]
        r = 0 if self.repeated else 1
        bits = [0, 0, p0, p1, 1, r, 0, 1]
        for octet in ((self.src >> 8) & 0xFF, self.src & 0xFF):
            bits.extend((octet >> i) & 1 for i in range(8))
        return tuple(bits)


class Station:
    """A device on the line. Sub-classes override on_frame / on_con."""

    def __init__(self, line: "TP1Line", addr: int, name: str = "") -> None:
        self.line = line
        self.addr = addr
        self.name = name or ia_str(addr)
        self.group_table: set[int] = set()   # addresses this station acknowledges
        self.txq: list[Frame] = []
        self.p_rx_miss: Optional[float] = None  # None: use the line default
        line.attach(self)

    def send(self, frame: Frame) -> None:
        frame.src = self.addr
        frame.enqueued_at = self.line.sched.now
        self.txq.append(frame)
        self.line.kick()

    def before_tx(self, frame: Frame) -> bool:
        """Called at the instant the frame would start. Return False to withdraw it."""
        return True

    def on_frame(self, frame: Frame) -> None:  # pragma: no cover - interface
        pass

    def on_con(self, frame: Frame, ok: bool) -> None:  # pragma: no cover - interface
        pass


@dataclass
class LineStats:
    frames: int = 0             # transmissions including repetitions
    telegrams: int = 0          # distinct telegrams completed
    repetitions: int = 0
    failed: int = 0             # telegrams given up after all repetitions
    busy_ns: int = 0
    arbitrations: int = 0       # starts with more than one station


class TP1Line:
    def __init__(self, sched: Scheduler, seed: int = 1, p_frame_corrupt: float = 0.0,
                 p_rx_miss: float = 0.0, p_ack_corrupt: float = 0.0) -> None:
        self.sched = sched
        self.rng = random.Random(seed)
        self.p_frame_corrupt = p_frame_corrupt
        self.p_rx_miss = p_rx_miss
        self.p_ack_corrupt = p_ack_corrupt
        self.stations: list[Station] = []
        self.busy = False
        self.idle_since = -10**12          # bus idle "for ever" at start
        self._start_ev = None
        self.stats = LineStats()
        self.monitors: list[Callable[[dict], None]] = []

    def attach(self, st: Station) -> None:
        self.stations.append(st)

    # -- medium access ---------------------------------------------------------------
    def _ready_at(self, f: Frame) -> int:
        return max(self.idle_since + f.wait_bits() * BIT_NS, f.enqueued_at)

    def kick(self) -> None:
        if self.busy:
            return
        t_min = None
        for st in self.stations:
            if st.txq:
                t = self._ready_at(st.txq[0])
                if t_min is None or t < t_min:
                    t_min = t
        if t_min is None:
            return
        if self._start_ev is not None:
            if self._start_ev.fn is not None and self._start_ev.t == t_min:
                return
            self._start_ev.cancel()
        self._start_ev = self.sched.at(t_min, self._start)

    def _start(self) -> None:
        self._start_ev = None
        if self.busy:
            return
        now = self.sched.now
        cands = [st for st in self.stations if st.txq and self._ready_at(st.txq[0]) <= now]
        if not cands:
            self.kick()
            return
        if len(cands) > 1:
            self.stats.arbitrations += 1
        winner = min(cands, key=lambda s: s.txq[0].wire_key())
        f = winner.txq[0]
        if not f.repeated and not winner.before_tx(f):
            if winner.txq and winner.txq[0] is f:
                winner.txq.pop(0)
            self.kick()
            return
        self.busy = True
        self.stats.frames += 1
        t_frame_end = now + f.n_chars * CHAR_BITS * BIT_NS
        self.sched.at(t_frame_end, self._frame_end, winner, f, now)

    def _frame_end(self, sender: Station, f: Frame, t_start: int) -> None:
        now = self.sched.now
        rng = self.rng
        corrupted = rng.random() < self.p_frame_corrupt
        ack = None
        receivers: list[Station] = []
        for st in self.stations:
            if st is sender:
                continue
            p = self.p_rx_miss if st.p_rx_miss is None else st.p_rx_miss
            if p and rng.random() < p:
                continue                      # this station did not get the frame at all
            addressed = f.dst in st.group_table
            if corrupted:
                if addressed:
                    ack = "nack"              # INACK overwrites IACK
            else:
                receivers.append(st)
                if addressed and ack is None:
                    ack = "ack"
        ok = ack == "ack" and not (self.p_ack_corrupt and rng.random() < self.p_ack_corrupt)
        t_idle = now + (ACK_SLOT_CHARS * CHAR_BITS * BIT_NS if ack else 0)
        for st in receivers:
            if id(st) in f.seen_by:
                continue                      # repeated frame, already passed up
            f.seen_by.add(id(st))
            st.on_frame(f)
        for mon in self.monitors:
            mon({"t_start": t_start, "t_end": t_idle, "frame": f, "ack": ack, "ok": ok,
                 "attempt": f.attempts})
        self.sched.at(t_idle, self._slot_end, sender, f, ok, t_start)

    def _slot_end(self, sender: Station, f: Frame, ok: bool, t_start: int) -> None:
        now = self.sched.now
        self.stats.busy_ns += now - t_start
        self.busy = False
        self.idle_since = now
        if ok:
            sender.txq.pop(0)
            self.stats.telegrams += 1
            sender.on_con(f, True)
        else:
            f.attempts += 1
            if f.attempts > NAK_RETRY:
                sender.txq.pop(0)
                self.stats.telegrams += 1
                self.stats.failed += 1
                sender.on_con(f, False)
            else:
                f.repeated = True
                self.stats.repetitions += 1
        self.kick()
