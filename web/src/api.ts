// Thin client for the gateway. Everything under /api/v2 is the KNX IoT 3rd Party API subset;
// everything under /demo exists only in demo mode (ground truth, wall-switch button, bus monitor).

export interface Room { key: string; title: string; datapoints: Record<string, string> }
export interface Config { design: string; speed: number; base: string; rooms: Room[]; gateway_address: string; polite: boolean }
export interface Cached { value: string; version: number; ageMs: number | null; source: string; pending?: boolean; target?: string }
export interface Truth {
  now_ms: number; truth: Record<string, string>; queue: number;
  bus: { telegrams: number; repetitions: number; load: number };
  race?: { key: string; tau_ms: number; api_writes: string; wall_presses: string; api_result?: string };
}
export interface BusLine { n: number; t_ms: number; src: string; dst: string; apci: string; value: unknown; origin: string; ack: string | null; attempt: number }
export interface Update { id: string; value: string; version: number; source: string; at: number }

const MEDIA = "application/vnd.api+json";

export class Gateway {
  token = "";
  constructor(public cfg: Config) {}

  static async connect(): Promise<Gateway> {
    const cfg: Config = await (await fetch("/demo/config")).json();
    const gw = new Gateway(cfg);
    gw.token = (await (await fetch("/demo/token")).json()).access_token;
    return gw;
  }

  private headers(): HeadersInit { return { Authorization: `Bearer ${this.token}` }; }

  async datapoints(): Promise<Record<string, Cached>> {
    const body = await (await fetch(`${this.cfg.base}/datapoints`, { headers: this.headers() })).json();
    const out: Record<string, Cached> = {};
    for (const it of body.data) {
      out[it.id] = {
        value: it.attributes.value, version: it.meta["gw:version"], ageMs: it.meta["gw:ageMs"],
        source: it.meta["gw:source"], pending: it.meta["gw:pending"], target: it.meta["gw:pendingTarget"],
      };
    }
    return out;
  }

  /** PUT /datapoints/values. Returns the HTTP status and, for errors, the detail text. */
  async write(id: string, value: string, ifVersion?: number): Promise<{ status: number; detail: string; ms: number }> {
    const item: Record<string, unknown> = { id, type: "datapoint", attributes: { value } };
    if (ifVersion !== undefined && this.cfg.design === "versioned") item.meta = { "gw:ifVersion": ifVersion };
    const t0 = performance.now();
    const r = await fetch(`${this.cfg.base}/datapoints/values`, {
      method: "PUT", headers: { ...this.headers(), "Content-Type": MEDIA }, body: JSON.stringify({ data: [item] }),
    });
    let detail = "";
    if (r.status >= 400) detail = (await r.json()).errors?.[0]?.detail ?? "";
    return { status: r.status, detail, ms: performance.now() - t0 };
  }

  subscribe(onUpdate: (u: Update) => void): WebSocket {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}${this.cfg.base}/messaging/ws`, "gw.knx.org");
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: this.token }));
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "ready") ws.send(JSON.stringify({ type: "subscribe", datapoints: "all" }));
      if (msg.type === "update") {
        for (const it of msg.data) {
          onUpdate({ id: it.id, value: it.attributes.value, version: it.meta["gw:version"], source: it.meta["gw:source"], at: Date.now() });
        }
      }
    };
    return ws;
  }

  async truth(): Promise<Truth> { return (await fetch("/demo/truth")).json(); }
  async buslog(after: number): Promise<BusLine[]> { return (await fetch(`/demo/buslog?after=${after}`)).json(); }
  async press(key: string): Promise<void> {
    await fetch("/demo/press", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) });
  }
  async race(key: string, tau_ms: number): Promise<void> {
    await fetch("/demo/race", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, tau_ms }) });
  }
}
