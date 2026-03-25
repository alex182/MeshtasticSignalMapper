import io
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
    elevation = 900.0

class _FakeGPSMock:
    def __init__(self, start_lat=37.7749, start_lon=-122.4194):
        pass
    def get_reading(self):
        return _FakeGPSReading()

gps_mock_mod.GPSMock = _FakeGPSMock
sys.modules.setdefault("gps_mock", gps_mock_mod)

# Now safe to import
import importlib
import client.main as main_module  # noqa: E402  (imported after stubs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOnReceive(unittest.TestCase):
    def setUp(self):
        main_module.sent_messages.clear()
        main_module._messages_by_id.clear()

    def _make_packet(self, text: str) -> dict:
        return {"decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text}}

    def test_ignores_non_text_portnum(self):
        packet = {"decoded": {"portnum": "POSITION_APP", "text": "{}"}}
        # Should not raise
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
        # Should not raise
        main_module.on_receive(packet, MagicMock())

    def test_handles_missing_decoded_key(self):
        packet = {}
        main_module.on_receive(packet, MagicMock())

    def test_handles_empty_text(self):
        packet = self._make_packet("")
        main_module.on_receive(packet, MagicMock())

    def test_ack_calls_ack_point_when_message_known(self):
        entry = {
            "lat": 1.0,
            "lon": 2.0,
            "elevation": 0.0,
            "timestamp": "ts",
            "sentAt": 0.0,
            "status": "pending",
        }
        main_module._messages_by_id["abc-123"] = entry
        main_module.map_handler.ack_point.reset_mock()
        payload = {"messageId": "abc-123", "snr": 7.5, "rssi": -85, "ack": True}
        packet = self._make_packet(json.dumps(payload))
        with self.assertLogs(level="INFO"):
            main_module.on_receive(packet, MagicMock())
        main_module.map_handler.ack_point.assert_called_once_with(
            message_id="abc-123", snr=7.5, rssi=-85
        )

    def test_ack_does_not_call_ack_point_for_unknown_message(self):
        main_module.map_handler.ack_point.reset_mock()
        payload = {"messageId": "unknown-id", "snr": 7.5, "rssi": -85, "ack": True}
        packet = self._make_packet(json.dumps(payload))
        with self.assertLogs(level="INFO"):
            main_module.on_receive(packet, MagicMock())
        main_module.map_handler.ack_point.assert_not_called()


class TestSendLocation(unittest.TestCase):
    def setUp(self):
        self.mock_interface = MagicMock()
        main_module.interface = self.mock_interface
        self._original_node = main_module.SERVER_NODE_ID
        main_module.SERVER_NODE_ID = "!testnode"
        main_module.sent_messages.clear()
        main_module._messages_by_id.clear()

    def tearDown(self):
        main_module.SERVER_NODE_ID = self._original_node

    def test_returns_string_uuid(self):
        import uuid
        result = main_module.send_location()
        # Should be a valid UUID4 string
        parsed = uuid.UUID(result, version=4)
        self.assertEqual(str(parsed), result)

    def test_calls_send_text_once(self):
        main_module.send_location()
        self.mock_interface.sendText.assert_called_once()

    def test_payload_contains_required_keys(self):
        main_module.send_location()
        call_args = self.mock_interface.sendText.call_args
        payload = json.loads(call_args[0][0])
        for key in ("messageId", "lat", "lon", "timestamp", "elevation"):
            self.assertIn(key, payload)

    def test_payload_message_id_matches_return_value(self):
        returned_id = main_module.send_location()
        call_args = self.mock_interface.sendText.call_args
        payload = json.loads(call_args[0][0])
        self.assertEqual(payload["messageId"], returned_id)

    def test_destination_id_passed_to_send_text(self):
        original = main_module.SERVER_NODE_ID
        main_module.SERVER_NODE_ID = "!deadbeef"
        main_module.send_location()
        call_kwargs = self.mock_interface.sendText.call_args[1]
        self.assertEqual(call_kwargs["destinationId"], "!deadbeef")
        main_module.SERVER_NODE_ID = original

    def test_calls_add_pending_point(self):
        main_module.map_handler.add_pending_point.reset_mock()
        main_module.send_location()
        main_module.map_handler.add_pending_point.assert_called_once()

    def test_add_pending_point_called_with_correct_lat_lon(self):
        main_module.map_handler.add_pending_point.reset_mock()
        main_module.send_location()
        call_kwargs = main_module.map_handler.add_pending_point.call_args[1]
        self.assertEqual(call_kwargs["lat"], 37.7749)
        self.assertEqual(call_kwargs["lon"], -122.4194)


class TestFlaskRoutes(unittest.TestCase):
    def setUp(self):
        main_module.app.config["TESTING"] = True
        self.client = main_module.app.test_client()
        main_module.sent_messages.clear()
        main_module._messages_by_id.clear()
        main_module._sending_enabled = True
        main_module.SEND_INTERVAL = 10
        main_module._autosave_path = None
        main_module._last_autosave_at = None

    def test_get_send_interval_returns_values(self):
        resp = self.client.get("/api/send-interval")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("sendInterval", data)
        self.assertIn("min", data)
        self.assertIn("max", data)

    def test_post_send_interval_updates_value(self):
        resp = self.client.post(
            "/api/send-interval",
            data=json.dumps({"sendInterval": 30}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(main_module.SEND_INTERVAL, 30)

    def test_post_send_interval_rejects_out_of_range(self):
        resp = self.client.post(
            "/api/send-interval",
            data=json.dumps({"sendInterval": 9999}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_get_sending_returns_state(self):
        resp = self.client.get("/api/sending")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("sending", data)

    def test_post_sending_stops(self):
        resp = self.client.post(
            "/api/sending",
            data=json.dumps({"sending": False}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(main_module._sending_enabled)

    def test_post_sending_starts(self):
        main_module._sending_enabled = False
        resp = self.client.post(
            "/api/sending",
            data=json.dumps({"sending": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(main_module._sending_enabled)

    def test_clear_empties_messages(self):
        main_module.sent_messages.append({"messageId": "test"})
        self.client.post("/api/clear")
        self.assertEqual(len(main_module.sent_messages), 0)

    def test_clear_resets_autosave_path(self):
        main_module._autosave_path = "something"
        self.client.post("/api/clear")
        self.assertIsNone(main_module._autosave_path)

    def test_save_returns_attachment(self):
        resp = self.client.post("/api/save")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

    def test_save_body_is_valid_json(self):
        resp = self.client.post("/api/save")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("messages", data)

    def test_import_loads_messages(self):
        payload = {
            "messages": [
                {
                    "messageId": "x",
                    "lat": 1.0,
                    "lon": 2.0,
                    "elevation": 0.0,
                    "timestamp": "ts",
                    "status": "acked",
                    "snr": 5.0,
                    "rssi": -90,
                    "seq": 1,
                    "sentAt": 0,
                    "ackTime": "ts",
                    "rttMs": 1.0,
                }
            ]
        }
        data = {"file": (io.BytesIO(json.dumps(payload).encode()), "session.json")}
        resp = self.client.post(
            "/api/import", data=data, content_type="multipart/form-data"
        )
        self.assertEqual(resp.status_code, 200)
        result = json.loads(resp.data)
        self.assertEqual(result["imported"], 1)

    def test_import_stops_sending(self):
        payload = {
            "messages": [
                {
                    "messageId": "x",
                    "lat": 1.0,
                    "lon": 2.0,
                    "elevation": 0.0,
                    "timestamp": "ts",
                    "status": "acked",
                    "snr": 5.0,
                    "rssi": -90,
                    "seq": 1,
                    "sentAt": 0,
                    "ackTime": "ts",
                    "rttMs": 1.0,
                }
            ]
        }
        data = {"file": (io.BytesIO(json.dumps(payload).encode()), "session.json")}
        self.client.post("/api/import", data=data, content_type="multipart/form-data")
        self.assertFalse(main_module._sending_enabled)

    def test_import_invalid_json_returns_400(self):
        data = {"file": (io.BytesIO(b"not-json"), "session.json")}
        resp = self.client.post(
            "/api/import", data=data, content_type="multipart/form-data"
        )
        self.assertEqual(resp.status_code, 400)

    def test_import_missing_file_returns_400(self):
        resp = self.client.post("/api/import", data={}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)

    def test_status_includes_autosave_fields(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("lastAutosaveAt", data)
        self.assertIn("autosaveInterval", data)


if __name__ == "__main__":
    unittest.main()
