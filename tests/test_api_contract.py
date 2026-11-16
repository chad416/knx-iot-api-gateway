"""Contract tests against the vendored KNX IoT 3rd Party API description (spec/).

Responses are validated with the official JSON schemas (spec/schemas-2020), and every path,
method and status code the gateway uses is checked to exist in the OpenAPI file. This shows
that the implemented subset has the documented shape. It is not a conformance statement.
"""
import json
import os
import re

import pytest
import yaml

from knxgw.gateway.api import API_VERSION, BASE, MEDIA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def oas():
    with open(os.path.join(ROOT, "spec", "knxiot_api_openapi.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def spec_op(oas, method, path):
    """Find the OpenAPI operation for a concrete request path."""
    rel = path[len(BASE):] if path.startswith(BASE) else path
    for tmpl, ops in oas["paths"].items():
        rx = "^" + re.sub(r"\{[^}]+\}", "[^/]+", tmpl.strip()) + "$"
        if re.match(rx, rel) and method.lower() in ops:
            return ops[method.lower()]
    raise AssertionError(f"{method} {rel} is not in the OpenAPI description")


def declared(oas, method, path, status):
    op = spec_op(oas, method, path)
    assert str(status) in {str(k) for k in op["responses"]}, f"{method} {path} -> {status} is not declared"


def test_api_version_matches_the_vendored_description(oas):
    assert oas["info"]["version"] == API_VERSION
    assert oas["openapi"].startswith("3.0")


def test_well_known(api, schema, oas):
    r = api.http.get("/.well-known/knx")
    assert r.status_code == 200
    schema("WellKnownKNX", r.json())
    assert r.json()["api"] == {"version": API_VERSION, "base": BASE}
    declared(oas, "GET", "/.well-known/knx", 200)


def test_token_response(api, schema, oas):
    cid, secret = api.client()
    r = api.token_response(cid, secret, "read write")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    schema("Access", r.json())
    assert r.json()["token_type"] == "Bearer" and r.json()["scope"] == "read write"
    declared(oas, "POST", BASE + "/oauth/access", 200)


@pytest.mark.parametrize("form,status,error", [
    ({"scope": "read admin"}, 400, "invalid_scope"),
    ({"grant_type": "password"}, 400, "unsupported_grant_type"),
    ({"grant_type": "authorization_code", "code": "x"}, 400, "unsupported_grant_type"),
    ({"grant_type": "refresh_token", "refresh_token": "nope"}, 400, "invalid_grant"),
])
def test_token_errors_follow_the_oauth_error_schema(api, schema, oas, form, status, error):
    cid, secret = api.client()
    r = api.token_response(cid, secret, **form)
    assert r.status_code == status
    schema("ErrorsOAuth2", r.json())
    assert r.json()["error"] == error
    declared(oas, "POST", BASE + "/oauth/access", status)


def test_wrong_client_secret(api, schema, oas):
    cid, _ = api.client()
    r = api.token_response(cid, "wrong")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    schema("ErrorsOAuth2", r.json())
    declared(oas, "POST", BASE + "/oauth/access", 401)


@pytest.mark.parametrize("path,name", [
    ("/locations", "LocationCollection"),
    ("/functions", "FunctionCollection"),
    ("/datapoints", "DatapointCollection"),
    ("/subscriptions", "SubscriptionCollection"),
])
def test_collections(api, schema, oas, path, name):
    t = api.token()
    r = api.get(path, t)
    assert r.status_code == 200 and r.headers["content-type"].startswith(MEDIA)
    body = r.json()
    schema(name, body)
    c = body["meta"]["collection"]
    assert c["size"] == len(body["data"]) and c["number"] == 0
    declared(oas, "GET", BASE + path, 200)


def test_collection_sizes_match_the_installation(api):
    t = api.token()
    assert api.get("/datapoints", t).json()["meta"]["collection"]["total"] == 64
    assert api.get("/functions", t).json()["meta"]["collection"]["total"] == 37
    assert api.get("/locations", t).json()["meta"]["collection"]["total"] == 13


def test_single_items_and_related_collections(api, schema, oas):
    t = api.token()
    d = api.dp("room1.light")
    for path, name in ((f"/datapoints/{d.id}", "Datapoint"),
                       (f"/functions/{d.function}", "Function"),
                       (f"/locations/{d.location}", "Location"),
                       (f"/functions/{d.function}/datapoints", "DatapointCollection"),
                       (f"/datapoints/{d.id}/functions", "FunctionCollection"),
                       (f"/locations/{d.location}/functions", "FunctionCollection"),
                       (f"/locations/{api.rt.inst.floor_id}/childlocations", "LocationCollection")):
        r = api.get(path, t)
        assert r.status_code == 200, path
        schema(name, r.json())
        declared(oas, "GET", BASE + path, 200)


def test_every_datapoint_validates_and_carries_freshness(api, schema):
    t = api.token()
    for item in api.get("/datapoints", t).json()["data"]:
        schema("DatapointItem", item)
        a, m = item["attributes"], item["meta"]
        assert a["valueType"] == "string" and isinstance(a["value"], str)
        assert ("enum" in a) != ("unit" in a)
        assert m["gw:version"] >= 1 and m["gw:confirmedAt"].endswith("Z") and m["gw:ageMs"] >= 0


def test_pagination(api, schema):
    t = api.token()
    r = api.get("/datapoints", t, params={"page[size]": 10, "page[number]": 6})
    body = r.json()
    schema("DatapointCollection", body)
    assert body["meta"]["collection"] == {"number": 6, "size": 4, "total": 64}
    assert api.get("/datapoints", t, params={"page[size]": 0}).status_code == 400
    assert api.get("/datapoints", t, params={"page[number]": "x"}).status_code == 400


def test_put_values_request_matches_the_schema_and_returns_declared_codes(api, schema, oas):
    t = api.token()
    light, blind = api.dp("room1.light"), api.dp("room1.blind")
    schema("DatapointCollectionWithValues",
           {"data": [{"id": light.id, "type": "datapoint", "attributes": {"value": "on"}}]})
    r = api.put_values(t, [(light.id, "on")])
    assert r.status_code == 204 and r.content == b""
    r2 = api.put_values(t, [(blind.id, "100")])
    assert r2.status_code == 202
    for code in (204, 202, 400, 401, 403, 404, 409, 415, 422, 429, 504):
        declared(oas, "PUT", BASE + "/datapoints/values", code)


@pytest.mark.parametrize("case,status", [
    ("unknown", 404), ("bad_value", 422), ("range", 422), ("read_only", 403), ("not_json", 400),
    ("no_data", 400), ("wrong_type", 400), ("bad_uuid", 400), ("duplicate", 422), ("ctype", 415),
    ("stale_version", 409),
])
def test_errors_follow_the_json_api_error_schema(api, schema, case, status):
    t = api.token()
    light, dim, temp = api.dp("room1.light"), api.dp("room1.dim.val"), api.dp("room1.temp")
    h = {**api.h(t), "Content-Type": MEDIA}
    url = api.base + "/datapoints/values"
    if case == "unknown":
        r = api.put_values(t, [("00000000-0000-4000-8000-000000000000", "on")])
    elif case == "bad_value":
        r = api.put_values(t, [(light.id, "maybe")])
    elif case == "range":
        r = api.put_values(t, [(dim.id, "320")])
    elif case == "read_only":
        r = api.put_values(t, [(temp.id, "21")])
    elif case == "not_json":
        r = api.http.put(url, content=b"{data:", headers=h)
    elif case == "no_data":
        r = api.http.put(url, content=b'{"data": []}', headers=h)
    elif case == "wrong_type":
        r = api.http.put(url, content=json.dumps({"data": [{"id": light.id, "type": "light",
                                                           "attributes": {"value": "on"}}]}), headers=h)
    elif case == "bad_uuid":
        r = api.put_values(t, [("../../etc/passwd", "on")])
    elif case == "duplicate":
        r = api.put_values(t, [(light.id, "on"), (light.id, "off")])
    elif case == "ctype":
        r = api.put_values(t, [(light.id, "on")], ctype="text/plain")
    else:
        r = api.put_values(t, [(light.id, "on", 999)])
    assert r.status_code == status, r.text
    schema("Errors", r.json())
    err = r.json()["errors"][0]
    assert err["status"] == str(status) and err["title"]


def test_unimplemented_parts_answer_501_not_404(api, schema, oas):
    t = api.token()
    d = api.dp("room1.light")
    for path in ("/devices", "/installations", "/sites", "/node", f"/datapoints/{d.id}/timeseries",
                 f"/datapoints/{d.id}/device", f"/functions/{d.function}/location"):
        r = api.get(path, t)
        assert r.status_code == 501, path
        schema("Errors", r.json())
        declared(oas, "GET", BASE + path, 501)
    assert api.get("/nonsense", t).status_code == 404
    assert api.http.get("/.well-known/knx/idevid").status_code == 501


def test_subscription_life_cycle(api, schema, oas):
    t = api.token(hosts={"hooks.example.org"})
    d = api.dp("room1.light")
    req = {"data": {"type": "subscription",
                    "attributes": {"url": "https://hooks.example.org/knx", "secret": "s" * 32, "lifetime": 600},
                    "relationships": {"subscriptionDatapoints": {"data": [
                        {"type": "datapoint", "id": d.id},
                        {"type": "location", "id": d.location, "meta": {"expand": True}}]}}}}
    schema("SubscribeRequest", req)
    r = api.http.post(api.base + "/subscriptions", content=json.dumps(req), headers={**api.h(t), "Content-Type": MEDIA})
    assert r.status_code == 201, r.text
    schema("SubscribeResponse", r.json())
    declared(oas, "POST", BASE + "/subscriptions", 201)
    sid = r.json()["data"]["id"]
    one = api.get(f"/subscriptions/{sid}", t)
    schema("Subscription", one.json())
    assert "secret" not in one.json()["data"]["attributes"]          # never echoed back
    lst = api.get("/subscriptions", t)
    schema("SubscriptionCollection", lst.json())
    assert lst.json()["meta"]["collection"]["total"] == 1
    dps = api.get(f"/subscriptions/{sid}/datapoints", t)
    schema("DatapointCollection", dps.json())
    assert dps.json()["meta"]["collection"]["total"] == 7            # all datapoints of the room
    assert api.http.delete(api.base + f"/subscriptions/{sid}", headers=api.h(t)).status_code == 204
    declared(oas, "DELETE", BASE + f"/subscriptions/{sid}", 204)
    assert api.get(f"/subscriptions/{sid}", t).status_code == 404


def test_callback_body_matches_the_update_event_schema(api, schema):
    t = api.token(hosts={"hooks.example.org"})
    d = api.dp("room1.light")
    req = {"data": {"type": "subscription", "attributes": {"url": "https://hooks.example.org/knx", "secret": "s" * 32},
                    "relationships": {"subscriptionDatapoints": {"data": [{"type": "datapoint", "id": d.id}]}}}}
    assert api.http.post(api.base + "/subscriptions", content=json.dumps(req),
                         headers={**api.h(t), "Content-Type": MEDIA}).status_code == 201
    sent = []
    api.rt.notifier.sender = lambda url, headers, body: sent.append((url, headers, body))
    assert api.put_values(t, [(d.id, "on")]).status_code == 204
    api.rt.advance(0.1)
    assert sent, "no callback was produced"
    url, headers, body = sent[0]
    event = json.loads(body)
    schema("UpdateEvent", event)
    assert event["data"][0]["id"] == d.id and event["data"][0]["attributes"]["value"] == "on"
    assert {"Host", "Date", "Content-Length", "X-Callback-Signature"} <= set(headers)
