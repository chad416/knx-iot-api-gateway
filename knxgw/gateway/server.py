"""Run the gateway with its simulated installation.

    python -m knxgw.gateway.server --demo --design versioned --speed 0.2

--speed scales bus time against the wall clock: 0.2 makes one telegram take about 100 ms, so
the races can be seen in the web client. Without --demo a client is provisioned at start and
its credentials are printed once; there is no built-in password.
"""
from __future__ import annotations

import argparse
import os
import random

import uvicorn
from fastapi.staticfiles import StaticFiles

from ..simtime import sec
from .api import Runtime, create_app
from .core import DESIGNS


def background_traffic(rt: Runtime, mean_s: float, seed: int = 7) -> None:
    """People press wall switches now and then."""
    rng = random.Random(seed)
    keys = [d.key for d in rt.inst.writable(("switch", "dim_switch")) if not d.key.startswith("corridor")]

    def fire() -> None:
        k = rng.choice(keys)
        rt.inst.press(k, not rt.inst.truth.last(k), "wall:bg")
        rt.sched.after(sec(rng.expovariate(1.0 / mean_s)), fire)

    rt.sched.after(sec(rng.expovariate(1.0 / mean_s)), fire)


def build(design: str, speed: float, demo: bool, traffic_s: float = 0.0, audit_path: str | None = None):
    rt = Runtime(design=design, speed=speed, demo=demo, audit_path=audit_path)
    if traffic_s > 0:
        background_traffic(rt, traffic_s)
    app = create_app(rt)
    dist = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web", "dist")
    if demo and os.path.isdir(dist):
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return rt, app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--design", choices=DESIGNS, default="versioned")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--demo", action="store_true", help="mount the demo routes and the web client (binds to 127.0.0.1)")
    ap.add_argument("--traffic", type=float, default=0.0, help="mean seconds between simulated wall presses")
    ap.add_argument("--audit", default=None, help="write the audit log to this file")
    a = ap.parse_args()
    rt, app = build(a.design, a.speed, a.demo, a.traffic, a.audit)
    if not a.demo:
        cid, secret = rt.auth.register("first client", {"read", "write", "manage"})
        print(f"provisioned client (shown once): client_id={cid} client_secret={secret}")
    # TLS is mandatory in the specification. Terminate it in front of this process or pass
    # --ssl-keyfile / --ssl-certfile to uvicorn; the demo runs plain HTTP on the loopback only.
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
