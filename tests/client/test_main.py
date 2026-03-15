import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

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
pub_mod = types.ModuleType("pubsub.pub")
pub_mock = MagicMock()
pubsub_mod.pub = pub_mock
sys.modules.setdefault("pubsub", pubsub_mod)

# map_handler (client-local import)
map_handler_mod = types.ModuleType("map_handler")
map_handler_mod.MapHandler = MagicMock()
map_handler_mod.render_points_to_file = MagicMock()
map_handler_mod.TILES_LIGHT = "OpenStreetMap"
map_handler_mod.TILES_DARK = "CartoDB dark_matter"
sys.modules.setdefault("map_handler", map_handler_mod)

# gps_mock (client-local import)
gps_mock_mod = types.ModuleType("gps_mock")

class _FakeGPSReading:
    lat = 37.7749
    lon = -122.4194
    timestamp = "2024-01-01T00:00:00+00:00"

class _FakeGPSMock:
    def __init__(self, start_lat=37.7749, start_lon=-122.4194):
        pass
    def get_reading(self):
        return _FakeGPSReading()

gps_mock_mod.GPSMock = _FakeGPSMock
sys.modules.setdefault("gps_mock", gps_mock_mod)

# Now safe to import
import client.main as main_module  # noqa: E402  (imported after stubs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOnReceive(unittest.TestCase):
    def _make_packet(self, text: str) -> dict:
        return {"decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text}}

    def test_ignores_non_text_portnum(self):
        packet = {"decoded": {"portnum": "POSITION_APP", "text": "{}"}}
        main_module.on_receive(packet, MagicMock())

    def test_ignores_packet_without_ack_flag(self):
        packet = self._make_packet(json.dumps({"messageId": "x", "ack": False}))
        with patch.object(main_module.logger, "info") as mock_info:
            main_module.on_receive(packet, MagicMock())
        ack_calls = [c for c in mock_info.call_args_list if "ACK received" in str(c)]
        self.assertEqual(len(ack_calls), 0)

    def test_logs_ack_on_valid_ack_packet(self):
        payload = {"messageId": "abc-123", "snr": 7.5, "rssi": -85, "ack": True}
        packet = self._make_packet(json.dumps(payload))
        with self.assertLogs(level="INFO") as cm:
            main_module.on_receive(packet, MagicMock())
        self.assertTrue(any("ACK received" in line for line in cm.output))
        self.assertTrue(any("abc-123" in line for line in cm.output))

    def test_handles_invalid_json_gracefully(self):
        packet = self._make_packet("not-json")
        main_module.on_receive(packet, MagicMock())

    def test_handles_missing_decoded_key(self):
        packet = {}
        main_module.on_receive(packet, MagicMock())

    def test_handles_empty_text(self):
        packet = self._make_packet("")
        main_module.on_receive(packet, MagicMock())


class TestSendLocation(unittest.TestCase):
    def setUp(self):
        self.mock_interface = MagicMock()
        main_module.interface = self.mock_interface
        self._original_node = main_module.SERVER_NODE_ID
        main_module.SERVER_NODE_ID = "!testnode"

    def tearDown(self):
        main_module.SERVER_NODE_ID = self._original_node

    def test_returns_string_uuid(self):
        import uuid
        result = main_module.send_location()
        parsed = uuid.UUID(result, version=4)
        self.assertEqual(str(parsed), result)

    def test_calls_send_text_once(self):
        main_module.send_location()
        self.mock_interface.sendText.assert_called_once()

    def test_payload_contains_required_keys(self):
        main_module.send_location()
        call_args = self.mock_interface.sendText.call_args
        payload = json.loads(call_args[0][0])
        for key in ("messageId", "lat", "lon", "timestamp"):
            self.assertIn(key, payload)

    def test_payload_message_id_matches_return_value(self):
        returned_id = main_module.send_location()
        call_args = self.mock_interface.sendText.call_args
        payload = json.loads(call_args[0][0])
        self.assertEqual(payload["messageId"], returned_id)

    def test_destination_id_passed_to_send_text(self):
        main_module.SERVER_NODE_ID = "!deadbeef"
        main_module.send_location()
        call_kwargs = self.mock_interface.sendText.call_args[1]
        self.assertEqual(call_kwargs["destinationId"], "!deadbeef")

    def test_returns_none_when_no_target_configured(self):
        main_module.SERVER_NODE_ID = None
        result = main_module.send_location()
        self.assertIsNone(result)
        self.mock_interface.sendText.assert_not_called()


if __name__ == "__main__":
    unittest.main()
