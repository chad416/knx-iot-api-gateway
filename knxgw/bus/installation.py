"""The simulated installation: one floor on one TP1 line, and its semantic model.

Nine rooms (eight offices, one meeting room) each have a switched light, a
dimmer, a blind, a presence detector, a temperature and a brightness sensor and
a wall switch. The corridor has a light driven directly by its presence
detector. A visualisation panel listens to every status and sensor address.
That gives 64 datapoints, 37 of them writable, on 44 bus devices plus the gateway.

The semantic model follows the KNX IoT 3rd Party API: locations (site ->
building -> floor -> room), functions inside a location, datapoints inside a
function.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..simtime import Scheduler
from .devices import (BlindActuator, CyclicSensor, DimActuator, Panel, PresenceDetector,
                      PushButton, SwitchActuator, Truth)
from .dpt import DPT, DPTS
from .tp1 import TP1Line, ga, ia

_NS = uuid.UUID("6f1d7c1e-2f0b-4a57-9c39-0d6c5b6a3e10")


def uid(key: str) -> str:
    return str(uuid.uuid5(_NS, key))


@dataclass
class LocationDef:
    id: str
    title: str
    kind: str                        # site | building | floor | room
    parent: Optional[str] = None
    children: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)


@dataclass
class FunctionDef:
    id: str
    title: str
    location: str
    types: list[str]
    datapoints: list[str] = field(default_factory=list)


@dataclass
class DatapointDef:
    id: str
    key: str                         # truth key
    title: str
    function: str
    location: str
    dpt: DPT
    kind: str                        # switch | dim_switch | dim_value | blind_pos | presence | temperature | brightness
    status_ga: int
    cmd_ga: Optional[int] = None
    settle_s: float = 0.0            # longest physical travel before the status can arrive
    tags: list[str] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return self.cmd_ga is not None

    @property
    def slow(self) -> bool:
        return self.settle_s > 0


class Installation:
    def __init__(self, sched: Scheduler, seed: int = 1, n_rooms: int = 9,
                 p_frame_corrupt: float = 0.0, p_rx_miss: float = 0.0,
                 p_ack_corrupt: float = 0.0, status_mode: str = "always",
                 react_ms: tuple[float, float] = (8.0, 20.0), ramp_full_s: float = 2.0,
                 travel_s: float = 30.0, sensors: bool = True) -> None:
        self.sched = sched
        self.truth = Truth()
        self.line = TP1Line(sched, seed, p_frame_corrupt, p_rx_miss, p_ack_corrupt)
        self.locations: dict[str, LocationDef] = {}
        self.functions: dict[str, FunctionDef] = {}
        self.datapoints: dict[str, DatapointDef] = {}
        self.by_key: dict[str, DatapointDef] = {}
        self.buttons: dict[str, PushButton] = {}
        self.detectors: dict[str, PresenceDetector] = {}
        self.rooms: list[str] = []
        line, truth = self.line, self.truth

        site = self._loc("site", "Demo site", "site")
        bld = self._loc("building", "Building A", "building", site)
        floor = self._loc("floor2", "Floor 2", "floor", bld)
        self.floor_id = floor

        self.sw_act = SwitchActuator(line, ia(1, 1, 1), "switch actuator 12ch", truth, seed + 11)
        dims = [DimActuator(line, ia(1, 1, 2 + i), f"dim actuator {i + 1}", truth, seed + 21 + i,
                            ramp_full_s=ramp_full_s) for i in range(3)]
        blinds = [BlindActuator(line, ia(1, 1, 5 + i), f"blind actuator {i + 1}", truth,
                                seed + 31 + i, travel_s=travel_s) for i in range(2)]
        self.actuators = [self.sw_act, *dims, *blinds]
        for a in self.actuators:
            a.status_mode = status_mode
            a.react_ms = react_ms

        for n in range(1, n_rooms + 1):
            name = f"Office 2.{n:02d}" if n < n_rooms else f"Meeting room 2.{n:02d}"
            rk = f"room{n}"
            room = self._loc(rk, name, "room", floor)
            self.rooms.append(rk)
            btn = PushButton(line, ia(1, 1, 30 + n), f"wall switch {name}", truth, seed + 100 + n)
            self.buttons[rk] = btn

            g_c, g_s = ga(1, 1, n), ga(1, 2, n)
            self.sw_act.add_channel(f"{rk}.light", g_c, g_s)
            fn = self._fn(f"{rk}.light", f"Ceiling light {name}", room,
                          ["knx:switching", "urn:knx:fct.switching"])
            self._dp(f"{rk}.light", "Switch", fn, room, "1.001", "switch", g_s, g_c,
                     tags=["urn:knx:dpt.switch"])
            btn.listen(g_c, g_s)

            d = dims[(n - 1) % len(dims)]
            g_sw, g_sws, g_v, g_vs = ga(1, 3, n), ga(1, 4, n), ga(1, 5, n), ga(1, 6, n)
            d.add_channel(f"{rk}.dim", g_sw, g_sws, g_v, g_vs)
            fn = self._fn(f"{rk}.dim", f"Dimmed light {name}", room,
                          ["knx:dimming", "urn:knx:fct.dimming"])
            self._dp(f"{rk}.dim.sw", "Switch", fn, room, "1.001", "dim_switch", g_sws, g_sw,
                     tags=["urn:knx:dpt.switch"])
            self._dp(f"{rk}.dim.val", "Brightness", fn, room, "5.001", "dim_value", g_vs, g_v,
                     settle_s=ramp_full_s, tags=["urn:knx:dpt.scaling"])
            btn.listen(g_sw, g_sws)

            b = blinds[(n - 1) % len(blinds)]
            g_p, g_ps = ga(2, 1, n), ga(2, 2, n)
            b.add_channel(f"{rk}.blind", g_p, g_ps)
            fn = self._fn(f"{rk}.blind", f"Blind {name}", room,
                          ["knx:sunblind", "urn:knx:fct.sunblind"])
            self._dp(f"{rk}.blind", "Position", fn, room, "5.001", "blind_pos", g_ps, g_p,
                     settle_s=travel_s, tags=["urn:knx:dpt.scaling"])

            g_pr = ga(3, 1, n)
            self.detectors[rk] = PresenceDetector(line, ia(1, 1, 50 + n), f"presence {name}", truth,
                                                  seed + 200 + n, key=f"{rk}.presence", ga_=g_pr)
            fn = self._fn(f"{rk}.sense", f"Room sensors {name}", room,
                          ["knx:sensor", "urn:knx:fct.sensor"])
            self._dp(f"{rk}.presence", "Presence", fn, room, "1.002", "presence", g_pr,
                     tags=["urn:knx:dpt.occupancy"])
            g_t, g_l = ga(4, 1, n), ga(4, 2, n)
            self._dp(f"{rk}.temp", "Temperature", fn, room, "9.001", "temperature", g_t,
                     tags=["urn:knx:dpt.value_temp"])
            self._dp(f"{rk}.lux", "Brightness", fn, room, "9.004", "brightness", g_l,
                     tags=["urn:knx:dpt.value_lux"])
            if sensors:
                CyclicSensor(line, ia(1, 1, 60 + n), f"temperature {name}", truth, seed + 300 + n,
                             key=f"{rk}.temp", ga_=g_t, period_s=60.0, base=21.5, swing=1.0)
                CyclicSensor(line, ia(1, 1, 80 + n), f"brightness {name}", truth, seed + 400 + n,
                             key=f"{rk}.lux", ga_=g_l, period_s=30.0, base=420.0, swing=80.0)
            else:
                truth.set(f"{rk}.temp", 0, 21.5, "init")
                truth.set(f"{rk}.lux", 0, 420.0, "init")

        # corridor: the presence detector writes the light command address itself
        cor = self._loc("corridor", "Corridor", "room", floor)
        self.rooms.append("corridor")
        g_c, g_s = ga(1, 1, 100), ga(1, 2, 100)
        self.sw_act.add_channel("corridor.light", g_c, g_s)
        fn = self._fn("corridor.light", "Corridor light", cor, ["knx:switching", "urn:knx:fct.switching"])
        self._dp("corridor.light", "Switch", fn, cor, "1.001", "switch", g_s, g_c,
                 tags=["urn:knx:dpt.switch"])
        self.detectors["corridor"] = PresenceDetector(line, ia(1, 1, 50), "presence corridor", truth,
                                                      seed + 200, key="corridor.presence", ga_=g_c)

        # a panel that shows every status and sensor value (and therefore acknowledges them)
        self.panel = Panel(line, ia(1, 1, 20), "visualisation panel", truth, seed + 500)
        self.panel.listen(*(d.status_ga for d in self.datapoints.values()))

    # -- model helpers ---------------------------------------------------------------
    def _loc(self, key: str, title: str, kind: str, parent: Optional[str] = None) -> str:
        i = uid("loc:" + key)
        self.locations[i] = LocationDef(i, title, kind, parent)
        if parent:
            self.locations[parent].children.append(i)
        return i

    def _fn(self, key: str, title: str, loc: str, types: list[str]) -> str:
        i = uid("fn:" + key)
        self.functions[i] = FunctionDef(i, title, loc, types)
        self.locations[loc].functions.append(i)
        return i

    def _dp(self, key: str, title: str, fn: str, loc: str, dpt: str, kind: str, status_ga: int,
            cmd_ga: Optional[int] = None, settle_s: float = 0.0, tags: Optional[list[str]] = None) -> str:
        i = uid("dp:" + key)
        d = DatapointDef(i, key, title, fn, loc, DPTS[dpt], kind, status_ga, cmd_ga, settle_s,
                         tags or [])
        self.datapoints[i] = d
        self.by_key[key] = d
        self.functions[fn].datapoints.append(i)
        return i

    # -- queries ---------------------------------------------------------------------
    def dp(self, key: str) -> DatapointDef:
        return self.by_key[key]

    def locations_under(self, loc_id: str) -> set[str]:
        out, stack = set(), [loc_id]
        while stack:
            x = stack.pop()
            if x in out:
                continue
            out.add(x)
            stack.extend(self.locations[x].children)
        return out

    def writable(self, kinds: Optional[tuple[str, ...]] = None) -> list[DatapointDef]:
        return [d for d in self.datapoints.values()
                if d.writable and (kinds is None or d.kind in kinds)]

    def room_of(self, dp: DatapointDef) -> str:
        return dp.key.split(".")[0]

    def press(self, key: str, value: Any, origin: str = "wall") -> None:
        """A person presses the wall switch rocker for datapoint `key`."""
        d = self.by_key[key]
        btn = self.buttons.get(self.room_of(d))
        if btn is None or d.cmd_ga is None:
            raise KeyError(key)
        btn.press(d.cmd_ga, value, d.dpt.data_bytes, origin)
