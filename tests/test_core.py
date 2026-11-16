"""Gateway core: cache, queue, the three designs. All in simulated time."""
import pytest

from knxgw.gateway.core import DESIGNS, GatewayConfig, GatewayCore
from knxgw.simtime import ms, sec
from knxgw.study.harness import SimClient, World


def world(design="versioned", **gw):
    return World(design, 1, gw or None, {"sensors": False})


def write(w, key, value, client="app", ifv=None, at=None):
    box = {}
    d = w.inst.dp(key)
    fn = lambda: w.gw.write(client, [(d.id, value, ifv)], lambda r: box.update(res=r, t=w.sched.now))
    w.sched.at(at if at is not None else w.sched.now, fn)
    return box


def test_unknown_design_is_refused():
    w = world()
    with pytest.raises(ValueError):
        GatewayCore(w.inst, GatewayConfig(design="magic"))


@pytest.mark.parametrize("design", DESIGNS)
def test_a_write_reaches_the_device(design):
    w = world(design)
    box = write(w, "room1.light", True)
    w.run(1.0)
    assert box["res"].status == "ok"
    assert w.inst.truth.last("room1.light") is True
    assert w.gw.cache[w.inst.dp("room1.light").id].value is True


def test_optimistic_answers_before_the_bus_has_carried_anything():
    w = world("optimistic")
    box = write(w, "room1.light", True)
    w.run(1.0)
    assert box["t"] == 0
    assert w.inst.truth.changes("room1.light")[-1][0] > ms(20)


def test_confirmed_answers_after_the_status_telegram_two_slots_later():
    w = world("confirmed")
    box = write(w, "room1.light", True)
    w.run(1.0)
    applied = w.inst.truth.changes("room1.light")[-1][0]
    assert box["t"] > applied
    assert ms(30) < box["t"] < ms(60)            # command slot + reaction + status frame


def test_freshness_is_the_time_of_the_last_status_telegram_not_of_the_write():
    w = world("optimistic")
    d = w.inst.dp("room1.light")
    write(w, "room1.light", True, at=sec(5))
    w.run(5.001)
    snap = w.gw.snapshot(d.id)
    assert snap["value"] is True and snap["source"] == "optimistic"
    assert snap["confirmed_at"] == 0 and snap["age_ns"] > sec(5) - 1     # written, not yet confirmed
    w.run(1.0)
    snap = w.gw.snapshot(d.id)
    assert snap["source"] == "bus-status" and snap["confirmed_at"] > sec(5)


def test_version_counts_state_changes():
    w = world("versioned")
    d = w.inst.dp("room1.light")
    v0 = w.gw.cache[d.id].version
    write(w, "room1.light", True)
    w.run(1)
    write(w, "room1.light", True)               # same value again: confirmed, no new version
    w.run(1)
    assert w.gw.cache[d.id].version == v0 + 1
    w.inst.press("room1.light", False)
    w.run(1)
    assert w.gw.cache[d.id].version == v0 + 2


def test_conditional_write_with_old_version_is_refused_and_nothing_is_sent():
    w = world("versioned")
    d = w.inst.dp("room1.light")
    v = w.gw.cache[d.id].version
    w.inst.press("room1.light", True)
    w.run(1)
    frames = w.inst.line.stats.frames
    box = write(w, "room1.light", False, ifv=v)
    w.run(1)
    assert box["res"].status == "conflict"
    assert w.inst.line.stats.frames == frames and w.inst.truth.last("room1.light") is True


def test_conditional_write_is_refused_while_a_wall_command_is_in_flight():
    # the wall telegram has been heard, its status has not: the cache still shows the old state
    w = world("versioned")
    d = w.inst.dp("room1.light")
    v = w.gw.cache[d.id].version
    w.inst.press("room1.light", True)
    w.run(0.016)                                # frame is over, actuator has not reported yet
    assert w.gw.cache[d.id].version == v
    box = write(w, "room1.light", False, ifv=v)
    w.run(1)
    assert box["res"].status == "conflict"


def test_conditional_write_is_refused_while_another_api_write_is_pending():
    w = world("versioned")
    d = w.inst.dp("room1.dim.val")
    v = w.gw.cache[d.id].version
    a = write(w, "room1.dim.val", 60.0, client="A", ifv=v)
    b = write(w, "room1.dim.val", 60.0, client="B", ifv=v, at=ms(200))    # A's dimmer is still ramping
    w.run(5)
    assert a["res"].status == "accepted" and b["res"].status == "conflict"


def test_read_your_writes_blocks_the_writer_and_nobody_else():
    w = world("versioned")
    d = w.inst.dp("room1.light")
    got = {}
    write(w, "room1.light", True, client="A")
    w.sched.at(ms(1), lambda: w.gw.read("A", [d.id], lambda s: got.update(A=(w.sched.now, s[0]))))
    w.sched.at(ms(1), lambda: w.gw.read("B", [d.id], lambda s: got.update(B=(w.sched.now, s[0]))))
    w.run(1)
    assert got["B"][0] == ms(1) and got["B"][1]["value"] is False and got["B"][1]["pending"] is True
    assert got["A"][0] > ms(30) and got["A"][1]["value"] is True


def test_slow_datapoint_is_accepted_and_shows_a_pending_target():
    w = world("versioned")
    d = w.inst.dp("room1.blind")
    box = write(w, "room1.blind", 100.0)
    w.run(1)
    snap = w.gw.snapshot(d.id)
    assert box["res"].status == "accepted"
    assert snap["value"] == 0.0 and snap["pending"] and snap["pending_target"] == 100.0
    w.run(35)
    snap = w.gw.snapshot(d.id)
    assert snap["value"] == 100.0 and not snap["pending"]


def test_queue_bound_and_retry_after():
    w = world("confirmed", queue_max=8, client_burst=1000)
    res = []
    for i in range(1, 10):
        d = w.inst.dp(f"room{i}.light")
        w.gw.write("c", [(d.id, True, None)], res.append)
    for i in range(1, 4):
        d = w.inst.dp(f"room{i}.dim.sw")
        w.gw.write("c", [(d.id, True, None)], res.append)
    full = [r for r in res if r.status == "full"]
    assert full and all(r.retry_after_s > 0 for r in full)
    assert w.gw.stats["q_max"] <= 8


def test_client_token_bucket():
    w = world("optimistic", client_rate=2.0, client_burst=3)
    d = w.inst.dp("room1.light")
    res = []
    for _ in range(5):
        w.gw.write("c", [(d.id, True, None)], res.append)
    assert [r.status for r in res] == ["ok", "ok", "ok", "rate", "rate"]
    assert res[-1].retry_after_s == pytest.approx(0.5, abs=0.01)
    w.run(1.0)
    w.gw.write("c", [(d.id, True, None)], res.append)
    assert res[-1].status == "ok"


def test_round_robin_between_clients_and_interactive_before_bulk():
    w = world("confirmed", pace_util=1.0)
    order = []
    w.gw.op_hooks.append(lambda op: order.append(op.client))
    scene = [(w.inst.dp(f"room{i}.light").id, True, None) for i in range(1, 9)]
    w.gw.write("bulk", scene, lambda r: None)
    for i in range(1, 4):
        w.gw.write("a", [(w.inst.dp(f"room{i}.dim.sw").id, True, None)], lambda r: None)
        w.gw.write("b", [(w.inst.dp(f"room{i + 3}.dim.sw").id, True, None)], lambda r: None)
    w.run(3)
    sent = sorted(w.obs.ops.values(), key=lambda o: o.t_tx)
    who = [o.client for o in sent]
    # the first scene item was already on its way; then the single writes, alternating, then the rest
    assert who[1:7] == ["a", "b", "a", "b", "a", "b"] and set(who[7:]) == {"bulk"}


def test_optimistic_cache_reverts_when_the_bus_gives_no_acknowledge():
    w = world("optimistic")
    d = w.inst.dp("room1.dim.val")                 # only the dim actuator is a member of this address
    for st in w.inst.line.stations:
        if d.cmd_ga in st.group_table:
            st.p_rx_miss = 1.0
    notes = []
    w.gw.listeners.append(lambda ev: notes.append((ev["value"], ev["source"])))
    write(w, "room1.dim.val", 60.0)
    w.run(2)
    assert [n[1] for n in notes] == ["optimistic", "revert"]
    assert w.gw.cache[d.id].value == 0.0 and w.gw.stats["bus_nack"] == 1


def test_refresh_repairs_a_cache_that_missed_a_status_telegram():
    def run(refresh):
        w = world("confirmed", refresh_period_s=refresh)
        d = w.inst.dp("room1.light")
        w.gw.p_rx_miss = 1.0                       # the gateway is deaf for one telegram exchange
        w.inst.press("room1.light", True)
        w.run(0.2)
        w.gw.p_rx_miss = 0.0
        assert w.gw.cache[d.id].value is False and w.inst.truth.last("room1.light") is True
        w.run(40)
        return w.gw.cache[d.id].value, w
    stale, _ = run(0.0)
    fixed, w = run(30.0)
    assert stale is False and fixed is True
    assert w.gw.stats["refresh_tx"] >= 37


def test_gateway_does_not_acknowledge_telegrams_it_only_monitors():
    w = world()
    assert w.gw.group_table == set()


def test_polite_gateway_waits_three_bit_times_longer_than_other_stations():
    w = world("versioned", polite=True)
    order = []
    w.inst.line.monitors.append(lambda r: order.append(r["frame"].origin.split(":")[0]))
    w.inst.press("room2.light", True, "wall:0")      # occupies the bus
    write(w, "room1.light", True, at=ms(1))          # gateway and a second wall switch both wait
    w.sched.at(ms(2), w.inst.press, "room3.light", True, "wall:1")
    w.run(1)
    assert order[:3] == ["wall", "wall", "status"] and "api" in order[3:]


def test_simclient_view_follows_notifications():
    w = world("confirmed")
    c = SimClient(w, "app", net_ms=5.0)
    d = w.inst.dp("room1.light")
    w.inst.press("room1.light", True)
    w.run(1)
    assert c.view[d.id][0] is True
