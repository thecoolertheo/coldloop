"""Tests for the one failure mode in this project that can damage hardware.

`liquidctl set fan speed 0` is accepted silently, with exit status 0, and stops
the radiator fans outright. It looks exactly like success. Applied at every
start by an auto-restarting service, that is a self-reinstating thermal
shutdown, and it is what made this machine unusable across reboots once already.

Nothing at runtime can catch that: the command succeeds, the fans stop, and the
first symptom is thermal throttling. The only defences are the clamps and the
curve values checked here, and until now they were held in place by nothing but
whoever remembered why they mattered.

Deliberately stdlib `unittest`, so running the tests needs no dependency that
`requirements.txt` does not already pin.

    venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Must precede any PyQt6 import: the GUI tests below construct a real window,
# and there is no display on a CI runner.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import kraken_controller as kc  # noqa: E402

# The floor is a project policy, not a driver limit -- the driver's own floor
# is 0, which is the hazard. Pinning the number here means lowering it has to
# be a deliberate edit to a file that explains why, rather than a quiet tweak
# to a constant.
POLICY_FAN_FLOOR = 25

# Every duty in the service unit's boot-time fan curve must be at least this.
# Higher than the GUI floor because this one is applied unattended, at boot,
# with nobody watching the temperatures.
POLICY_CURVE_FLOOR = 30


class FanConstants(unittest.TestCase):
    """The constants themselves, before anything uses them."""

    def test_fan_floor_is_not_zero(self):
        # The whole point. Zero is accepted by the driver and stops the fans.
        self.assertGreater(kc.FAN_MIN_SAFE, 0)

    def test_fan_floor_meets_policy(self):
        self.assertGreaterEqual(kc.FAN_MIN_SAFE, POLICY_FAN_FLOOR)

    def test_fan_ceiling_is_full_speed(self):
        self.assertEqual(kc.FAN_MAX, 100)

    def test_pump_floor_respects_the_driver(self):
        # _SPEED_CHANNELS_KRAKEN2023 clamps pump to 20..100; going below just
        # gets silently raised, so a lower number here would be a lie.
        self.assertGreaterEqual(kc.PUMP_MIN, 20)
        self.assertEqual(kc.PUMP_MAX, 100)


class ServiceUnitFanCurve(unittest.TestCase):
    """The boot-time curve in the systemd unit template.

    This is the exact thing that caused the original incident: an unsafe duty
    in a unit that reapplies it at every start. It is plain text in a template,
    so nothing but this test stops a stray zero from being typed into it.
    """

    @classmethod
    def setUpClass(cls):
        cls.unit = (REPO / "liquidctl.service.in").read_text()

    def _curve(self) -> list[int]:
        match = re.search(r"^ExecStartPre=.*set fan speed ([\d ]+)$", self.unit, re.M)
        self.assertIsNotNone(match, "no `set fan speed` curve found in the unit template")
        return [int(n) for n in match.group(1).split()]

    def test_curve_is_temperature_duty_pairs(self):
        values = self._curve()
        self.assertTrue(values, "curve is empty")
        self.assertEqual(len(values) % 2, 0, f"odd number of values: {values}")

    def test_every_curve_duty_is_safe(self):
        duties = self._curve()[1::2]
        for duty in duties:
            with self.subTest(duty=duty):
                self.assertGreaterEqual(
                    duty,
                    POLICY_CURVE_FLOOR,
                    f"fan curve duty {duty}% is below the {POLICY_CURVE_FLOOR}% floor; "
                    f"this is applied at boot with nobody watching",
                )

    def test_curve_temperatures_ascend(self):
        # liquidctl interpolates between the points; unsorted temperatures give
        # a curve that is not the one anybody intended.
        temps = self._curve()[0::2]
        self.assertEqual(temps, sorted(temps), f"temperatures out of order: {temps}")

    def test_curve_duties_never_decrease_as_it_gets_hotter(self):
        duties = self._curve()[1::2]
        self.assertEqual(duties, sorted(duties), f"duties fall as temperature rises: {duties}")

    def test_pump_speed_respects_the_driver_floor(self):
        match = re.search(r"^ExecStartPre=.*set pump speed (\d+)$", self.unit, re.M)
        self.assertIsNotNone(match, "no `set pump speed` line found in the unit template")
        self.assertGreaterEqual(int(match.group(1)), 20)


class DeviceGateOrdering(unittest.TestCase):
    """The gate must run before anything that sets a duty.

    `--match Kraken` selects any Kraken, so without the gate first, installing
    on an unverified cooler applies this curve to it at boot.
    """

    def test_gate_precedes_every_duty_command(self):
        for name in ("liquidctl.service.in", "coldloop-lighting.service.in"):
            with self.subTest(unit=name):
                lines = [
                    line
                    for line in (REPO / name).read_text().splitlines()
                    if line.startswith("ExecStartPre=")
                ]
                if not lines:
                    continue
                self.assertIn(
                    "--wait-for-device",
                    lines[0],
                    f"{name}: the device gate must be the first ExecStartPre",
                )
                for later in lines[1:]:
                    self.assertNotIn("--wait-for-device", later)

    def test_only_the_verified_product_id_is_accepted(self):
        import kraken_hud

        self.assertEqual(kraken_hud.SUPPORTED_VENDOR_ID, 0x1E71)
        self.assertEqual(kraken_hud.SUPPORTED_PRODUCT_ID, 0x3012)


class _Stub:
    """Stands in for a slider, so apply_fan can be handed values the real
    widget would refuse to hold."""

    def __init__(self, value: int):
        self._value = value

    def value(self) -> int:
        return self._value


class ApplyFanClamp(unittest.TestCase):
    """The clamp inside apply_fan, independent of the widget's own range.

    Both layers are tested separately on purpose. The slider range is the first
    defence and the clamp is the second, and the second exists precisely so
    that widening the slider later cannot reintroduce the hazard.
    """

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.window = kc.Controller()

    @classmethod
    def tearDownClass(cls):
        # Stop the polling timers before teardown, or they fire against a
        # half-destroyed window.
        cls.window.state_timer.stop()
        cls.window.telemetry_timer.stop()
        cls.window.close()

    def _capture(self, method: str) -> list[str]:
        """Run an apply_* method and return the argv it would have executed."""
        seen: list[list[str]] = []
        original = self.window.dispatch
        self.window.dispatch = lambda label, cmd, *a, **k: seen.append(cmd)
        try:
            getattr(self.window, method)()
        finally:
            self.window.dispatch = original
        self.assertEqual(len(seen), 1, f"{method} did not dispatch exactly one command")
        return seen[0]

    @staticmethod
    def _duty(cmd: list[str]) -> int:
        return int(cmd[-1])

    # These assert POLICY_FAN_FLOOR rather than kc.FAN_MIN_SAFE on purpose.
    # Checking the code against its own constant passes vacuously the moment
    # somebody lowers that constant -- which is precisely the change these
    # tests exist to catch. Verified by mutation: setting FAN_MIN_SAFE = 0
    # fails these, where asserting against it failed only the constants tests.

    def test_slider_cannot_reach_an_unsafe_value(self):
        slider = self.window.fan_slider
        self.assertGreaterEqual(slider.minimum(), POLICY_FAN_FLOOR)
        slider.setValue(0)
        self.assertGreaterEqual(slider.value(), POLICY_FAN_FLOOR)

    def test_clamp_holds_when_the_widget_does_not(self):
        original = self.window.fan_slider
        try:
            for unsafe in (0, -1, -100, 1, 24):
                with self.subTest(requested=unsafe):
                    self.window.fan_slider = _Stub(unsafe)
                    duty = self._duty(self._capture("apply_fan"))
                    self.assertGreaterEqual(
                        duty,
                        POLICY_FAN_FLOOR,
                        f"apply_fan passed {duty}% to liquidctl for input {unsafe}",
                    )
        finally:
            self.window.fan_slider = original

    def test_clamp_caps_the_top(self):
        original = self.window.fan_slider
        try:
            self.window.fan_slider = _Stub(500)
            self.assertEqual(self._duty(self._capture("apply_fan")), kc.FAN_MAX)
        finally:
            self.window.fan_slider = original

    def test_safe_values_pass_through_unchanged(self):
        original = self.window.fan_slider
        try:
            for safe in (kc.FAN_MIN_SAFE, 50, 99, kc.FAN_MAX):
                with self.subTest(requested=safe):
                    self.window.fan_slider = _Stub(safe)
                    self.assertEqual(self._duty(self._capture("apply_fan")), safe)
        finally:
            self.window.fan_slider = original

    def test_command_is_the_verified_form(self):
        # Guards the shape too: `set fan speed <n>`. A malformed command would
        # fail loudly, but a command targeting the wrong channel would not.
        cmd = self._capture("apply_fan")
        self.assertEqual(cmd[-4:-1], ["set", "fan", "speed"])
        self.assertIn("--match", cmd)

    def test_pump_clamp(self):
        original = self.window.pump_slider
        try:
            for requested, expected in ((0, kc.PUMP_MIN), (500, kc.PUMP_MAX), (75, 75)):
                with self.subTest(requested=requested):
                    self.window.pump_slider = _Stub(requested)
                    self.assertEqual(self._duty(self._capture("apply_pump")), expected)
        finally:
            self.window.pump_slider = original


if __name__ == "__main__":
    unittest.main()
