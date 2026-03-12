import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import meshtastic.serial_interface
from pubsub import pub
from flask import Flask, jsonify, render_template, request, send_file, abort

from map_handler import MapHandler, render_points_to_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

SERIAL_PORT = os.environ.get("MESHTASTIC_PORT", "/dev/ttyUSB0")
MAP_OUTPUT = os.environ.get("MAP_OUTPUT", "/app/static/map.html")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "/app/static/sessions")
WEB_PORT = int(os.environ.get("WEB_PORT", "5000"))

os.makedirs(SESSIONS_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
map_handler = MapHandler(MAP_OUTPUT)

interface = None

# Active session state
active_session_name: str | None = None
active_session_created: str = ""
readings: list[dict] = []


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """Sanitise a session name to safe filename characters."""
    return re.sub(r"[^\w\-]", "_", name.strip())


def _session_path(name: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{name}.json")


def _session_map_path(name: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{name}_map.html")


def _save_active_session() -> None:
    if not active_session_name:
        return
    data = {
        "name": active_session_name,
        "created": active_session_created,
        "readings": readings,
    }
    try:
        with open(_session_path(active_session_name), "w") as f:
            json.dump(data, f)
    except Exception as exc:
        logger.warning("Could not save session: %s", exc)


def _load_session_file(name: str) -> dict | None:
    try:
        with open(_session_path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Could not read session %s: %s", name, exc)
        return None


def _list_sessions() -> list[dict]:
    sessions = []
    try:
        for fname in sorted(os.listdir(SESSIONS_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            data = _load_session_file(fname[:-5])
            if data:
                sessions.append({
                    "name": data["name"],
                    "created": data["created"],
                    "count": len(data.get("readings", [])),
                    "active": data["name"] == active_session_name,
                })
    except Exception as exc:
        logger.warning("Could not list sessions: %s", exc)
    return sessions


# ---------------------------------------------------------------------------
# Meshtastic callbacks
# ---------------------------------------------------------------------------

def on_receive(packet, interface):
    """Handle incoming packets from the client."""
    try:
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        data = json.loads(decoded.get("text", ""))

        if "lat" not in data or "lon" not in data:
            return

        snr: float = packet.get("rxSnr", 0.0)
        rssi: int = packet.get("rxRssi", 0)
        hop_limit: int = packet.get("hopLimit", 0)
        hop_start: int = packet.get("hopStart", hop_limit)
        hops_away: int = hop_start - hop_limit
        message_id: str = data["messageId"]
        lat: float = data["lat"]
        lon: float = data["lon"]
        elevation: float = data.get("elevation", 0.0)
        timestamp: str = data.get("timestamp", datetime.now(timezone.utc).isoformat())

        logger.info(
            "Received | messageId=%s | lat=%s | lon=%s | SNR=%s dB | RSSI=%s dBm | Elevation=%s m | hops=%s",
            message_id, lat, lon, snr, rssi, elevation, hops_away,
        )

        reading = {
            "messageId": message_id,
            "lat": lat,
            "lon": lon,
            "timestamp": timestamp,
            "snr": snr,
            "rssi": rssi,
            "elevation": elevation,
            "hopsAway": hops_away,
        }
        readings.append(reading)
        _save_active_session()

        map_handler.add_point(
            lat=lat,
            lon=lon,
            snr=snr,
            rssi=rssi,
            elevation=elevation,
            message_id=message_id,
            timestamp=timestamp,
        )

        ack_payload = json.dumps(
            {"messageId": message_id, "snr": snr, "elevation": elevation, "rssi": rssi, "ack": True}
        )
        sender_id = packet.get("fromId") or packet.get("from")
        interface.sendText(ack_payload, destinationId=sender_id)
        logger.info("ACK sent | messageId=%s", message_id)

    except (json.JSONDecodeError, KeyError) as exc:
        logger.debug("Non-JSON or unexpected packet: %s", exc)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/map")
def map_view():
    return app.send_static_file("map.html")


@app.route("/api/readings")
def api_readings():
    return jsonify(readings)


# Session API

@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    return jsonify(_list_sessions())


@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    global active_session_name, active_session_created, readings

    body = request.get_json(silent=True) or {}
    raw_name = body.get("name", "").strip()
    if not raw_name:
        return jsonify({"error": "name is required"}), 400

    name = _safe_name(raw_name)
    if os.path.exists(_session_path(name)):
        return jsonify({"error": f"Session '{name}' already exists"}), 409

    active_session_name = name
    active_session_created = datetime.now(timezone.utc).isoformat()
    readings = []
    map_handler.clear()
    _save_active_session()

    logger.info("New session started: %s", name)
    return jsonify({"name": name, "created": active_session_created}), 201


@app.route("/api/sessions/active", methods=["GET"])
def api_active_session():
    return jsonify({
        "name": active_session_name,
        "created": active_session_created,
        "count": len(readings),
    })


@app.route("/api/sessions/<name>/readings", methods=["GET"])
def api_session_readings(name):
    name = _safe_name(name)
    if name == active_session_name:
        return jsonify(readings)
    data = _load_session_file(name)
    if data is None:
        abort(404)
    return jsonify(data.get("readings", []))


@app.route("/api/sessions/<name>/map", methods=["GET"])
def api_session_map(name):
    name = _safe_name(name)
    if name == active_session_name:
        return app.send_static_file("map.html")

    map_path = _session_map_path(name)
    # Render on demand if not cached
    if not os.path.exists(map_path):
        data = _load_session_file(name)
        if data is None:
            abort(404)
        render_points_to_file(data.get("readings", []), map_path)

    return send_file(map_path, mimetype="text/html")


@app.route("/api/sessions/<name>", methods=["DELETE"])
def api_delete_session(name):
    global active_session_name, readings

    name = _safe_name(name)
    if name == active_session_name:
        return jsonify({"error": "Cannot delete the active session"}), 400

    deleted = False
    for path in (_session_path(name), _session_map_path(name)):
        if os.path.exists(path):
            os.remove(path)
            deleted = True

    if not deleted:
        abort(404)
    return jsonify({"deleted": name})


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
    logger.info("Connected. Listening for GPS packets.")

    def _shutdown(sig, frame):
        logger.info("Shutting down...")
        interface.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
