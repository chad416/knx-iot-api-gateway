"""OAuth 2.0 token endpoint logic and access checks.

Implemented (KNX IoT 3rd Party API, /oauth/access; RFC 6749, RFC 6750):
  * client credentials grant and refresh token grant, client authenticated with HTTP Basic
  * scopes read / write / manage (they do not include each other)
  * vendor scopes, which the specification allows:
      loc:<location id>   the token is valid only inside this location subtree
      time_<seconds>      shorter token lifetime (the spec's own example is time_3600)
  * opaque random bearer tokens (not UUIDs, as the specification recommends)

Controls that go beyond the API specification (see DOSSIER section 9):
  * a client can never obtain more than it was registered for (scopes and locations)
  * only hashes of secrets and tokens are stored
  * refresh tokens rotate; presenting a used refresh token revokes the whole token family
  * failed client authentications are counted; after 5 the client is locked for 60 s, doubling
  * no built-in credentials: client secrets are generated at provisioning time

Not implemented: authorization code grant (needs a user-facing authorization endpoint, which
the specification leaves out of scope), mTLS, token introspection.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

STD_SCOPES = ("read", "write", "manage")
DEFAULT_TTL_S = 900
REFRESH_TTL_S = 14 * 24 * 3600
LOCK_AFTER = 5
LOCK_BASE_S = 60


class AuthError(Exception):
    def __init__(self, status: int, error: str, description: str, retry_after: int = 0) -> None:
        super().__init__(description)
        self.status, self.error, self.description, self.retry_after = status, error, description, retry_after


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _secret_hash(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(secret.encode(), salt=salt, n=2 ** 12, r=8, p=1, dklen=32)


@dataclass
class Client:
    client_id: str
    name: str
    salt: bytes
    secret_hash: bytes
    scopes: frozenset[str]
    locations: Optional[frozenset[str]]          # None: whole installation
    callback_hosts: frozenset[str] = frozenset()  # hosts this client may register for callbacks
    enabled: bool = True
    fails: int = 0
    locked_until: float = 0.0
    lock_level: int = 0


@dataclass
class Token:
    client_id: str
    scopes: frozenset[str]
    locations: Optional[frozenset[str]]
    expires: float
    family: str
    kind: str                                    # access | refresh
    used: bool = False


@dataclass
class Principal:
    client_id: str
    scopes: frozenset[str]
    locations: Optional[frozenset[str]]
    family: str = ""

    def may(self, scope: str) -> bool:
        return scope in self.scopes

    def sees(self, location_id: str) -> bool:
        return self.locations is None or location_id in self.locations


class AuthService:
    def __init__(self, expand_location: Callable[[str], set[str]], known_location: Callable[[str], bool],
                 clock: Callable[[], float] = time.time) -> None:
        self.clients: dict[str, Client] = {}
        self.tokens: dict[str, Token] = {}
        self.revoked_families: set[str] = set()
        self._expand, self._known, self.clock = expand_location, known_location, clock
        self.events: list[dict] = []             # security telemetry (EN 303 645 provision 5.10-1)

    # ---- provisioning ----------------------------------------------------------------
    def register(self, name: str, scopes: set[str], locations: Optional[set[str]] = None,
                 callback_hosts: Optional[set[str]] = None) -> tuple[str, str]:
        """Create a client. Returns (client_id, secret); the secret is shown once, never stored."""
        cid = "c_" + secrets.token_urlsafe(9)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        locs = None
        if locations is not None:
            locs = frozenset(x for l in locations for x in self._expand(l))
        self.clients[cid] = Client(cid, name, salt, _secret_hash(secret, salt), frozenset(scopes), locs,
                                   frozenset(callback_hosts or ()))
        return cid, secret

    def disable(self, client_id: str) -> None:
        """Kill switch for a compromised integration: no new tokens, existing ones stop working."""
        c = self.clients.get(client_id)
        if c:
            c.enabled = False
        self._log("client_disabled", client_id)

    # ---- token endpoint --------------------------------------------------------------
    def _log(self, what: str, client_id: str, **kw: object) -> None:
        self.events.append({"t": self.clock(), "event": what, "client": client_id, **kw})

    def _authenticate(self, client_id: str, secret: str) -> Client:
        now = self.clock()
        c = self.clients.get(client_id)
        if c is not None and now < c.locked_until:
            self._log("auth_locked", client_id)
            raise AuthError(429, "invalid_client", "too many failed attempts", int(c.locked_until - now) + 1)
        ok = False
        if c is not None:
            ok = hmac.compare_digest(_secret_hash(secret, c.salt), c.secret_hash)
        else:
            _secret_hash(secret, b"0" * 16)      # same work for unknown clients
        if not ok or c is None or not c.enabled:
            if c is not None:
                c.fails += 1
                if c.fails >= LOCK_AFTER:
                    c.locked_until = now + LOCK_BASE_S * (2 ** c.lock_level)
                    c.lock_level += 1
                    c.fails = 0
                    self._log("client_locked", client_id, until=c.locked_until)
            self._log("auth_failed", client_id)
            raise AuthError(401, "invalid_client", "client authentication failed")
        c.fails = 0
        c.lock_level = 0
        return c

    def _parse_scope(self, c: Client, scope: str) -> tuple[frozenset[str], Optional[frozenset[str]], int]:
        if not scope or len(scope) > 2000:
            raise AuthError(400, "invalid_scope", "scope is missing or too long")
        std: set[str] = set()
        locs: Optional[set[str]] = None
        ttl = DEFAULT_TTL_S
        for s in scope.split():
            if s in STD_SCOPES:
                if s not in c.scopes:
                    raise AuthError(400, "invalid_scope", f"client is not registered for scope {s}")
                std.add(s)
            elif s.startswith("loc:"):
                if not self._known(s[4:]):
                    raise AuthError(400, "invalid_scope", "unknown location")
                locs = (locs or set()) | self._expand(s[4:])
            elif s.startswith("time_") and s[5:].isdigit():
                ttl = max(1, min(ttl, int(s[5:])))
            else:
                raise AuthError(400, "invalid_scope", f"unknown scope {s[:40]}")
        if not std:
            raise AuthError(400, "invalid_scope", "at least one of read, write, manage is needed")
        if c.locations is not None:
            if locs is None:
                locs = set(c.locations)
            elif not locs <= c.locations:
                raise AuthError(400, "invalid_scope", "location is outside the client registration")
        return frozenset(std), (None if locs is None else frozenset(locs)), ttl

    def _issue(self, c: Client, scopes: frozenset[str], locs: Optional[frozenset[str]], ttl: int,
               family: Optional[str] = None) -> dict:
        now = self.clock()
        family = family or secrets.token_urlsafe(8)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.tokens[_h(access)] = Token(c.client_id, scopes, locs, now + ttl, family, "access")
        self.tokens[_h(refresh)] = Token(c.client_id, scopes, locs, now + REFRESH_TTL_S, family, "refresh")
        self._log("token_issued", c.client_id, scopes=sorted(scopes), family=family, ttl=ttl)
        scope_out = " ".join(sorted(scopes))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ttl,
                "refresh_token": refresh, "scope": scope_out}

    def grant_client_credentials(self, client_id: str, secret: str, scope: str) -> dict:
        c = self._authenticate(client_id, secret)
        scopes, locs, ttl = self._parse_scope(c, scope)
        return self._issue(c, scopes, locs, ttl)

    def grant_refresh(self, client_id: str, secret: str, refresh_token: str) -> dict:
        c = self._authenticate(client_id, secret)
        t = self.tokens.get(_h(refresh_token))
        now = self.clock()
        if t is None or t.kind != "refresh" or t.client_id != c.client_id or t.expires < now \
                or t.family in self.revoked_families:
            self._log("refresh_rejected", client_id)
            raise AuthError(400, "invalid_grant", "refresh token is not valid")
        if t.used:
            # a refresh token came back a second time: one of the two holders stole it
            self.revoked_families.add(t.family)
            self._log("refresh_reuse_family_revoked", client_id, family=t.family)
            raise AuthError(400, "invalid_grant", "refresh token was already used; tokens revoked")
        t.used = True
        ttl = DEFAULT_TTL_S
        return self._issue(c, t.scopes, t.locations, ttl, t.family)

    # ---- resource access -------------------------------------------------------------
    def check(self, bearer: str) -> Principal:
        t = self.tokens.get(_h(bearer)) if bearer else None
        if t is None or t.kind != "access":
            self._log("token_unknown", "?")
            raise AuthError(401, "invalid_token", "access token is not valid")
        c = self.clients.get(t.client_id)
        if t.expires < self.clock() or t.family in self.revoked_families or c is None or not c.enabled:
            self._log("token_rejected", t.client_id)
            raise AuthError(401, "invalid_token", "access token is expired or revoked")
        return Principal(t.client_id, t.scopes, t.locations, t.family)

    def revoke_family(self, family: str) -> None:
        self.revoked_families.add(family)

    def failed_auth_count(self, window_s: float = 300.0) -> int:
        now = self.clock()
        return sum(1 for e in self.events if e["event"] in ("auth_failed", "token_unknown", "token_rejected")
                   and now - e["t"] <= window_s)
