import base64
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

from knxgw.gateway.api import BASE, Runtime, create_app  # noqa: E402

SCHEMA_DIR = os.path.join(ROOT, "spec", "schemas-2020")


@pytest.fixture(scope="session")
def schema():
    """Validator factory for the official JSON schemas of the KNX IoT 3rd Party API."""
    docs = {}
    for f in os.listdir(SCHEMA_DIR):
        with open(os.path.join(SCHEMA_DIR, f), encoding="utf-8") as fh:
            docs[f] = json.load(fh)
    base = "https://schema.invalid/knx/"
    reg = Registry().with_resources(
        (base + name, Resource.from_contents(doc, default_specification=DRAFT202012)) for name, doc in docs.items())

    def validate(name: str, instance) -> None:
        v = Draft202012Validator({"$ref": base + name + ".json"}, registry=reg)
        errs = sorted(v.iter_errors(instance), key=lambda e: list(e.path))
        assert not errs, f"{name}: " + "; ".join(f"{list(e.path)}: {e.message[:160]}" for e in errs[:5])

    return validate


class Api:
    """Small helper around TestClient: provisioning, tokens, JSON:API calls."""

    def __init__(self, design="versioned", speed=40.0, **kw):
        self.rt = Runtime(design=design, speed=speed, manual_time=True, inst={"sensors": False}, **kw)
        self.app = create_app(self.rt)
        self.http = TestClient(self.app)
        self.http.__enter__()
        self.base = BASE

    def close(self):
        self.http.__exit__(None, None, None)

    def client(self, scopes=("read", "write", "manage"), locations=None, hosts=None):
        return self.rt.auth.register("test", set(scopes), locations, hosts)

    def token_response(self, cid, secret, scope="read write manage", **form):
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        data = {"grant_type": "client_credentials", "scope": scope}
        data.update(form)
        return self.http.post(self.base + "/oauth/access", data=data, headers={"Authorization": "Basic " + basic})

    def token(self, scopes=("read", "write", "manage"), locations=None, scope=None, hosts=None):
        cid, secret = self.client(scopes, locations, hosts)
        r = self.token_response(cid, secret, scope or " ".join(scopes))
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def h(self, token):
        return {"Authorization": "Bearer " + token}

    def get(self, path, token, **kw):
        return self.http.get(self.base + path, headers=self.h(token), **kw)

    def put_values(self, token, items, ctype="application/vnd.api+json"):
        data = []
        for it in items:
            d = {"id": it[0], "type": "datapoint", "attributes": {"value": it[1]}}
            if len(it) > 2:
                d["meta"] = {"gw:ifVersion": it[2]}
            data.append(d)
        return self.http.put(self.base + "/datapoints/values", content=json.dumps({"data": data}),
                             headers={**self.h(token), "Content-Type": ctype})

    def dp(self, key):
        return self.rt.inst.dp(key)

    def loc_of(self, key):
        return self.rt.inst.dp(key).location


@pytest.fixture
def api():
    a = Api()
    yield a
    a.close()


@pytest.fixture
def make_api():
    made = []

    def make(**kw):
        a = Api(**kw)
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()
