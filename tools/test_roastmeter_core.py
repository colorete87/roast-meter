import io

import pytest

from roastmeter_core import (
    Captured,
    Loaded,
    Reading,
    Removed,
    Reporter,
    SampleTracker,
    SerialParser,
    Session,
    agtron_from_ir,
    ble_reading,
    next_brightness,
    parse_led_brightness,
)


# -- agtron_from_ir: must match the firmware's mapIRToAgtron() --


def test_agtron_matches_firmware_formula_for_default_calibration():
    # 100 - (117 - 100) * 0.165 = 97.195 -> 97
    assert agtron_from_ir(100) == 97


def test_agtron_uses_given_intersection_point_and_deviation():
    # 200 - (150 - 200) * 0.2 = 210
    assert agtron_from_ir(200, intersection_point=150, deviation=0.2) == 210


def test_agtron_rounds_half_away_from_zero_like_c_round():
    # 217 - (117 - 217) * 0.165 = 233.5 -> 234 (Python's round() would give 234 by
    # luck here, so the next test pins the float32 behaviour separately)
    assert agtron_from_ir(217) == 234


def test_agtron_uses_float32_arithmetic_like_the_firmware():
    # In float32, (29 - 4) * 0.14f rounds to exactly 3.5, so 4 - 3.5 = 0.5 rounds to 1.
    # In float64 the product is 3.5000000000000004 and the result would round to 0.
    assert agtron_from_ir(4, intersection_point=29, deviation=0.14) == 1


# -- ble_reading: particle_sensor characteristic (IR / 1000) to a Reading --


def test_ble_reading_is_none_when_no_sample_is_loaded():
    assert ble_reading(0, intersection_point=117, deviation=0.165) is None


def test_ble_reading_computes_agtron_from_ir():
    assert ble_reading(100, intersection_point=117, deviation=0.165) == Reading(agtron=97, ir_k=100)


def test_ble_reading_does_not_wrap_above_255():
    # The firmware's agtron characteristic is a single byte; computing from IR avoids the wrap.
    reading = ble_reading(300, intersection_point=117, deviation=0.165)
    assert reading.agtron == 330


# -- SerialParser: lines printed by the firmware --


def test_serial_parser_emits_reading_with_raw_ir_after_agtron_line():
    parser = SerialParser()
    assert parser.feed("real:214532") is None
    assert parser.feed("agtron:230") == Reading(agtron=230, ir_k=214.532)


def test_serial_parser_ignores_separator_lines():
    parser = SerialParser()
    assert parser.feed("===========================") is None


def test_serial_parser_returns_other_lines_as_messages():
    parser = SerialParser()
    assert parser.feed("EEPROM is valid") == "EEPROM is valid"


def test_serial_parser_ignores_blank_lines():
    parser = SerialParser()
    assert parser.feed("  \r") is None


def test_serial_parser_does_not_reuse_raw_ir_for_the_next_reading():
    parser = SerialParser()
    parser.feed("real:214532")
    parser.feed("agtron:230")
    assert parser.feed("agtron:231") == Reading(agtron=231, ir_k=None)


# -- SampleTracker: decides when a loaded sample gives a stable reading --


def feed(tracker, start, stop, agtron, step=0.1, ir_k=None):
    """Feeds one reading every `step` seconds in [start, stop] and returns all events."""
    events = []
    n = round((stop - start) / step)
    for i in range(n + 1):
        events += tracker.update(start + i * step, Reading(agtron=agtron, ir_k=ir_k))
    return events


def test_tracker_reports_loaded_on_first_reading():
    tracker = SampleTracker()
    assert tracker.update(0.0, Reading(agtron=230, ir_k=214)) == [Loaded()]


def test_tracker_ignores_no_sample_while_idle():
    tracker = SampleTracker()
    assert tracker.update(0.0, None) == []


def test_tracker_does_not_capture_before_stable_secs():
    tracker = SampleTracker(stable_secs=2.0)
    events = feed(tracker, 0.0, 1.9, agtron=230)
    assert not any(isinstance(e, Captured) for e in events)


def test_tracker_captures_mean_once_readings_are_stable_for_stable_secs():
    tracker = SampleTracker(stable_secs=2.0, tolerance=2)
    events = feed(tracker, 0.0, 1.0, agtron=230, ir_k=214)
    events += feed(tracker, 1.1, 2.0, agtron=231, ir_k=215)
    captured = [e for e in events if isinstance(e, Captured)]
    assert len(captured) == 1
    assert captured[0].agtron == pytest.approx(230.5, abs=0.06)
    assert captured[0].ir_k == pytest.approx(214.5, abs=0.06)


def test_tracker_waits_while_readings_move_more_than_tolerance():
    tracker = SampleTracker(stable_secs=2.0, tolerance=2)
    events = feed(tracker, 0.0, 1.0, agtron=220)  # still tamping
    events += feed(tracker, 1.1, 2.5, agtron=230)
    assert not any(isinstance(e, Captured) for e in events)
    events = feed(tracker, 2.6, 3.1, agtron=230)
    assert [e for e in events if isinstance(e, Captured)] == [Captured(agtron=230.0, ir_k=None)]


def test_tracker_captures_only_once_per_load():
    tracker = SampleTracker(stable_secs=2.0)
    events = feed(tracker, 0.0, 6.0, agtron=230)
    assert len([e for e in events if isinstance(e, Captured)]) == 1


def test_tracker_reports_removed_when_sample_is_taken_off():
    tracker = SampleTracker(stable_secs=2.0)
    feed(tracker, 0.0, 2.0, agtron=230)
    assert tracker.update(2.1, None) == [Removed(captured=True)]


def test_tracker_reports_removed_without_capture_for_unstable_sample():
    tracker = SampleTracker(stable_secs=2.0)
    feed(tracker, 0.0, 0.5, agtron=230)
    assert tracker.update(0.6, None) == [Removed(captured=False)]


def test_tracker_can_capture_again_after_reloading():
    tracker = SampleTracker(stable_secs=2.0)
    feed(tracker, 0.0, 2.0, agtron=230)
    tracker.update(2.1, None)
    events = feed(tracker, 3.0, 5.0, agtron=228)
    assert events[0] == Loaded()
    assert [e for e in events if isinstance(e, Captured)] == [Captured(agtron=228.0, ir_k=None)]


def test_tracker_reports_removed_when_readings_stop_for_absent_timeout():
    # The serial port only prints while a sample is loaded, so silence means "removed".
    tracker = SampleTracker(stable_secs=2.0, absent_timeout=0.5)
    feed(tracker, 0.0, 1.0, agtron=230)
    assert tracker.tick(1.4) == []
    assert tracker.tick(1.6) == [Removed(captured=False)]


def test_tracker_without_absent_timeout_keeps_sample_on_silence():
    tracker = SampleTracker(stable_secs=2.0)
    tracker.update(0.0, Reading(agtron=230, ir_k=None))
    assert tracker.tick(1.0) == []


def test_tracker_tick_captures_held_value_when_updates_are_sparse():
    tracker = SampleTracker(stable_secs=2.0)
    tracker.update(0.0, Reading(agtron=230, ir_k=None))
    assert tracker.tick(1.9) == []
    assert tracker.tick(2.0) == [Captured(agtron=230.0, ir_k=None)]


def test_tracker_counts_value_held_from_before_the_window():
    tracker = SampleTracker(stable_secs=2.0, tolerance=2)
    tracker.update(0.0, Reading(agtron=220, ir_k=None))
    tracker.update(1.0, Reading(agtron=230, ir_k=None))
    assert tracker.tick(2.5) == []  # 220 was still showing at 0.5
    assert tracker.tick(3.0) == [Captured(agtron=230.0, ir_k=None)]


# -- Session: averages the captured readings --


def test_session_summary_with_no_readings():
    assert Session().summary() == "No readings captured."


def test_session_summary_with_one_reading_has_no_spread():
    session = Session()
    session.add(230.0)
    assert session.summary() == "1 reading: average 230.0"


def test_session_summary_reports_mean_spread_and_range():
    session = Session()
    for value in (229.0, 230.0, 231.5):
        session.add(value)
    assert session.mean == pytest.approx(230.1667, abs=1e-4)
    assert session.summary() == "3 readings: average 230.2 (sd 1.3, min 229.0, max 231.5)"


# -- next_brightness: the wiki's calibration rule (+/-1 LED brightness per round) --


def test_next_brightness_is_none_when_average_is_within_tolerance_of_target():
    assert next_brightness(135, mean=228.0, target=230, tolerance=3) is None


def test_next_brightness_raises_led_when_reading_is_below_target():
    assert next_brightness(135, mean=220.0, target=230, tolerance=3) == 136


def test_next_brightness_lowers_led_when_reading_is_above_target():
    assert next_brightness(135, mean=240.0, target=230, tolerance=3) == 134


def test_next_brightness_stays_within_a_byte():
    assert next_brightness(255, mean=200.0, target=230, tolerance=3) == 255
    assert next_brightness(0, mean=260.0, target=230, tolerance=3) == 0


# -- Reporter: prints tracker events and collects captured readings --


def test_reporter_adds_captured_readings_to_the_session():
    out = io.StringIO()
    reporter = Reporter(Session(), out=out, live=False)
    reporter.handle([Captured(agtron=230.04, ir_k=214.5)])
    assert reporter.session.values == [230.04]
    assert out.getvalue() == "Reading 1: 230.0  (IR 214.5k)  -> 1 reading: average 230.0\n"


def test_reporter_omits_ir_when_unknown():
    out = io.StringIO()
    reporter = Reporter(Session(), out=out, live=False)
    reporter.handle([Captured(agtron=97.0, ir_k=None)])
    assert out.getvalue() == "Reading 1: 97.0  -> 1 reading: average 97.0\n"


def test_reporter_warns_when_sample_is_removed_before_settling():
    out = io.StringIO()
    reporter = Reporter(Session(), out=out, live=False)
    reporter.handle([Loaded(), Removed(captured=False)])
    assert out.getvalue() == (
        "Sample loaded, waiting for the reading to settle...\n"
        "Sample removed before the reading settled (not counted).\n"
    )


def test_reporter_live_line_is_overwritten_in_place_and_cleared_before_messages():
    out = io.StringIO()
    reporter = Reporter(Session(), out=out, live=True)
    reporter.show_live("Live: 230")
    reporter.message("hello")
    assert out.getvalue() == "\r\033[KLive: 230\r\033[Khello\n"


def test_reporter_without_live_mode_does_not_print_live_line():
    out = io.StringIO()
    reporter = Reporter(Session(), out=out, live=False)
    reporter.show_live("Live: 230")
    assert out.getvalue() == ""


# -- parse_led_brightness: boot message printed by setupEEPROM() --


def test_parse_led_brightness_from_boot_message():
    assert parse_led_brightness("Set ledBrightness to 135") == 135


def test_parse_led_brightness_ignores_other_messages():
    assert parse_led_brightness("Set intersection point to 117") is None
