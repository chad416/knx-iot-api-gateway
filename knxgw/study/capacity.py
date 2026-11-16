"""Capacity and overload study.

Three questions:
  1. A scene for the whole floor arrives in one API call. How long does it take, and how long
     does a person at a wall switch wait meanwhile, for different ways of sharing the bus?
  2. An integration misbehaves: it polls everything, or it writes far too fast. What reaches
     the bus, and what happens to the other clients?
  3. Where is the limit: how many confirmed writes per second can the API promise?
"""
from __future__ import annotations

import random
from typing import Any, Optional

from ..bus.tp1 import ia, max_telegrams_per_s
from ..simtime import ms, sec
from .harness import FAST, SimClient, World, _pcts


def _lat(vals: list[float]) -> dict:
    fin = sorted(v for v in vals if v != float("inf"))
    if not fin:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "never": len(vals)}
    q = lambda p: round(fin[min(len(fin) - 1, int(round(p * (len(fin) - 1))))], 1)
    return {"n": len(fin), "p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "max": round(fin[-1], 1),
            "never": len(vals) - len(fin)}


SHARING = {
    # name: gateway options
    "unpaced_addr_1.1.250": dict(pace_util=1.0, addr=ia(1, 1, 250)),
    "unpaced_addr_1.1.255": dict(pace_util=1.0, addr=ia(1, 1, 255)),
    "paced_50_addr_1.1.250": dict(pace_util=0.5, addr=ia(1, 1, 250)),
    "paced_33_addr_1.1.250": dict(pace_util=1 / 3, addr=ia(1, 1, 250)),
    "paced_25_addr_1.1.250": dict(pace_util=0.25, addr=ia(1, 1, 250)),
    "polite_addr_1.1.250": dict(pace_util=1.0, addr=ia(1, 1, 250), polite=True),
    "polite_capped_addr_1.1.250": dict(pace_util=0.4, addr=ia(1, 1, 250), polite=True),
}


def scene_burst(sharing: str, seeds: int = 90, design: str = "confirmed", whole_floor: bool = True) -> dict:
    """One API call switches every light and dimmer on the floor (and moves dimmers and blinds if
    whole_floor). While the gateway works through it, one wall switch is pressed once, at a
    random moment in the first 0.75 s (the length of the burst if nothing else used the bus)."""
    wall_odd: list[float] = []
    wall_even: list[float] = []
    done_ms: list[float] = []
    statuses: dict[str, int] = {}
    nofb = 0
    n_items = 0
    for seed in range(1, seeds + 1):
        w = World(design, seed, dict(SHARING[sharing], queue_max=128, client_burst=200),
                  {"sensors": False})
        inst = w.inst
        c = SimClient(w, "automation", net_ms=60.0)
        rng = random.Random(f"burst:{seed}")
        rooms = [r for r in inst.rooms if r != "corridor"]
        items: list[tuple] = []
        for r in rooms:
            items.append((inst.dp(f"{r}.light"), True))
            items.append((inst.dp(f"{r}.dim.sw"), True))
        items.append((inst.dp("corridor.light"), True))
        if whole_floor:
            for r in rooms:
                items.append((inst.dp(f"{r}.dim.val"), inst.dp(f"{r}.dim.val").dpt.quantise(60.0)))
                items.append((inst.dp(f"{r}.blind"), 100.0))
        n_items = len(items)
        t0 = sec(1.0)
        box: dict[str, Any] = {}
        w.sched.at(t0, lambda: box.__setitem__("rec", c.write(items, "scene")))
        burst_s = n_items * 0.0204
        i = (seed - 1) % len(rooms)
        # the press goes to the light rocker of one room: wall switch address 1.1.(31+i)
        w.sched.at(t0 + ms(62.0) + sec(rng.uniform(0.0, burst_s)), _press, w, f"{rooms[i]}.light", "wall:1")
        w.run(45.0)
        rec = box["rec"]
        statuses[rec.status or "none"] = statuses.get(rec.status or "none", 0) + 1
        nofb += w.gw.stats["no_feedback"]
        last = 0
        for oid, op in w.obs.ops.items():
            if op.dp.kind in FAST and op.t_done:
                last = max(last, op.t_done)
        done_ms.append((last - rec.t_gw_in) / 1e6)
        app = inst.truth.applied.get("wall:1")
        lat = (app[0] - w.obs.wall["wall:1"]) / 1e6 if app else float("inf")
        (wall_odd if (31 + i) % 2 else wall_even).append(lat)
    return {"sharing": sharing, "items": n_items, "seeds": seeds,
            "fast_done_ms": _lat(done_ms), "wall_ms": _lat(wall_odd + wall_even),
            "wall_odd_addr_ms": _lat(wall_odd), "wall_even_addr_ms": _lat(wall_even),
            "no_feedback_timeouts": nofb, "response": statuses}


def _press(w: World, key: str, tag: str) -> None:
    w.obs.wall[tag] = w.sched.now
    w.inst.press(key, not w.inst.truth.last(key), tag)


def write_rate_sweep(rates: list[float], seconds: float = 60.0, seed: int = 1,
                     gw: Optional[dict] = None, design: str = "confirmed") -> list[dict]:
    """Poisson stream of single switch writes from 4 clients at a total rate, plus one wall press
    per second. Reports confirmed latency, rejections, bus load and wall-switch latency."""
    out = []
    for rate in rates:
        cfg = dict(queue_max=64, client_rate=1e6, client_burst=10**6)
        cfg.update(gw or {})
        w = World(design, seed, cfg, {"sensors": True})
        inst = w.inst
        clients = [SimClient(w, f"c{i}", net_ms=5.0, subscribe=False) for i in range(4)]
        rng = random.Random(f"sweep:{seed}:{rate}")
        dps = inst.writable(FAST)
        t = 1.0
        n = 0
        while t < 1.0 + seconds:
            t += rng.expovariate(rate)
            d = rng.choice(dps)
            w.sched.at(sec(t), lambda d=d, c=clients[n % 4], v=rng.random() < 0.5: c.write([(d, v)]))
            n += 1
        rooms = [r for r in inst.rooms if r != "corridor"]
        k = 0
        tw = 1.5
        while tw < 1.0 + seconds:
            k += 1
            w.sched.at(sec(tw), _press, w, f"{rng.choice(rooms)}.light", f"wall:{k}")
            tw += 1.0
        w.run(seconds + 6.0)
        cw = w.obs.classify_writes()
        ok = cw["ok"]
        out.append({"offered_per_s": rate, "sent": n, "ok": ok, "rejected": cw["full"] + cw["rate"],
                    "failed": cw["failed"], "ok_per_s": round(ok / seconds, 2),
                    "gateway_latency_ms": cw["latency_gateway_ms"],
                    "wall_ms": _lat(w.obs.wall_latencies_ms()),
                    "bus_load": round(inst.line.stats.busy_ns / w.sched.now, 3),
                    "q_max": w.gw.stats["q_max"]})
    return out


def poll_everything(periods_s: list[float], read_through: bool, seconds: float = 30.0, seed: int = 1) -> list[dict]:
    """A misbehaving integration reads all 37 actuator datapoints every `period`. With the cache
    the bus never sees it. With read-through every read is a GroupValueRead plus its response."""
    out = []
    for period in periods_s:
        w = World("confirmed", seed, dict(read_through=read_through, queue_max=64, pace_util=0.4, polite=True),
                  {"sensors": True})
        inst = w.inst
        bad = SimClient(w, "poller", net_ms=60.0, subscribe=False)
        good = SimClient(w, "app", net_ms=5.0, subscribe=False)
        dps = inst.writable()
        polls = {"n": 0, "answered": 0}
        lat: list[float] = []

        def poll() -> None:
            t_call = w.sched.now
            polls["n"] += 1

            def got(_snap: list, _t: int) -> None:
                polls["answered"] += 1
                lat.append((w.sched.now - t_call) / 1e6)

            bad.read(dps, got)
            w.sched.after(sec(period), poll)

        w.sched.at(sec(1.0), poll)
        rng = random.Random(f"poll:{seed}")
        rooms = [r for r in inst.rooms if r != "corridor"]
        k, t = 0, 1.2
        while t < 1.0 + seconds:
            k += 1
            w.sched.at(sec(t), _press, w, f"{rng.choice(rooms)}.light", f"wall:{k}")
            w.sched.at(sec(t + 0.5), lambda d=inst.dp(f"{rng.choice(rooms)}.dim.sw"), v=rng.random() < 0.5:
                       good.write([(d, v)]))
            t += 1.0
        w.run(seconds + 5.0)
        cw = w.obs.classify_writes()
        out.append({"period_s": period, "read_through": read_through,
                    "offered_bus_telegrams_per_s": round(2 * len(dps) / period, 1) if read_through else 0.0,
                    "polls": polls["n"], "polls_answered": polls["answered"], "poll_latency_ms": _lat(lat),
                    "reads_dropped_queue_full": w.gw.stats["rejected_full"],
                    "app_write_latency_ms": cw["latency_gateway_ms"], "app_writes_ok": cw["ok"],
                    "app_writes_rejected": cw["full"] + cw["rate"], "app_writes_failed": cw["failed"],
                    "wall_ms": _lat(w.obs.wall_latencies_ms()),
                    "bus_load": round(inst.line.stats.busy_ns / w.sched.now, 3)})
    return out


def flooding_writer(fair: bool, limited: bool, seconds: float = 30.0, seed: int = 1) -> dict:
    """An integration writes 100 times per second. A normal app writes once per second."""
    cfg = dict(queue_max=64, pace_util=0.4, polite=True, fair=fair)
    cfg.update(dict(client_rate=10.0, client_burst=40) if limited else dict(client_rate=1e6, client_burst=10**6))
    w = World("confirmed", seed, cfg, {"sensors": True})
    inst = w.inst
    bad = SimClient(w, "flooder", net_ms=60.0, subscribe=False)
    good = SimClient(w, "app", net_ms=5.0, subscribe=False)
    rng = random.Random(f"flood:{seed}")
    dps = inst.writable(FAST)
    t = 1.0
    while t < 1.0 + seconds:
        w.sched.at(sec(t), lambda d=rng.choice(dps), v=rng.random() < 0.5: bad.write([(d, v)]))
        t += 0.01
    rooms = [r for r in inst.rooms if r != "corridor"]
    good_recs = []
    k, t = 0, 1.3
    while t < 1.0 + seconds:
        k += 1
        w.sched.at(sec(t), _press, w, f"{rng.choice(rooms)}.light", f"wall:{k}")
        w.sched.at(sec(t + 0.4), lambda d=inst.dp(f"{rng.choice(rooms)}.dim.sw"), v=rng.random() < 0.5:
                   good_recs.append(good.write([(d, v)])))
        t += 1.0
    w.run(seconds + 6.0)
    g_ok = [r for r in good_recs if r.status == "ok"]
    g_lat = sorted(r.t_resp_gw - r.t_gw_in for r in g_ok)
    b = [r for r in w.obs.writes if r.client == "flooder"]
    return {"fair_queue": fair, "client_rate_limit": limited,
            "app_writes": len(good_recs), "app_ok": len(g_ok),
            "app_rejected": sum(r.status in ("full", "rate") for r in good_recs),
            "app_latency_ms": _pcts(g_lat),
            "flooder_sent": len(b), "flooder_ok": sum(r.status == "ok" for r in b),
            "flooder_rejected": sum(r.status in ("full", "rate") for r in b),
            "wall_ms": _lat(w.obs.wall_latencies_ms()),
            "bus_load": round(inst.line.stats.busy_ns / w.sched.now, 3)}


def bus_numbers() -> dict:
    from ..bus.tp1 import BIT_NS, telegram_ms
    return {"bit_time_us": BIT_NS / 1000.0,
            "telegram_ms_1bit_value": round(telegram_ms(9), 3),
            "telegram_ms_1byte_value": round(telegram_ms(10), 3),
            "telegram_ms_2byte_value": round(telegram_ms(11), 3),
            "telegram_ms_min_8_chars": round(telegram_ms(8, "urgent"), 3),
            "telegram_ms_max_23_chars": round(telegram_ms(23), 3),
            "max_telegrams_per_s_1bit_value": round(max_telegrams_per_s(9), 2),
            "max_confirmed_writes_per_s": round(max_telegrams_per_s(9) / 2, 2)}
