"""Notifications: HTTP callback subscriptions (as in the specification) and WebSocket fan-out.

Callback signature, from the specification (callbacks/eventNotification): X-Callback-Signature
is the base64 HMAC-SHA256 over the request line including CRLF, the values of the Host, Date
and Content-Length headers, and the message body, keyed with the subscription secret.

Beyond the specification:
  * `verify_callback` is the receiver side, and it also rejects a Date older than max_age_s.
    The signature alone does not stop somebody from replaying a captured notification.
  * a client may only register callback hosts that were allowed when it was provisioned, and
    only https URLs. Without that, "manage" scope would let a client make the gateway send
    POST requests to any address inside the home network.
  * per subscription the queue is bounded and keeps only the newest value per datapoint.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, Callable, Optional
from urllib.parse import urlsplit


def sign_callback(secret: str, request_line: str, host: str, date: str, content_length: str, body: bytes) -> str:
    msg = (request_line + "\r\n" + host + date + content_length).encode() + body
    return base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()


def verify_callback(secret: str, request_line: str, host: str, date: str, content_length: str, body: bytes,
                    signature: str, now: datetime, max_age_s: float = 120.0) -> tuple[bool, str]:
    expected = sign_callback(secret, request_line, host, date, content_length, body)
    if not hmac.compare_digest(expected, signature):
        return False, "bad signature"
    try:
        sent = parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return False, "bad date"
    if abs((now - sent).total_seconds()) > max_age_s:
        return False, "stale date (replay?)"
    return True, "ok"


@dataclass
class Subscription:
    id: str
    client_id: str
    url: str
    secret: str
    datapoints: set[str]
    expires: float
    pending: "OrderedDict[str, dict]" = field(default_factory=OrderedDict)
    delivered: int = 0
    dropped: int = 0


class SubscriptionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


class Notifier:
    MAX_PENDING = 256
    MAX_SUBS_PER_CLIENT = 8
    DEFAULT_LIFETIME_S = 24 * 3600

    def __init__(self, clock: Callable[[], float], sender: Optional[Callable[[str, dict, bytes], Any]] = None,
                 allow_insecure_hosts: frozenset[str] = frozenset()) -> None:
        self.clock, self.sender = clock, sender
        self.subs: dict[str, Subscription] = {}
        self.ws: dict[int, dict] = {}                # connection id -> {"send": fn, "principal": p, "dps": set|None}
        self.allow_insecure_hosts = allow_insecure_hosts

    # ---- callback subscriptions ------------------------------------------------------
    def create(self, client_id: str, allowed_hosts: frozenset[str], url: str, secret: str,
               datapoints: set[str], lifetime: int = 0) -> Subscription:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" and host not in self.allow_insecure_hosts:
            raise SubscriptionError(422, "callback url must use https")
        if not host or host not in allowed_hosts:
            raise SubscriptionError(403, "callback host is not registered for this client")
        if len(secret) < 16:
            raise SubscriptionError(422, "secret must have at least 16 characters")
        if not datapoints:
            raise SubscriptionError(422, "subscription selects no datapoint")
        if sum(1 for s in self.subs.values() if s.client_id == client_id) >= self.MAX_SUBS_PER_CLIENT:
            raise SubscriptionError(429, "too many subscriptions for this client")
        life = lifetime if lifetime and lifetime > 0 else self.DEFAULT_LIFETIME_S
        s = Subscription(str(uuid.uuid4()), client_id, url, secret, set(datapoints), self.clock() + life)
        self.subs[s.id] = s
        return s

    def delete(self, sub_id: str) -> None:
        self.subs.pop(sub_id, None)

    def drop_client(self, client_id: str) -> None:
        for i in [i for i, s in self.subs.items() if s.client_id == client_id]:
            del self.subs[i]
        for cid in [cid for cid, c in self.ws.items() if c["principal"].client_id == client_id]:
            del self.ws[cid]

    # ---- fan-out ---------------------------------------------------------------------
    def publish(self, item: dict, location_id: str) -> None:
        """item is one UpdateEvent item (type, id, links, attributes, meta)."""
        dp = item["id"]
        now = self.clock()
        for s in list(self.subs.values()):
            if s.expires < now:
                del self.subs[s.id]
                continue
            if dp in s.datapoints:
                if dp in s.pending:
                    s.pending.pop(dp)                      # keep only the newest value
                elif len(s.pending) >= self.MAX_PENDING:
                    s.pending.popitem(last=False)
                    s.dropped += 1
                s.pending[dp] = item
        for c in list(self.ws.values()):
            if c["principal"].sees(location_id) and (c["dps"] is None or dp in c["dps"]):
                c["send"]({"type": "update", "data": [item]})

    def flush(self) -> list[tuple[str, dict, bytes]]:
        """Build one signed POST per subscription with pending events. Returns what was sent."""
        out = []
        for s in self.subs.values():
            if not s.pending:
                continue
            body = json.dumps({"data": list(s.pending.values())}, separators=(",", ":")).encode()
            s.pending.clear()
            u = urlsplit(s.url)
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            date = format_datetime(datetime.fromtimestamp(self.clock(), timezone.utc), usegmt=True)
            headers = {"Host": u.netloc, "Date": date, "Content-Length": str(len(body)),
                       "Content-Type": "application/vnd.api+json"}
            headers["X-Callback-Signature"] = sign_callback(
                s.secret, f"POST {path} HTTP/1.1", headers["Host"], date, headers["Content-Length"], body)
            s.delivered += 1
            out.append((s.url, headers, body))
            if self.sender:
                self.sender(s.url, headers, body)
        return out
