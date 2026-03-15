import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import threading
import time

# ---------------------------------------------------------------------------
# Stub serial before importing gps_hat
# ---------------------------------------------------------------------------

serial_mod = types.ModuleType("serial")

class _FakeSerial:
    """Feeds pre-loaded NMEA lines one at a time; blocks on empty queue."""

    def __init__(self, port, baud, timeout=1):
        self._lines: list[bytes] = []
        self._lock = threading.Lock()
        self._event = threading.Event()

    def feed(self, line: str) -> None:
        """Queue a raw NMEA line (without trailing newline — readline adds it)."""
        with self._lock:
            self._lines.append((line + "\r\n").encode("ascii"))
        self._event.set()

    def readline(self) -> bytes:
        while True:
            self._event.wait(timeout=0.1)
            with self._lock:
                if self._lines:
                    line = self._lines.pop(0)
                    if not self._lines:
                        self._event.clear()
                    return line

serial_mod.Serial = _FakeSerial
sys.modules.setdefault("serial", serial_mod)

# Stub gps_mock so GPSReading is available without side-effects
from dataclasses import dataclass

gps_mock_mod = types.ModuleType("gps_mock")

@dataclass
class _GPSReading:
    lat: float
    lon: float
    timestamp: str
    elevation: float

class _GPSMock:
    def __init__(self, start_lat=37.7749, start_lon=-122.4194):
        pass
    def get_reading(self):
        return _GPSReading(lat=37.7749, lon=-122.4194,
                           timestamp="2024-01-01T00:00:00+00:00", elevation=900.0)

gps_mock_mod.GPSReading = _GPSReading
gps_mock_mod.GPSMock = _GPSMock
sys.modules.setdefault("gps_mock", gps_mock_mod)

import client.gps_hat as gps_hat_module  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A valid GGA with fix quality=1, lat=48.1173, lon=11.5167, alt=545.4 m
_GGA_FIX    = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
# Same sentence but fix quality=0 (no fix)
_GGA_NO_FIX = "$GPGGA,123519,4807.038,N,01131.000,E,0,00,,,M,,M,,*66"
# RMC (not a GGA — should be ignored for state updates)
_RMC        = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"


def _make_hat() -> tuple[gps_hat_module.GPSHat, _FakeSerial]:
    hat = gps_hat_module.GPSHat(port="/dev/ttyS0", baud=9600)
    serial_instance = hat._ser
    return hat, serial_instance


def _wait_for_fix(hat: gps_hat_module.GPSHat, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if hat._latest is not None:
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGPSHatNoFix(unittest.TestCase):
    def test_raises_before_any_fix(self):
        hat, _ = _make_hat()
        with self.assertRaises(RuntimeError):
            hat.get_reading()

    def test_no_fix_sentence_does_not_update_state(self):
        hat, serial = _make_hat()
        serial.feed(_GGA_NO_FIX)
        time.sleep(0.1)
        with self.assertRaises(RuntimeError):
            hat.get_reading()

    def test_non_gga_sentence_does_not_update_state(self):
        hat, serial = _make_hat()
        serial.feed(_RMC)
        time.sleep(0.1)
        with self.assertRaises(RuntimeError):
            hat.get_reading()


class TestGPSHatWithFix(unittest.TestCase):
    def setUp(self):
        self.hat, self.serial = _make_hat()
        self.serial.feed(_GGA_FIX)
        self.assertTrue(_wait_for_fix(self.hat), "Timed out waiting for GPS fix")

    def test_get_reading_returns_correct_lat(self):
        reading = self.hat.get_reading()
        self.assertAlmostEqual(reading.lat, 48.1173, places=3)

    def test_get_reading_returns_correct_lon(self):
        reading = self.hat.get_reading()
        self.assertAlmostEqual(reading.lon, 11.5167, places=3)

    def test_get_reading_returns_correct_elevation(self):
        reading = self.hat.get_reading()
        self.assertAlmostEqual(reading.elevation, 545.4, places=1)

    def test_get_reading_has_timestamp(self):
        reading = self.hat.get_reading()
        self.assertIsInstance(reading.timestamp, str)
        self.assertIn("T", reading.timestamp)  # ISO 8601

    def test_subsequent_fix_updates_reading(self):
        # Feed a second sentence with different altitude
        second_gga = "$GPGGA,123520,4807.038,N,01131.000,E,1,08,0.9,600.0,M,46.9,M,,*4B"
        self.serial.feed(second_gga)

        deadline = time.time() + 1.0
        while time.time() < deadline:
            if self.hat._latest and self.hat._latest.elevation == 600.0:
                break
            time.sleep(0.01)

        reading = self.hat.get_reading()
        self.assertAlmostEqual(reading.elevation, 600.0, places=1)

    def test_no_fix_after_valid_fix_preserves_last_reading(self):
        # A no-fix sentence should NOT overwrite the last good fix
        reading_before = self.hat.get_reading()
        self.serial.feed(_GGA_NO_FIX)
        time.sleep(0.1)
        reading_after = self.hat.get_reading()
        self.assertAlmostEqual(reading_before.lat, reading_after.lat)

    def test_get_reading_is_thread_safe(self):
        errors = []
        results = []

        def read():
            try:
                results.append(self.hat.get_reading())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=read) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10)


class TestGPSHatMalformedInput(unittest.TestCase):
    def test_garbage_line_does_not_crash(self):
        hat, serial = _make_hat()
        serial.feed("not-nmea-garbage$$$$")
        time.sleep(0.1)
        # Still no fix, no crash
        with self.assertRaises(RuntimeError):
            hat.get_reading()

    def test_empty_line_does_not_crash(self):
        hat, serial = _make_hat()
        serial.feed("")
        time.sleep(0.1)
        with self.assertRaises(RuntimeError):
            hat.get_reading()


if __name__ == "__main__":
    unittest.main()
