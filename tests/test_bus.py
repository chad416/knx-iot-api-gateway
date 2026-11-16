"""TP1 line model against the published timing and the frame coding."""
import pytest

from knxgw.bus import tp1
from knxgw.bus.dpt import DPTS, decode_dpt9, encode_dpt9
from knxgw.bus.tp1 import Frame, Station, TP1Line, ga, ia
from knxgw.simtime import Scheduler, sec


class Rec(Station):
    def __init__(self, line, addr, gas=()):
        super().__init__(line, addr)
        self.group_table.update(gas)
        self.got, self.cons = [], []

    def on_frame(self, f):
        self.got.append((self.line.sched.now, f))

    def on_con(self, f, ok):
        self.cons.append((self.line.sched.now, f, ok))


def mk(**kw):
    s = Scheduler()
    return s, TP1Line(s, seed=3, **kw)


def test_bit_time_is_one_over_9600():
    assert tp1.BIT_NS == pytest.approx(1e9 / 9600, abs=0.5)


def test_telegram_duration_matches_published_20_to_40_ms():
    # KNX training material: t = t1 + t3 + (n + 2) * 13 bit times, n = 8..23 -> "20 to 40 ms"
    assert tp1.telegram_bits(8, "urgent") == 50 + 10 * 13
    assert tp1.telegram_bits(23, "low") == 3 + 50 + 25 * 13
    assert 18.5 < tp1.telegram_ms(8, "urgent") < 20.5
    assert 39.0 < tp1.telegram_ms(23, "low") < 40.0


def test_one_bit_group_write_takes_20_4_ms():
    assert tp1.frame_chars(0) == 9
    assert tp1.telegram_ms(9) == pytest.approx(20.42, abs=0.01)
    assert tp1.max_telegrams_per_s(9) == pytest.approx(48.98, abs=0.01)


def test_simulated_throughput_equals_the_formula():
    s, line = mk()
    a = Rec(line, ia(1, 1, 1))
    Rec(line, ia(1, 1, 2), [ga(1, 1, 1)])
    for _ in range(200):
        a.send(Frame(0, ga(1, 1, 1), "write", True))
    s.run()
    t_first, t_last = a.cons[0][0], a.cons[-1][0]
    rate = 199 / ((t_last - t_first) / 1e9)
    assert rate == pytest.approx(tp1.max_telegrams_per_s(9), rel=1e-6)
    assert all(ok for _, _, ok in a.cons)


@pytest.mark.parametrize("hi,lo", [("system", "urgent"), ("urgent", "normal"), ("normal", "low")])
def test_priority_order(hi, lo):
    s, line = mk()
    a, b = Rec(line, ia(1, 1, 1)), Rec(line, ia(1, 1, 2))
    r = Rec(line, ia(1, 1, 9), [ga(1, 1, 1)])
    a.send(Frame(0, ga(1, 1, 1), "write", "low", priority=lo))     # both start in the same bit
    b.send(Frame(0, ga(1, 1, 1), "write", "high", priority=hi))
    s.run()
    assert [f.value for _, f in r.got] == ["high", "low"]


def test_repeated_frame_beats_same_priority():
    f1, f2 = Frame(ia(1, 1, 9), 1, "write", priority="low"), Frame(ia(1, 1, 1), 1, "write", priority="low")
    f1.repeated = True
    assert f1.wire_key() < f2.wire_key()
    assert f1.wait_bits() == 50 and f2.wait_bits() == 53


def test_arbitration_is_lsb_first_not_lowest_address():
    # "data bit 0 is sent first": 1.1.250 (..11111010) beats 1.1.31 (..00011111) at the first bit,
    # 1.1.32 (..00100000) beats 1.1.250 at the second bit, 1.1.255 loses against everybody.
    key = lambda dev: Frame(ia(1, 1, dev), 1, "write").wire_key()
    assert key(250) < key(31)
    assert key(32) < key(250)
    assert all(key(d) < key(255) for d in (1, 31, 32, 250, 254))
    s, line = mk()
    r = Rec(line, ia(1, 1, 9), [1])
    for dev in (31, 250, 32):
        Rec(line, ia(1, 1, dev)).send(Frame(0, 1, "write", dev))
    s.run()
    assert [f.value for _, f in r.got] == [32, 250, 31]


def test_no_acknowledge_gives_three_repetitions_then_negative_confirm():
    s, line = mk()
    a = Rec(line, ia(1, 1, 1))
    Rec(line, ia(1, 1, 2))                      # hears the frame but is not a group member
    a.send(Frame(0, ga(5, 0, 1), "write", 1))
    s.run()
    assert line.stats.frames == 4 and line.stats.repetitions == 3 and line.stats.failed == 1
    assert a.cons[0][2] is False


def test_corrupted_frame_is_repeated_and_delivered_once():
    s, line = mk(p_frame_corrupt=1.0)
    a = Rec(line, ia(1, 1, 1))
    b = Rec(line, ia(1, 1, 2), [7])
    a.send(Frame(0, 7, "write", 1))
    s.at(13_000_000, lambda: setattr(line, "p_frame_corrupt", 0.0))   # only the first attempt is damaged
    s.run()
    assert line.stats.frames == 2 and len(b.got) == 1 and a.cons[0][2] is True


def test_damaged_acknowledge_repeats_but_receiver_passes_frame_up_once():
    s, line = mk(p_ack_corrupt=1.0)
    a = Rec(line, ia(1, 1, 1))
    b = Rec(line, ia(1, 1, 2), [7])
    a.send(Frame(0, 7, "write", 1))
    s.run()
    assert line.stats.frames == 4 and len(b.got) == 1 and a.cons[0][2] is False


def test_silent_loss_one_member_acknowledges_the_other_missed_the_frame():
    s, line = mk()
    a = Rec(line, ia(1, 1, 1))
    deaf = Rec(line, ia(1, 1, 2), [7])
    other = Rec(line, ia(1, 1, 3), [7])
    deaf.p_rx_miss = 1.0
    a.send(Frame(0, 7, "write", 1))
    s.run()
    assert a.cons[0][2] is True and len(other.got) == 1 and deaf.got == []
    assert line.stats.repetitions == 0


def test_bus_load_never_exceeds_the_frame_share_of_a_slot():
    s, line = mk()
    a = Rec(line, ia(1, 1, 1))
    Rec(line, ia(1, 1, 2), [1])
    for _ in range(100):
        a.send(Frame(0, 1, "write", 1))
    s.run()
    load = line.stats.busy_ns / (a.cons[-1][0] - a.cons[0][0] + tp1.telegram_bits(9) * tp1.BIT_NS)
    assert load == pytest.approx(143 / 196, abs=0.01)


# ---- datapoint types --------------------------------------------------------------------
def test_dpt9_known_vector_and_round_trip():
    assert encode_dpt9(21.0) == 0x0C1A          # the usual worked example for 21.0 degrees C
    for v in (-30.0, -0.01, 0.0, 0.5, 21.5, 100.0, 670760.0 * 0.99):
        assert decode_dpt9(encode_dpt9(v)) == pytest.approx(v, rel=0.01, abs=0.01)


def test_dpt5_scaling_is_quantised_to_255_steps():
    d = DPTS["5.001"]
    assert d.parse("100") == 100.0 and d.parse("0") == 0.0
    assert d.parse("50") == pytest.approx(50.2, abs=0.05)      # 128 / 255
    assert d.fmt(d.parse("60")) == "60"


@pytest.mark.parametrize("dpt,bad", [
    ("1.001", "ON"), ("1.001", "1"), ("1.001", ""), ("1.001", 1), ("1.001", None), ("1.001", ["on"]),
    ("5.001", "101"), ("5.001", "-1"), ("5.001", "nan"), ("5.001", "inf"), ("5.001", "1e999"),
    ("5.001", "50; DROP"), ("5.001", "5" * 40), ("5.001", 50), ("9.001", "-300"),
])
def test_invalid_values_are_rejected(dpt, bad):
    with pytest.raises(ValueError):
        DPTS[dpt].parse(bad)


def test_scheduler_orders_equal_times_first_in_first_out():
    s = Scheduler()
    out = []
    for i in range(5):
        s.at(sec(1), out.append, i)
    ev = s.at(sec(1), out.append, 99)
    ev.cancel()
    s.run()
    assert out == [0, 1, 2, 3, 4] and s.now == sec(1)
