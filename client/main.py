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
from flask import Flask, jsonify, render_template, request

from map_handler import MapHandler, TILES_LIGHT, TILES_DARK

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CLIENT] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

SERIAL_PORT = os.environ.get("MESHTASTIC_PORT", "/dev/ttyUSB0")
SEND_INTERVAL = int(os.environ.get("SEND_INTERVAL", "10"))
_raw_node = os.environ.get("SERVER_NODE_ID", "").strip()
SERVER_NODE_ID: str | None = _raw_node if len(_raw_node) > 1 else None  # "!" alone is not valid
MAP_OUTPUT = os.environ.get("MAP_OUTPUT", "/app/static/map.html")
WEB_PORT = int(os.environ.get("WEB_PORT", "5001"))

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
                map_handler.add_point(
                    lat=entry["lat"],
                    lon=entry["lon"],
                    snr=snr,
                    rssi=rssi,
                    message_id=message_id,
                    timestamp=entry["timestamp"],
                )

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
        try:
            send_location()
        except Exception as exc:
            logger.error("Failed to send location: %s", exc)
        time.sleep(SEND_INTERVAL)


if __name__ == "__main__":
    main()
