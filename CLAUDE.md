# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Firmware and PC tools for a DIY coffee roast-color meter. Hardware: SparkFun Artemis Thing Plus (Apollo3), with a MAX30101 particle sensor and a SparkFun Micro OLED on Qwiic (I2C). The meter shines the IR LED on a ground-coffee sample and maps the reflected IR level to an Agtron number. Assembly, usage and calibration instructions live in the upstream wiki (links in `README.md`).

`origin` is the fork `colorete87/roast-meter`. Upstream is `juztins-lab/roast-meter`.

## Commands

Firmware (PlatformIO, single env `SparkFun_Thing_Plus_Artemis`):

```sh
pio run                                          # build
pio run -t upload --upload-port /dev/ttyUSB0     # flash over the SVL bootloader (~7 s)
pio device monitor -b 9600 -p /dev/ttyUSB0       # serial console (opening the port resets the board)
```

PC tools (Python, dependencies declared inline with PEP 723, so run them through `uv`):

```sh
uv run tools/roastmeter_ble.py info|measure [--count N]|calibrate|set-brightness N
uv run tools/roastmeter_serial.py --count 9 [--target 230]
python3 -m pytest tools                                        # all tests
python3 -m pytest tools/test_roastmeter_core.py::test_name     # one test
python3 -m pytest tools -k serial_parser                       # by name pattern
```

The tests only cover `roastmeter_core.py`, which uses only the standard library, so plain `python3` with pytest is enough. There is no linter or formatter config.

## Build configuration gotchas (`platformio.ini`)

- `platform = apollo3blue` is not in the PlatformIO registry. The platform is pulled from git (`nigelb/platform-apollo3blue`, pinned commit).
- `platform_packages` pins the SparkFun `Arduino_Apollo3` core to **v2.2.1**. ArduinoBLE requires the v2 core; do not drop this pin.
- `build_src_filter` compiles **only** `src/roast_meter_ble.cpp`. `src/roast_meter.ino` is never built by PlatformIO.

## Two firmware variants

- `src/roast_meter_ble.cpp`: the Bluetooth firmware. It is the one PlatformIO builds and the one the PC tools talk to. Calibration settings live in EEPROM and can be changed over BLE.
- `src/roast_meter.ino`: the older firmware without Bluetooth, used by the wiki's Arduino IDE instructions. Its calibration is hardcoded and it has no EEPROM.

The Agtron formula `round(x - (intersectionPoint - x) * deviation)`, with `x = IR / 1000` and C `round` in float32, exists in **three places** that must stay in sync: `mapIRToAgtron()` in the BLE firmware, `f()` in the `.ino`, and `agtron_from_ir()` in `tools/roastmeter_core.py`. The Python version reproduces the float32 arithmetic and the C rounding exactly so that the PC numbers match the OLED; its tests pin that behavior.

## BLE firmware flow (`roast_meter_ble.cpp`)

`setup()` → `setupEEPROM()` (loads or initializes the settings) → `setupBLE()` → sensor init (IR LED only) → two 2 s startup screens → `warmUpLED()` (a 60 s blocking countdown that keeps calling `BLE.poll()`) → `loop()`, which runs `measureSampleJob()` every 100 ms plus `BLE.poll()`.

- A sample counts as loaded when IR > `unblockedValue + 100`. `unblockedValue` is a fixed 30000; the comment says it is measured at power-up, but it is not.
- The meter state (`STATE_SETUP` / `WARMUP` / `READY` / `MEASURED`, 0–3) is published on the `meter_state` characteristic.
- Every 100 ms the firmware writes, and therefore notifies, `particle_sensor` (IR/1000, uint32), `agtron` (one byte) and `meter_state`, even when the value has not changed.

**EEPROM layout** (on Apollo3 the EEPROM is emulated in flash): byte 0 holds the valid marker `0xAA`, byte 1 the LED brightness, byte 2 the intersection point, bytes 3–6 the deviation (float32), and from byte 128 the BLE name (a length byte plus up to 63 chars).

- **Invariant:** the valid marker must be written *last* during initialization. If it is written first, a reset in the middle of the init leaves the EEPROM marked valid but without a name, and it never recovers. This bug was fixed in the fork; keep the order.
- The `EEPROM_*_DEFAULT` values only apply to a blank or invalid EEPROM. Changing them in code does nothing on a board that is already initialized; change the setting over BLE instead.
- With the v2 core, every single-byte EEPROM write erases and reprograms a whole 8 KB flash page.

**BLE settings service:** writes to `led_brightness_level`, `intersection_point`, `deviation` and `ble_name` take effect immediately (brightness re-runs `setupParticleSensor()`) and are saved to EEPROM. The UUIDs are `#define`d at the top of the `.cpp` and copied into `tools/roastmeter_ble.py`; keep both in sync.

**Serial protocol** (9600 baud): at boot the firmware prints status lines such as `Set ledBrightness to N` and `... device active, waiting for connections...`. While a sample is loaded it prints, every 100 ms, `real:<raw IR>`, then `agtron:<n>`, then a `=====` line. `SerialParser`, `parse_led_brightness()` and `BOOT_DONE` in the tools match these strings literally, so changing the firmware's prints breaks `roastmeter_serial.py`.

Known issues, observed but not fixed:

- `BLE.begin()` is called redundantly: in `setup()`, in `setupEEPROM()` and in `setupBLE()`.
- The `agtron` characteristic is one byte and wraps above 255. The tools therefore compute the Agtron number from `particle_sensor` with the settings they read from the meter.

## PC tools (`tools/`)

- `roastmeter_core.py` holds all the pure, tested logic. It is shared by both scripts, which import it as a sibling module, so run them as `uv run tools/<script>.py`.
  - The Agtron formula, `SerialParser` and `ble_reading()`.
  - `SampleTracker`, an event-based state machine fed with readings plus periodic `tick()` calls. It emits `Loaded`, then `Captured` once the value stays within ±2 for 2 s, then `Removed`. It captures one reading per loaded sample.
  - `Session` (mean and spread), `next_brightness()` (moves the LED ±1 per calibration round, as in the wiki) and `Reporter` (overwrites a live line in place on a TTY).
- `roastmeter_ble.py` (bleak) and `roastmeter_serial.py` (pyserial) are thin I/O shells around the core and have no unit tests.
  - Only the BLE script can write settings.
  - `calibrate` asks for confirmation before each write.

## Hardware and runtime facts

- The board's USB serial (CH340) shows up as `/dev/ttyUSB0`. **Opening the port resets the board** through DTR, so after that it needs about 65 s (boot screens plus warm-up) before it measures again.
- Only one process can hold the serial port: close minicom or any monitor before flashing or running `roastmeter_serial.py`.
- On this Fedora host Bluetooth can be soft-blocked; enable it with `rfkill unblock bluetooth && bluetoothctl power on`.
- Calibration reference (from the wiki): a heaping teaspoon of baking soda should read about 230. Adjust the LED brightness one step at a time.
