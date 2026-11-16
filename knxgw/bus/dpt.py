"""The few KNX datapoint types the floor uses, with strict input validation.

API values are JSON strings (the KNX IoT 3rd Party API carries every value as a
string, see spec/knxiot_api_openapi.yaml, DataPoint200). `parse` turns an API
string into the raw bus value or raises ValueError with a message that is safe
to return to the client. `fmt` goes the other way.

DPT 9 (2 octet float) uses value = 0.01 * M * 2^E with a 4 bit exponent and a
12 bit two's complement mantissa (KNX datapoint types, DPT 9.xxx).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class DPT:
    id: str
    name: str
    kind: str                      # "enum" | "number"
    data_bytes: int                # value octets beyond the 6 bit short form
    enum: tuple[str, ...] = ()
    unit: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    resolution: Optional[float] = None

    def parse(self, s: Any) -> Any:
        if not isinstance(s, str):
            raise ValueError("value must be a JSON string")
        if len(s) > 32:
            raise ValueError("value too long")
        if self.kind == "enum":
            if s not in self.enum:
                raise ValueError(f"value must be one of {list(self.enum)}")
            return self.enum.index(s) == 0 if self.id.startswith("1.") else s
        # plain ASCII decimal only: float() would also take "1e400", "nan", " 5" and other scripts' digits
        if not _NUM.fullmatch(s):
            raise ValueError("value is not a plain decimal number")
        v = float(s)
        if math.isnan(v) or math.isinf(v):
            raise ValueError("value is not a finite number")
        if self.minimum is not None and v < self.minimum:
            raise ValueError(f"value below minimum {self.minimum:g}")
        if self.maximum is not None and v > self.maximum:
            raise ValueError(f"value above maximum {self.maximum:g}")
        return self.quantise(v)

    def quantise(self, v: float) -> float:
        if self.id == "5.001":
            return round(round(v * 255 / 100) * 100 / 255, 1)
        if self.id.startswith("9."):
            return decode_dpt9(encode_dpt9(v))
        return v

    def fmt(self, raw: Any) -> Optional[str]:
        if raw is None:
            return None
        if self.kind == "enum":
            if self.id.startswith("1."):
                return self.enum[0] if raw else self.enum[1]
            return str(raw)
        return f"{raw:g}"


_NUM = re.compile(r"-?[0-9]{1,7}(\.[0-9]{1,4})?", re.ASCII)


def encode_dpt9(v: float) -> int:
    m = round(v * 100)
    e = 0
    while not -2048 <= m <= 2047:
        m = round(m / 2)
        e += 1
        if e > 15:
            raise ValueError("value out of range for DPT 9")
    return ((m >> 11) & 1) << 15 | e << 11 | (m & 0x7FF)


def decode_dpt9(raw: int) -> float:
    e = (raw >> 11) & 0x0F
    m = raw & 0x7FF
    if raw & 0x8000:
        m -= 2048
    return round(0.01 * m * (1 << e), 2)


def encode_dpt5_scaling(percent: float) -> int:
    return round(percent * 255 / 100)


DPTS: dict[str, DPT] = {d.id: d for d in (
    DPT("1.001", "switch", "enum", 0, enum=("on", "off")),
    DPT("1.002", "boolean", "enum", 0, enum=("true", "false")),
    DPT("5.001", "percentage", "number", 1, unit="%", minimum=0, maximum=100,
        resolution=round(100 / 255, 3)),
    DPT("9.001", "temperature", "number", 2, unit="°C", minimum=-273, maximum=670760,
        resolution=0.01),
    DPT("9.004", "illuminance", "number", 2, unit="lx", minimum=0, maximum=670760,
        resolution=0.01),
)}
