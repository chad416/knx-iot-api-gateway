"""One deterministic scenario per anomaly, then the overload cases. Simulated time, fixed seeds."""
import pytest

from knxgw.gateway.core import _same
from knxgw.simtime import ms, sec
from knxgw.study import capacity
from knxgw.study.consistency import race_scan, rmw_scan, run_mixed
from knxgw.study.harness import SimClient, World


def world(design, **gw):
    return World(design, 1, gw or None, {"sensors": False})


# ---- A1 stale read -----------------------------------------------------------------------
def test_a1_optimistic_read_returns_a_state_the_device_does_not_have_yet():
    w = world("optimistic")
    d = w.inst.dp("room1.blind")
    w.gw.write("app", [(d.id, 100.0, None)], lambda r: None)
    w.run(10)                                           # the blind needs 30 s
    assert w.gw.snapshot(d.id)["value"] == 100.0 and w.inst.truth.last("room1.blind") == 0.0


def test_a1_confirmed_read_lags_a_wall_press_by_the_status_telegram():
    w = world("confirmed")
    d = w.inst.dp("room1.light")
    w.inst.press("room1.light", True)
    seen = []
    for t in range(0, 80, 2):
        w.sched.at(ms(t), lambda: seen.append((w.sched.now, w.gw.snapshot(d.id)["value"],
                                               w.inst.truth.last("room1.light"))))
    w.run(1)
    stale = [t for t, cache, truth in seen if cache != truth]
    assert stale and ms(10) < max(stale) - min(stale) < ms(30)


# ---- A2 acknowledged, but not true --------------------------------------------------------
def test_a2_confirmed_design_reports_done_although_the_wall_switch_already_won():
    res = race_scan("confirmed", [20.0], seeds=5)[0]
    assert res["ok_false"] == 5 and res["final_on"] == 5


def test_a2_versioned_design_reports_the_same_race_as_conflict():
    res = race_scan("versioned", [20.0], seeds=5)[0]
    assert res["conflict"] == 5 and res["ok_false"] == 0


def test_a2_false_confirmation_window_is_about_one_status_latency_wide():
    res = race_scan("confirmed", list(range(-10, 61, 2)), seeds=5)
    bad = [r["tau_ms"] for r in res if r["ok_false"]]
    assert 15 <= max(bad) - min(bad) <= 40


def test_a2_optimistic_always_answers_before_the_device_acted():
    res = race_scan("optimistic", [-50.0, 0.0, 50.0], seeds=3)
    assert all(r["ok_not_yet"] == r["n"] for r in res)


# ---- A3 two API clients interleave --------------------------------------------------------
def test_a3_confirmed_cache_loses_an_update_when_two_clients_step_a_dimmer():
    res = rmw_scan("confirmed", [100.0], seeds=3)[0]
    assert res["lost_silent"] == 3


def test_a3_optimistic_cache_serialises_api_clients_unless_they_read_at_the_same_moment():
    late, same = rmw_scan("optimistic", [100.0, -100.0], seeds=3)
    assert late["final_80"] == 3 and same["lost_silent"] == 3


def test_a3_versioned_design_keeps_both_updates():
    res = rmw_scan("versioned", [-100.0, 0.0, 100.0, 300.0], seeds=3)
    assert all(r["final_80"] == r["n"] for r in res)


def test_a3_versions_alone_are_not_enough_while_the_dimmer_is_still_moving():
    res = rmw_scan("versioned", [100.0], seeds=3, gw={"cond_blocks_on_pending": False})[0]
    assert res["lost_silent"] == 3


# ---- A4 status feedback versus command echo ------------------------------------------------
def test_a4_optimistic_subscribers_see_a_light_flicker_that_never_happened():
    res = {r["tau_ms"]: r for r in race_scan("optimistic", [-20.0, 20.0], seeds=5)}
    assert res[-20.0]["spurious"] >= 5 and res[20.0]["spurious"] >= 5


@pytest.mark.parametrize("design", ["confirmed", "versioned"])
def test_a4_feedback_only_cache_tells_exactly_what_happened(design):
    res = race_scan(design, [-20.0, 0.0, 20.0], seeds=5)
    assert all(r["spurious"] == 0 for r in res)


# ---- A5 lost telegram ---------------------------------------------------------------------
def deaf_actuator(w, key):
    d = w.inst.dp(key)
    w.inst.sw_act.p_rx_miss = 1.0                       # the wall switch still acknowledges
    return d


def test_a5_silent_loss_optimistic_says_ok_and_keeps_a_wrong_value():
    w = world("optimistic")
    d = deaf_actuator(w, "room1.light")
    box = {}
    w.gw.write("app", [(d.id, True, None)], lambda r: box.update(r=r))
    w.run(5)
    assert box["r"].status == "ok" and w.gw.stats["bus_nack"] == 0
    assert w.gw.cache[d.id].value is True and w.inst.truth.last("room1.light") is False


@pytest.mark.parametrize("design", ["confirmed", "versioned"])
def test_a5_silent_loss_is_reported_as_failure_when_feedback_is_required(design):
    w = world(design)
    d = deaf_actuator(w, "room1.light")
    box = {}
    w.gw.write("app", [(d.id, True, None)], lambda r: box.update(r=r, t=w.sched.now))
    w.run(5)
    assert box["r"].status == "failed" and box["r"].items[0]["outcome"] == "no_feedback"
    assert w.gw.cache[d.id].value is False
    assert ms(400) < box["t"] < ms(450)                 # the price: the full feedback time-out


def test_a5_remaining_anomaly_gateway_misses_the_status_and_reports_a_failure_that_was_none():
    w = world("versioned")
    d = w.inst.dp("room1.light")
    box = {}
    w.gw.write("app", [(d.id, True, None)], lambda r: box.update(r=r))
    w.sched.at(ms(16), lambda: setattr(w.gw, "p_rx_miss", 1.0))     # deaf for the status frame only
    w.sched.at(ms(60), lambda: setattr(w.gw, "p_rx_miss", 0.0))
    w.run(60)
    assert w.inst.truth.last("room1.light") is True          # the light did switch
    assert box["r"].status == "failed"                       # the client was told it did not
    assert w.gw.cache[d.id].value is False                   # and the cache stays wrong, unnoticed


def test_a5_refresh_bounds_that_staleness_at_a_price_in_bus_load():
    w = world("versioned", refresh_period_s=30.0)
    d = w.inst.dp("room1.light")
    w.gw.write("app", [(d.id, True, None)], lambda r: None)
    w.sched.at(ms(16), lambda: setattr(w.gw, "p_rx_miss", 1.0))
    w.sched.at(ms(60), lambda: setattr(w.gw, "p_rx_miss", 0.0))
    w.run(31)
    assert w.gw.cache[d.id].value is True
    per_s = w.inst.line.stats.telegrams / 31
    assert 2.0 < per_s < 3.0                                 # 37 datapoints, read + response, every 30 s


# ---- overload -----------------------------------------------------------------------------
def test_unpaced_gateway_starves_wall_switches_and_its_own_feedback():
    bad = capacity.scene_burst("unpaced_addr_1.1.250", seeds=18)
    good = capacity.scene_burst("polite_addr_1.1.250", seeds=18)
    assert bad["wall_ms"]["p50"] > 300 and bad["no_feedback_timeouts"] > 0
    assert good["wall_ms"]["max"] < 150 and good["no_feedback_timeouts"] == 0
    assert good["fast_done_ms"]["p50"] < 1.2 * bad["fast_done_ms"]["p50"]


def test_wall_switch_delay_depends_on_the_bit_pattern_of_its_address():
    r = capacity.scene_burst("unpaced_addr_1.1.250", seeds=36)
    assert r["wall_odd_addr_ms"]["p50"] > 3 * r["wall_even_addr_ms"]["p50"]


def test_confirmed_write_capacity_is_below_half_the_raw_telegram_rate():
    r = capacity.write_rate_sweep([40.0], seconds=20.0, gw={"polite": True, "pace_util": 1.0})[0]
    assert 15.0 < r["ok_per_s"] < 24.5 and r["rejected"] > 0
    assert r["wall_ms"]["max"] < 200


def test_polling_served_from_the_cache_puts_nothing_on_the_bus():
    cached = capacity.poll_everything([0.5], read_through=False, seconds=10.0)[0]
    through = capacity.poll_everything([0.5], read_through=True, seconds=10.0)[0]
    assert cached["bus_load"] < 0.15 and cached["app_writes_rejected"] == 0
    assert through["bus_load"] > 0.6 and through["app_writes_rejected"] > 0


def test_fair_queue_needs_a_per_client_limit_to_protect_the_other_clients():
    fifo = capacity.flooding_writer(False, False, seconds=10.0)
    fair = capacity.flooding_writer(True, False, seconds=10.0)
    limited = capacity.flooding_writer(True, True, seconds=10.0)
    assert fifo["app_ok"] < fifo["app_writes"] and fair["app_ok"] < fair["app_writes"]
    assert limited["app_ok"] == limited["app_writes"]
    assert limited["flooder_rejected"] > 0.7 * limited["flooder_sent"]


# ---- determinism ---------------------------------------------------------------------------
def test_mixed_traffic_run_is_reproducible():
    a = run_mixed("versioned", "contended", 0.25, seed=3, inst=dict(p_rx_miss=1e-3))
    b = run_mixed("versioned", "contended", 0.25, seed=3, inst=dict(p_rx_miss=1e-3))
    assert a == b
    c = run_mixed("versioned", "contended", 0.25, seed=4, inst=dict(p_rx_miss=1e-3))
    assert c["writes"]["writes"] != a["writes"]["writes"] or c["bus"] != a["bus"]


def test_mixed_traffic_feedback_designs_never_acknowledge_a_write_that_was_not_applied():
    for design in ("confirmed", "versioned"):
        r = run_mixed(design, "contended", 0.5, seed=2, inst=dict(p_rx_miss=1e-3))
        assert r["writes"]["ack_never_applied"] == 0 and r["notifications"]["spurious"] == 0
    r = run_mixed("optimistic", "contended", 0.5, seed=2, inst=dict(p_rx_miss=1e-3))
    assert r["writes"]["ack_before_effect"] > 0 and r["notifications"]["spurious"] > 0
