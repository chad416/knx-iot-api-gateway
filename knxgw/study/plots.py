"""Figures for results/. Static PNGs; the numbers behind every mark are in summary.json."""
from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
DESIGN = {"optimistic": ("D1 optimistic", ORANGE), "confirmed": ("D2 confirmed", BLUE), "versioned": ("D3 versioned", AQUA)}


def _style(ax, xlabel="", ylabel="", title=""):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(AXIS)
        ax.spines[s].set_linewidth(0.8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=3, width=0.8)
    ax.set_xlabel(xlabel, color=INK2, fontsize=9.5)
    ax.set_ylabel(ylabel, color=INK2, fontsize=9.5)
    if title:
        ax.set_title(title, color=INK, fontsize=10.5, loc="left", pad=8)


def _fig(w, h, rows=1, cols=1, **kw):
    fig, ax = plt.subplots(rows, cols, figsize=(w, h), facecolor=SURFACE, **kw)
    return fig, ax


def _save(fig, out_dir, name):
    fig.savefig(os.path.join(out_dir, name), dpi=150, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def _legend(ax, **kw):
    leg = ax.legend(frameon=False, fontsize=8.5, labelcolor=INK2, **kw)
    return leg


def race_map(S, out_dir):
    fig, axes = _fig(9.2, 6.6, 3, 1, sharex=True)
    parts = (("ok_true", GOOD, "answered done, and it was true"),
             ("ok_not_yet", WARN, "answered done before the device acted"),
             ("ok_false", CRIT, "answered done, light was already on again"),
             ("conflict", BLUE, "answered 409: overwritten from the bus"))
    for ax, key in zip(axes, ("optimistic", "confirmed", "versioned")):
        rows = S["race_scan"][key]["rows"]
        x = np.array([r["tau_ms"] for r in rows])
        width = (x[1] - x[0]) * 0.86
        bottom = np.zeros(len(x))
        for k, col, lab in parts:
            y = np.array([r[k] / r["n"] for r in rows])
            ax.bar(x, y, width, bottom=bottom, color=col, label=lab, linewidth=0)
            bottom += y
        fl = np.array([r["spurious"] > 0 for r in rows])
        if fl.any():
            ax.scatter(x[fl], np.full(fl.sum(), -0.09), marker="|", s=40, color=INK, linewidths=1.2,
                       label="subscribers saw a change that did not happen", clip_on=False)
        _style(ax, ylabel="share of runs", title=DESIGN[key][0])
        ax.set_ylim(-0.16, 1.05)
        ax.set_yticks([0, 0.5, 1.0])
        ax.axvline(0, color=AXIS, linewidth=0.8)
    axes[-1].set_xlabel("wall switch pressed this many ms after the API call arrived (opposite intent)", color=INK2, fontsize=9.5)
    h, l = [], []
    for ax in axes:
        for hh, ll in zip(*ax.get_legend_handles_labels()):
            if ll not in l:
                h.append(hh)
                l.append(ll)
    fig.legend(h, l, frameon=False, fontsize=8.5, labelcolor=INK2, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.03))
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    _save(fig, out_dir, "fig1_race_map.png")


def latency_cdf(S, raw, out_dir):
    fig, ax = _fig(8.6, 4.6)
    slot = S["bus"]["telegram_ms_1bit_value"]

    def cdf(vals, color, label, lw=2.0, ls="-"):
        v = np.sort(np.array(vals))
        if len(v) == 0:
            return
        ax.plot(v, np.arange(1, len(v) + 1) / len(v), color=color, linewidth=lw, linestyle=ls, label=label,
                drawstyle="steps-post")

    r1, r2 = raw[("office", "optimistic")], raw[("office", "confirmed")]
    cdf(r1["gateway"], ORANGE, "D1 optimistic: API answer (2 ms handler time assumed)")
    cdf(r2["effect"], YELLOW, "light actually switches (any design)")
    cdf(r2["gateway"], BLUE, "D2 / D3: API answer = status telegram heard")
    for k, lab in ((1, "1 telegram"), (2, "2 telegrams")):
        ax.axvline(k * slot, color=AXIS, linewidth=0.9)
        ax.text(k * slot * (0.97 if k == 1 else 1.03), 1.035, f"{lab}, {k * slot:.1f} ms", color=MUTED, fontsize=8,
                va="bottom", ha="right" if k == 1 else "left", clip_on=False)
    h = S["http"]["put_one_value_optimistic"]["p50_ms"]
    ax.axvline(h, color=AXIS, linewidth=0.9)
    ax.text(h * 0.97, 1.035, f"measured HTTP handler, {h:.1f} ms", color=MUTED, fontsize=8, va="bottom",
            ha="left", clip_on=False)
    ax.set_xscale("log")
    ax.set_xlim(0.8, 1000)
    ax.set_ylim(0, 1.02)
    _style(ax, "ms after the request reached the gateway (log scale)", "share of writes to switch datapoints",
           "")
    fig.suptitle("Where the time goes: the bus, not the server", color=INK, fontsize=10.5, x=0.125, ha="left", y=1.0)
    _legend(ax, loc="center left", bbox_to_anchor=(0.14, 0.5))
    _save(fig, out_dir, "fig2_latency_cdf.png")


def overload(S, out_dir):
    fig, (a, b) = _fig(10.4, 4.3, 1, 2)
    series = (("unpaced_addr_1.1.250", ORANGE, "sends as fast as it can"),
              ("paced_50_addr_1.1.250", BLUE, "paced to 50 % of the bus"),
              ("polite_addr_1.1.250", AQUA, "polite: waits 3 bit times longer"))
    for key, col, lab in series:
        rows = S["rate_sweep"][key]
        x = [r["offered_per_s"] for r in rows]
        a.plot(x, [r["ok_per_s"] for r in rows], color=col, linewidth=2, marker="o", markersize=4.5, label=lab)
        b.plot(x, [max(r["wall_ms"]["p99"], 1.0) if r["wall_ms"]["never"] == 0 else 60000 for r in rows],
               color=col, linewidth=2, marker="o", markersize=4.5, label=lab)
    cap = S["bus"]["max_confirmed_writes_per_s"]
    a.axhline(cap, color=AXIS, linewidth=0.9)
    a.text(2, cap + 0.5, f"bus limit {cap:.1f} /s (one command + one status telegram each)", color=MUTED, fontsize=8)
    lim = max(r["offered_per_s"] for r in S["rate_sweep"]["polite_addr_1.1.250"])
    a.plot([0, cap], [0, cap], color=GRID, linewidth=1)
    a.set_xlim(0, lim + 1)
    a.set_ylim(0, cap + 4)
    _style(a, "API writes offered per second", "writes confirmed per second", "Goodput")
    _legend(a, loc="lower right")
    b.set_yscale("log")
    b.axhline(100, color=AXIS, linewidth=0.9)
    b.text(2, 112, "100 ms", color=MUTED, fontsize=8)
    b.set_xlim(0, lim + 1)
    _style(b, "API writes offered per second", "wall switch: press to light, p99, ms (log)",
           "What a person at the wall switch gets")
    b.text(1.5, 55000, "top of scale: some presses were still waiting when the run ended", color=MUTED, fontsize=8,
           ha="left", va="top")
    _save(fig, out_dir, "fig3_overload.png")


def scene_burst(S, out_dir):
    fig, ax = _fig(8.8, 4.2)
    keys = list(S["scene_burst"].keys())
    names = {"unpaced_addr_1.1.250": "as fast as possible, address 1.1.250",
             "unpaced_addr_1.1.255": "as fast as possible, address 1.1.255",
             "paced_50_addr_1.1.250": "paced 50 %, address 1.1.250",
             "paced_33_addr_1.1.250": "paced 33 %, address 1.1.250",
             "paced_25_addr_1.1.250": "paced 25 %, address 1.1.250",
             "polite_addr_1.1.250": "polite (+3 bit times), address 1.1.250"}
    y = np.arange(len(keys))[::-1]
    hgt = 0.34
    for off, part, col, lab in ((hgt / 2 + 0.02, "wall_odd_addr_ms", BLUE, "wall switch with odd device number (1.1.31, .33 ...)"),
                                (-hgt / 2 - 0.02, "wall_even_addr_ms", AQUA, "wall switch with even device number (1.1.32, .34 ...)")):
        p50 = [S["scene_burst"][k][part]["p50"] for k in keys]
        mx = [S["scene_burst"][k][part]["max"] for k in keys]
        ax.barh(y + off, p50, hgt, color=col, label=lab, linewidth=0)
        ax.hlines(y + off, p50, mx, color=col, linewidth=1.2)
        ax.scatter(mx, y + off, color=col, s=14, zorder=3)
        for yy, a_, b_ in zip(y + off, p50, mx):
            ax.text(b_ + 18, yy, f"{a_:.0f} / {b_:.0f}", va="center", fontsize=8, color=INK2)
    ax.set_yticks(y)
    ax.set_yticklabels([names[k] for k in keys], fontsize=8.5, color=INK2)
    _style(ax, "press to light, ms: bar = median, dot = worst of the runs", "",
           f"A wall switch pressed while the gateway sends a {S['scene_burst'][keys[0]]['items']}-telegram scene")
    ax.grid(axis="y", visible=False)
    _legend(ax, loc="lower right")
    _save(fig, out_dir, "fig4_scene_burst.png")


def lost_update(S, out_dir):
    fig, ax = _fig(8.6, 4.0)
    series = (("optimistic", ORANGE, "D1 optimistic"), ("confirmed", BLUE, "D2 confirmed"),
              ("versioned_without_pending_rule", YELLOW, "D3 with versions only"),
              ("versioned", AQUA, "D3 versions + in-flight rule"))
    for i, (key, col, lab) in enumerate(series):
        rows = S["rmw_scan"][key]["rows"]
        x = [r["tau_ms"] for r in rows]
        yv = [r["lost_silent"] / r["n"] + 0.012 * (len(series) - 1 - i) for r in rows]
        ax.plot(x, yv, color=col, linewidth=2, drawstyle="steps-mid", label=f"{lab}: {S['rmw_scan'][key]['lost_width_ms']:.0f} ms wide")
    ax.axvline(0, color=AXIS, linewidth=0.8)
    ax.set_ylim(-0.03, 1.12)
    ax.set_yticks([0, 0.5, 1])
    _style(ax, "second client starts this many ms after the first (both: read brightness, add 20 %, write)",
           "share of runs with one update lost", "Two API clients step the same dimmer: when is an update lost without anyone being told?")
    _legend(ax, loc="center right")
    _save(fig, out_dir, "fig5_lost_update_window.png")


def refresh_trade(S, out_dir):
    fig, ax = _fig(7.6, 4.0)
    rows = S["refresh_trade"]
    x = [r["telegrams_per_s"] for r in rows]
    yv = [max(r["longest_stale_s"], 0.05) for r in rows]
    ax.plot(x, yv, color=BLUE, linewidth=2, marker="o", markersize=6)
    for r, xx, yy in zip(rows, x, yv):
        lab = "no refresh" if r["refresh_period_s"] == 0 else f"every {r['refresh_period_s']:.0f} s"
        ax.annotate(lab, (xx, yy), textcoords="offset points", xytext=(7, 6), fontsize=8.5, color=INK2)
    ax.set_yscale("log")
    _style(ax, "telegrams per second on the bus (traffic + status read-back)",
           "longest time the cache was wrong, s (log)",
           "Read-back of all 37 actuator states: what a staleness bound costs")
    _save(fig, out_dir, "fig6_refresh_trade.png")


def make_all(S, raw, out_dir):
    plt.rcParams["font.family"] = "DejaVu Sans"
    race_map(S, out_dir)
    latency_cdf(S, raw, out_dir)
    overload(S, out_dir)
    scene_burst(S, out_dir)
    lost_update(S, out_dir)
    refresh_trade(S, out_dir)
