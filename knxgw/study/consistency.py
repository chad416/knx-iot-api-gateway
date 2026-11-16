"""Consistency study: race scan (controlled) and mixed traffic (statistical).

Anomalies, as measured here against the ground truth of the simulated devices:

  A1 stale read            a read returns a value that is not the device state at that moment
  A2 untrue acknowledge    the call returned success, but the device never took the value
                           (ack_never_applied), had not taken it yet (ack_before_effect), or had
                           already been switched to something else when the client got the
                           answer (ack_untrue_at_response)
  A3 lost update           a write that was computed from a state that was no longer the device
                           state when the telegram took effect, and nobody was told (stale_intent)
  A4 unfaithful stream     subscribers are told a value sequence the device never went through
                           (spurious) or are not told a change that happened (missed)
  A5 lost telegram         a frame is not received by one station although the sender got a
                           link-layer acknowledge from another group member
"""
from __future__ import annotations

import random
from typing import Any, Optional

from ..gateway.core import _same
from ..simtime import ms, sec
from .harness import FAST, SimClient, World, WriteRec, _pcts

# Mean seconds between events. Assumptions (DOSSIER section 7, A6): there is no public data set
# of switch presses per room. "office" is a busy working hour, "contended" is a stress profile
# in which people at the wall and people on the phone act on the same light at the same time.
PROFILES: dict[str, dict] = {
    "office": dict(wall_light_s=600.0, wall_dim_s=900.0, wall_blind_s=1800.0, corridor_s=120.0,
                   app_users=2, app_s=120.0, voice_s=300.0, scene_s=600.0, rmw_s=300.0, coincide=0.0),
    "contended": dict(wall_light_s=60.0, wall_dim_s=120.0, wall_blind_s=600.0, corridor_s=30.0,
                      app_users=3, app_s=20.0, voice_s=60.0, scene_s=120.0, rmw_s=60.0, coincide=0.3),
}


def _rng(seed: int, name: str) -> random.Random:
    return random.Random(f"{seed}:{name}")


class MixedTraffic:
    def __init__(self, world: World, profile: str, seed: int) -> None:
        self.w, self.p, self.seed = world, PROFILES[profile], seed
        inst = world.inst
        self.reads: list[dict] = []
        self.n_wall = 0
        self.apps = [SimClient(world, f"app{i + 1}", net_ms=5.0, use_versions=True)
                     for i in range(self.p["app_users"])]
        self.voice = SimClient(world, "voice", net_ms=60.0, use_versions=False)
        self.auto = SimClient(world, "automation", net_ms=60.0, use_versions=False)
        self.rooms = [r for r in inst.rooms if r != "corridor"]
        w = self.p
        for r in self.rooms:
            self._poisson(_rng(seed, f"wl:{r}"), w["wall_light_s"], self._wall, f"{r}.light")
            self._poisson(_rng(seed, f"wd:{r}"), w["wall_dim_s"], self._wall, f"{r}.dim.sw")
            self._poisson(_rng(seed, f"wb:{r}"), w["wall_blind_s"], self._wall, f"{r}.blind")
        self._poisson(_rng(seed, "corr"), w["corridor_s"], self._corridor)
        for i, a in enumerate(self.apps):
            self._poisson(_rng(seed, f"app{i}"), w["app_s"], self._app_action, a, _rng(seed, f"appc{i}"))
        self._poisson(_rng(seed, "voice"), w["voice_s"], self._voice, _rng(seed, "voicec"))
        self._poisson(_rng(seed, "scene"), w["scene_s"], self._scene, _rng(seed, "scenec"))
        self._poisson(_rng(seed, "rmw"), w["rmw_s"], self._rmw, _rng(seed, "rmwc"))
        self._co_rng = _rng(seed, "coincide")

    def _poisson(self, rng: random.Random, mean_s: float, fn: Any, *args: Any) -> None:
        s = self.w.sched

        def fire() -> None:
            fn(*args)
            s.after(sec(rng.expovariate(1.0 / mean_s)), fire)

        s.after(sec(rng.expovariate(1.0 / mean_s)), fire)

    # ---- people at the wall ---------------------------------------------------------
    def _wall(self, key: str) -> None:
        inst, s = self.w.inst, self.w.sched
        d = inst.dp(key)
        if self.p["coincide"] and d.kind in FAST and self._co_rng.random() < self.p["coincide"]:
            # somebody else acts on the same light from the phone within +-150 ms
            off = self._co_rng.uniform(-150.0, 150.0)
            app = self.apps[self._co_rng.randrange(len(self.apps))]
            s.after(ms(150.0 + off), self._toggle, app, d)
            s.after(ms(150.0), self._press, key)
        else:
            self._press(key)

    def _press(self, key: str) -> None:
        inst = self.w.inst
        d = inst.dp(key)
        cur = inst.truth.last(key)
        value: Any = (not cur) if d.kind in FAST else (0.0 if cur > 50 else 100.0)
        self.n_wall += 1
        tag = f"wall:{self.n_wall}"
        if d.kind in FAST:
            self.w.obs.wall[tag] = self.w.sched.now
        inst.press(key, value, tag)

    def _corridor(self) -> None:
        det = self.w.inst.detectors["corridor"]
        det.trigger(not self.w.inst.truth.last("corridor.light"))

    # ---- API clients ----------------------------------------------------------------
    def _toggle(self, app: SimClient, d: Any) -> None:
        seen = app.view[d.id][0]
        app.write([(d, not seen)], "toggle", lambda rec: self._after_write(app, rec, 0.5))

    def _app_action(self, app: SimClient, rng: random.Random) -> None:
        inst = self.w.inst
        kind = rng.choices(("switch", "dim_switch", "dim_value", "blind_pos"), (4, 2, 2, 1))[0]
        d = inst.dp(f"{rng.choice(self.rooms)}." + {"switch": "light", "dim_switch": "dim.sw",
                                                     "dim_value": "dim.val", "blind_pos": "blind"}[kind])
        if kind in FAST:
            self._toggle(app, d)
            return
        seen = app.view[d.id][0]
        opts = [v for v in ((20.0, 40.0, 60.0, 80.0, 100.0) if kind == "dim_value" else (0.0, 50.0, 100.0))
                if not _same(v, seen)]
        app.write([(d, d.dpt.quantise(rng.choice(opts)))], "explicit",
                  lambda rec: self._after_write(app, rec, 0.5))

    def _voice(self, rng: random.Random) -> None:
        d = self.w.inst.dp(f"{rng.choice(self.rooms)}." + rng.choice(("light", "dim.sw")))
        self.voice.write([(d, rng.random() < 0.5)], "explicit",
                         lambda rec: self._after_write(self.voice, rec, 1.0))

    def _scene(self, rng: random.Random) -> None:
        rooms = rng.sample(self.rooms, 2)
        on = rng.random() < 0.5
        items = [(self.w.inst.dp(f"{r}.{k}"), on) for r in rooms for k in ("light", "dim.sw")]
        self.auto.write(items, "scene")

    def _rmw(self, rng: random.Random) -> None:
        d = self.w.inst.dp(f"{rng.choice(self.rooms)}.dim.val")

        def got(snap: list[dict], _t: int) -> None:
            base = snap[0]["value"]
            new = d.dpt.quantise(20.0 if base >= 100 else min(100.0, base + 20.0))
            cond = self.w.gw.cfg.design == "versioned"
            rec = self.auto.write([(d, new, snap[0]["version"] if cond else None)], "rmw")
            rec.seen = [base]

        self.auto.read([d], got)

    def _after_write(self, c: SimClient, rec: WriteRec, p_read: float) -> None:
        if rec.status == "conflict":
            c.read([it[0] for it in rec.items], lambda snap, _t: [
                c.view.__setitem__(x["dp"], (x["value"], x["version"])) for x in snap])
            return
        if rec.status not in ("ok", "accepted") or self._co_rng.random() >= p_read:
            return
        d, target, _ = rec.items[0]

        def got(snap: list[dict], t_gw: int) -> None:
            x = snap[0]
            truth_now = self.w.inst.truth.at(d.key, t_gw)
            flagged = x["pending"] and self.w.gw.cfg.design == "versioned"   # only D3 shows the marker
            self.reads.append({"kind": d.kind, "status": rec.status,
                               "stale": not _same(x["value"], truth_now),
                               "ryw_broken": not _same(x["value"], target) and not flagged,
                               "flagged": flagged})

        c.read([d], got)

    def read_metrics(self) -> dict:
        out: dict[str, Any] = {}
        for cls, kinds in (("fast", FAST), ("slow", ("dim_value", "blind_pos"))):
            rs = [r for r in self.reads if r["kind"] in kinds]
            out[cls] = {"n": len(rs), "stale": sum(r["stale"] for r in rs),
                        "ryw_broken": sum(r["ryw_broken"] for r in rs),
                        "stale_but_flagged_pending": sum(r["stale"] and r["flagged"] for r in rs)}
        return out


def run_mixed(design: str, profile: str, hours: float, seed: int = 1,
              gw: Optional[dict] = None, inst: Optional[dict] = None, keep_raw: bool = False) -> dict:
    world = World(design, seed, gw, inst)
    traffic = MixedTraffic(world, profile, seed)
    world.run(hours * 3600.0)
    obs = world.obs
    line = world.inst.line
    wl = sorted(x for x in obs.wall_latencies_ms() if x != float("inf"))
    res = {
        "design": design, "profile": profile, "hours": hours, "seed": seed,
        "writes": obs.classify_writes(),
        "reads_after_write": traffic.read_metrics(),
        "staleness": obs.staleness(),
        "notifications": obs.notification_faithfulness(),
        "gateway": dict(world.gw.stats),
        "bus": {"telegrams": line.stats.telegrams, "repetitions": line.stats.repetitions,
                "failed": line.stats.failed,
                "load_fraction": round(line.stats.busy_ns / world.sched.now, 5),
                "telegrams_per_s": round(line.stats.telegrams / (hours * 3600.0), 3)},
        "wall_presses": traffic.n_wall,
        "wall_latency_ms": {"n": len(wl), "p50": round(wl[len(wl) // 2], 2) if wl else 0,
                            "p99": round(wl[int(len(wl) * 0.99)], 2) if wl else 0,
                            "max": round(wl[-1], 2) if wl else 0},
    }
    if keep_raw:
        res["_raw"] = obs.raw
    return res


# ------------------------------------------------------------------------------- race scan --
def race_scan(design: str, taus_ms: list[float], seeds: int = 5, gw: Optional[dict] = None,
              net_ms: float = 5.0) -> list[dict]:
    """A light is off. An API client sends "off" (part of an all-off scene) at t = 0; a person
    presses "on" at the wall at t = tau. For every tau: what was the client told, and was it true?"""
    out = []
    for tau in taus_ms:
        agg = {"tau_ms": tau, "n": 0, "ok_true": 0, "ok_not_yet": 0, "ok_false": 0, "conflict": 0,
               "failed": 0, "spurious": 0, "readback_stale": 0, "final_on": 0}
        for seed in range(1, seeds + 1):
            w = World(design, seed, gw, {"sensors": False})
            c = SimClient(w, "app", net_ms=net_ms, use_versions=False)
            d = w.inst.dp("room1.light")
            t0 = sec(1.0)
            box: dict[str, WriteRec] = {}
            def back(rec: WriteRec) -> None:
                if rec.status == "ok":
                    c.read([d], lambda snap, t_gw: box.__setitem__(
                        "stale", not _same(snap[0]["value"], w.inst.truth.at(d.key, t_gw))))

            w.sched.at(t0, lambda: box.__setitem__("rec", c.write([(d, False)], "explicit", back)))
            w.sched.at(t0 + ms(tau), w.inst.press, "room1.light", True, "wall:1")
            w.run(4.0)
            rec = box["rec"]
            truth = w.inst.truth
            agg["n"] += 1
            if rec.status == "ok":
                app = truth.applied.get(f"api:{rec.ops[0]}")
                if app is None or app[0] > rec.t_resp_gw:
                    agg["ok_not_yet"] += 1          # answered before the device did anything
                elif _same(truth.at(d.key, rec.t_resp_gw), False):
                    agg["ok_true"] += 1
                else:
                    agg["ok_false"] += 1            # answered "done" when the light was already on again
            elif rec.status == "conflict":
                agg["conflict"] += 1
            else:
                agg["failed"] += 1
            agg["spurious"] += w.obs.notification_faithfulness()["spurious"]
            agg["readback_stale"] += bool(box.get("stale", False))
            agg["final_on"] += bool(truth.last(d.key))
        out.append(agg)
    return out


def rmw_scan(design: str, taus_ms: list[float], seeds: int = 3, gw: Optional[dict] = None) -> list[dict]:
    """Two API clients each raise one dimmer by 20 % (read, add, write), the second tau later.
    Serial execution ends at 80 %. A final 60 % with both clients told "fine" is a lost update."""
    out = []
    for tau in taus_ms:
        agg = {"tau_ms": tau, "n": 0, "final_80": 0, "lost_silent": 0, "lost_reported": 0}
        for seed in range(1, seeds + 1):
            w = World(design, seed, gw, {"sensors": False})
            d = w.inst.dp("room1.dim.val")
            boot = SimClient(w, "boot", net_ms=1.0)
            boot.write([(d, d.dpt.quantise(40.0))])
            w.run(3.0)
            cond = design == "versioned"
            told: dict[str, str] = {}

            def step(c: SimClient, tries: int = 2) -> None:
                def got(snap: list[dict], _t: int) -> None:
                    new = d.dpt.quantise(min(100.0, snap[0]["value"] + 20.0))

                    def done(rec: WriteRec) -> None:
                        told[c.name] = rec.status
                        if rec.status == "conflict" and tries > 1:
                            w.sched.after(ms(600), step, c, tries - 1)

                    c.write([(d, new, snap[0]["version"] if cond else None)], "rmw", done)
                c.read([d], got)

            a, b = SimClient(w, "A", net_ms=5.0), SimClient(w, "B", net_ms=60.0)
            t0 = w.sched.now + sec(0.5)
            w.sched.at(t0, step, a)
            w.sched.at(t0 + ms(tau), step, b)
            w.run(8.0)
            final = w.inst.truth.last(d.key)
            agg["n"] += 1
            if _same(final, d.dpt.quantise(80.0)):
                agg["final_80"] += 1
            elif any(v == "conflict" for v in told.values()):
                agg["lost_reported"] += 1
            else:
                agg["lost_silent"] += 1
        out.append(agg)
    return out
