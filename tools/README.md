# Roast meter PC tools

Python scripts to read the roast meter (BLE firmware, `src/roast_meter_ble.cpp`) from a PC.
They count one reading per sample: load a sample, wait until the reading holds still for
2 seconds, lift it off, load the next one. At the end they print the average.

Run them with [uv](https://docs.astral.sh/uv/), which installs the dependencies on the fly:

## Over USB serial

```sh
uv run tools/roastmeter_serial.py --count 9
uv run tools/roastmeter_serial.py --count 5 --target 230   # baking soda: tells you how to adjust the LED
```

- Opening the serial port resets the roast meter, so it warms up again (about 65 s).
- Only one program can read the port: close minicom or the Arduino serial monitor first.
- It can't change settings; use the Bluetooth script for that.

## Over Bluetooth

```sh
uv run tools/roastmeter_ble.py info                 # name, firmware and calibration settings
uv run tools/roastmeter_ble.py measure --count 9
uv run tools/roastmeter_ble.py calibrate            # baking soda, target 230 +/- 3, 5 readings per round
uv run tools/roastmeter_ble.py set-brightness 136   # saved on the roast meter
```

- The PC's Bluetooth must be on: `rfkill unblock bluetooth && bluetoothctl power on`.
- `calibrate` follows the wiki's method: after each round it proposes moving the LED brightness
  one step towards the target and, if you accept, writes it to the roast meter and measures again.
- Readings are computed from the IR level with the roast meter's own calibration settings,
  because the firmware's `agtron` characteristic is a single byte and wraps above 255.

## Tests

```sh
python3 -m pytest tools
```
