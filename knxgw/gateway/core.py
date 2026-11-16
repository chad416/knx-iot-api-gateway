"""Gateway core: bus driver, state cache with freshness, command queue, three consistency designs.

The core is transport-free. The HTTP layer (api.py) and the simulated clients
of the studies call the same `write` and `read` methods, so what the studies
measure is the code that serves real requests.

Designs (cfg.design):
  optimistic  D1  the cache takes the written value at once; the call returns at once.
  confirmed   D2  the cache changes only on a status telegram from the device; the call
                  returns when that telegram has been heard (fast datapoints) or with
                  "accepted" (datapoints that travel: dimmer value, blind position).
  versioned   D3  as D2, plus a version per datapoint, conditional writes (if_version),
                  read-your-writes per client, and a report when a write was applied and
                  then overwritten from the bus.

The gateway is a listen-only member of every group address: it does not send
link-layer acknowledgements for telegrams it only monitors, so it never hides
a missing actuator from the sender.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..bus.installation import DatapointDef, Installation
from ..bus.tp1 import BIT_NS, Frame, Station, ia, telegram_bits
from ..simtime import ms, sec

DESIGNS = ("optimistic", "confirmed", "versioned")
SLOT_NS = telegram_bits(9, "low") * BIT_NS       # one 1-bit telegram incl. acknowledge and idle time


@dataclass
class GatewayConfig:
    design: str = "versioned"
    addr: int = ia(1, 1, 250)
    priority: str = "low"
    pace_util: float = 0.5          # share of bus time the gateway allows itself, 1.0 = no pacing
    queue_max: int = 64             # commands waiting for the bus
    client_rate: float = 10.0       # writes per second per client (token bucket)
    client_burst: int = 40
    fb_timeout_ms: float = 400.0    # wait for the status telegram after the link-layer confirm
    slow_margin_s: float = 2.0      # extra wait on top of the travel time of a slow datapoint
    refresh_period_s: float = 0.0   # read every actuator status once per period, 0 = off
    dp_min_interval_ms: float = 0.0  # shortest time between two accepted writes to one datapoint
    recheck_at_dequeue: bool = True  # D3: check if_version again when the frame goes to the bus
    cond_blocks_on_pending: bool = True  # D3: a conditional write fails while another write is in flight
    fair: bool = True               # round robin between clients; False = one first-in first-out queue
    polite: bool = False            # wait 3 bit times longer than any other station before sending,
                                    # and check a conditional write again at that instant
    read_through: bool = False      # naive variant: every API read is a GroupValueRead on the bus
    bulk_threshold: int = 4         # batches of this many items or more are queued as "bulk"


@dataclass
class CacheEntry:
    value: Any = None
    version: int = 0
    changed_at: int = 0
    confirmed_at: Optional[int] = None    # last time the bus confirmed this value
    source: str = "init"
    bus_value: Any = None                 # last value heard from the device, whatever the design
    foreign_seq: int = 0                  # commands from other bus devices heard on the command address
    foreign_at: Optional[int] = None      # such a command was heard and its status has not come yet
    foreign_value: Any = None
    pending_target: Any = None
    pending_n: int = 0


@dataclass
class Op:
    """One queued bus operation: an API write, or a read (refresh / read-through)."""
    oid: int
    client: str
    dp: DatapointDef
    value: Any = None
    if_version: Optional[int] = None
    is_read: bool = False
    klass: int = 0
    batch: Optional["Batch"] = None
    t_call: int = 0
    t_tx: Optional[int] = None
    t_con: Optional[int] = None
    t_done: Optional[int] = None
    state: str = "queued"
    outcome: Optional[str] = None
    v_at_tx: int = 0
    f_at_tx: int = 0
    v_done: int = 0                       # cache version at the moment this op was confirmed
    foreign_after_con: bool = False
    timer: Any = None
    cb: Optional[Callable[["Op"], None]] = None


@dataclass
class Batch:
    client: str
    ops: list[Op]
    cb: Callable[["BatchResult"], None]
    answered: bool = False


@dataclass
class BatchResult:
    status: str                       # ok | accepted | conflict | rate | full | failed
    items: list[dict] = field(default_factory=list)
    retry_after_s: float = 0.0
    detail: str = ""


OK_OUTCOMES = ("confirmed", "sent")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 0.5
    return a == b


class GatewayCore(Station):
    def __init__(self, inst: Installation, cfg: Optional[GatewayConfig] = None) -> None:
        self.cfg = cfg or GatewayConfig()
        if self.cfg.design not in DESIGNS:
            raise ValueError(f"unknown design {self.cfg.design}")
        super().__init__(inst.line, self.cfg.addr, "gateway")
        self.inst = inst
        self.sched = inst.sched
        self.by_cmd = {d.cmd_ga: d for d in inst.datapoints.values() if d.cmd_ga is not None}
        self.by_status = {d.status_ga: d for d in inst.datapoints.values()}
        self.cache: dict[str, CacheEntry] = {d.id: CacheEntry() for d in inst.datapoints.values()}
        self.q: list[OrderedDict[str, deque[Op]]] = [OrderedDict() for _ in range(3)]
        self.q_len = 0
        self._inflight: Optional[Op] = None
        self._next_tx = 0
        self._pump_ev = None
        self._await: dict[str, list[Op]] = {}
        self._oid = 0
        self._buckets: dict[str, tuple[float, int]] = {}
        self._last_write: dict[str, int] = {}
        self._session: dict[str, dict[str, int]] = {}       # client -> dp -> unresolved fast writes
        self._read_waiters: list[tuple[str, list[str], Callable]] = []
        self.listeners: list[Callable[[dict], None]] = []    # cache change notifications
        self.op_hooks: list[Callable[[Op], None]] = []       # called when an op is resolved
        self.stats = {"tx": 0, "rejected_rate": 0, "rejected_full": 0, "conflict_early": 0,
                      "conflict_dequeue": 0, "conflict_late": 0, "superseded": 0,
                      "bus_nack": 0, "no_feedback": 0, "refresh_tx": 0, "q_max": 0}
        self._refresh_list = [d for d in inst.datapoints.values() if d.cmd_ga is not None]
        self._refresh_i = 0
        if self.cfg.refresh_period_s > 0 and self._refresh_list:
            self.sched.after(sec(self.cfg.refresh_period_s / len(self._refresh_list)), self._refresh_tick)

    # ---------------------------------------------------------------- cache ------------
    def _notify(self, dp: DatapointDef, e: CacheEntry, source: str) -> None:
        ev = {"dp": dp.id, "key": dp.key, "value": e.value, "version": e.version,
              "t": self.sched.now, "source": source}
        for fn in self.listeners:
            fn(ev)

    def _set(self, dp: DatapointDef, value: Any, source: str, confirmed: bool) -> None:
        e = self.cache[dp.id]
        now = self.sched.now
        if confirmed:
            e.confirmed_at = now
            e.bus_value = value
        if e.value is None or not _same(e.value, value):
            e.value = value
            e.version += 1
            e.changed_at = now
            e.source = source
            self._notify(dp, e, source)
        elif confirmed:
            e.source = source

    def _foreign_open(self, d: DatapointDef, e: CacheEntry) -> bool:
        """A command from another bus device is on its way to the actuator: the state is about
        to change, although the cache (which follows status telegrams) does not show it yet."""
        if e.foreign_at is None:
            return False
        limit = ms(self.cfg.fb_timeout_ms) + sec(d.settle_s)
        return self.sched.now - e.foreign_at < limit

    def snapshot(self, dp_id: str) -> dict:
        e = self.cache[dp_id]
        now = self.sched.now
        return {"dp": dp_id, "value": e.value, "version": e.version, "changed_at": e.changed_at,
                "confirmed_at": e.confirmed_at,
                "age_ns": None if e.confirmed_at is None else now - e.confirmed_at,
                "source": e.source, "pending": e.pending_n > 0,
                "pending_target": e.pending_target if e.pending_n > 0 else None}

    # ---------------------------------------------------------------- API: read --------
    def read(self, client: str, dp_ids: list[str], cb: Callable[[list[dict]], None]) -> None:
        if self.cfg.read_through:
            self._read_through(client, dp_ids, cb)
            return
        if self.cfg.design == "versioned":
            sess = self._session.get(client)
            if sess and any(sess.get(i, 0) > 0 for i in dp_ids):
                self._read_waiters.append((client, dp_ids, cb))     # read-your-writes: wait
                return
        cb([self.snapshot(i) for i in dp_ids])

    def _release_readers(self, client: str) -> None:
        if not self._read_waiters:
            return
        sess = self._session.get(client, {})
        keep = []
        for c, ids, cb in self._read_waiters:
            if c == client and not any(sess.get(i, 0) > 0 for i in ids):
                cb([self.snapshot(i) for i in ids])
            else:
                keep.append((c, ids, cb))
        self._read_waiters = keep

    def _read_through(self, client: str, dp_ids: list[str], cb: Callable) -> None:
        todo = {"n": 0}
        ops: list[Op] = []

        def done(_op: Op) -> None:
            todo["n"] -= 1
            if todo["n"] == 0:
                cb([self.snapshot(i) for i in dp_ids])

        for i in dp_ids:
            d = self.inst.datapoints[i]
            if d.cmd_ga is None:
                continue
            if self.q_len >= self.cfg.queue_max:
                self.stats["rejected_full"] += 1
                continue
            ops.append(self._new_op(client, d, None, None, True, 0, done))
        todo["n"] = len(ops)
        if not ops:
            cb([self.snapshot(i) for i in dp_ids])
            return
        for op in ops:
            self._enqueue(op)
        self._pump()

    # ---------------------------------------------------------------- API: write -------
    def _take_tokens(self, client: str, n: int) -> float:
        """Token bucket per client. Returns 0 if granted, else seconds until n tokens exist."""
        now = self.sched.now
        tokens, t0 = self._buckets.get(client, (float(self.cfg.client_burst), now))
        tokens = min(float(self.cfg.client_burst), tokens + (now - t0) / 1e9 * self.cfg.client_rate)
        if tokens >= n:
            self._buckets[client] = (tokens - n, now)
            return 0.0
        self._buckets[client] = (tokens, now)
        return (n - tokens) / self.cfg.client_rate

    def write(self, client: str, items: list[tuple[str, Any, Optional[int]]],
              cb: Callable[[BatchResult], None]) -> None:
        """items: (datapoint id, raw value, if_version or None). Values are already validated."""
        cfg, now = self.cfg, self.sched.now
        dps = [self.inst.datapoints[i] for i, _, _ in items]
        if cfg.design == "versioned":
            bad = [{"dp": d.id, "outcome": "conflict", "version": self.cache[d.id].version}
                   for d, (_, _, v) in zip(dps, items)
                   if v is not None and (self.cache[d.id].version != v
                                         or self._foreign_open(d, self.cache[d.id])
                                         or (cfg.cond_blocks_on_pending and self.cache[d.id].pending_n > 0))]
            if bad:
                self.stats["conflict_early"] += len(bad)
                cb(BatchResult("conflict", bad, detail="datapoint changed since the given version"))
                return
        if cfg.dp_min_interval_ms > 0:
            for d in dps:
                last = self._last_write.get(d.id)
                if last is not None and now - last < ms(cfg.dp_min_interval_ms):
                    self.stats["rejected_rate"] += 1
                    cb(BatchResult("rate", retry_after_s=(ms(cfg.dp_min_interval_ms) - (now - last)) / 1e9,
                                   detail="datapoint is written too often"))
                    return
        if len(items) > cfg.queue_max:
            self.stats["rejected_full"] += 1
            cb(BatchResult("full", detail="batch is larger than the command queue"))
            return
        if self.q_len + len(items) > cfg.queue_max:
            self.stats["rejected_full"] += 1
            wait = (self.q_len + len(items) - cfg.queue_max) * SLOT_NS / max(cfg.pace_util, 0.05) / 1e9
            cb(BatchResult("full", retry_after_s=wait, detail="command queue is full"))
            return
        wait = self._take_tokens(client, len(items))
        if wait > 0:
            self.stats["rejected_rate"] += 1
            cb(BatchResult("rate", retry_after_s=wait, detail="client write rate exceeded"))
            return
        klass = 1 if (cfg.fair and len(items) >= cfg.bulk_threshold) else 0
        batch = Batch(client, [], cb)
        for d, (_, value, ifv) in zip(dps, items):
            op = self._new_op(client, d, value, ifv, False, klass, None)
            op.batch = batch
            batch.ops.append(op)
            self._last_write[d.id] = now
            e = self.cache[d.id]
            e.pending_n += 1
            e.pending_target = value
            if cfg.design == "optimistic":
                self._set(d, value, "optimistic", confirmed=False)
            elif cfg.design == "versioned" and not d.slow:
                s = self._session.setdefault(client, {})
                s[d.id] = s.get(d.id, 0) + 1
            self._enqueue(op)
        if cfg.design == "optimistic":
            self._answer(batch, BatchResult("ok", [self._item(o) for o in batch.ops]))
        elif any(o.dp.slow for o in batch.ops):
            self._answer(batch, BatchResult("accepted", [self._item(o) for o in batch.ops]))
        self._pump()

    def _item(self, op: Op) -> dict:
        return {"dp": op.dp.id, "op": op.oid, "outcome": op.outcome or "queued",
                "version": self.cache[op.dp.id].version}

    def _answer(self, batch: Batch, res: BatchResult) -> None:
        if not batch.answered:
            batch.answered = True
            batch.cb(res)

    # ---------------------------------------------------------------- queue ------------
    def _new_op(self, client: str, d: DatapointDef, value: Any, ifv: Optional[int], is_read: bool,
                klass: int, cb: Optional[Callable]) -> Op:
        self._oid += 1
        return Op(self._oid, client, d, value, ifv, is_read, klass, t_call=self.sched.now, cb=cb)

    def _enqueue(self, op: Op) -> None:
        self.q[op.klass].setdefault(op.client if self.cfg.fair else "_fifo", deque()).append(op)
        self.q_len += 1
        if self.q_len > self.stats["q_max"]:
            self.stats["q_max"] = self.q_len

    def _pop(self) -> Optional[Op]:
        """Strict priority between classes, round robin between clients inside a class."""
        for cls in self.q:
            if cls:
                client, dq = next(iter(cls.items()))
                op = dq.popleft()
                del cls[client]
                if dq:
                    cls[client] = dq          # this client goes to the back of the line
                self.q_len -= 1
                return op
        return None

    def _pump(self) -> None:
        while self._inflight is None and self.q_len:
            now = self.sched.now
            if now < self._next_tx:
                if self._pump_ev is None or self._pump_ev.fn is None or self._pump_ev.t <= now:
                    self._pump_ev = self.sched.at(self._next_tx, self._pump)
                return
            op = self._pop()
            if op is None:
                return
            e = self.cache[op.dp.id]
            if (not op.is_read and self.cfg.design == "versioned" and op.if_version is not None
                    and self.cfg.recheck_at_dequeue
                    and (e.version != op.if_version or self._foreign_open(op.dp, e))):
                self.stats["conflict_dequeue"] += 1
                self._resolve(op, "conflict")
                continue
            op.t_tx, op.state = now, "sent"
            op.v_at_tx, op.f_at_tx = e.version, e.foreign_seq
            self._inflight = op
            self.stats["tx"] += 1
            extra = 3 if self.cfg.polite else 0
            if op.is_read:
                self.send(Frame(self.addr, op.dp.status_ga, "read", None, 0, self.cfg.priority,
                                origin=f"read:{op.oid}", extra_wait_bits=extra))
            else:
                self.send(Frame(self.addr, op.dp.cmd_ga, "write", op.value, op.dp.dpt.data_bytes,
                                self.cfg.priority, origin=f"api:{op.oid}", extra_wait_bits=extra))

    def before_tx(self, frame: Frame) -> bool:
        op = self._inflight
        if op is None or op.is_read or not self.cfg.polite:
            return True
        e = self.cache[op.dp.id]
        if (self.cfg.design == "versioned" and op.if_version is not None
                and (e.version != op.v_at_tx or e.foreign_seq != op.f_at_tx)):
            # last look before the first bit goes out: the state moved, do not send
            self._inflight = None
            self.stats["conflict_dequeue"] += 1
            self._resolve(op, "conflict")
            self.sched.after(0, self._pump)
            return False
        return True

    def on_con(self, frame: Frame, ok: bool) -> None:
        op, self._inflight = self._inflight, None
        now = self.sched.now
        if self.cfg.pace_util < 1.0:
            self._next_tx = now + int(SLOT_NS * (1.0 / max(self.cfg.pace_util, 0.05) - 1.0))
        if op is not None:
            op.t_con = now
            e = self.cache[op.dp.id]
            if not ok:
                self.stats["bus_nack"] += 1
                self._resolve(op, "bus_nack")
            elif op.is_read:
                op.state = "await_fb"
                self._await.setdefault(op.dp.id, []).append(op)
                op.timer = self.sched.after(ms(self.cfg.fb_timeout_ms), self._fb_timeout, op)
            else:
                if (self.cfg.design == "versioned" and op.if_version is not None
                        and (e.version != op.v_at_tx or e.foreign_seq != op.f_at_tx)):
                    # the state moved between hand-over and transmission: the bus has no
                    # compare-and-set, the telegram is out. Report it, do not hide it.
                    op.foreign_after_con = True
                    op.state = "late"
                if self.cfg.design == "optimistic":
                    self._resolve(op, "sent")
                else:
                    if op.state != "late":
                        op.state = "await_fb"
                    self._await.setdefault(op.dp.id, []).append(op)
                    wait = ms(self.cfg.fb_timeout_ms) + sec(op.dp.settle_s + (self.cfg.slow_margin_s if op.dp.slow else 0))
                    op.timer = self.sched.after(wait, self._fb_timeout, op)
        self._pump()

    def _fb_timeout(self, op: Op) -> None:
        if op.outcome is None:
            self.stats["no_feedback"] += 1
            self._resolve(op, "no_feedback")

    # ---------------------------------------------------------------- bus -> gateway ---
    def on_frame(self, f: Frame) -> None:
        if f.apci == "read":
            return
        d = self.by_status.get(f.dst)
        if d is not None:
            self._status_heard(d, f)
            return
        d = self.by_cmd.get(f.dst)
        if d is not None and f.apci == "write":
            e = self.cache[d.id]
            e.foreign_seq += 1
            e.foreign_at = self.sched.now
            e.foreign_value = f.value
            for op in self._await.get(d.id, ()):
                if not _same(op.value, f.value):
                    op.foreign_after_con = True       # a different command follows ours on the bus
            if self.cfg.design == "optimistic":
                self._set(d, f.value, "bus-command", confirmed=False)

    def _status_heard(self, d: DatapointDef, f: Frame) -> None:
        self._set(d, f.value, "bus-status" if f.apci == "write" else "read-response", confirmed=True)
        self.cache[d.id].foreign_at = None
        waiting = self._await.get(d.id)
        if not waiting:
            return
        for op in list(waiting):
            if op.is_read:
                if f.apci == "response":
                    self._resolve(op, "confirmed")
            elif _same(op.value, f.value):
                if self.cfg.design == "versioned" and op.foreign_after_con:
                    if op.state == "late":
                        self.stats["conflict_late"] += 1
                        self._resolve(op, "conflict_late")
                    else:
                        self.stats["superseded"] += 1
                        self._resolve(op, "superseded")
                else:
                    self._resolve(op, "confirmed")

    # ---------------------------------------------------------------- resolve ----------
    def _resolve(self, op: Op, outcome: str) -> None:
        if op.outcome is not None:
            return
        op.outcome, op.t_done, op.state = outcome, self.sched.now, "done"
        if op.timer is not None:
            op.timer.cancel()
        lst = self._await.get(op.dp.id)
        if lst and op in lst:
            lst.remove(op)
        if not op.is_read:
            e = self.cache[op.dp.id]
            op.v_done = e.version
            e.pending_n = max(0, e.pending_n - 1)
            if self.cfg.design == "optimistic" and outcome == "bus_nack" and e.bus_value is not None:
                self._set(op.dp, e.bus_value, "revert", confirmed=False)
            if self.cfg.design == "versioned" and not op.dp.slow:
                s = self._session.get(op.client)
                if s and s.get(op.dp.id, 0) > 0:
                    s[op.dp.id] -= 1
                self._release_readers(op.client)
        for fn in self.op_hooks:
            fn(op)
        if op.cb is not None:
            op.cb(op)
        b = op.batch
        if b is not None and not b.answered and all(o.outcome is not None for o in b.ops):
            if self.cfg.design == "versioned":
                # A batch is answered when its last item is confirmed. An item confirmed earlier
                # may have been changed since; the gateway heard that, so it says so.
                for o in b.ops:
                    e = self.cache[o.dp.id]
                    if o.outcome == "confirmed" and (e.version != o.v_done or (
                            self._foreign_open(o.dp, e) and not _same(e.foreign_value, o.value))):
                        o.outcome = "superseded"
                        self.stats["superseded"] += 1
            outs = [o.outcome for o in b.ops]
            items = [self._item(o) for o in b.ops]
            if all(o in OK_OUTCOMES for o in outs):
                self._answer(b, BatchResult("ok", items))
            elif any(o in ("conflict", "conflict_late", "superseded") for o in outs):
                self._answer(b, BatchResult("conflict", items,
                                            detail="a datapoint changed on the bus while the write was under way"))
            else:
                self._answer(b, BatchResult("failed", items, detail="the bus did not confirm the write"))

    # ---------------------------------------------------------------- refresh ----------
    def _refresh_tick(self) -> None:
        d = self._refresh_list[self._refresh_i % len(self._refresh_list)]
        self._refresh_i += 1
        if not self.q[2] and self.q_len < self.cfg.queue_max:
            self.stats["refresh_tx"] += 1
            self._enqueue(self._new_op("_refresh", d, None, None, True, 2, None))
            self._pump()
        self.sched.after(sec(self.cfg.refresh_period_s / len(self._refresh_list)), self._refresh_tick)

    # ---------------------------------------------------------------- helpers ----------
    def prime(self) -> None:
        """Start-up: take the current device state as if every status had been read once."""
        for d in self.inst.datapoints.values():
            v = self.inst.truth.last(d.key)
            e = self.cache[d.id]
            e.value, e.bus_value, e.version = v, v, 1
            e.confirmed_at = self.sched.now
            e.changed_at = self.sched.now
            e.source = "read-response"
