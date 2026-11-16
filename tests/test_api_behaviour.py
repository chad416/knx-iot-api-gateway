"""What the three designs look like through HTTP."""
import pytest

from knxgw.gateway.api import MEDIA


def test_schema_validator_really_rejects_wrong_shapes(schema):
    good = {"data": [{"id": "1f48bf82-acee-45a1-9038-e2748860a462", "type": "datapoint", "attributes": {"value": "on"}}]}
    schema("DatapointCollectionWithValues", good)
    for bad in ({"data": [{"type": "datapoint", "attributes": {"value": "on"}}]},
                {"data": [{"id": "x", "type": "datapoint", "attributes": {}}]},
                {}):
        with pytest.raises(AssertionError):
            schema("DatapointCollectionWithValues", bad)
    with pytest.raises(AssertionError):
        schema("Access", {"access_token": "a", "token_type": "MAC", "expires_in": 5})
    with pytest.raises(AssertionError):
        schema("Errors", {"errors": [{"detail": "no title"}]})


def test_optimistic_put_then_get_shows_a_value_the_bus_has_not_confirmed(make_api):
    a = make_api(design="optimistic")
    t = a.token()
    d = a.dp("room1.blind")
    assert a.put_values(t, [(d.id, "100")]).status_code == 204
    item = a.get(f"/datapoints/{d.id}", t).json()["data"]
    assert item["attributes"]["value"] == "100" and item["meta"]["gw:source"] == "optimistic"
    assert a.rt.inst.truth.last("room1.blind") == 0.0
    assert "gw:pending" not in item["meta"]


def test_confirmed_put_returns_after_the_device_reported(make_api):
    a = make_api(design="confirmed")
    t = a.token()
    d = a.dp("room1.light")
    assert a.put_values(t, [(d.id, "on")]).status_code == 204
    assert a.rt.inst.truth.last("room1.light") is True
    item = a.get(f"/datapoints/{d.id}", t).json()["data"]
    assert item["attributes"]["value"] == "on" and item["meta"]["gw:source"] == "bus-status"


def test_versioned_get_carries_etag_pending_marker_and_read_your_writes(make_api):
    a = make_api(design="versioned")
    t = a.token()
    blind, light = a.dp("room1.blind"), a.dp("room1.light")
    r = a.get(f"/datapoints/{light.id}", t)
    v = r.json()["data"]["meta"]["gw:version"]
    assert r.headers["etag"] == f'"{v}"'
    assert a.put_values(t, [(blind.id, "100")]).status_code == 202
    m = a.get(f"/datapoints/{blind.id}", t).json()["data"]
    assert m["attributes"]["value"] == "0" and m["meta"]["gw:pending"] is True and m["meta"]["gw:pendingTarget"] == "100"
    assert a.put_values(t, [(light.id, "on", v)]).status_code == 204
    after = a.get(f"/datapoints/{light.id}", t).json()["data"]
    assert after["attributes"]["value"] == "on" and after["meta"]["gw:version"] == v + 1


def test_conflict_response_tells_the_current_version(make_api):
    a = make_api(design="versioned")
    t = a.token()
    d = a.dp("room1.light")
    r = a.put_values(t, [(d.id, "on", 0)])
    assert r.status_code == 409
    assert r.json()["meta"]["gw:items"][0]["version"] == 1


def test_failed_write_is_a_504_with_the_reason(make_api):
    a = make_api(design="confirmed")
    t = a.token()
    a.rt.inst.sw_act.p_rx_miss = 1.0                  # actuator misses, the wall switch acknowledges
    r = a.put_values(t, [(a.dp("room1.light").id, "on")])
    assert r.status_code == 504 and r.json()["meta"]["gw:items"][0]["outcome"] == "no_feedback"


def test_queue_full_is_429_with_retry_after(make_api):
    a = make_api(design="optimistic", gw={"queue_max": 4, "client_burst": 100})
    t = a.token()
    a.rt.freeze()
    codes = []
    for i in range(1, 9):
        r = a.put_values(t, [(a.dp(f"room{i}.light").id, "on"), (a.dp(f"room{i}.dim.sw").id, "on")])
        codes.append(r.status_code)
        if r.status_code == 429:
            assert int(r.headers["retry-after"]) >= 1
    assert 429 in codes and codes[0] == 204


def test_application_json_is_accepted_as_fallback(api):
    t = api.token()
    assert api.put_values(t, [(api.dp("room1.light").id, "on")], ctype="application/json").status_code == 204


def test_batch_larger_than_the_queue_is_refused(make_api):
    a = make_api(design="optimistic", gw={"queue_max": 3})
    t = a.token()
    items = [(a.dp(f"room{i}.light").id, "on") for i in range(1, 6)]
    assert a.put_values(t, items).status_code == 422


def test_filters_are_not_implemented(api):
    t = api.token()
    assert api.get("/datapoints", t, params={"filter[type]": "x"}).status_code == 501


def test_error_content_type(api):
    r = api.http.get(api.base + "/datapoints")
    assert r.headers["content-type"].startswith(MEDIA) and r.headers["x-content-type-options"] == "nosniff"
