"""Shared logic for the roast meter PC tools (serial and Bluetooth).

Mirrors what the firmware in src/roast_meter_ble.cpp does, so the numbers
shown here match the ones on the OLED display.
"""

import math
import re
import statistics
import struct
import sys
from dataclasses import dataclass

DEFAULT_INTERSECTION_POINT = 117
DEFAULT_DEVIATION = 0.165


@dataclass(frozen=True)
class Reading:
    agtron: int
    ir_k: float | None  # IR level in thousands (the firmware's rLevel / 1000)


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _c_round(value):
    # C's round(): halves are rounded away from zero
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def agtron_from_ir(ir_k, intersection_point=DEFAULT_INTERSECTION_POINT, deviation=DEFAULT_DEVIATION):
    """Same as the firmware's mapIRToAgtron(), including its float32 arithmetic."""
    product = _f32((intersection_point - ir_k) * _f32(deviation))
    return _c_round(_f32(ir_k - product))


def ble_reading(particle_k, intersection_point, deviation):
    """Reading from the particle_sensor characteristic, or None when no sample is loaded."""
    if particle_k == 0:
        return None
    return Reading(agtron=agtron_from_ir(particle_k, intersection_point, deviation), ir_k=particle_k)


@dataclass(frozen=True)
class Loaded:
    pass


@dataclass(frozen=True)
class Captured:
    agtron: float
    ir_k: float | None


@dataclass(frozen=True)
class Removed:
    captured: bool


class SampleTracker:
    """Captures one reading per loaded sample, once it stops changing.

    A reading is captured when the values seen during the last `stable_secs`
    (counting the value still showing at the start of that window) stay within
    `tolerance` of each other. Feed it with update() for every reading (None
    means "no sample") and call tick() periodically so silence and sparse
    updates are noticed too.
    """

    def __init__(self, stable_secs=2.0, tolerance=2, absent_timeout=None):
        self.stable_secs = stable_secs
        self.tolerance = tolerance
        self.absent_timeout = absent_timeout  # seconds without readings that mean "removed"
        self._readings = None  # [(t, Reading)] for the loaded sample, None when idle
        self._captured = False
        self._loaded_at = None
        self._last_seen = None

    def update(self, t, reading):
        if reading is None:
            return self._remove()
        events = []
        if self._readings is None:
            self._readings = []
            self._captured = False
            self._loaded_at = t
            events.append(Loaded())
        self._last_seen = t
        if not self._captured:
            self._readings.append((t, reading))
        return events + self._check_stable(t)

    def tick(self, t):
        if self._readings is None:
            return []
        if self.absent_timeout is not None and t - self._last_seen > self.absent_timeout:
            return self._remove()
        return self._check_stable(t)

    def _remove(self):
        if self._readings is None:
            return []
        self._readings = None
        return [Removed(captured=self._captured)]

    def _check_stable(self, t):
        if self._captured or t - self._loaded_at < self.stable_secs:
            return []
        start = t - self.stable_secs
        before = [i for i, (rt, _) in enumerate(self._readings) if rt <= start]
        self._readings = self._readings[before[-1]:]  # keep the value showing at `start` onwards
        window = [r for _, r in self._readings]
        agtrons = [r.agtron for r in window]
        if max(agtrons) - min(agtrons) > self.tolerance:
            return []
        self._captured = True
        self._readings = []
        ir_ks = [r.ir_k for r in window]
        ir_k = math.fsum(ir_ks) / len(ir_ks) if None not in ir_ks else None
        return [Captured(agtron=math.fsum(agtrons) / len(agtrons), ir_k=ir_k)]


class SerialParser:
    """Turns the firmware's serial output into Readings and status messages."""

    def __init__(self):
        self._raw = None

    def feed(self, line):
        """Returns a Reading, a status message (str), or None for lines to ignore."""
        line = line.strip()
        if not line or line.startswith("==="):
            return None
        if line.startswith("real:"):
            self._raw = int(line[len("real:"):])
            return None
        if line.startswith("agtron:"):
            ir_k = self._raw / 1000 if self._raw is not None else None
            self._raw = None
            return Reading(agtron=int(line[len("agtron:"):]), ir_k=ir_k)
        return line


def parse_led_brightness(message):
    """LED brightness from the firmware's "Set ledBrightness to N" boot message."""
    match = re.fullmatch(r"Set ledBrightness to (\d+)", message)
    return int(match.group(1)) if match else None


class Session:
    """Captured readings of one measuring session."""

    def __init__(self):
        self.values = []

    def add(self, value):
        self.values.append(value)

    @property
    def mean(self):
        return statistics.fmean(self.values)

    def summary(self):
        n = len(self.values)
        if n == 0:
            return "No readings captured."
        if n == 1:
            return f"1 reading: average {self.mean:.1f}"
        return (
            f"{n} readings: average {self.mean:.1f} "
            f"(sd {statistics.stdev(self.values):.1f}, min {min(self.values):.1f}, max {max(self.values):.1f})"
        )


def next_brightness(current, mean, target, tolerance):
    """LED brightness for the next calibration round, or None when already on target.

    Follows the wiki: move the brightness one step towards the target per round.
    """
    if abs(mean - target) <= tolerance:
        return None
    step = 1 if mean < target else -1
    return min(255, max(0, current + step))


class Reporter:
    """Prints tracker events and collects the captured readings into a Session.

    In live mode (a terminal) the current reading is shown on one line that is
    overwritten in place; messages are printed on their own lines.
    """

    def __init__(self, session, out=None, live=None):
        self.session = session
        self.out = out or sys.stdout
        self.live = self.out.isatty() if live is None else live

    def show_live(self, text):
        if self.live:
            self.out.write("\r\033[K" + text)
            self.out.flush()

    def message(self, text):
        if self.live:
            self.out.write("\r\033[K")
        self.out.write(text + "\n")
        self.out.flush()

    def handle(self, events):
        for event in events:
            if isinstance(event, Loaded):
                self.message("Sample loaded, waiting for the reading to settle...")
            elif isinstance(event, Captured):
                self.session.add(event.agtron)
                ir = f"  (IR {event.ir_k:.1f}k)" if event.ir_k is not None else ""
                n = len(self.session.values)
                self.message(f"Reading {n}: {event.agtron:.1f}{ir}  -> {self.session.summary()}")
            elif isinstance(event, Removed) and not event.captured:
                self.message("Sample removed before the reading settled (not counted).")
