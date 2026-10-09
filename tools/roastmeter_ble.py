#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak>=0.22"]
# ///
"""Talk to the roast meter over Bluetooth: live readings, averages and LED calibration.

    uv run tools/roastmeter_ble.py info
    uv run tools/roastmeter_ble.py measure --count 9
    uv run tools/roastmeter_ble.py calibrate            # baking soda, target 230
    uv run tools/roastmeter_ble.py set-brightness 136
"""

import argparse
import asyncio
import struct
import time

from bleak import BleakClient, BleakScanner

from roastmeter_core import Captured, Loaded, Removed, Reporter, SampleTracker, Session, ble_reading, next_brightness

# UUIDs from src/roast_meter_ble.cpp
ROAST_METER_SERVICE_UUID = "875a0ee0-03dd-4225-ae06-35e8ae92b84c"
PARTICLE_SENSOR_UUID = "c32afdba-e9f2-453e-9612-85fbf4108ab2"
METER_STATE_UUID = "8ace2828-996f-48e4-8e9c-8284678b4b57"
LED_BRIGHTNESS_UUID = "8313695f-3ea1-458b-bd2a-df4aee218514"
INTERSECTION_POINT_UUID = "69548c4b-87d0-4e3e-ac6c-b143c7b2ab30"
DEVIATION_UUID = "d17234fa-0f48-429a-9e9b-f5db774ef682"
BLE_NAME_UUID = "cde44fd7-4c1e-42a0-8368-531dc87f6b56"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"

STATE_SETUP, STATE_WARMUP, STATE_READY, STATE_MEASURED = range(4)
STATE_NAMES = {STATE_SETUP: "starting", STATE_WARMUP: "warming up", STATE_READY: "ready", STATE_MEASURED: "measuring"}


def parse_args():
    ap = argparse.ArgumentParser(description="Roast meter over Bluetooth.")
    ap.add_argument("--address", help="Bluetooth address of the roast meter (default: scan for it)")
    ap.add_argument("--scan-timeout", type=float, default=15, help="seconds to look for the roast meter (default: %(default)s)")
    ap.add_argument("--stable-secs", type=float, default=2.0, help="how long a reading must hold still (default: %(default)s)")
    ap.add_argument("--tolerance", type=int, default=2, help="max change while holding still (default: %(default)s)")
    commands = ap.add_subparsers(dest="command")

    commands.add_parser("info", help="show the roast meter's settings")

    measure = commands.add_parser("measure", help="show readings and their average (default)")
    measure.add_argument("--count", type=int, help="stop after this many readings (default: until Ctrl+C)")

    calibrate = commands.add_parser("calibrate", help="calibrate the LED brightness against a reference")
    calibrate.add_argument("--target", type=float, default=230, help="reading to aim for (default: %(default)s, baking soda)")
    calibrate.add_argument("--target-tolerance", type=float, default=3, help="allowed distance from the target (default: %(default)s)")
    calibrate.add_argument("--count", type=int, default=5, help="readings per round (default: %(default)s)")

    set_brightness = commands.add_parser("set-brightness", help="set the LED brightness (saved on the roast meter)")
    set_brightness.add_argument("value", type=int, choices=range(256), metavar="VALUE", help="0-255")

    args = ap.parse_args()
    args.command = args.command or "measure"
    return args


async def find_meter(address, timeout):
    if address:
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
    else:
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: ROAST_METER_SERVICE_UUID in [u.lower() for u in ad.service_uuids]
            or (ad.local_name or d.name or "").startswith("Roast Meter"),
            timeout=timeout,
        )
    if device is None:
        raise SystemExit(
            "Roast meter not found. Is it on and in range, and is the laptop's Bluetooth on?\n"
            "  rfkill unblock bluetooth && bluetoothctl power on"
        )
    return device


class Meter:
    def __init__(self, client):
        self.client = client

    async def read_byte(self, uuid):
        return (await self.client.read_gatt_char(uuid))[0]

    async def read_settings(self):
        return {
            "name": (await self.client.read_gatt_char(BLE_NAME_UUID)).decode(errors="replace"),
            "firmware": (await self.client.read_gatt_char(FIRMWARE_REVISION_UUID)).decode(errors="replace"),
            "led_brightness": await self.read_byte(LED_BRIGHTNESS_UUID),
            "intersection_point": await self.read_byte(INTERSECTION_POINT_UUID),
            "deviation": struct.unpack("<f", await self.client.read_gatt_char(DEVIATION_UUID))[0],
            "state": await self.read_byte(METER_STATE_UUID),
        }

    async def set_brightness(self, value):
        await self.client.write_gatt_char(LED_BRIGHTNESS_UUID, bytes([value]), response=True)
        stored = await self.read_byte(LED_BRIGHTNESS_UUID)
        if stored != value:
            raise SystemExit(f"LED brightness write failed: wrote {value}, roast meter reports {stored}")

    async def measure(self, reporter, tracker, settings, count):
        """Shows live readings until `count` readings are captured (or forever if None)."""
        state = settings["state"]
        loaded = False
        last = None
        target = len(reporter.session.values) + count if count is not None else None

        def on_particle(_, data):
            nonlocal loaded, last
            last = ble_reading(int.from_bytes(data, "little"), settings["intersection_point"], settings["deviation"])
            loaded = handle(reporter, tracker.update(time.monotonic(), last), loaded)

        def on_state(_, data):
            nonlocal state
            state = data[0]

        await self.client.start_notify(PARTICLE_SENSOR_UUID, on_particle)
        await self.client.start_notify(METER_STATE_UUID, on_state)
        try:
            while target is None or len(reporter.session.values) < target:
                loaded = handle(reporter, tracker.tick(time.monotonic()), loaded)
                if state in (STATE_SETUP, STATE_WARMUP):
                    reporter.show_live(f"Roast meter is {STATE_NAMES[state]}...")
                elif loaded and last is not None:
                    reporter.show_live(f"Live: {last.agtron}  (IR {last.ir_k}k)")
                else:
                    reporter.show_live("Ready: load a sample")
                await asyncio.sleep(0.1)
        finally:
            await self.client.stop_notify(PARTICLE_SENSOR_UUID)
            await self.client.stop_notify(METER_STATE_UUID)


def handle(reporter, events, loaded):
    """Prints events and returns whether a sample is loaded afterwards."""
    reporter.handle(events)
    for event in events:
        if isinstance(event, Loaded):
            loaded = True
        elif isinstance(event, (Captured, Removed)):
            loaded = isinstance(event, Captured)
    return loaded


def print_settings(reporter, settings, address):
    reporter.message(f"{settings['name']} ({address}), firmware {settings['firmware']}, {STATE_NAMES.get(settings['state'], settings['state'])}")
    reporter.message(
        f"  LED brightness {settings['led_brightness']}, intersection point {settings['intersection_point']}, "
        f"deviation {settings['deviation']:.3f}"
    )


async def calibrate(meter, reporter, args, settings):
    brightness = settings["led_brightness"]
    reporter.message(
        f"Calibrating against {args.target:g} +/- {args.target_tolerance:g}. Load the reference "
        f"(baking soda: a heaping teaspoon, tamped) {args.count} times per round, lifting it off between readings."
    )
    round_number = 1
    while True:
        reporter.session = Session()
        tracker = SampleTracker(stable_secs=args.stable_secs, tolerance=args.tolerance)
        reporter.message(f"Round {round_number}: LED brightness {brightness}")
        await meter.measure(reporter, tracker, settings, args.count)
        mean = reporter.session.mean
        new = next_brightness(brightness, mean, args.target, args.target_tolerance)
        if new is None:
            reporter.message(f"On target: average {mean:.1f}. Calibrated with LED brightness {brightness}.")
            return
        if new == brightness:
            reporter.message(f"Average {mean:.1f} is off target, but LED brightness {brightness} is already at its limit.")
            return
        direction = "below" if mean < args.target else "above"
        answer = await asyncio.to_thread(
            input, f"Average {mean:.1f} is {direction} the target. Set LED brightness to {new} and measure again? [Y/n] "
        )
        if answer.strip().lower() in ("n", "no", "q"):
            reporter.message(f"Stopped. LED brightness stays at {brightness}.")
            return
        await meter.set_brightness(new)
        brightness = new
        settings["led_brightness"] = new
        round_number += 1


async def run(args, reporter):
    reporter.message("Looking for the roast meter...")
    device = await find_meter(args.address, args.scan_timeout)
    async with BleakClient(device) as client:
        meter = Meter(client)
        settings = await meter.read_settings()
        print_settings(reporter, settings, device.address)
        if args.command == "info":
            return
        if args.command == "set-brightness":
            await meter.set_brightness(args.value)
            reporter.message(f"LED brightness set to {args.value} (saved on the roast meter).")
            return
        if args.command == "calibrate":
            await calibrate(meter, reporter, args, settings)
            return
        tracker = SampleTracker(stable_secs=args.stable_secs, tolerance=args.tolerance)
        await meter.measure(reporter, tracker, settings, args.count)


def main():
    args = parse_args()
    reporter = Reporter(Session())
    try:
        asyncio.run(run(args, reporter))
    except KeyboardInterrupt:
        pass
    if args.command == "measure":
        reporter.message(reporter.session.summary())


if __name__ == "__main__":
    main()
