#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""Read roast meter measurements over the USB serial port and average them.

Opening the serial port resets the roast meter, so it boots and warms up again
(about 65 seconds) every time this script starts.

    uv run tools/roastmeter_serial.py --count 9
    uv run tools/roastmeter_serial.py --count 5 --target 230   # baking soda calibration
"""

import argparse
import time

import serial

from roastmeter_core import (
    Captured,
    Loaded,
    Reading,
    Removed,
    Reporter,
    SampleTracker,
    SerialParser,
    Session,
    next_brightness,
    parse_led_brightness,
)

BOOT_DONE = "device active"  # last line printed by setup() before the startup screens
WARMUP_SECS = 64  # two 2 s startup screens, then the firmware's 60 s warm-up
ABSENT_TIMEOUT = 0.5  # the firmware prints every 100 ms while a sample is loaded


def parse_args():
    ap = argparse.ArgumentParser(description="Average roast meter readings from the USB serial port.")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="serial port (default: %(default)s)")
    ap.add_argument("--count", type=int, help="stop after this many readings (default: until Ctrl+C)")
    ap.add_argument("--target", type=float, help="calibration target, e.g. 230 for baking soda")
    ap.add_argument("--target-tolerance", type=float, default=3, help="allowed distance from the target (default: %(default)s)")
    ap.add_argument("--stable-secs", type=float, default=2.0, help="how long a reading must hold still (default: %(default)s)")
    ap.add_argument("--tolerance", type=int, default=2, help="max change while holding still (default: %(default)s)")
    return ap.parse_args()


def print_calibration_advice(reporter, session, brightness, target, tolerance):
    if not session.values:
        return
    if brightness is None:
        reporter.message("Can't advise: the LED brightness wasn't in the boot messages. Try: uv run tools/roastmeter_ble.py calibrate")
        return
    mean = session.mean
    new = next_brightness(brightness, mean, target, tolerance)
    if new is None:
        reporter.message(f"On target ({target:g} +/- {tolerance:g}): LED brightness {brightness} is calibrated.")
    elif new == brightness:
        reporter.message(f"Average {mean:.1f} is off target, but LED brightness {brightness} is already at its limit.")
    else:
        direction = "raise" if new > brightness else "lower"
        reporter.message(
            f"Average {mean:.1f} is off target {target:g} +/- {tolerance:g}: {direction} the LED brightness "
            f"from {brightness} to {new} (uv run tools/roastmeter_ble.py set-brightness {new})."
        )


def main():
    args = parse_args()
    session = Session()
    reporter = Reporter(session)
    tracker = SampleTracker(stable_secs=args.stable_secs, tolerance=args.tolerance, absent_timeout=ABSENT_TIMEOUT)
    parser = SerialParser()
    brightness = None
    booted = False
    warmup_until = None
    loaded = False
    last = None

    reporter.message(f"Opening {args.port} (this resets the roast meter)...")
    try:
        with serial.Serial(args.port, 9600, timeout=0.1) as port:
            buffer = b""
            while args.count is None or len(session.values) < args.count:
                buffer += port.read(port.in_waiting or 1)
                *lines, buffer = buffer.split(b"\n")
                for raw_line in lines:
                    item = parser.feed(raw_line.decode(errors="replace"))
                    if isinstance(item, Reading):
                        booted = True
                        warmup_until = None  # the firmware only measures after warming up
                        last = item
                        loaded = handle(reporter, tracker.update(time.monotonic(), item), loaded)
                    elif isinstance(item, str):
                        reporter.message(f"[meter] {item}")
                        parsed = parse_led_brightness(item)
                        if parsed is not None:
                            brightness = parsed
                        if BOOT_DONE in item:
                            booted = True
                            warmup_until = time.monotonic() + WARMUP_SECS
                now = time.monotonic()
                loaded = handle(reporter, tracker.tick(now), loaded)
                if not booted:
                    reporter.show_live("Waiting for the roast meter to boot...")
                elif warmup_until is not None and now < warmup_until:
                    reporter.show_live(f"Warming up... about {warmup_until - now:.0f} s left")
                elif loaded and last is not None:
                    ir = f"  (IR {last.ir_k:.1f}k)" if last.ir_k is not None else ""
                    reporter.show_live(f"Live: {last.agtron}{ir}")
                else:
                    reporter.show_live("Ready: load a sample")
    except KeyboardInterrupt:
        pass
    except serial.SerialException as error:
        reporter.message(f"Serial port error: {error}")
        reporter.message("Is another program (minicom, a serial monitor) using the port? Only one can read it at a time.")
        raise SystemExit(1)

    reporter.message(session.summary())
    if args.target is not None:
        print_calibration_advice(reporter, session, brightness, args.target, args.target_tolerance)


def handle(reporter, events, loaded):
    """Prints events and returns whether a sample is loaded afterwards."""
    reporter.handle(events)
    for event in events:
        if isinstance(event, Loaded):
            loaded = True
        elif isinstance(event, (Captured, Removed)):
            loaded = isinstance(event, Captured)
    return loaded


if __name__ == "__main__":
    main()
