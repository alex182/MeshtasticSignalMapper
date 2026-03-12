import json
import logging
import os
import signal
import sys
import time
import uuid

import meshtastic.serial_interface
from pubsub import pub

from gps_mock import GPSMock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CLIENT] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

SERIAL_PORT = os.environ.get("MESHTASTIC_PORT", "/dev/ttyUSB0")
SEND_INTERVAL = int(os.environ.get("SEND_INTERVAL", "10"))
SERVER_NODE_ID = os.environ.get("SERVER_NODE_ID")  # e.g. "!a1b2c3d4"; None = broadcast

gps = GPSMock(
    start_lat=float(os.environ.get("GPS_START_LAT", "37.7749")),
    start_lon=float(os.environ.get("GPS_START_LON", "-122.4194")),
)

interface = None


def on_receive(packet, interface):
    """Handle incoming packets — expecting ACK messages from the server."""
    try:
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        data = json.loads(decoded.get("text", ""))
        if not data.get("ack"):
            return

        logger.info(
            "ACK received | messageId=%s | SNR=%s dB | RSSI=%s dBm",
            data.get("messageId"),
            data.get("snr"),
            data.get("rssi"),
        )
    except (json.JSONDecodeError, KeyError) as exc:
        logger.debug("Non-JSON or unexpected packet: %s", exc)


def send_location() -> str:
    """Read current GPS position and broadcast it via Meshtastic."""
    reading = gps.get_reading()
    message_id = str(uuid.uuid4())
    payload = {
        "messageId": message_id,
        "lat": reading.lat,
        "lon": reading.lon,
        "timestamp": reading.timestamp,
        "elevation": reading.elevation
    }
    interface.sendText(json.dumps(payload), destinationId=SERVER_NODE_ID)
    logger.info(
        "Sent | messageId=%s | lat=%s | lon=%s | elevation=%s m",
        message_id,
        reading.lat,
        reading.lon,
        reading.elevation
    )
    return message_id


def main():
    global interface

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
