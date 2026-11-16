import { useEffect, useRef, useState } from "react";
import { BusLine, Cached, Gateway, Room, Truth, Update } from "./api";

type Cache = Record<string, Cached>;
const WRITABLE: [string, string][] = [["light", "Light"], ["dim.sw", "Dimmer"], ["dim.val", "Brightness"], ["blind", "Blind"]];

function Cell(props: { label: string; device?: string; cached?: Cached; unit: string; children: React.ReactNode }) {
  const { device, cached } = props;
  const differs = device !== undefined && cached !== undefined && device !== cached.value;
  return (
    <div className={"cell" + (differs ? " differs" : "")}>
      <div className="cell-head">
        <span>{props.label}</span>
        {cached?.pending && <span className="tag pending">moving to {cached.target}{props.unit}</span>}
        {differs && <span className="tag wrong">gateway differs from device</span>}
      </div>
      <div className="pair">
        <span className="who">device</span><b>{device ?? "?"}{props.unit}</b>
        <span className="who">gateway</span><b>{cached?.value ?? "?"}{props.unit}</b>
        <span className="meta">v{cached?.version} · {cached?.source}</span>
      </div>
      <div className="buttons">{props.children}</div>
    </div>
  );
}

function RoomCard(props: { room: Room; cache: Cache; truth?: Truth; gw: Gateway; say: (s: string) => void }) {
  const { room, cache, truth, gw, say } = props;
  const id = (n: string) => room.datapoints[n];
  const dev = (n: string) => truth?.truth[`${room.key}.${n}`];
  const write = async (n: string, value: string) => {
    const c = cache[id(n)];
    const r = await gw.write(id(n), value, c?.version);
    say(`PUT ${room.title} ${n} = ${value}  ->  ${r.status}${r.detail ? " " + r.detail : ""}  (${r.ms.toFixed(0)} ms)`);
  };
  const flip = (n: string) => (cache[id(n)]?.value === "on" ? "off" : "on");
  const corridor = room.key === "corridor";
  return (
    <section className="room">
      <h2>{room.title}</h2>
      {WRITABLE.filter(([n]) => id(n)).map(([n, label]) => {
        const binary = n === "light" || n === "dim.sw";
        return (
          <Cell key={n} label={label} device={dev(n)} cached={cache[id(n)]} unit={binary ? "" : " %"}>
            {binary ? (
              <>
                <button onClick={() => write(n, flip(n))}>API: toggle</button>
                {!corridor && <button className="wall" onClick={() => gw.press(`${room.key}.${n}`)}>press wall switch</button>}
              </>
            ) : (
              (n === "blind" ? ["0", "50", "100"] : ["20", "60", "100"]).map((v) => (
                <button key={v} onClick={() => write(n, v)}>API: {v} %</button>
              ))
            )}
          </Cell>
        );
      })}
      {id("temp") && (
        <p className="sensors">{cache[id("temp")]?.value} °C · {cache[id("lux")]?.value} lx · presence {cache[id("presence")]?.value}</p>
      )}
    </section>
  );
}

export default function App() {
  const [gw, setGw] = useState<Gateway>();
  const [cache, setCache] = useState<Cache>({});
  const [truth, setTruth] = useState<Truth>();
  const [bus, setBus] = useState<BusLine[]>([]);
  const [updates, setUpdates] = useState<Update[]>([]);
  const [log, setLog] = useState<string[]>([]);
  const [tau, setTau] = useState(20);
  const lastN = useRef(0);
  const say = (s: string) => setLog((l) => [s, ...l].slice(0, 6));

  useEffect(() => {
    let ws: WebSocket | undefined;
    let timer: number | undefined;
    Gateway.connect().then(async (g) => {
      setGw(g);
      setCache(await g.datapoints());
      ws = g.subscribe((u) => {
        setCache((c) => ({ ...c, [u.id]: { ...c[u.id], value: u.value, version: u.version, source: u.source } }));
        setUpdates((l) => [u, ...l].slice(0, 12));
      });
      timer = window.setInterval(async () => {
        setTruth(await g.truth());
        const lines = await g.buslog(lastN.current);
        if (lines.length) {
          lastN.current = lines[lines.length - 1].n;
          setBus((b) => [...lines.reverse(), ...b].slice(0, 14));
        }
        const fresh = await g.datapoints();           // freshness, pending marker
        setCache((c) => {
          const out = { ...c };
          for (const k of Object.keys(fresh)) out[k] = { ...fresh[k], ...(c[k] && c[k].version > fresh[k].version ? c[k] : {}) };
          return out;
        });
      }, 400);
    });
    return () => { ws?.close(); if (timer) window.clearInterval(timer); };
  }, []);

  if (!gw) return <p className="loading">connecting to the gateway ...</p>;
  const name = (id: string) => {
    for (const r of gw.cfg.rooms) for (const [n, i] of Object.entries(r.datapoints)) if (i === id) return `${r.title} ${n}`;
    return id;
  };
  const guarantee: Record<string, string> = {
    optimistic: "204 means: queued for the bus. The value shown is the last one written or heard, not what the device did.",
    confirmed: "204 means: the device reported this state after our telegram. It may have changed again since.",
    versioned: "204 means: the device reported this state and nothing else was heard on the bus. 409 means: changed under you.",
  };
  return (
    <main>
      <header>
        <h1>Floor 2 through the gateway</h1>
        <p className="design"><b>design: {gw.cfg.design}</b> · bus time runs at {gw.cfg.speed}× · gateway {gw.cfg.gateway_address}{gw.cfg.polite ? ", polite" : ""}</p>
        <p className="guarantee">{guarantee[gw.cfg.design]}</p>
        <p className="stats">
          bus: {truth?.bus.telegrams ?? 0} telegrams, load {((truth?.bus.load ?? 0) * 100).toFixed(1)} %, {truth?.bus.repetitions ?? 0} repetitions · gateway queue {truth?.queue ?? 0}
        </p>
      </header>
      <div className="layout">
        <div className="rooms">
          {gw.cfg.rooms.map((r) => <RoomCard key={r.key} room={r} cache={cache} truth={truth} gw={gw} say={say} />)}
        </div>
        <aside>
          <section className="panel">
            <h2>Race: API and wall switch on one light</h2>
            <p>The API writes the state the light has; the wall switch asks for the opposite, {tau} ms {tau >= 0 ? "later" : "earlier"} (bus time).</p>
            <input type="range" min={-60} max={80} step={2} value={tau} onChange={(e) => setTau(Number(e.target.value))} />
            <button id="race" onClick={() => gw.race("room1.light", tau).then(() => say(`race started, wall switch ${tau} ms after the API write`))}>run in Office 2.01</button>
            {truth?.race && <p className="race-result">API answer: <b>{truth.race.api_result ?? "waiting"}</b> · device now: <b>{truth.truth[truth.race.key]}</b> · API wrote {truth.race.api_writes}, wall pressed {truth.race.wall_presses}</p>}
          </section>
          <section className="panel">
            <h2>What subscribers were told</h2>
            <ol className="updates">{updates.map((u, i) => <li key={i}>{name(u.id)} = <b>{u.value}</b> <span>v{u.version} · {u.source}</span></li>)}</ol>
          </section>
          <section className="panel">
            <h2>API calls</h2>
            <ol className="updates">{log.map((l, i) => <li key={i}>{l}</li>)}</ol>
          </section>
          <section className="panel">
            <h2>Bus monitor</h2>
            <table>
              <thead><tr><th>ms</th><th>from</th><th>to</th><th>value</th><th>cause</th></tr></thead>
              <tbody>{bus.map((b) => (
                <tr key={b.n} className={b.attempt ? "rep" : ""}><td>{b.t_ms.toFixed(0)}</td><td>{b.src}</td><td>{b.dst}</td><td>{String(b.value)}</td><td>{b.origin}{b.attempt ? ` (repeat ${b.attempt})` : ""}</td></tr>
              ))}</tbody>
            </table>
          </section>
        </aside>
      </div>
    </main>
  );
}
