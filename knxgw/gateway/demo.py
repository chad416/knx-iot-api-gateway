"""Routes for the demonstration web client. Only mounted when the server is started with --demo.

They expose what a real gateway cannot know or must not offer: the ground truth of the
simulated devices, a button that presses a simulated wall switch, a bus monitor, and a token
without a client secret. The server binds to 127.0.0.1 in demo mode.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..simtime import ms


def add_demo_routes(app: FastAPI, rt: Any) -> None:
    inst, core = rt.inst, rt.core
    cid, secret = rt.auth.register("demo web client", {"read", "write", "manage"})

    @app.get("/demo/config")
    async def config() -> JSONResponse:
        rooms = []
        for rk in inst.rooms:
            dps = {d.key.split(".", 1)[1]: d.id for d in inst.datapoints.values() if d.key.startswith(rk + ".")}
            loc = next(l for l in inst.locations.values() if any(
                inst.datapoints[i].key.startswith(rk + ".") for f in l.functions for i in inst.functions[f].datapoints))
            rooms.append({"key": rk, "title": loc.title, "datapoints": dps})
        return JSONResponse({"design": core.cfg.design, "speed": rt.speed, "base": "/api/v2", "rooms": rooms,
                             "gateway_address": f"1.1.{core.addr & 0xFF}", "polite": core.cfg.polite})

    @app.get("/demo/token")
    async def token() -> JSONResponse:
        return JSONResponse(rt.auth.grant_client_credentials(cid, secret, "read write manage"),
                            headers={"Cache-Control": "no-store"})

    @app.get("/demo/truth")
    async def truth() -> JSONResponse:
        rt.sync()
        st = inst.line.stats
        return JSONResponse({
            "now_ms": round(rt.sched.now / 1e6, 1),
            "truth": {d.key: d.dpt.fmt(inst.truth.last(d.key)) for d in inst.datapoints.values()},
            "bus": {"telegrams": st.telegrams, "repetitions": st.repetitions,
                    "load": round(st.busy_ns / max(1, rt.sched.now), 4)},
            "queue": core.q_len, "stats": core.stats})

    @app.get("/demo/buslog")
    async def buslog(after: int = 0) -> JSONResponse:
        rt.sync()
        return JSONResponse([e for e in rt.buslog[-200:] if e["n"] > after])

    @app.post("/demo/press")
    async def press(request: Request) -> JSONResponse:
        body = await request.json()
        key = str(body.get("key", ""))
        if key not in inst.by_key or not inst.by_key[key].writable or key.startswith("corridor"):
            return JSONResponse({"error": "unknown key"}, status_code=404)
        rt.sync()
        cur = inst.truth.last(key)
        value = (not cur) if isinstance(cur, bool) else (0.0 if cur > 50 else 100.0)
        inst.press(key, value, "wall:demo")
        return JSONResponse({"pressed": key, "value": inst.by_key[key].dpt.fmt(value)})

    @app.post("/demo/race")
    async def race(request: Request) -> JSONResponse:
        """API write and wall press with opposite intent, `tau_ms` apart in bus time."""
        body = await request.json()
        key = str(body.get("key", "room1.light"))
        tau = float(body.get("tau_ms", 15.0))
        if key not in inst.by_key or inst.by_key[key].kind != "switch" or key.startswith("corridor"):
            return JSONResponse({"error": "unknown key"}, status_code=404)
        rt.sync()
        d = inst.by_key[key]
        cur = bool(inst.truth.last(key))
        lead = ms(max(0.0, -tau))
        out: dict = {"key": key, "api_writes": d.dpt.fmt(cur), "wall_presses": d.dpt.fmt(not cur),
                     "tau_ms": tau, "log_after": len(rt.buslog)}
        rt.sched.after(lead + ms(max(0.0, tau)), inst.press, key, not cur, "wall:race")
        rt.sched.after(lead, core.write, "demo-race", [(d.id, cur, None)],
                       lambda res: out.__setitem__("api_result", res.status))
        return JSONResponse(out)
