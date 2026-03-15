import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, mock_open
import tempfile
import os

# ---------------------------------------------------------------------------
# Stub heavy dependencies before importing the module under test
# ---------------------------------------------------------------------------

# meshtastic
meshtastic_mod = types.ModuleType("meshtastic")
serial_interface_mod = types.ModuleType("meshtastic.serial_interface")
serial_interface_mod.SerialInterface = MagicMock()
meshtastic_mod.serial_interface = serial_interface_mod
sys.modules.setdefault("meshtastic", meshtastic_mod)
sys.modules.setdefault("meshtastic.serial_interface", serial_interface_mod)

# pubsub
pubsub_mod = types.ModuleType("pubsub")
pubsub_mod.pub = MagicMock()
sys.modules.setdefault("pubsub", pubsub_mod)

# map_handler
map_handler_mod = types.ModuleType("map_handler")
map_handler_mod.MapHandler = MagicMock()
map_handler_mod.render_points_to_file = MagicMock()
map_handler_mod.TILES_LIGHT = "OpenStreetMap"
map_handler_mod.TILES_DARK = "CartoDB dark_matter"
sys.modules.setdefault("map_handler", map_handler_mod)

# Redirect paths that are created at module-load time
_tmp_sessions = tempfile.mkdtemp()
os.environ.setdefault("SESSIONS_DIR", _tmp_sessions)
os.environ.setdefault("MAP_OUTPUT", os.path.join(_tmp_sessions, "map.html"))

import server.main as main_module  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_packet(text: str, snr: float = 5.0, rssi: int = -90,
                 from_id: str = "!abc123", hop_limit: int = 3, hop_start: int = 3) -> dict:
    return {
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
        "rxSnr": snr,
        "rxRssi": rssi,
        "fromId": from_id,
        "hopLimit": hop_limit,
        "hopStart": hop_start,
    }


def _location_text(message_id="msg-1", lat=37.7, lon=-122.4, elevation=900.0) -> str:
    return json.dumps({
        "messageId": message_id,
        "lat": lat,
        "lon": lon,
        "elevation": elevation,
        "timestamp": "2024-01-01T00:00:00+00:00",
    })


# ---------------------------------------------------------------------------
# Tests: _safe_name
# ---------------------------------------------------------------------------

class TestSafeName(unittest.TestCase):
    def test_allows_alphanumeric_and_dashes(self):
        self.assertEqual(main_module._safe_name("my-session_1"), "my-session_1")

    def test_replaces_spaces(self):
        self.assertEqual(main_module._safe_name("my session"), "my_session")

    def test_replaces_special_chars(self):
        self.assertEqual(main_module._safe_name("test@2024!"), "test_2024_")

    def test_strips_whitespace(self):
        self.assertEqual(main_module._safe_name("  hello  "), "hello")


# ---------------------------------------------------------------------------
# Tests: on_receive
# ---------------------------------------------------------------------------

class TestOnReceive(unittest.TestCase):
    def setUp(self):
        main_module.readings.clear()
        main_module.active_session_name = None
        self.mock_interface = MagicMock()
        main_module.map_handler.add_point = MagicMock()

    def test_ignores_non_text_portnum(self):
        packet = {"decoded": {"portnum": "POSITION_APP", "text": "{}"}}
        main_module.on_receive(packet, self.mock_interface)
        self.mock_interface.sendText.assert_not_called()

    def test_ignores_packet_missing_lat_lon(self):
        packet = _make_packet(json.dumps({"messageId": "x"}))
        main_module.on_receive(packet, self.mock_interface)
        self.mock_interface.sendText.assert_not_called()

    def test_handles_invalid_json(self):
        packet = _make_packet("not-json")
        main_module.on_receive(packet, self.mock_interface)
        self.mock_interface.sendText.assert_not_called()

    def test_appends_reading_on_valid_packet(self):
        packet = _make_packet(_location_text())
        main_module.on_receive(packet, self.mock_interface)
        self.assertEqual(len(main_module.readings), 1)

    def test_reading_contains_expected_fields(self):
        packet = _make_packet(_location_text(message_id="test-id", lat=1.1, lon=2.2, elevation=500.0),
                               snr=7.5, rssi=-80)
        main_module.on_receive(packet, self.mock_interface)
        r = main_module.readings[0]
        self.assertEqual(r["messageId"], "test-id")
        self.assertAlmostEqual(r["lat"], 1.1)
        self.assertAlmostEqual(r["lon"], 2.2)
        self.assertAlmostEqual(r["elevation"], 500.0)
        self.assertAlmostEqual(r["snr"], 7.5)
        self.assertEqual(r["rssi"], -80)

    def test_sends_ack_on_valid_packet(self):
        packet = _make_packet(_location_text(), from_id="!sender")
        main_module.on_receive(packet, self.mock_interface)
        self.mock_interface.sendText.assert_called_once()
        call_args = self.mock_interface.sendText.call_args
        ack = json.loads(call_args[0][0])
        self.assertTrue(ack["ack"])
        self.assertEqual(call_args[1]["destinationId"], "!sender")

    def test_ack_contains_snr_and_rssi(self):
        packet = _make_packet(_location_text(), snr=9.0, rssi=-70)
        main_module.on_receive(packet, self.mock_interface)
        ack = json.loads(self.mock_interface.sendText.call_args[0][0])
        self.assertEqual(ack["snr"], 9.0)
        self.assertEqual(ack["rssi"], -70)

    def test_ack_message_id_matches_packet(self):
        packet = _make_packet(_location_text(message_id="match-me"))
        main_module.on_receive(packet, self.mock_interface)
        ack = json.loads(self.mock_interface.sendText.call_args[0][0])
        self.assertEqual(ack["messageId"], "match-me")

    def test_hops_away_calculated_correctly(self):
        packet = _make_packet(_location_text(), hop_start=5, hop_limit=3)
        main_module.on_receive(packet, self.mock_interface)
        self.assertEqual(main_module.readings[0]["hopsAway"], 2)

    def test_map_handler_add_point_called(self):
        packet = _make_packet(_location_text(lat=10.0, lon=20.0))
        main_module.on_receive(packet, self.mock_interface)
        main_module.map_handler.add_point.assert_called_once()
        kwargs = main_module.map_handler.add_point.call_args[1]
        self.assertAlmostEqual(kwargs["lat"], 10.0)
        self.assertAlmostEqual(kwargs["lon"], 20.0)

    def test_uses_from_id_for_ack_destination(self):
        packet = _make_packet(_location_text(), from_id="!node99")
        main_module.on_receive(packet, self.mock_interface)
        dest = self.mock_interface.sendText.call_args[1]["destinationId"]
        self.assertEqual(dest, "!node99")

    def test_elevation_defaults_to_zero_when_missing(self):
        text = json.dumps({"messageId": "x", "lat": 1.0, "lon": 2.0})
        packet = _make_packet(text)
        main_module.on_receive(packet, self.mock_interface)
        self.assertEqual(main_module.readings[0]["elevation"], 0.0)


# ---------------------------------------------------------------------------
# Tests: Flask routes (via test client)
# ---------------------------------------------------------------------------

class TestFlaskRoutes(unittest.TestCase):
    def setUp(self):
        main_module.app.config["TESTING"] = True
        self.client = main_module.app.test_client()
        main_module.readings.clear()
        main_module.active_session_name = None
        main_module.active_session_created = ""

    def test_api_readings_returns_list(self):
        resp = self.client.get("/api/readings")
        self.assertEqual(resp.status_code, 200)

    def test_api_active_session_returns_none_when_none(self):
        resp = self.client.get("/api/sessions/active")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIsNone(data["name"])

    def test_api_create_session_requires_name(self):
        resp = self.client.post("/api/sessions",
                                data=json.dumps({}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_api_create_session_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_module.SESSIONS_DIR = tmpdir
            resp = self.client.post("/api/sessions",
                                    data=json.dumps({"name": "testsession"}),
                                    content_type="application/json")
            self.assertEqual(resp.status_code, 201)
            data = json.loads(resp.data)
            self.assertEqual(data["name"], "testsession")
            self.assertEqual(main_module.active_session_name, "testsession")

    def test_api_create_session_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_module.SESSIONS_DIR = tmpdir
            # Create first
            self.client.post("/api/sessions",
                             data=json.dumps({"name": "dupe"}),
                             content_type="application/json")
            # Try again
            resp = self.client.post("/api/sessions",
                                    data=json.dumps({"name": "dupe"}),
                                    content_type="application/json")
            self.assertEqual(resp.status_code, 409)

    def test_api_delete_active_session_blocked(self):
        main_module.active_session_name = "active_one"
        resp = self.client.delete("/api/sessions/active_one")
        self.assertEqual(resp.status_code, 400)

    def test_api_list_sessions_returns_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_module.SESSIONS_DIR = tmpdir
            resp = self.client.get("/api/sessions")
            self.assertEqual(resp.status_code, 200)
            self.assertIsInstance(json.loads(resp.data), list)


if __name__ == "__main__":
    unittest.main()
