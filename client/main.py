import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import meshtastic.serial_interface
from pubsub import pub
import re
from flask import Flask, jsonify, render_template, request, Response

from map_handler import MapHandler, TILES_LIGHT, TILES_DARK

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CLIENT] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

SERIAL_PORT = os.environ.get("MESHTASTIC_PORT", "/dev/ttyUSB0")
SEND_INTERVAL = int(os.environ.get("SEND_INTERVAL", "10"))
_SEND_INTERVAL_MIN = 1
_SEND_INTERVAL_MAX = 120
_raw_node = os.environ.get("SERVER_NODE_ID", "").strip()
SERVER_NODE_ID: str | None = _raw_node if len(_raw_node) > 1 else None  # "!" alone is not valid
MAP_OUTPUT = os.environ.get("MAP_OUTPUT", "/app/static/map.html")
WEB_PORT = int(os.environ.get("WEB_PORT", "5001"))
AUTOSAVE_DIR = os.environ.get("AUTOSAVE_DIR", "/app/saves")
AUTOSAVE_INTERVAL = int(os.environ.get("AUTOSAVE_INTERVAL", "60"))

_gps_source = os.environ.get("GPS_SOURCE", "mock").lower()
if _gps_source == "hat":
    from gps_hat import GPSHat
    gps = GPSHat(
        port=os.environ.get("GPS_SERIAL_PORT", "/dev/ttyS0"),
        baud=int(os.environ.get("GPS_BAUD_RATE", "9600")),
    )
else:
    from gps_mock import GPSMock
    gps = GPSMock(
        start_lat=float(os.environ.get("GPS_START_LAT", "37.7749")),
        start_lon=float(os.environ.get("GPS_START_LON", "-122.4194")),
    )
GPS_SOURCE_NAME: str = _gps_source  # tracks current runtime source ("hat" or "mock")

interface = None
map_handler = MapHandler(MAP_OUTPUT)
app = Flask(__name__, static_folder="static", template_folder="templates")

# Sent messages — ordered list + fast lookup by messageId
sent_messages: list[dict] = []
_messages_by_id: dict[str, dict] = {}
_messages_lock = threading.Lock()

# Sending control
_sending_enabled = True

# Auto-save state
_autosave_path: str | None = None        # fixed for the lifetime of a session
_last_autosave_at: str | None = None     # ISO timestamp of last successful write


# ---------------------------------------------------------------------------
# Meshtastic callbacks
# ---------------------------------------------------------------------------

def on_receive(packet, interface):
    """Handle incoming packets — expecting ACK messages from the server."""
    try:
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        data = json.loads(decoded.get("text", ""))
        if not data.get("ack"):
            return

        message_id = data.get("messageId")
        snr = data.get("snr")
        rssi = data.get("rssi")

        logger.info(
            "ACK received | messageId=%s | SNR=%s dB | RSSI=%s dBm",
            message_id, snr, rssi,
        )

        with _messages_lock:
            entry = _messages_by_id.get(message_id)
            if entry:
                ack_time = time.time()
                entry["status"] = "acked"
                entry["snr"] = snr
                entry["rssi"] = rssi
                entry["ackTime"] = datetime.fromtimestamp(ack_time, tz=timezone.utc).isoformat()
                entry["rttMs"] = round(ack_time - entry["sentAt"], 2)
                map_handler.ack_point(message_id=message_id, snr=snr, rssi=rssi)

    except (json.JSONDecodeError, KeyError) as exc:
        logger.debug("Non-JSON or unexpected packet: %s", exc)


def send_location() -> str | None:
    """Read current GPS position and broadcast it via Meshtastic."""
    if SERVER_NODE_ID is None:
        logger.debug("No target node configured, skipping send.")
        return None

    try:
        reading = gps.get_reading()
    except RuntimeError as exc:
        logger.warning("GPS not ready: %s", exc)
        return None
    message_id = str(uuid.uuid4())
    payload = {
        "messageId": message_id,
        "lat": reading.lat,
        "lon": reading.lon,
        "timestamp": reading.timestamp,
        "elevation": reading.elevation
    }
    interface.sendText(json.dumps(payload), destinationId=SERVER_NODE_ID)

    with _messages_lock:
        seq = len(sent_messages) + 1
        entry = {
            "messageId": message_id,
            "seq": seq,
            "lat": reading.lat,
            "lon": reading.lon,
            "elevation": reading.elevation,
            "timestamp": reading.timestamp,
            "sentAt": time.time(),
            "status": "pending",
            "snr": None,
            "rssi": None,
            "ackTime": None,
            "rttMs": None,
        }
        sent_messages.append(entry)
        _messages_by_id[message_id] = entry

    map_handler.add_pending_point(
        lat=reading.lat,
        lon=reading.lon,
        elevation=reading.elevation,
        message_id=message_id,
        timestamp=reading.timestamp,
    )

    logger.info(
        "Sent | messageId=%s | lat=%s | lon=%s | elevation=%s m",
        message_id,
        reading.lat,
        reading.lon,
        reading.elevation
    )
    return message_id


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/map")
def map_view():
    return app.send_static_file("map.html")


@app.route("/api/messages")
def api_messages():
    with _messages_lock:
        return jsonify(list(sent_messages))


@app.route("/api/nodes")
def api_nodes():
    if interface is None:
        return jsonify([])
    try:
        my_num = (interface.myInfo or {}).get("myNodeNum")
    except Exception:
        my_num = None
    nodes = []
    for node_id, node in (interface.nodes or {}).items():
        if node.get("num") == my_num:
            continue
        user = node.get("user", {})
        nodes.append({
            "nodeId": user.get("id") or node_id,
            "longName": user.get("longName") or node_id,
            "shortName": user.get("shortName", "?"),
        })
    nodes.sort(key=lambda n: n["longName"].lower())
    return jsonify(nodes)


@app.route("/api/map-style", methods=["POST"])
def api_set_map_style():
    body = request.get_json(silent=True) or {}
    dark = body.get("dark", False)
    tiles = TILES_DARK if dark else TILES_LIGHT
    map_handler.set_tiles(tiles)
    logger.info("Map tiles set to %s", tiles)
    return jsonify({"dark": dark, "tiles": tiles})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify({"serverNodeId": SERVER_NODE_ID})


@app.route("/api/config", methods=["POST"])
def api_set_config():
    global SERVER_NODE_ID
    body = request.get_json(silent=True) or {}
    SERVER_NODE_ID = body.get("serverNodeId") or None
    logger.info("SERVER_NODE_ID updated to %s", SERVER_NODE_ID)
    return jsonify({"serverNodeId": SERVER_NODE_ID})


@app.route("/api/gps-source", methods=["GET"])
def api_get_gps_source():
    return jsonify({"source": GPS_SOURCE_NAME})


@app.route("/api/gps-source", methods=["POST"])
def api_set_gps_source():
    global gps, GPS_SOURCE_NAME
    body = request.get_json(silent=True) or {}
    source = body.get("source", "").lower()
    if source not in ("hat", "mock"):
        return jsonify({"error": "source must be 'hat' or 'mock'"}), 400
    if source == GPS_SOURCE_NAME:
        return jsonify({"source": GPS_SOURCE_NAME})
    try:
        if source == "hat":
            from gps_hat import GPSHat
            new_gps = GPSHat(
                port=os.environ.get("GPS_SERIAL_PORT", "/dev/ttyS0"),
                baud=int(os.environ.get("GPS_BAUD_RATE", "9600")),
            )
        else:
            from gps_mock import GPSMock
            new_gps = GPSMock(
                start_lat=float(os.environ.get("GPS_START_LAT", "37.7749")),
                start_lon=float(os.environ.get("GPS_START_LON", "-122.4194")),
            )
        gps = new_gps
        GPS_SOURCE_NAME = source
        logger.info("GPS source switched to %s", source)
        return jsonify({"source": GPS_SOURCE_NAME})
    except Exception as exc:
        logger.error("Failed to switch GPS source to %s: %s", source, exc)
        return jsonify({"error": str(exc)}), 500


def _slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_") or "unknown"


def _resolve_target_name() -> str:
    target_name = "broadcast"
    if SERVER_NODE_ID and interface is not None:
        try:
            for node in (interface.nodes or {}).values():
                if node.get("user", {}).get("id") == SERVER_NODE_ID:
                    target_name = node.get("user", {}).get("longName") or target_name
                    break
        except Exception:
            pass
    elif SERVER_NODE_ID:
        target_name = SERVER_NODE_ID
    return target_name


def _resolve_my_name() -> str:
    my_name = "unknown"
    if interface is not None:
        try:
            my_num = (interface.myInfo or {}).get("myNodeNum")
            if my_num is not None:
                for node in (interface.nodes or {}).values():
                    if node.get("num") == my_num:
                        my_name = node.get("user", {}).get("longName") or my_name
                        break
        except Exception:
            pass
    return my_name


def _build_payload() -> dict:
    """Build the session payload. Must be called with _messages_lock held."""
    return {
        "savedAt": datetime.now(tz=timezone.utc).isoformat(),
        "myNode": _resolve_my_name(),
        "targetNode": _resolve_target_name(),
        "gpsSource": GPS_SOURCE_NAME,
        "totalSent": len(sent_messages),
        "totalAcked": sum(1 for m in sent_messages if m["status"] == "acked"),
        "messages": list(sent_messages),
    }


def _do_autosave() -> None:
    global _autosave_path, _last_autosave_at
    with _messages_lock:
        if not sent_messages:
            return
        if _autosave_path is None:
            session_dt = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"autosave_{session_dt}_{_slugify(_resolve_target_name())}.json"
            _autosave_path = os.path.join(AUTOSAVE_DIR, filename)
        payload = _build_payload()

    os.makedirs(AUTOSAVE_DIR, exist_ok=True)
    with open(_autosave_path, "w") as f:
        json.dump(payload, f, indent=2)
    _last_autosave_at = datetime.now(tz=timezone.utc).isoformat()
    logger.info("Auto-saved %d messages → %s", len(payload["messages"]), _autosave_path)


def _autosave_loop() -> None:
    while True:
        time.sleep(AUTOSAVE_INTERVAL)
        try:
            _do_autosave()
        except Exception as exc:
            logger.error("Auto-save failed: %s", exc)


@app.route("/api/save", methods=["POST"])
def api_save():
    target_name = _resolve_target_name()
    now = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{now}_{_slugify(target_name)}.json"

    with _messages_lock:
        payload = _build_payload()

    data = json.dumps(payload, indent=2)
    return Response(
        data,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/clear", methods=["POST"])
def api_clear():
    global _autosave_path, _last_autosave_at
    with _messages_lock:
        sent_messages.clear()
        _messages_by_id.clear()
    map_handler.clear()
    _autosave_path = None
    _last_autosave_at = None
    logger.info("Session cleared")
    return jsonify({"ok": True})


@app.route("/api/import", methods=["POST"])
def api_import():
    global _sending_enabled
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file provided"}), 400
    try:
        data = json.loads(f.read())
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"invalid JSON: {exc}"}), 400

    messages = data.get("messages")
    if not isinstance(messages, list):
        return jsonify({"error": "'messages' key missing or not a list"}), 400

    _sending_enabled = False

    with _messages_lock:
        sent_messages.clear()
        _messages_by_id.clear()
        for msg in messages:
            sent_messages.append(msg)
            _messages_by_id[msg["messageId"]] = msg

    map_points = [
        {
            "lat": msg["lat"],
            "lon": msg["lon"],
            "snr": msg.get("snr"),
            "rssi": msg.get("rssi"),
            "elevation": msg.get("elevation", 0.0),
            "message_id": msg["messageId"],
            "timestamp": msg["timestamp"],
            "pending": msg.get("status") == "pending",
        }
        for msg in messages
    ]
    map_handler.replace_points(map_points)

    logger.info("Imported %d messages from file; sending stopped", len(messages))
    return jsonify({"imported": len(messages), "sending": _sending_enabled})


@app.route("/api/send-interval", methods=["GET"])
def api_get_send_interval():
    return jsonify({"sendInterval": SEND_INTERVAL, "min": _SEND_INTERVAL_MIN, "max": _SEND_INTERVAL_MAX})


@app.route("/api/send-interval", methods=["POST"])
def api_set_send_interval():
    global SEND_INTERVAL
    body = request.get_json(silent=True) or {}
    value = body.get("sendInterval")
    if not isinstance(value, int) or not (_SEND_INTERVAL_MIN <= value <= _SEND_INTERVAL_MAX):
        return jsonify({"error": f"sendInterval must be an integer between {_SEND_INTERVAL_MIN} and {_SEND_INTERVAL_MAX}"}), 400
    SEND_INTERVAL = value
    logger.info("Send interval updated to %d s", SEND_INTERVAL)
    return jsonify({"sendInterval": SEND_INTERVAL})


@app.route("/api/sending", methods=["GET"])
def api_get_sending():
    return jsonify({"sending": _sending_enabled})


@app.route("/api/sending", methods=["POST"])
def api_set_sending():
    global _sending_enabled
    body = request.get_json(silent=True) or {}
    _sending_enabled = bool(body.get("sending", True))
    logger.info("Sending %s", "started" if _sending_enabled else "stopped")
    return jsonify({"sending": _sending_enabled})


@app.route("/api/status")
def api_status():
    with _messages_lock:
        total = len(sent_messages)
        acked = sum(1 for m in sent_messages if m["status"] == "acked")
        last = sent_messages[-1] if sent_messages else None
    return jsonify({
        "total": total,
        "acked": acked,
        "sendInterval": SEND_INTERVAL,
        "gpsSource": GPS_SOURCE_NAME,
        "last_lat": last["lat"] if last else None,
        "last_lon": last["lon"] if last else None,
        "last_timestamp": last["timestamp"] if last else None,
        "lastAutosaveAt": _last_autosave_at,
        "autosaveInterval": AUTOSAVE_INTERVAL,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_web():
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, use_reloader=False)


def main():
    global interface

    map_handler.generate_map()

    web_thread = threading.Thread(target=_run_web, daemon=True)
    web_thread.start()

    autosave_thread = threading.Thread(target=_autosave_loop, daemon=True)
    autosave_thread.start()
    logger.info("Auto-save enabled every %d seconds → %s", AUTOSAVE_INTERVAL, AUTOSAVE_DIR)
    logger.info("Web server started on http://0.0.0.0:%d", WEB_PORT)

    logger.info("Connecting to Meshtastic on %s ...", SERIAL_PORT)
    pub.subscribe(on_receive, "meshtastic.receive")
    interface = meshtastic.serial_interface.SerialInterface(SERIAL_PORT)
    logger.info("Connected. Sending GPS position every %d seconds.", SEND_INTERVAL)

    def _shutdown(sig, frame):
        logger.info("Shutting down...")
        interface.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        if _sending_enabled:
            try:
                send_location()
            except Exception as exc:
                logger.error("Failed to send location: %s", exc)
        time.sleep(SEND_INTERVAL)


if __name__ == "__main__":
    main()
