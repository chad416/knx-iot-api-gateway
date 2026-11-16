"""Security controls of the threat model (DOSSIER section 9), each shown working."""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from knxgw.gateway.api import MEDIA
from knxgw.gateway.audit import AuditLog
from knxgw.gateway.auth import DEFAULT_TTL_S, LOCK_AFTER, AuthError
from knxgw.gateway.notify import sign_callback, verify_callback


# ---- T1 no token, wrong token ---------------------------------------------------------------
@pytest.mark.parametrize("path", ["/locations", "/functions", "/datapoints", "/subscriptions"])
def test_every_resource_needs_a_token(api, path):
    r = api.http.get(api.base + path)
    assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    assert api.http.get(api.base + path, headers={"Authorization": "Bearer guess"}).status_code == 401


def test_only_the_version_document_is_readable_without_authentication(api):
    r = api.http.get("/.well-known/knx")
    assert r.status_code == 200 and set(r.json()) == {"api"}       # no device or software details
    for route in api.app.routes:
        path = getattr(route, "path", "")
        assert not path.startswith("/demo"), "demo routes must not exist outside demo mode"
        assert path not in ("/docs", "/redoc", "/openapi.json")


# ---- T2 over-broad scopes -------------------------------------------------------------------
def test_scopes_do_not_include_each_other(api):
    d = api.dp("room1.light")
    read, write, manage = api.token(scope="read"), api.token(scope="write"), api.token(scope="manage")
    assert api.get("/datapoints", read).status_code == 200
    assert api.put_values(read, [(d.id, "on")]).status_code == 403
    assert api.put_values(write, [(d.id, "on")]).status_code == 204
    assert api.get("/datapoints", write).status_code == 403
    assert api.get("/subscriptions", manage).status_code == 200
    assert api.get("/subscriptions", read).status_code == 403
    assert api.get("/datapoints", manage).status_code == 403


def test_client_cannot_ask_for_more_than_it_was_registered_for(api):
    cid, secret = api.client(scopes=("read",))
    assert api.token_response(cid, secret, "read write").json()["error"] == "invalid_scope"
    r = api.token_response(cid, secret, "read")
    assert r.status_code == 200 and r.json()["scope"] == "read"


def test_location_scoped_token_sees_and_controls_one_room_only(api):
    room1, room2 = api.loc_of("room1.light"), api.loc_of("room2.light")
    t = api.token(locations={room1})
    dps = api.get("/datapoints", t).json()
    assert dps["meta"]["collection"]["total"] == 7
    assert api.put_values(t, [(api.dp("room1.light").id, "on")]).status_code == 204
    assert api.put_values(t, [(api.dp("room2.light").id, "on")]).status_code == 403
    assert api.get(f"/datapoints/{api.dp('room2.light').id}", t).status_code == 403
    assert api.get(f"/locations/{room2}", t).status_code == 403
    assert api.get(f"/locations/{room1}/functions", t).json()["meta"]["collection"]["total"] == 4


def test_batch_with_one_datapoint_outside_the_scope_sends_nothing(api):
    t = api.token(locations={api.loc_of("room1.light")})
    frames = api.rt.inst.line.stats.frames
    r = api.put_values(t, [(api.dp("room1.light").id, "on"), (api.dp("room2.light").id, "on")])
    api.rt.advance(1.0)
    assert r.status_code == 403
    assert api.rt.inst.line.stats.frames == frames
    assert api.rt.inst.truth.last("room1.light") is False


def test_token_can_be_narrowed_with_loc_scope_but_not_widened(api):
    room1, room2 = api.loc_of("room1.light"), api.loc_of("room2.light")
    cid, secret = api.client(locations={room1})
    assert api.token_response(cid, secret, f"read loc:{room2}").json()["error"] == "invalid_scope"
    cid2, secret2 = api.client()
    tok = api.token_response(cid2, secret2, f"read write loc:{room1}").json()["access_token"]
    assert api.get("/datapoints", tok).json()["meta"]["collection"]["total"] == 7
    assert api.put_values(tok, [(api.dp("room2.light").id, "on")]).status_code == 403


def test_blast_radius_of_a_stolen_token(api):
    floor = api.token()
    room = api.token(locations={api.loc_of("room1.light")})
    count = lambda t: sum(i["attributes"]["writable"] for i in api.get("/datapoints", t).json()["data"])
    assert count(floor) == 37 and count(room) == 4


# ---- T3 token theft -------------------------------------------------------------------------
def test_access_token_expires(api):
    cid, secret = api.client()
    tok = api.token_response(cid, secret, "read time_60").json()
    assert tok["expires_in"] == 60
    assert api.get("/datapoints", tok["access_token"]).status_code == 200
    api.rt.advance(61)
    assert api.get("/datapoints", tok["access_token"]).status_code == 401


def test_default_lifetime_is_short_and_cannot_be_extended(api):
    cid, secret = api.client()
    assert api.token_response(cid, secret, "read").json()["expires_in"] == DEFAULT_TTL_S
    assert api.token_response(cid, secret, "read time_999999").json()["expires_in"] == DEFAULT_TTL_S


def test_refresh_token_rotates_and_reuse_revokes_the_family(api):
    cid, secret = api.client()
    first = api.token_response(cid, secret, "read").json()
    second = api.token_response(cid, secret, grant_type="refresh_token", refresh_token=first["refresh_token"])
    assert second.status_code == 200
    second = second.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert api.get("/datapoints", second["access_token"]).status_code == 200
    # the thief (or the victim) presents the old refresh token again
    again = api.token_response(cid, secret, grant_type="refresh_token", refresh_token=first["refresh_token"])
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert api.get("/datapoints", second["access_token"]).status_code == 401
    assert api.get("/datapoints", first["access_token"]).status_code == 401


def test_refresh_token_of_another_client_is_refused(api):
    cid, secret = api.client()
    other, other_secret = api.client()
    first = api.token_response(cid, secret, "read").json()
    r = api.token_response(other, other_secret, grant_type="refresh_token", refresh_token=first["refresh_token"])
    assert r.status_code == 400


def test_secrets_and_tokens_are_not_stored_or_logged_in_clear(api):
    cid, secret = api.client()
    tok = api.token_response(cid, secret, "read write").json()
    api.put_values(tok["access_token"], [(api.dp("room1.light").id, "on")])
    store = repr(api.rt.auth.clients) + repr(api.rt.auth.tokens) + repr(api.rt.auth.events)
    log = json.dumps(api.rt.audit.records)
    for s in (secret, tok["access_token"], tok["refresh_token"]):
        assert s not in store and s not in log


def test_each_provisioned_client_gets_its_own_random_secret(api):
    secrets_ = {api.client()[1] for _ in range(20)}
    assert len(secrets_) == 20 and all(len(s) >= 40 for s in secrets_)


# ---- T4 brute force -------------------------------------------------------------------------
def test_wrong_secret_locks_the_client_and_the_lock_doubles(api):
    cid, secret = api.client()
    for _ in range(LOCK_AFTER):
        assert api.token_response(cid, "wrong").status_code == 401
    locked = api.token_response(cid, secret)                 # even the right secret waits now
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) >= 55
    api.rt.advance(61)
    for _ in range(LOCK_AFTER):
        api.token_response(cid, "wrong")
    assert int(api.token_response(cid, secret).headers["retry-after"]) >= 115
    api.rt.advance(121)
    assert api.token_response(cid, secret).status_code == 200
    assert api.rt.auth.failed_auth_count(window_s=3600) >= 2 * LOCK_AFTER


# ---- T5 compromised cloud integration ------------------------------------------------------
def test_disabling_a_client_stops_its_tokens_at_once(api):
    cid, secret = api.client()
    tok = api.token_response(cid, secret, "read write").json()["access_token"]
    assert api.get("/datapoints", tok).status_code == 200
    api.rt.auth.disable(cid)
    assert api.get("/datapoints", tok).status_code == 401
    assert api.token_response(cid, secret).status_code == 401


def test_write_rate_limit_per_client_and_per_datapoint(make_api):
    a = make_api(design="optimistic", gw={"client_rate": 5.0, "client_burst": 5, "dp_min_interval_ms": 0.0})
    t = a.token()
    a.rt.freeze()
    codes = [a.put_values(t, [(a.dp(f"room{i % 9 + 1}.light").id, "on")]).status_code for i in range(12)]
    assert codes.count(429) >= 5 and codes[:5] == [204] * 5
    b = make_api(design="optimistic", gw={"dp_min_interval_ms": 1000.0})
    t = b.token()
    b.rt.freeze()
    d = b.dp("room1.light")
    r1, r2 = b.put_values(t, [(d.id, "on")]), b.put_values(t, [(d.id, "off")])
    assert (r1.status_code, r2.status_code) == (204, 429) and int(r2.headers["retry-after"]) >= 1


def test_every_write_and_every_denial_is_in_the_audit_log(api):
    t = api.token(scope="read write")
    d = api.dp("room1.light")
    api.put_values(t, [(d.id, "on")])
    api.put_values(t, [(d.id, "banana")])
    api.put_values(api.token(scope="read"), [(d.id, "on")])
    acts = [(r["action"], r["result"]) for r in api.rt.audit.records]
    assert ("write", "ok") in acts and ("write", "invalid_value") in acts and ("scope_check", "denied") in acts
    w = next(r for r in api.rt.audit.records if r["action"] == "write" and r["result"] == "ok")
    assert w["target"] == [{"dp": "room1.light", "value": "on"}] and w["client"].startswith("c_")


def test_audit_log_detects_a_changed_and_a_removed_record(api):
    t = api.token()
    for v in ("on", "off", "on"):
        api.put_values(t, [(api.dp("room1.light").id, v)])
        api.rt.advance(0.2)
    recs = [dict(r) for r in api.rt.audit.records]
    assert AuditLog.verify(recs) == (True, -1)
    edited = [dict(r) for r in recs]
    edited[2]["result"] = "ok-really"
    assert AuditLog.verify(edited) == (False, 2)
    assert AuditLog.verify(recs[:1] + recs[2:])[0] is False


# ---- T6 replay ------------------------------------------------------------------------------
def test_callback_signature_and_replay_window():
    body = b'{"data":[]}'
    now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
    date = "Mon, 16 Nov 2026 12:00:00 GMT"
    sig = sign_callback("k" * 32, "POST /hook HTTP/1.1", "hooks.example.org", date, str(len(body)), body)
    args = ("POST /hook HTTP/1.1", "hooks.example.org", date, str(len(body)), body, sig)
    assert verify_callback("k" * 32, *args, now=now) == (True, "ok")
    assert verify_callback("x" * 32, *args, now=now)[0] is False
    assert verify_callback("k" * 32, *args[:4], body + b" ", sig, now=now)[0] is False
    # the same bytes ten minutes later: signature is fine, the message is old
    ok, why = verify_callback("k" * 32, *args, now=now + timedelta(minutes=10))
    assert not ok and "stale" in why


def test_a_captured_request_is_useless_after_the_token_expired(api):
    cid, secret = api.client()
    tok = api.token_response(cid, secret, "write time_30").json()["access_token"]
    d = api.dp("room1.light")
    assert api.put_values(tok, [(d.id, "on")]).status_code == 204
    api.rt.advance(31)
    assert api.put_values(tok, [(d.id, "off")]).status_code == 401
    assert api.rt.inst.truth.last("room1.light") is True


def test_conditional_write_cannot_be_replayed(make_api):
    a = make_api(design="versioned")
    t = a.token()
    d = a.dp("room1.light")
    v = a.get(f"/datapoints/{d.id}", t).json()["data"]["meta"]["gw:version"]
    assert a.put_values(t, [(d.id, "on", v)]).status_code == 204
    assert a.put_values(t, [(d.id, "on", v)]).status_code == 409        # same bytes again


# ---- T7 callback abuse ----------------------------------------------------------------------
@pytest.mark.parametrize("url,status", [
    ("https://hooks.example.org/knx", 201),
    ("http://hooks.example.org/knx", 422),             # no TLS
    ("https://192.168.1.1/admin", 403),                # not a registered host: no request forgery
    ("https://evil.example.net/x", 403),
    ("file:///etc/passwd", 422),
])
def test_callback_url_must_be_https_and_on_a_registered_host(api, url, status):
    t = api.token(hosts={"hooks.example.org"})
    d = api.dp("room1.light")
    req = {"data": {"type": "subscription", "attributes": {"url": url, "secret": "s" * 32},
                    "relationships": {"subscriptionDatapoints": {"data": [{"type": "datapoint", "id": d.id}]}}}}
    r = api.http.post(api.base + "/subscriptions", content=json.dumps(req), headers={**api.h(t), "Content-Type": MEDIA})
    assert r.status_code == status, r.text


def test_subscription_cannot_reach_outside_the_token_locations(api):
    t = api.token(locations={api.loc_of("room1.light")}, hosts={"hooks.example.org"})
    req = {"data": {"type": "subscription", "attributes": {"url": "https://hooks.example.org/k", "secret": "s" * 32},
                    "relationships": {"subscriptionDatapoints": {"data": [
                        {"type": "datapoint", "id": api.dp("room2.light").id}]}}}}
    r = api.http.post(api.base + "/subscriptions", content=json.dumps(req), headers={**api.h(t), "Content-Type": MEDIA})
    assert r.status_code == 403


def test_subscriptions_are_private_to_their_client(api):
    t1, t2 = api.token(hosts={"hooks.example.org"}), api.token(hosts={"hooks.example.org"})
    req = {"data": {"type": "subscription", "attributes": {"url": "https://hooks.example.org/k", "secret": "s" * 32},
                    "relationships": {"subscriptionDatapoints": {"data": [
                        {"type": "datapoint", "id": api.dp("room1.light").id}]}}}}
    sid = api.http.post(api.base + "/subscriptions", content=json.dumps(req),
                        headers={**api.h(t1), "Content-Type": MEDIA}).json()["data"]["id"]
    assert api.get(f"/subscriptions/{sid}", t2).status_code == 404
    assert api.http.delete(api.base + f"/subscriptions/{sid}", headers=api.h(t2)).status_code == 404
    assert api.get("/subscriptions", t2).json()["meta"]["collection"]["total"] == 0


# ---- T8 malformed input never reaches the bus ----------------------------------------------
FUZZ = ["", " ", "ON", "On", "1", "true", "null", "on\n", "on\x00", "<script>", "'; DROP TABLE--", "0x10", "1e400",
        "-0", "１００", "100.0000001", "9" * 400, "NaN", "Infinity", "-Infinity", "on,off", "{}", "[]"]


def test_fuzzed_values_are_refused_before_the_bus(api):
    t = api.token()
    light, dim = api.dp("room1.light"), api.dp("room1.dim.val")
    api.rt.freeze()
    frames = api.rt.inst.line.stats.frames
    for v in FUZZ:
        assert api.put_values(t, [(light.id, v)]).status_code in (400, 422), repr(v)
        if v not in ("1", "-0"):                                   # those two are valid percentages
            assert api.put_values(t, [(dim.id, v)]).status_code in (400, 422), repr(v)
    for raw in (b"", b"[]", b'{"data": "x"}', b'{"data": [1]}', b'{"data": [{"id": 5}]}', b"\xff\xfe", b"{" * 5000):
        r = api.http.put(api.base + "/datapoints/values", content=raw, headers={**api.h(t), "Content-Type": MEDIA})
        assert r.status_code in (400, 422), raw[:20]
    big = json.dumps({"data": [{"id": light.id, "type": "datapoint", "attributes": {"value": "x" * 70000}}]})
    assert api.http.put(api.base + "/datapoints/values", content=big,
                        headers={**api.h(t), "Content-Type": MEDIA}).status_code == 400
    api.rt.advance(1.0)
    assert api.rt.inst.line.stats.frames == frames


def test_non_string_json_values_are_refused(api):
    t = api.token()
    light = api.dp("room1.light")
    for v in (True, 1, None, ["on"], {"v": "on"}):
        body = json.dumps({"data": [{"id": light.id, "type": "datapoint", "attributes": {"value": v}}]})
        r = api.http.put(api.base + "/datapoints/values", content=body, headers={**api.h(t), "Content-Type": MEDIA})
        assert r.status_code == 422, repr(v)


# ---- WebSocket ------------------------------------------------------------------------------
def test_websocket_needs_subprotocol_token_and_manage_scope(api):
    from starlette.websockets import WebSocketDisconnect
    url = api.base + "/messaging/ws"
    with pytest.raises(WebSocketDisconnect):
        with api.http.websocket_connect(url) as ws:               # no sub-protocol
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect) as e:
        with api.http.websocket_connect(url, subprotocols=["gw.knx.org"]) as ws:
            ws.send_text(json.dumps({"type": "auth", "token": "wrong"}))
            ws.receive_text()
    assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect):
        with api.http.websocket_connect(url, subprotocols=["gw.knx.org"]) as ws:
            ws.send_text(json.dumps({"type": "auth", "token": api.token(scope="read")}))
            ws.receive_text()


def test_websocket_delivers_updates_only_inside_the_token_locations(api):
    t = api.token(locations={api.loc_of("room1.light")})
    with api.http.websocket_connect(api.base + "/messaging/ws", subprotocols=["gw.knx.org"]) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": t}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "subscribe", "datapoints": "all"}))
        assert json.loads(ws.receive_text())["type"] == "subscribed"
        api.rt.inst.press("room2.light", True)                    # other room: must not be delivered
        api.rt.inst.press("room1.light", True)
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "update" and msg["data"][0]["id"] == api.dp("room1.light").id
        assert msg["data"][0]["attributes"]["value"] == "on"


def test_auth_service_rejects_unknown_scope_and_location_directly(api):
    cid, secret = api.client()
    with pytest.raises(AuthError):
        api.rt.auth.grant_client_credentials(cid, secret, "read loc:not-a-location")
    with pytest.raises(AuthError):
        api.rt.auth.grant_client_credentials(cid, secret, "time_60")
    with pytest.raises(AuthError):
        api.rt.auth.check("")
    assert base64.b64encode(b"x")                                 # keep the import used
