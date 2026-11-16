"""Regenerate everything in results/: summary.json and the figures.

    python scripts/run_all.py            full run, a few minutes
    python scripts/run_all.py --quick    short run for CI (same code paths, fewer hours and seeds)

All numbers in README.md and DOSSIER.md come from results/summary.json of the full run.
The only numbers that depend on the machine are under "http" (handler time of the real
FastAPI process); everything else is simulated time and reproduces exactly.
"""
from __future__ import annotations

import argparse
import warnings

warnings.filterwarnings("ignore", message="Using `httpx`")
import base64
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from knxgw.bus import tp1  # noqa: E402
from knxgw.bus.tp1 import Frame, Station, TP1Line, ga, ia  # noqa: E402
from knxgw.simtime import Scheduler  # noqa: E402
from knxgw.study import capacity, plots  # noqa: E402
from knxgw.study.consistency import PROFILES, race_scan, rmw_scan, run_mixed  # noqa: E402

LOSS = dict(p_rx_miss=1e-4, p_frame_corrupt=1e-3)       # assumption A4, swept below
POLICY = dict(polite=True, pace_util=1.0)               # bus sharing used for all three designs


def model_validation() -> dict:
    """Simulated back-to-back throughput against the formula from the KNX training material."""
    s = Scheduler()
    line = TP1Line(s, seed=1)

    class R(Station):
        cons: list

        def on_con(self, f, ok):
            self.cons.append(s.now)

    a = R(line, ia(1, 1, 1))
    a.cons = []
    b = Station(line, ia(1, 1, 2))
    b.group_table.add(ga(1, 1, 1))
    for _ in range(500):
        a.send(Frame(0, ga(1, 1, 1), "write", True))
    s.run()
    sim = 499 / ((a.cons[-1] - a.cons[0]) / 1e9)
    return {"formula_telegrams_per_s": round(tp1.max_telegrams_per_s(9), 4), "simulated_telegrams_per_s": round(sim, 4),
            "published_range_ms": [20, 40],
            "model_range_ms": [round(tp1.telegram_ms(8, "urgent"), 2), round(tp1.telegram_ms(23, "low"), 2)]}


def http_measurement(n: int) -> dict:
    """Handler time of the real HTTP layer on this machine (the only wall-clock measurement)."""
    from fastapi.testclient import TestClient

    from knxgw.gateway.api import BASE, MEDIA, Runtime, create_app

    rt = Runtime(design="optimistic", manual_time=True, inst={"sensors": False}, gw={"client_rate": 1e9, "client_burst": 10**9})
    app = create_app(rt)
    out = {}
    with TestClient(app) as c:
        cid, secret = rt.auth.register("bench", {"read", "write"})
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        tok = c.post(BASE + "/oauth/access", data={"grant_type": "client_credentials", "scope": "read write"},
                     headers={"Authorization": "Basic " + basic}).json()["access_token"]
        h = {"Authorization": "Bearer " + tok}
        d = rt.inst.dp("room1.light")

        def bench(name, fn):
            rt.handler_s.clear()
            for i in range(n):
                fn(i)
            xs = sorted(rt.handler_s)
            out[name] = {"n": len(xs), "p50_ms": round(xs[len(xs) // 2] * 1e3, 3),
                         "p95_ms": round(xs[int(len(xs) * 0.95)] * 1e3, 3)}

        bench("get_one_datapoint", lambda i: c.get(BASE + f"/datapoints/{d.id}", headers=h))
        bench("get_all_64_datapoints", lambda i: c.get(BASE + "/datapoints", headers=h))
        body = lambda i: json.dumps({"data": [{"id": d.id, "type": "datapoint",
                                               "attributes": {"value": "on" if i % 2 else "off"}}]})
        bench("put_one_value_optimistic", lambda i: c.put(BASE + "/datapoints/values", content=body(i),
                                                          headers={**h, "Content-Type": MEDIA}))
    out["note"] = "wall-clock time inside the FastAPI process on the development laptop; machine dependent"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--plots-only", action="store_true", help="redraw the figures from the saved results")
    a = ap.parse_args()
    q = a.quick
    out_dir = os.path.join(ROOT, "results")
    os.makedirs(out_dir, exist_ok=True)
    if a.plots_only:
        with open(os.path.join(out_dir, "summary.json"), encoding="utf-8") as f:
            S0 = json.load(f)
        with open(os.path.join(out_dir, "latency_samples.json"), encoding="utf-8") as f:
            raw0 = {tuple(k.split("/")): v for k, v in json.load(f).items()}
        plots.make_all(S0, raw0, out_dir)
        return
    t_start = time.time()
    S: dict = {"quick": q, "assumptions": {"loss": LOSS, "bus_policy_for_design_comparison": POLICY,
                                           "profiles_mean_seconds_between_events": PROFILES}}

    def step(name: str) -> None:
        print(f"[{time.time() - t_start:6.1f} s] {name}", flush=True)

    step("bus numbers and model validation")
    S["bus"] = capacity.bus_numbers()
    S["model_validation"] = model_validation()

    step("HTTP handler time")
    S["http"] = http_measurement(60 if q else 300)

    step("race scan")
    taus = [float(x) for x in range(-80, 121, 4 if q else 2)]
    S["race_scan"] = {}
    for name, design, gw in (("optimistic", "optimistic", POLICY), ("confirmed", "confirmed", POLICY),
                             ("versioned", "versioned", POLICY)):
        rows = race_scan(design, taus, seeds=4 if q else 10, gw=gw)
        bad = [r["tau_ms"] for r in rows if r["ok_false"]]
        flick = [r["tau_ms"] for r in rows if r["spurious"]]
        stale = [r["tau_ms"] for r in rows if r["readback_stale"]]
        S["race_scan"][name] = {
            "rows": rows,
            "false_confirm_window_ms": [min(bad), max(bad)] if bad else None,
            "false_confirm_probability_mass_ms": round(sum(r["ok_false"] / r["n"] for r in rows) * (taus[1] - taus[0]), 1),
            "conflict_reported_mass_ms": round(sum(r["conflict"] / r["n"] for r in rows) * (taus[1] - taus[0]), 1),
            "answered_before_effect_fraction": round(sum(r["ok_not_yet"] for r in rows) / sum(r["n"] for r in rows), 4),
            "flicker_window_ms": [min(flick), max(flick)] if flick else None,
            "stale_readback_window_ms": [min(stale), max(stale)] if stale else None,
        }

    step("lost-update scan")
    rt = [float(x) for x in range(-300, 801, 50 if q else 25)]
    S["rmw_scan"] = {}
    for name, design, gw in (("optimistic", "optimistic", POLICY), ("confirmed", "confirmed", POLICY),
                             ("versioned", "versioned", POLICY),
                             ("versioned_without_pending_rule", "versioned", dict(POLICY, cond_blocks_on_pending=False))):
        rows = rmw_scan(design, rt, seeds=2 if q else 5, gw=gw)
        lost = [r["tau_ms"] for r in rows if r["lost_silent"]]
        S["rmw_scan"][name] = {"rows": rows, "lost_window_ms": [min(lost), max(lost)] if lost else None,
                               "lost_width_ms": round(sum(r["lost_silent"] / r["n"] for r in rows) * (rt[1] - rt[0]), 0)}

    step("mixed traffic, three designs")
    hours = {"office": 20.0 if q else 300.0, "contended": 5.0 if q else 60.0}
    S["mixed"] = {}
    raw = {}
    for profile in ("office", "contended"):
        S["mixed"][profile] = {}
        for design in ("optimistic", "confirmed", "versioned"):
            r = run_mixed(design, profile, hours[profile], seed=1, gw=POLICY, inst=LOSS, keep_raw=True)
            raw[(profile, design)] = r.pop("_raw")
            S["mixed"][profile][design] = r
            step(f"  {profile} {design}")

    step("loss sensitivity")
    S["loss_sensitivity"] = {}
    for p in (0.0, 1e-4, 1e-3, 1e-2):
        S["loss_sensitivity"][str(p)] = {}
        for design in ("optimistic", "confirmed", "versioned"):
            r = run_mixed(design, "contended", 3.0 if q else 10.0, seed=2, gw=POLICY,
                          inst=dict(p_rx_miss=p, p_frame_corrupt=1e-3))
            w = r["writes"]
            S["loss_sensitivity"][str(p)][design] = {
                "writes_answered_ok": w["ok"], "ok_but_never_applied_fast": w["ack_never_applied_fast"],
                "failed_but_applied": w["failed_but_applied"], "failed": w["failed"],
                "stale_fraction_fast": r["staleness"]["fast"]["stale_time_fraction"],
                "longest_stale_s": r["staleness"]["fast"]["episode_max_s"],
                "missed_notifications": r["notifications"]["missed"],
                "repetitions": r["bus"]["repetitions"], "telegrams": r["bus"]["telegrams"]}

    step("refresh period trade")
    S["refresh_trade"] = []
    for period in (0.0, 600.0, 120.0, 30.0, 10.0):
        r = run_mixed("versioned", "contended", 3.0 if q else 10.0, seed=2,
                      gw=dict(POLICY, refresh_period_s=period), inst=dict(p_rx_miss=1e-3, p_frame_corrupt=1e-3))
        S["refresh_trade"].append({
            "refresh_period_s": period, "bus_load": r["bus"]["load_fraction"],
            "telegrams_per_s": r["bus"]["telegrams_per_s"],
            "stale_fraction_fast": r["staleness"]["fast"]["stale_time_fraction"],
            "longest_stale_s": r["staleness"]["fast"]["episode_max_s"],
            "wall_p99_ms": r["wall_latency_ms"]["p99"]})

    step("actuator reaction time sensitivity")
    S["reaction_sensitivity"] = []
    for lo, hi in ((2.0, 5.0), (8.0, 20.0), (30.0, 60.0)):
        r = run_mixed("confirmed", "contended", 2.0 if q else 5.0, seed=3, gw=POLICY, inst=dict(LOSS, react_ms=(lo, hi)))
        S["reaction_sensitivity"].append({"react_ms": [lo, hi],
                                          "confirmed_answer_ms": r["writes"]["latency_gateway_ms"]})

    step("capacity: scene burst")
    S["scene_burst"] = {k: capacity.scene_burst(k, seeds=18 if q else 90) for k in capacity.SHARING}

    step("capacity: write rate sweep")
    rates = [5.0, 15.0, 20.0, 30.0] if q else [2.0, 5.0, 10.0, 15.0, 18.0, 20.0, 22.0, 25.0, 30.0, 40.0]
    S["rate_sweep"] = {}
    for name, gw in (("unpaced_addr_1.1.250", dict(pace_util=1.0)), ("paced_50_addr_1.1.250", dict(pace_util=0.5)),
                     ("polite_addr_1.1.250", dict(polite=True, pace_util=1.0))):
        S["rate_sweep"][name] = capacity.write_rate_sweep(rates, seconds=20.0 if q else 60.0, gw=gw)

    step("capacity: polling and flooding")
    periods = [5.0, 1.5, 0.5] if q else [10.0, 5.0, 3.0, 2.0, 1.5, 1.0, 0.5]
    S["poll_everything"] = {"cached": capacity.poll_everything(periods, False, 10.0 if q else 30.0),
                            "read_through": capacity.poll_everything(periods, True, 10.0 if q else 30.0)}
    S["flooding_writer"] = [capacity.flooding_writer(f, l, 10.0 if q else 30.0)
                            for f, l in ((False, False), (True, False), (True, True))]

    from knxgw.bus.installation import Installation
    inst = Installation(Scheduler(), sensors=False)
    S["installation"] = {"datapoints": len(inst.datapoints), "writable": len(inst.writable()),
                         "functions": len(inst.functions), "locations": len(inst.locations),
                         "bus_devices_without_gateway": len(Installation(Scheduler()).line.stations)}
    S["blast_radius"] = {"token_for_floor_writable_datapoints": len(inst.writable()),
                         "token_for_one_room_writable_datapoints": len([d for d in inst.writable() if d.key.startswith("room1.")])}

    step("figures")
    if q:
        out_dir = os.path.join(out_dir, "quick")       # never overwrite the figures of the full run
        os.makedirs(out_dir, exist_ok=True)
    samples = {f"{p}/{d}": {k: [round(x, 1) for x in v] for k, v in r.items()} for (p, d), r in raw.items()}
    with open(os.path.join(out_dir, "latency_samples.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(samples, f, separators=(",", ":"), sort_keys=True)
    plots.make_all(S, raw, out_dir)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(S, f, indent=1, sort_keys=True)
    step(f"wrote {os.path.relpath(out_dir, ROOT)}/summary.json")


if __name__ == "__main__":
    main()
