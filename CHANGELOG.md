# Changelog

## 0.1.0 - 2026-11-16

First version.

- Telegram-level model of one KNX TP1 line: 9600 bit/s, 13 bit times per character, acknowledge
  slot, 50 / 53 bit times idle, bitwise arbitration in wire order, three repetitions, frame loss
  per receiver.
- Simulated floor: 9 rooms and a corridor, 64 datapoints (37 writable), 44 bus devices, wall
  switches, presence detectors and cyclic sensors that send on their own.
- Gateway core with state cache (value, version, time of last bus confirmation), command queue
  (priority classes, round robin between clients, token bucket, bounded length) and three
  consistency designs: optimistic, confirmed, versioned.
- "Polite" bus access: the gateway waits 3 bit times longer than any other station.
- HTTP layer: a stated subset of the KNX IoT 3rd Party API 2.1.0 (FastAPI), OAuth 2.0 client
  credentials and refresh grants, scopes read / write / manage, location scopes, callback
  subscriptions with signed notifications, WebSocket notifications, audit log with hash chain.
- Studies: race scan, lost-update scan, mixed traffic for three designs, loss and reaction-time
  sensitivity, refresh trade, scene burst, write-rate sweep, polling and flooding clients.
- 157 tests: bus model against published timing, core, API contract against the official JSON
  schemas, one scenario per anomaly, security controls.
- Small React client that shows device state next to gateway state, with screenshots.
