"""Shared machinery of the studies: world set-up, simulated API clients, ground-truth observers.

Simulated clients call GatewayCore.write / read, the same entry points the HTTP
layer uses. Each client has a one-way network delay; the gateway's own
processing time is a constant taken from the measured HTTP handler time
(results/summary.json, "http").

The observer compares three things per datapoint: what the device really did
(Truth), what the gateway cache says, and what the clients were told.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..bus.installation import DatapointDef, Installation
from ..gateway.core import BatchResult, GatewayConfig, GatewayCore, _same
from ..simtime import Scheduler, ms, sec

T_SERVER_MS = 2.0          # request handling in the gateway process (measured about 1-2 ms, see summary)
FAST = ("switch", "dim_switch")


class World:
    def __init__(self, design: str = "versioned", seed: int = 1, gw: Optional[dict] = None,
                 inst: Optional[dict] = None) -> None:
        self.sched = Scheduler()
        self.inst = Installation(self.sched, seed=seed, **(inst or {}))
        cfg = GatewayConfig(design=design, **(gw or {}))
        self.gw = GatewayCore(self.inst, cfg)
        self.gw.prime()
        self.obs = Observer(self)

    def run(self, seconds: float) -> None:
        self.sched.run_until(self.sched.now + sec(seconds))


@dataclass
class WriteRec:
    client: str
    kind: str                         # toggle | explicit | scene | rmw
    t_call: int
    items: list[tuple[DatapointDef, Any, Optional[int]]]
    seen: list[Any]                   # value the client believed when it decided (per item)
    status: Optional[str] = None
    t_resp_gw: Optional[int] = None
    t_resp: Optional[int] = None      # at the client
    ops: list[int] = field(default_factory=list)
    retry_after: float = 0.0
    t_gw_in: Optional[int] = None     # request arrives at the gateway
    readback: Optional[list[dict]] = None
    t_readback: Optional[int] = None


class SimClient:
    def __init__(self, world: World, name: str, net_ms: float = 5.0, use_versions: bool = False,
                 subscribe: bool = True) -> None:
        self.w, self.name, self.net = world, name, ms(net_ms)
        self.use_versions = use_versions and world.gw.cfg.design == "versioned"
        self.view: dict[str, tuple[Any, int]] = {}
        for d in world.inst.datapoints.values():
            e = world.gw.cache[d.id]
            self.view[d.id] = (e.value, e.version)
        if subscribe:
            world.gw.listeners.append(self._on_event)

    def _on_event(self, ev: dict) -> None:
        self.w.sched.after(self.net, self._apply_event, ev["dp"], ev["value"], ev["version"])

    def _apply_event(self, dp_id: str, value: Any, version: int) -> None:
        if version >= self.view[dp_id][1]:
            self.view[dp_id] = (value, version)

    def write(self, items: list[tuple], kind: str = "explicit",
              done: Optional[Callable[[WriteRec], None]] = None, conditional: Optional[bool] = None) -> WriteRec:
        s = self.w.sched
        cond = self.use_versions if conditional is None else (conditional and self.w.gw.cfg.design == "versioned")
        full = [(it[0], it[1], it[2] if len(it) > 2 else (self.view[it[0].id][1] if cond else None))
                for it in items]
        rec = WriteRec(self.name, kind, s.now, full, [self.view[it[0].id][0] for it in items])
        self.w.obs.writes.append(rec)

        def at_gw() -> None:
            rec.t_gw_in = s.now - ms(T_SERVER_MS)
            self.w.gw.write(self.name, [(d.id, v, ifv) for d, v, ifv in full], on_res)

        def on_res(res: BatchResult) -> None:
            rec.status, rec.t_resp_gw = res.status, s.now
            rec.ops = [it["op"] for it in res.items if "op" in it]
            rec.retry_after = res.retry_after_s
            s.after(self.net, at_client)

        def at_client() -> None:
            rec.t_resp = s.now
            if done:
                done(rec)

        s.after(self.net + ms(T_SERVER_MS), at_gw)
        return rec

    def read(self, dps: list[DatapointDef], done: Callable[[list[dict], int], None]) -> None:
        s = self.w.sched

        def at_gw() -> None:
            self.w.gw.read(self.name, [d.id for d in dps], lambda snap: s.after(self.net, done, snap, s.now))

        s.after(self.net + ms(T_SERVER_MS), at_gw)


class Observer:
    """Ground-truth bookkeeping. Nothing in here is visible to the gateway."""

    def __init__(self, world: World) -> None:
        self.w = world
        inst, gw = world.inst, world.gw
        self.writes: list[WriteRec] = []
        self.ops: dict[int, Any] = {}
        self.notified: dict[str, list[tuple[int, Any, str]]] = {}
        self.div_since: dict[str, Optional[int]] = {}
        self.div_total = {"fast": 0, "slow": 0}
        self.div_episodes: dict[str, list[int]] = {"fast": [], "slow": []}
        self.cache_val: dict[str, Any] = {}
        self._cls: dict[str, str] = {}
        self._dp_by_key = {d.key: d for d in inst.datapoints.values() if d.writable}
        for d in self._dp_by_key.values():
            self.cache_val[d.key] = gw.cache[d.id].value
            self.div_since[d.key] = None
            self._cls[d.key] = "fast" if d.kind in FAST else "slow"
            self.notified[d.key] = []
        self.t0 = world.sched.now
        self.evidence: dict[int, int] = {}      # op id -> time its status telegram was on the bus
        self.wall: dict[str, int] = {}          # wall press tag -> press time
        inst.line.monitors.append(self._on_bus)
        gw.listeners.append(self._on_cache)
        gw.op_hooks.append(lambda op: self.ops.__setitem__(op.oid, op))
        inst.truth.listeners.append(self._on_truth)

    def _eval(self, key: str, now: int) -> None:
        same = _same(self.cache_val[key], self.w.inst.truth.last(key))
        since = self.div_since[key]
        if same and since is not None:
            c = self._cls[key]
            self.div_total[c] += now - since
            self.div_episodes[c].append(now - since)
            self.div_since[key] = None
        elif not same and since is None:
            self.div_since[key] = now

    def _on_cache(self, ev: dict) -> None:
        key = ev["key"]
        if key in self.cache_val:
            self.cache_val[key] = ev["value"]
            self.notified[key].append((ev["t"], ev["value"], ev["source"]))
            self._eval(key, ev["t"])

    def _on_bus(self, rec: dict) -> None:
        o = rec["frame"].origin
        if rec["ok"] and o.startswith("status:api:"):
            self.evidence.setdefault(int(o[11:]), self.w.sched.now)

    def wall_latencies_ms(self, t_from: int = 0, t_to: Optional[int] = None) -> list[float]:
        out = []
        for tag, t in self.wall.items():
            if t < t_from or (t_to is not None and t > t_to):
                continue
            app = self.w.inst.truth.applied.get(tag)
            out.append((app[0] - t) / 1e6 if app else float("inf"))
        return out

    def _on_truth(self, key: str, t: int, value: Any, cause: str) -> None:
        if key in self.cache_val:
            self._eval(key, t)

    def close(self) -> None:
        now = self.w.sched.now
        for key, since in self.div_since.items():
            if since is not None:
                c = self._cls[key]
                self.div_total[c] += now - since
                self.div_episodes[c].append(now - since)
                self.div_since[key] = now

    # ---------------------------------------------------------------- derived metrics --
    def staleness(self) -> dict:
        self.close()
        dur = self.w.sched.now - self.t0
        out = {}
        for c in ("fast", "slow"):
            n = sum(1 for k in self._cls.values() if k == c)
            ep = sorted(self.div_episodes[c])
            out[c] = {
                "stale_time_fraction": self.div_total[c] / (dur * n) if dur and n else 0.0,
                "episodes": len(ep),
                "episode_p50_ms": _pct(ep, 50) / 1e6,
                "episode_p99_ms": _pct(ep, 99) / 1e6,
                "episode_max_s": (ep[-1] / 1e9) if ep else 0.0,
            }
        return out

    def notification_faithfulness(self) -> dict:
        """Compare the sequence of values the subscribers were told with what really happened."""
        spurious = missed = told = real = 0
        truth = self.w.inst.truth
        for key, notes in self.notified.items():
            d = self._dp_by_key[key]
            horizon = sec(d.settle_s + 3.0)
            tr = [e for e in truth.changes(key) if e[0] > self.t0]
            real += len(tr)
            told += len(notes)
            ptr = 0
            for t_n, v_n, _src in notes:
                j = ptr
                found = None
                while j < len(tr) and tr[j][0] <= t_n + horizon:
                    if _same(tr[j][1], v_n):
                        found = j
                        break
                    j += 1
                if found is None:
                    spurious += 1
                else:
                    missed += found - ptr
                    ptr = found + 1
            missed += len(tr) - ptr
        return {"told": told, "real": real, "spurious": spurious, "missed": missed}

    def classify_writes(self) -> dict:
        """What each acknowledged write was worth, judged against the device."""
        truth = self.w.inst.truth
        c = {"writes": 0, "items": 0, "ok": 0, "accepted": 0, "conflict": 0, "rate": 0, "full": 0,
             "failed": 0, "unanswered": 0,
             "ack_never_applied": 0, "ack_never_applied_fast": 0, "ack_before_effect": 0, "ack_untrue_at_response": 0,
             "failed_but_applied": 0, "conflict_but_applied": 0, "stale_intent": 0,
             "stale_intent_reported": 0, "toggle_like": 0}
        lat_gw: list[int] = []
        lat_resp: list[int] = []
        lat_effect: list[int] = []
        lat_conf: list[int] = []
        lead: list[int] = []
        lat_slow: dict[str, list[int]] = {"dim_value": [], "blind_pos": []}
        for r in self.writes:
            c["writes"] += 1
            if r.status is None or r.t_resp is None:
                c["unanswered"] += 1
                continue
            c[r.status] += 1
            c["items"] += len(r.items)
            fast_only = all(d.kind in FAST for d, _, _ in r.items)
            if r.status in ("ok", "accepted", "failed", "conflict") and fast_only:
                lat_resp.append(r.t_resp - r.t_call)
                lat_gw.append(r.t_resp_gw - r.t_gw_in)
            for idx, (d, target, _ifv) in enumerate(r.items):
                oid = r.ops[idx] if idx < len(r.ops) else None
                app = truth.applied.get(f"api:{oid}") if oid is not None else None
                op = self.ops.get(oid) if oid is not None else None
                if r.kind in ("toggle", "rmw"):
                    c["toggle_like"] += 1
                    if app is not None:
                        before = truth.at(d.key, app[0] - 1)
                        stale = _same(before, target) if r.kind == "toggle" else not _same(before, r.seen[idx])
                        if stale:
                            if r.status == "conflict":
                                c["stale_intent_reported"] += 1
                            else:
                                c["stale_intent"] += 1
                if r.status == "ok":
                    if app is None:
                        c["ack_never_applied"] += 1
                        c["ack_never_applied_fast"] += d.kind in FAST
                    elif app[0] > r.t_resp_gw:
                        c["ack_before_effect"] += 1
                        lead.append(app[0] - r.t_resp_gw)
                    elif not _same(truth.at(d.key, r.t_resp_gw), target):
                        c["ack_untrue_at_response"] += 1
                elif r.status == "failed" and app is not None:
                    c["failed_but_applied"] += 1
                elif r.status == "conflict" and app is not None:
                    c["conflict_but_applied"] += 1
                if d.kind in lat_slow and oid in self.evidence:
                    lat_slow[d.kind].append(self.evidence[oid] - r.t_gw_in)
                if d.kind in FAST and app is not None:
                    lat_effect.append(app[0] - r.t_gw_in)
                    if oid in self.evidence:
                        lat_conf.append(self.evidence[oid] - r.t_gw_in)
        c["latency_response_ms"] = _pcts(lat_resp)
        c["latency_gateway_ms"] = _pcts(lat_gw)
        c["latency_effect_ms"] = _pcts(lat_effect)
        c["latency_confirmed_ms"] = _pcts(lat_conf)
        c["ack_lead_ms"] = _pcts(lead)
        c["latency_confirmed_dimmer_value_ms"] = _pcts(lat_slow["dim_value"])
        c["latency_confirmed_blind_position_ms"] = _pcts(lat_slow["blind_pos"])
        self.raw = {"gateway": [x / 1e6 for x in lat_gw], "effect": [x / 1e6 for x in lat_effect],
                    "confirmed": [x / 1e6 for x in lat_conf]}
        return c


def _pct(sorted_vals: list[int], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, max(0, int(round(p / 100 * (len(sorted_vals) - 1)))))
    return float(sorted_vals[k])


def _pcts(vals: list[int]) -> dict:
    s = sorted(vals)
    return {"n": len(s), "p50": round(_pct(s, 50) / 1e6, 2), "p95": round(_pct(s, 95) / 1e6, 2),
            "p99": round(_pct(s, 99) / 1e6, 2), "max": round((s[-1] if s else 0) / 1e6, 2)}
