"""HTTP layer: a subset of the KNX IoT 3rd Party API 2.1.0 on top of GatewayCore.

Implemented paths (relative to BASE, see spec/knxiot_api_openapi.yaml for the full list):
  GET  /.well-known/knx                      (server root, no authentication)
  POST /oauth/access                         client credentials and refresh token grants
  GET  /locations, /locations/{id}, /locations/{id}/childlocations, /locations/{id}/functions
  GET  /functions, /functions/{id}, /functions/{id}/datapoints
  GET  /datapoints, /datapoints/{id}, /datapoints/{id}/functions
  PUT  /datapoints/values
  GET, POST /subscriptions; GET, DELETE /subscriptions/{id}; GET /subscriptions/{id}/datapoints
  GET  /messaging/ws                         WebSocket, sub-protocol gw.knx.org

Everything else in the specification answers 501 Not Implemented: devices, installations,
sites, node, timeseries, datafields, proxies, the certificate endpoints, the authorization
code grant, the query filters (type, tag, attribute, time), PATCH of subscriptions, node and
installation subscriptions, and reading or writing values over the WebSocket.

Vendor extensions, all inside JSON:API `meta` members with the prefix "gw:" or in headers the
specification does not define: freshness and version of every value, the pending marker,
conditional writes (meta "gw:ifVersion" per item), Retry-After on 429.
The envelope of WebSocket messages is this project's own: the OpenAPI file states that the
WebSocket data format is not part of it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from ..bus.installation import DatapointDef, Installation
from ..bus.tp1 import ga_str, ia_str
from ..simtime import Scheduler, sec
from .audit import AuditLog
from .auth import AuthError, AuthService, Principal
from .core import BatchResult, GatewayConfig, GatewayCore
from .notify import Notifier, SubscriptionError

API_VERSION = "2.1.0"
BASE = "/api/v2"
MEDIA = "application/vnd.api+json"
MAX_BODY = 64 * 1024
NOT_IMPLEMENTED = ("devices", "installations", "sites", "node", "timeseries", "datafields")


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ApiError(Exception):
    def __init__(self, status: int, title: str, detail: str = "", headers: Optional[dict] = None,
                 meta: Optional[dict] = None) -> None:
        super().__init__(detail or title)
        self.status, self.title, self.detail, self.headers, self.meta = status, title, detail, headers or {}, meta


def error_response(e: ApiError) -> JSONResponse:
    err: dict[str, Any] = {"title": e.title, "status": str(e.status)}
    if e.detail:
        err["detail"] = e.detail
    body: dict[str, Any] = {"errors": [err]}
    if e.meta:
        body["meta"] = e.meta
    return JSONResponse(body, status_code=e.status, media_type=MEDIA, headers=e.headers)


class Runtime:
    """One simulated installation plus gateway, tied to the wall clock for the live server."""

    def __init__(self, design: str = "versioned", seed: int = 1, speed: float = 1.0,
                 gw: Optional[dict] = None, inst: Optional[dict] = None, demo: bool = False,
                 audit_path: Optional[str] = None, manual_time: bool = False) -> None:
        self.sched = Scheduler()
        self.inst = Installation(self.sched, seed=seed, **(inst or {}))
        cfg = dict(polite=True, pace_util=0.4)        # chosen bus access, DOSSIER section 6.3
        cfg.update(gw or {})
        self.core = GatewayCore(self.inst, GatewayConfig(design=design, **cfg))
        self.core.prime()
        self.speed, self.demo, self.manual_time = speed, demo, manual_time
        # manual_time (tests): the clock is simulated time itself; `speed` only sets how far each
        # pump step moves it. Otherwise simulated time follows the wall clock, scaled by `speed`.
        self.tscale = 1.0 if manual_time else speed
        self.pump_step_s = 0.004 * speed
        self.t0_wall = time.time()
        self._mono0 = time.monotonic()
        self.auth = AuthService(self.inst.locations_under, lambda i: i in self.inst.locations, self.clock)
        self.audit = AuditLog(audit_path, self.clock)
        self.notifier = Notifier(self.clock)
        self.core.listeners.append(self._on_cache_event)
        self.buslog: list[dict] = []
        self.inst.line.monitors.append(self._on_bus)
        self.handler_s: list[float] = []

    def clock(self) -> float:
        return self.t0_wall + self.sched.now / 1e9 if self.manual_time else time.time()

    def wall(self, t_ns: int) -> float:
        return self.t0_wall + t_ns / 1e9 / self.tscale

    def freeze(self) -> None:
        """Tests: simulated time moves only through advance()."""
        self.pump_step_s = 0.0

    def sync(self) -> None:
        if not self.manual_time:
            self.sched.run_until(int((time.monotonic() - self._mono0) * 1e9 * self.speed))

    def advance(self, seconds: float) -> None:
        self.sched.run_until(self.sched.now + sec(seconds))
        self.notifier.flush()

    async def pump(self) -> None:
        while True:
            if self.manual_time:
                # tests: simulated time moves in fixed steps, independent of the wall clock
                self.sched.run_until(self.sched.now + int(self.pump_step_s * 1e9))
            else:
                self.sync()
            self.notifier.flush()
            await asyncio.sleep(0.004 if not self.manual_time else 0.001)

    # ---- serialisation ---------------------------------------------------------------
    def dp_item(self, d: DatapointDef, snap: Optional[dict] = None) -> dict:
        s = snap or self.core.snapshot(d.id)
        attrs: dict[str, Any] = {"title": d.title, "readable": True, "writable": d.writable,
                                 "valueType": "string"}
        v = d.dpt.fmt(s["value"])
        if v is not None:
            attrs["value"] = v
        attrs["timestamp"] = iso(self.wall(s["changed_at"]))
        if d.dpt.kind == "enum":
            attrs["enum"] = list(d.dpt.enum)
        else:
            attrs.update(unit=d.dpt.unit, minimum=d.dpt.minimum, maximum=d.dpt.maximum,
                         resolution=d.dpt.resolution)
        attrs["datapointType"] = d.tags
        attrs["knx:groupAddress"] = ga_str(d.cmd_ga if d.cmd_ga is not None else d.status_ga)
        meta: dict[str, Any] = {
            "@type": d.tags,
            "gw:version": s["version"],
            "gw:confirmedAt": None if s["confirmed_at"] is None else iso(self.wall(s["confirmed_at"])),
            "gw:ageMs": None if s["age_ns"] is None else round(s["age_ns"] / 1e6 / self.tscale, 1),
            "gw:source": s["source"],
        }
        if self.core.cfg.design == "versioned":
            meta["gw:pending"] = s["pending"]
            if s["pending"]:
                meta["gw:pendingTarget"] = d.dpt.fmt(s["pending_target"])
        return {"id": d.id, "type": "datapoint", "attributes": attrs, "meta": meta,
                "relationships": {"datapointFunctions": {"links": {"related": f"{BASE}/datapoints/{d.id}/functions"}}}}

    def fn_item(self, fid: str) -> dict:
        f = self.inst.functions[fid]
        return {"id": f.id, "type": "function", "attributes": {"title": f.title},
                "meta": {"@type": f.types},
                "relationships": {"functionLocation": {"links": {"related": f"{BASE}/functions/{f.id}/location"}},
                                  "functionDatapoints": {"links": {"related": f"{BASE}/functions/{f.id}/datapoints"}}}}

    def loc_item(self, lid: str) -> dict:
        l = self.inst.locations[lid]
        return {"id": l.id, "type": "location", "attributes": {"title": l.title},
                "meta": {"@type": [f"loc:{l.kind}"]},
                "relationships": {
                    "parentLocation": {"links": {"related": f"{BASE}/locations/{l.id}/parentlocation"}},
                    "childLocations": {"links": {"related": f"{BASE}/locations/{l.id}/childlocations"}},
                    "locationFunctions": {"links": {"related": f"{BASE}/locations/{l.id}/functions"}}}}

    def update_item(self, ev: dict) -> dict:
        d = self.inst.datapoints[ev["dp"]]
        return {"id": d.id, "type": "datapoint", "links": {"self": f"{BASE}/datapoints/{d.id}"},
                "attributes": {"value": d.dpt.fmt(ev["value"]), "timestamp": iso(self.wall(ev["t"]))},
                "meta": {"gw:version": ev["version"], "gw:source": ev["source"]}}

    def _on_cache_event(self, ev: dict) -> None:
        d = self.inst.datapoints[ev["dp"]]
        self.notifier.publish(self.update_item(ev), d.location)

    def _on_bus(self, rec: dict) -> None:
        f = rec["frame"]
        self.buslog.append({"n": len(self.buslog) + 1, "t_ms": round(rec["t_start"] / 1e6, 1),
                            "src": ia_str(f.src), "dst": ga_str(f.dst), "apci": f.apci,
                            "value": f.value if not isinstance(f.value, float) else round(f.value, 1),
                            "origin": f.origin, "ack": rec["ack"], "attempt": rec["attempt"]})
        if len(self.buslog) > 5000:
            del self.buslog[:1000]


def _collection(items: list, request: Request) -> dict:
    q = request.query_params
    try:
        size = int(q.get("page[size]", 65536))
        number = int(q.get("page[number]", 0))
    except ValueError:
        raise ApiError(400, "Bad Request", "page[size] and page[number] must be integers") from None
    if size < 1 or number < 0 or size > 2 ** 31 - 1 or number > 2 ** 31 - 1:
        raise ApiError(400, "Bad Request", "page[size] must be >= 1 and page[number] >= 0")
    page = items[number * size:(number + 1) * size]
    return {"meta": {"collection": {"number": number, "size": len(page), "total": len(items)}}, "data": page}


def create_app(rt: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(rt.pump())
        yield
        task.cancel()

    app = FastAPI(title="KNX IoT 3rd Party API gateway (study subset)", version=API_VERSION,
                  lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    inst: Installation = rt.inst
    core: GatewayCore = rt.core

    @app.exception_handler(ApiError)
    async def _api_error(_req: Request, e: ApiError) -> JSONResponse:
        return error_response(e)

    @app.middleware("http")
    async def _timing(request: Request, call_next):
        t = time.perf_counter()
        resp = await call_next(request)
        rt.handler_s.append(time.perf_counter() - t)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    def ok(body: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(body, status_code=status, media_type=MEDIA)

    def principal(request: Request, scope: str) -> Principal:
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer "):
            raise ApiError(401, "Unauthorized", "bearer token missing",
                           {"WWW-Authenticate": 'Bearer realm="knx"'})
        try:
            p = rt.auth.check(h[7:].strip())
        except AuthError as e:
            rt.audit.write("?", "token_check", request.url.path, "denied")
            raise ApiError(401, "Unauthorized", e.description,
                           {"WWW-Authenticate": 'Bearer error="invalid_token"'}) from None
        if not p.may(scope):
            rt.audit.write(p.client_id, "scope_check", request.url.path, "denied", needed=scope)
            raise ApiError(403, "Forbidden", f"scope {scope} is required")
        return p

    def get_dp(p: Principal, dp_id: str) -> DatapointDef:
        d = inst.datapoints.get(dp_id)
        if d is None:
            raise ApiError(404, "Not Found", "no such datapoint")
        if not p.sees(d.location):
            raise ApiError(403, "Forbidden", "datapoint is outside the granted locations")
        return d

    async def read(p: Principal, dps: list[DatapointDef]) -> list[dict]:
        rt.sync()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        core.read(p.client_id, [d.id for d in dps], lambda snap: fut.done() or fut.set_result(snap))
        try:
            snap = await asyncio.wait_for(fut, 3.0 + 1.0 / rt.tscale)
        except asyncio.TimeoutError:
            raise ApiError(504, "Gateway Timeout", "state could not be read in time") from None
        return [rt.dp_item(d, s) for d, s in zip(dps, snap)]

    # ---------------------------------------------------------------- information ------
    @app.get("/.well-known/knx")
    async def well_known() -> JSONResponse:
        return JSONResponse({"api": {"version": API_VERSION, "base": BASE}})

    # ---------------------------------------------------------------- authorization ----
    @app.post(BASE + "/oauth/access")
    async def oauth_access(request: Request) -> JSONResponse:
        def oauth_err(e: AuthError) -> JSONResponse:
            headers = {"Cache-Control": "no-store"}
            if e.status == 401:
                headers["WWW-Authenticate"] = 'Basic realm="knx"'
            if e.retry_after:
                headers["Retry-After"] = str(e.retry_after)
            return JSONResponse({"title": "token request refused", "status": str(e.status), "error": e.error,
                                 "error_description": e.description}, status_code=e.status, headers=headers)

        raw = await request.body()
        if len(raw) > 4096:
            return oauth_err(AuthError(400, "invalid_request", "request too large"))
        if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
            return oauth_err(AuthError(400, "invalid_request", "form encoding expected"))
        from urllib.parse import parse_qs
        try:
            form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), strict_parsing=True).items()}
        except (ValueError, UnicodeDecodeError):
            return oauth_err(AuthError(400, "invalid_request", "malformed form body"))
        h = request.headers.get("authorization", "")
        cid = secret = ""
        if h.lower().startswith("basic "):
            try:
                cid, _, secret = base64.b64decode(h[6:].strip(), validate=True).decode().partition(":")
            except Exception:
                return oauth_err(AuthError(401, "invalid_client", "malformed Basic credentials"))
        if not cid:
            return oauth_err(AuthError(401, "invalid_client", "client authentication required"))
        try:
            gt = form.get("grant_type", "")
            if gt == "client_credentials":
                tok = rt.auth.grant_client_credentials(cid, secret, form.get("scope", ""))
            elif gt == "refresh_token":
                tok = rt.auth.grant_refresh(cid, secret, form.get("refresh_token", ""))
            elif gt == "authorization_code":
                return oauth_err(AuthError(400, "unsupported_grant_type", "authorization code grant is not implemented"))
            else:
                return oauth_err(AuthError(400, "unsupported_grant_type", "unknown grant_type"))
        except AuthError as e:
            rt.audit.write(cid, "token_request", form.get("grant_type"), "denied", error=e.error)
            return oauth_err(e)
        rt.audit.write(cid, "token_request", form.get("grant_type"), "issued", scope=tok["scope"])
        return JSONResponse(tok, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    # ---------------------------------------------------------------- locations --------
    def visible_locs(p: Principal) -> list[str]:
        if p.locations is None:
            return list(inst.locations)
        # ancestors are listed too, otherwise the client could not walk the tree to its rooms
        keep = set(p.locations)
        for lid in list(keep):
            par = inst.locations[lid].parent
            while par:
                keep.add(par)
                par = inst.locations[par].parent
        return [l for l in inst.locations if l in keep]

    def get_loc(p: Principal, lid: str) -> str:
        if lid not in inst.locations:
            raise ApiError(404, "Not Found", "no such location")
        if lid not in visible_locs(p):
            raise ApiError(403, "Forbidden", "location is outside the granted locations")
        return lid

    @app.get(BASE + "/locations")
    async def locations(request: Request) -> JSONResponse:
        p = principal(request, "read")
        return ok(_collection([rt.loc_item(l) for l in visible_locs(p)], request))

    @app.get(BASE + "/locations/{lid}")
    async def location(lid: str, request: Request) -> JSONResponse:
        p = principal(request, "read")
        return ok({"data": rt.loc_item(get_loc(p, lid))})

    @app.get(BASE + "/locations/{lid}/childlocations")
    async def child_locations(lid: str, request: Request) -> JSONResponse:
        p = principal(request, "read")
        vis = set(visible_locs(p))
        return ok(_collection([rt.loc_item(c) for c in inst.locations[get_loc(p, lid)].children if c in vis], request))

    @app.get(BASE + "/locations/{lid}/functions")
    async def location_functions(lid: str, request: Request) -> JSONResponse:
        p = principal(request, "read")
        get_loc(p, lid)
        fns = inst.locations[lid].functions if p.sees(lid) else []
        return ok(_collection([rt.fn_item(f) for f in fns], request))

    # ---------------------------------------------------------------- functions --------
    @app.get(BASE + "/functions")
    async def functions(request: Request) -> JSONResponse:
        p = principal(request, "read")
        return ok(_collection([rt.fn_item(f.id) for f in inst.functions.values() if p.sees(f.location)], request))

    def get_fn(p: Principal, fid: str):
        f = inst.functions.get(fid)
        if f is None:
            raise ApiError(404, "Not Found", "no such function")
        if not p.sees(f.location):
            raise ApiError(403, "Forbidden", "function is outside the granted locations")
        return f

    @app.get(BASE + "/functions/{fid}")
    async def function(fid: str, request: Request) -> JSONResponse:
        p = principal(request, "read")
        return ok({"data": rt.fn_item(get_fn(p, fid).id)})

    @app.get(BASE + "/functions/{fid}/datapoints")
    async def function_datapoints(fid: str, request: Request) -> JSONResponse:
        p = principal(request, "read")
        f = get_fn(p, fid)
        return ok(_collection(await read(p, [inst.datapoints[i] for i in f.datapoints]), request))

    # ---------------------------------------------------------------- datapoints -------
    @app.get(BASE + "/datapoints")
    async def datapoints(request: Request) -> JSONResponse:
        p = principal(request, "read")
        for flt in ("filter[type]", "filter[tag]", "filter[attribute]", "filter[time]"):
            if any(k.startswith(flt.split("[")[0] + "[") for k in request.query_params):
                raise ApiError(501, "Not Implemented", "query filters are not implemented")
        dps = [d for d in inst.datapoints.values() if p.sees(d.location)]
        return ok(_collection(await read(p, dps), request))

    @app.put(BASE + "/datapoints/values")
    async def put_values(request: Request) -> Response:
        p = principal(request, "write")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in (MEDIA, "application/json"):
            raise ApiError(415, "Unsupported Media Type", f"use {MEDIA}")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ApiError(400, "Bad Request", "request body too large")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Bad Request", "body is not valid JSON") from None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            raise ApiError(400, "Bad Request", "member data must be a non-empty array")
        if len(data) > core.cfg.queue_max:
            raise ApiError(422, "Unprocessable Entity", f"at most {core.cfg.queue_max} datapoints per request")
        items: list[tuple[str, Any, Optional[int]]] = []
        seen: set[str] = set()
        log: list[dict] = []
        for it in data:
            if not isinstance(it, dict) or it.get("type") != "datapoint" or not isinstance(it.get("id"), str):
                raise ApiError(400, "Bad Request", "every item needs type 'datapoint' and an id")
            try:
                uuid.UUID(it["id"])
            except ValueError:
                raise ApiError(400, "Bad Request", "id is not a UUID") from None
            d = get_dp(p, it["id"])
            if not d.writable:
                raise ApiError(403, "Forbidden", f"datapoint {d.id} is read only")
            if d.id in seen:
                raise ApiError(422, "Unprocessable Entity", "a datapoint appears twice in the request")
            seen.add(d.id)
            attrs = it.get("attributes")
            if not isinstance(attrs, dict) or "value" not in attrs:
                raise ApiError(400, "Bad Request", "attributes.value is missing")
            try:
                value = d.dpt.parse(attrs["value"])
            except ValueError as e:
                rt.audit.write(p.client_id, "write", d.key, "invalid_value")
                raise ApiError(422, "Unprocessable Entity", f"{d.title}: {e}") from None
            ifv = None
            meta = it.get("meta")
            if isinstance(meta, dict) and "gw:ifVersion" in meta:
                ifv = meta["gw:ifVersion"]
                if isinstance(ifv, bool) or not isinstance(ifv, int) or ifv < 0:
                    raise ApiError(422, "Unprocessable Entity", "gw:ifVersion must be a non-negative integer")
            items.append((d.id, value, ifv))
            log.append({"dp": d.key, "value": attrs["value"]})
        rt.sync()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        core.write(p.client_id, items, lambda res: fut.done() or fut.set_result(res))
        try:
            res: BatchResult = await asyncio.wait_for(fut, 5.0 + 2.0 / rt.tscale)
        except asyncio.TimeoutError:
            rt.audit.write(p.client_id, "write", log, "timeout")
            raise ApiError(504, "Gateway Timeout", "no answer from the bus") from None
        rt.audit.write(p.client_id, "write", log, res.status)
        if res.status == "ok":
            return Response(status_code=204)
        if res.status == "accepted":
            return Response(status_code=202)
        meta = {"gw:items": res.items} if res.items else None
        if res.status == "conflict":
            raise ApiError(409, "Conflict", res.detail, meta=meta)
        if res.status in ("rate", "full"):
            raise ApiError(429, "Too Many Requests", res.detail,
                           {"Retry-After": str(max(1, int(res.retry_after_s / rt.tscale + 0.999)))})
        raise ApiError(504, "Gateway Timeout", res.detail, meta=meta)

    @app.get(BASE + "/datapoints/{dp_id}")
    async def datapoint(dp_id: str, request: Request) -> JSONResponse:
        p = principal(request, "read")
        d = get_dp(p, dp_id)
        item = (await read(p, [d]))[0]
        return JSONResponse({"data": item}, media_type=MEDIA, headers={"ETag": f'"{item["meta"]["gw:version"]}"'})

    @app.get(BASE + "/datapoints/{dp_id}/functions")
    async def datapoint_functions(dp_id: str, request: Request) -> JSONResponse:
        p = principal(request, "read")
        return ok(_collection([rt.fn_item(get_dp(p, dp_id).function)], request))

    # ---------------------------------------------------------------- subscriptions ----
    def sub_item(s) -> dict:
        return {"id": s.id, "type": "subscription",
                "attributes": {"url": s.url, "lifetime": max(0, int(s.expires - rt.clock()))},
                "relationships": {"subscriptionDatapoints": {"links": {"related": f"{BASE}/subscriptions/{s.id}/datapoints"}}}}

    def get_sub(p: Principal, sid: str):
        s = rt.notifier.subs.get(sid)
        if s is None or s.client_id != p.client_id:
            raise ApiError(404, "Not Found", "no such subscription")
        return s

    @app.get(BASE + "/subscriptions")
    async def subscriptions(request: Request) -> JSONResponse:
        p = principal(request, "manage")
        return ok(_collection([sub_item(s) for s in rt.notifier.subs.values() if s.client_id == p.client_id], request))

    @app.post(BASE + "/subscriptions")
    async def subscribe(request: Request) -> JSONResponse:
        p = principal(request, "manage")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ApiError(400, "Bad Request", "request body too large")
        try:
            body = json.loads(raw)
            data = body["data"]
            assert data["type"] == "subscription"
            attrs, rel = data["attributes"], data["relationships"]
            url, secret = attrs["url"], attrs["secret"]
            assert isinstance(url, str) and isinstance(secret, str)
        except (ValueError, KeyError, TypeError, AssertionError):
            raise ApiError(400, "Bad Request", "body does not match SubscribeRequest") from None
        if "subscriptionNode" in rel or "subscriptionInstallations" in rel:
            raise ApiError(501, "Not Implemented", "node and installation subscriptions are not implemented")
        try:
            wanted = rel["subscriptionDatapoints"]["data"]
            assert isinstance(wanted, list) and wanted
        except (KeyError, TypeError, AssertionError):
            raise ApiError(400, "Bad Request", "relationships.subscriptionDatapoints.data is missing") from None
        dps: set[str] = set()
        for w in wanted:
            if not isinstance(w, dict) or not isinstance(w.get("id"), str):
                raise ApiError(400, "Bad Request", "subscription item needs type and id")
            kind, i = w.get("type"), w["id"]
            expand = bool((w.get("meta") or {}).get("expand", False)) if isinstance(w.get("meta"), (dict, type(None))) else False
            if kind == "datapoint":
                dps.add(get_dp(p, i).id)
            elif kind == "function":
                dps.update(get_fn(p, i).datapoints)
            elif kind == "location":
                get_loc(p, i)
                locs = inst.locations_under(i) if expand else {i}
                dps.update(d.id for d in inst.datapoints.values() if d.location in locs and p.sees(d.location))
            else:
                raise ApiError(501, "Not Implemented", f"subscribing to type {str(kind)[:20]} is not implemented")
        client = rt.auth.clients[p.client_id]
        try:
            s = rt.notifier.create(p.client_id, client.callback_hosts, url, secret, dps,
                                   attrs.get("lifetime", 0) if isinstance(attrs.get("lifetime", 0), int) else 0)
        except SubscriptionError as e:
            rt.audit.write(p.client_id, "subscribe", url[:200], "denied", reason=e.detail)
            raise ApiError(e.status, "Subscription refused", e.detail) from None
        rt.audit.write(p.client_id, "subscribe", url[:200], "created", n=len(dps))
        return ok({"data": {"id": s.id, "type": "subscription",
                            "relationships": sub_item(s)["relationships"]}}, 201)

    @app.get(BASE + "/subscriptions/{sid}")
    async def subscription(sid: str, request: Request) -> JSONResponse:
        p = principal(request, "manage")
        return ok({"data": sub_item(get_sub(p, sid))})

    @app.delete(BASE + "/subscriptions/{sid}")
    async def unsubscribe(sid: str, request: Request) -> Response:
        p = principal(request, "manage")
        rt.notifier.delete(get_sub(p, sid).id)
        rt.audit.write(p.client_id, "unsubscribe", sid, "deleted")
        return Response(status_code=204)

    @app.get(BASE + "/subscriptions/{sid}/datapoints")
    async def subscription_datapoints(sid: str, request: Request) -> JSONResponse:
        p = principal(request, "manage")
        s = get_sub(p, sid)
        return ok(_collection([rt.dp_item(inst.datapoints[i]) for i in sorted(s.datapoints)], request))

    @app.websocket(BASE + "/messaging/ws")
    async def messaging(ws: WebSocket) -> None:
        offered = [x.strip() for x in ws.headers.get("sec-websocket-protocol", "").split(",") if x.strip()]
        if "gw.knx.org" not in offered:
            await ws.close(code=1002)
            return
        await ws.accept(subprotocol="gw.knx.org")
        token = ""
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), 5.0))
            token = first["token"] if first.get("type") == "auth" else ""
            p = rt.auth.check(token)
            if not p.may("manage"):
                raise AuthError(403, "insufficient_scope", "scope manage is required")
        except (AuthError, asyncio.TimeoutError, ValueError, KeyError, TypeError, WebSocketDisconnect):
            await ws.close(code=4401)
            return
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)

        def push(msg: dict) -> None:
            if q.full():
                q.get_nowait()
            q.put_nowait(msg)

        cid = id(ws)
        rt.notifier.ws[cid] = {"send": push, "principal": p, "dps": set()}
        rt.audit.write(p.client_id, "ws_open", None, "ok")

        async def sender() -> None:
            while True:
                msg = await q.get()
                try:
                    rt.auth.check(token)              # a revoked or expired token ends the stream
                except AuthError:
                    await ws.close(code=4401)
                    return
                await ws.send_text(json.dumps(msg))

        task = asyncio.create_task(sender())
        try:
            await ws.send_text(json.dumps({"type": "ready"}))
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("type") == "subscribe":
                    want = msg.get("datapoints")
                    if want == "all":
                        rt.notifier.ws[cid]["dps"] = None
                    elif isinstance(want, list):
                        rt.notifier.ws[cid]["dps"] = {i for i in want if isinstance(i, str)
                                                      and i in inst.datapoints and p.sees(inst.datapoints[i].location)}
                    await ws.send_text(json.dumps({"type": "subscribed"}))
                else:
                    await ws.send_text(json.dumps({"type": "error", "detail": "not implemented"}))
        except (WebSocketDisconnect, ValueError, RuntimeError):
            pass
        finally:
            task.cancel()
            rt.notifier.ws.pop(cid, None)

    # ---------------------------------------------------------------- demo only --------
    if rt.demo:
        from .demo import add_demo_routes
        add_demo_routes(app, rt)

    @app.api_route(BASE + "/{rest:path}", methods=["GET", "PUT", "POST", "PATCH", "DELETE"])
    async def not_implemented(rest: str, request: Request) -> JSONResponse:
        head = rest.split("/")[0]
        tail = rest.split("/")[-1]
        if head in NOT_IMPLEMENTED or tail in ("devices", "device", "datafields", "proxies", "timeseries",
                                               "location", "parentlocation", "installations", "node") \
                or (head == "subscriptions" and request.method == "PATCH"):
            raise ApiError(501, "Not Implemented", "this part of the KNX IoT 3rd Party API is not implemented here")
        raise ApiError(404, "Not Found", "unknown path")

    @app.get("/.well-known/knx/{cert}")
    async def certs(cert: str) -> JSONResponse:
        raise ApiError(501, "Not Implemented", "device certificates are not implemented")

    return app
