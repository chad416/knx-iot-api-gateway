"""Write the result tables into README.md and DOSSIER.md from results/summary.json.

Text between <!-- TABLE:name --> and <!-- /TABLE --> is replaced, so the tables cannot drift
away from the results file.

    python scripts/make_tables.py            rewrite the tables
    python scripts/make_tables.py --check    fail if a file would change
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {"optimistic": "D1 optimistic", "confirmed": "D2 confirmed", "versioned": "D3 versioned"}


def per(n: float, d: float, scale: float = 100.0, digits: int = 2) -> str:
    return "n/a" if not d else f"{n / d * scale:.{digits}f}"


def t_bus(S: dict) -> str:
    b, m = S["bus"], S["model_validation"]
    return "\n".join([
        "| Quantity | Value |", "|---|---|",
        f"| Bit time at 9600 bit/s | {b['bit_time_us']:.1f} us |",
        f"| Telegram with a 1-bit value (9 characters), incl. acknowledge and idle time | {b['telegram_ms_1bit_value']:.2f} ms |",
        f"| Telegram with a 1-byte value / 2-byte value | {b['telegram_ms_1byte_value']:.2f} / {b['telegram_ms_2byte_value']:.2f} ms |",
        f"| Shortest and longest telegram in the model (8 and 23 characters) | {m['model_range_ms'][0]:.2f} to {m['model_range_ms'][1]:.2f} ms (published: {m['published_range_ms'][0]} to {m['published_range_ms'][1]} ms) |",
        f"| Most 1-bit telegrams per second, formula / simulated back to back | {m['formula_telegrams_per_s']:.2f} / {m['simulated_telegrams_per_s']:.2f} |",
        f"| Most confirmed writes per second (command + status telegram) | {b['max_confirmed_writes_per_s']:.1f} |",
    ])


def t_designs(S: dict, profile: str) -> str:
    rows = ["| | " + " | ".join(NAMES[d] for d in NAMES) + " |", "|---|---|---|---|"]
    M = S["mixed"][profile]

    def row(label, fn):
        rows.append(f"| {label} | " + " | ".join(fn(M[d]) for d in NAMES) + " |")

    row("API writes in the run", lambda r: str(r["writes"]["writes"]))
    row("Answer time for switch writes, p50 / p95 / p99 (ms, at the gateway)",
        lambda r: "{p50:.0f} / {p95:.0f} / {p99:.0f}".format(**r["writes"]["latency_gateway_ms"]))
    row("Light really switches after, p50 (ms)", lambda r: f"{r['writes']['latency_effect_ms']['p50']:.0f}")
    row("Dimmer value / blind position confirmed after, p50 (s)",
        lambda r: f"{r['writes']['latency_confirmed_dimmer_value_ms']['p50'] / 1000:.1f} / {r['writes']['latency_confirmed_blind_position_ms']['p50'] / 1000:.1f}")
    row("Values answered 'done' before the device had acted",
        lambda r: f"{r['writes']['ack_before_effect']} of {r['writes']['ok_items']}")
    row("Answered 'done', own telegram was never executed (switches only)", lambda r: str(r["writes"]["ack_never_applied_fast"]))
    row("Answered 'done', device was already in another state", lambda r: str(r["writes"]["ack_untrue_at_response"]))
    row("Answered 409 conflict", lambda r: str(r["writes"]["conflict"]))
    row("Answered 504, although the device did switch", lambda r: str(r["writes"]["failed_but_applied"]))
    row("Toggles and steps computed from a state that was no longer true, nobody told / told",
        lambda r: f"{r['writes']['stale_intent']} / {r['writes']['stale_intent_reported']} of {r['writes']['toggle_like']}")
    row("Changes told to subscribers that never happened / real changes not told",
        lambda r: f"{r['notifications']['spurious']} / {r['notifications']['missed']} of {r['notifications']['real']}")
    row("Read straight after own write returns a value the device does not have (switch / moving datapoint)",
        lambda r: f"{per(r['reads_after_write']['fast']['stale'], r['reads_after_write']['fast']['n'], 100, 1)} % / "
                  f"{per(r['reads_after_write']['slow']['stale'], r['reads_after_write']['slow']['n'], 100, 1)} %")
    row("Read straight after own accepted write does not show the write and does not say so (moving datapoint)",
        lambda r: f"{per(r['reads_after_write']['slow']['ryw_broken'], r['reads_after_write']['slow']['n'], 100, 0)} %")
    row("Share of time the cache differs from the device (switches / moving datapoints)",
        lambda r: f"{r['staleness']['fast']['stale_time_fraction'] * 100:.3f} % / {r['staleness']['slow']['stale_time_fraction'] * 100:.2f} %")
    row("Longest time the cache was wrong about a switch (s)", lambda r: f"{r['staleness']['fast']['episode_max_s']:.1f}")
    row("Wall switch, press to light, p99 (ms)", lambda r: f"{r['wall_latency_ms']['p99']:.0f}")
    row("Bus load (share of time a frame is on the wire)", lambda r: f"{r['bus']['load_fraction'] * 100:.1f} %")
    return "\n".join(rows)


def t_burst(S: dict) -> str:
    names = {"unpaced_addr_1.1.250": "As fast as possible, gateway at 1.1.250",
             "unpaced_addr_1.1.255": "As fast as possible, gateway at 1.1.255",
             "paced_50_addr_1.1.250": "Paced to 50 % of the bus, 1.1.250",
             "paced_33_addr_1.1.250": "Paced to 33 %, 1.1.250", "paced_25_addr_1.1.250": "Paced to 25 %, 1.1.250",
             "polite_addr_1.1.250": "Polite (waits 3 bit times longer), 1.1.250",
             "polite_capped_addr_1.1.250": "Polite and capped at 40 % of the bus, 1.1.250"}
    rows = ["| Bus access of the gateway | Scene done, median (ms) | Wall switch, odd device number, median / worst (ms) | even device number, median / worst (ms) | Writes reported failed although they worked |",
            "|---|---|---|---|---|"]
    for k, r in S["scene_burst"].items():
        o, e = r["wall_odd_addr_ms"], r["wall_even_addr_ms"]
        rows.append(f"| {names[k]} | {r['fast_done_ms']['p50']:.0f} | {o['p50']:.0f} / {o['max']:.0f} | {e['p50']:.0f} / {e['max']:.0f} | "
                    f"{r['no_feedback_timeouts']} in {r['seeds']} runs |")
    return "\n".join(rows)


def t_sweep(S: dict) -> str:
    keys = (("unpaced_addr_1.1.250", "As fast as possible"), ("paced_50_addr_1.1.250", "Paced 50 %"),
            ("polite_addr_1.1.250", "Polite"), ("polite_capped_addr_1.1.250", "Polite, capped 40 %"))
    rows = ["Each cell: writes confirmed per second, answer time p50 (ms), wall switch press to light p99 (ms).", "",
            "| Offered writes/s | " + " | ".join(n for _, n in keys) + " |", "|---|---|---|---|---|"]
    n = len(S["rate_sweep"][keys[0][0]])
    for i in range(n):
        cells = []
        for k, _ in keys:
            r = S["rate_sweep"][k][i]
            wall = f"{r['wall_ms']['p99']:.0f}" if r["wall_ms"]["never"] == 0 else f"{r['wall_ms']['never']} of {r['wall_ms']['n'] + r['wall_ms']['never']} presses never served"
            cells.append(f"{r['ok_per_s']:.1f}, {r['gateway_latency_ms']['p50']:.0f}, {wall}")
        rows.append(f"| {S['rate_sweep'][keys[0][0]][i]['offered_per_s']:.0f} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def t_loss(S: dict) -> str:
    rows = ["| Chance that one station misses one frame | D1: 'done' but never applied | D2: 504 although applied | D3: 504 although applied | D2 longest wrong cache (s) | D3 longest wrong cache (s) | Real changes not told, D1 / D2 / D3 | Repetitions per 1000 telegrams |",
            "|---|---|---|---|---|---|---|---|"]
    for p, r in S["loss_sensitivity"].items():
        o, c, v = r["optimistic"], r["confirmed"], r["versioned"]
        rows.append(f"| {float(p):g} | {o['ok_but_never_applied_fast']} of {o['writes_answered_ok']} | {c['failed_but_applied']} | {v['failed_but_applied']} | "
                    f"{c['longest_stale_s']:.0f} | {v['longest_stale_s']:.0f} | "
                    f"{o['missed_notifications']} / {c['missed_notifications']} / {v['missed_notifications']} | "
                    f"{per(c['repetitions'], c['telegrams'], 1000, 1)} |")
    return "\n".join(rows)


def t_refresh(S: dict) -> str:
    rows = ["| Read-back of every actuator status | Telegrams/s on the bus | Longest wrong cache (s) | Share of time wrong | Wall switch p99 (ms) |", "|---|---|---|---|---|"]
    for r in S["refresh_trade"]:
        lab = "none" if r["refresh_period_s"] == 0 else f"every {r['refresh_period_s']:.0f} s"
        rows.append(f"| {lab} | {r['telegrams_per_s']:.2f} | {r['longest_stale_s']:.1f} | {r['stale_fraction_fast'] * 100:.3f} % | {r['wall_p99_ms']:.0f} |")
    return "\n".join(rows)


def t_poll(S: dict) -> str:
    rows = ["| Poll period (s) | Reads served from | Bus telegrams/s asked for | Bus load | Reads dropped | App writes ok / refused | Poll answer p50 (ms) |", "|---|---|---|---|---|---|---|"]
    for kind in ("cached", "read_through"):
        for r in S["poll_everything"][kind]:
            rows.append(f"| {r['period_s']:g} | {'cache' if kind == 'cached' else 'bus (read-through)'} | {r['offered_bus_telegrams_per_s']:.0f} | {r['bus_load'] * 100:.0f} % | "
                        f"{r['reads_dropped_queue_full']} | {r['app_writes_ok']} / {r['app_writes_rejected']} | {r['poll_latency_ms']['p50']:.0f} |")
    return "\n".join(rows)


def t_flood(S: dict) -> str:
    rows = ["| Queue | Per-client limit | Normal app: writes ok of sent | its answer p50 / p95 (ms) | Flooder: ok / refused of sent | Wall switch p99 (ms) |", "|---|---|---|---|---|---|"]
    for r in S["flooding_writer"]:
        rows.append(f"| {'round robin' if r['fair_queue'] else 'first in, first out'} | {'10 writes/s' if r['client_rate_limit'] else 'none'} | "
                    f"{r['app_ok']} of {r['app_writes']} | {r['app_latency_ms']['p50']:.0f} / {r['app_latency_ms']['p95']:.0f} | "
                    f"{r['flooder_ok']} / {r['flooder_rejected']} of {r['flooder_sent']} | {r['wall_ms']['p99']:.0f} |")
    return "\n".join(rows)


def t_scan(S: dict) -> str:
    rows = ["| | D1 optimistic | D2 confirmed | D3 versioned |", "|---|---|---|---|"]
    R = S["race_scan"]
    f = lambda v: "none" if v is None else f"{v[0]:.0f} to {v[1]:.0f} ms"
    rows.append("| Answered 'done' before the device acted | " + " | ".join(f"{R[d]['answered_before_effect_fraction'] * 100:.0f} %" for d in NAMES) + " |")
    rows.append("| Wall-press offsets with a false 'done' | " + " | ".join(f(R[d]["false_confirm_window_ms"]) for d in NAMES) + " |")
    rows.append("| Width of that window, weighted by probability | " + " | ".join(f"{R[d]['false_confirm_probability_mass_ms']:.0f} ms" for d in NAMES) + " |")
    rows.append("| Same offsets answered 409 instead | " + " | ".join(f"{R[d]['conflict_reported_mass_ms']:.0f} ms" for d in NAMES) + " |")
    rows.append("| Offsets where subscribers saw a change that did not happen | " + " | ".join(f(R[d]["flicker_window_ms"]) for d in NAMES) + " |")
    W = S["rmw_scan"]
    rows.append("| Two API clients step one dimmer: window in which an update is lost silently | "
                + " | ".join(f"{W[d]['lost_width_ms']:.0f} ms" for d in NAMES) + " |")
    rows.append(f"| D3 with versions but without the in-flight rule | | | {W['versioned_without_pending_rule']['lost_width_ms']:.0f} ms |")
    return "\n".join(rows)


def t_http(S: dict) -> str:
    h = S["http"]
    rows = ["| Request (FastAPI process, development laptop) | Handler time p50 / p95 (ms) |", "|---|---|"]
    for k, lab in (("get_one_datapoint", "GET one datapoint"), ("get_all_64_datapoints", "GET all 64 datapoints"),
                   ("put_one_value_optimistic", "PUT one value (optimistic design, no bus wait)")):
        rows.append(f"| {lab} | {h[k]['p50_ms']:.2f} / {h[k]['p95_ms']:.2f} |")
    return "\n".join(rows)


def t_react(S: dict) -> str:
    rows = ["| Actuator reaction time (assumed range, ms) | Confirmed answer p50 / p95 (ms) |", "|---|---|"]
    for r in S["reaction_sensitivity"]:
        rows.append(f"| {r['react_ms'][0]:.0f} to {r['react_ms'][1]:.0f} | {r['confirmed_answer_ms']['p50']:.0f} / {r['confirmed_answer_ms']['p95']:.0f} |")
    return "\n".join(rows)


def main() -> None:
    check = "--check" in sys.argv
    with open(os.path.join(ROOT, "results", "summary.json"), encoding="utf-8") as f:
        S = json.load(f)
    tables = {"bus": t_bus(S), "designs_office": t_designs(S, "office"), "designs_contended": t_designs(S, "contended"),
              "burst": t_burst(S), "sweep": t_sweep(S), "loss": t_loss(S), "refresh": t_refresh(S), "poll": t_poll(S),
              "flood": t_flood(S), "scan": t_scan(S), "http": t_http(S), "react": t_react(S)}
    changed = False
    for name in ("README.md", "DOSSIER.md"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()

        def repl(m: re.Match) -> str:
            return f"<!-- TABLE:{m.group(1)} -->\n{tables[m.group(1)]}\n<!-- /TABLE -->"

        new = re.sub(r"<!-- TABLE:(\w+) -->.*?<!-- /TABLE -->", repl, text, flags=re.S)
        if new != text:
            changed = True
            if not check:
                open(path, "w", encoding="utf-8", newline="\n").write(new)
                print("updated", name)
    if check and changed:
        sys.exit("tables are not up to date with results/summary.json: run python scripts/make_tables.py")
    if not changed:
        print("tables are up to date")


if __name__ == "__main__":
    main()
