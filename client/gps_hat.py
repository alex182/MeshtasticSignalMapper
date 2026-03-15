import logging
import threading
from datetime import datetime, timezone

import serial
import pynmea2

from gps_mock import GPSReading

logger = logging.getLogger(__name__)


class GPSHat:
    """
    Reads position from the Adafruit Ultimate GPS HAT via serial UART.

    Runs a background thread that continuously reads NMEA sentences.
    GGA sentences provide lat/lon/altitude and fix-quality status.
    get_reading() returns the latest confirmed fix, or raises RuntimeError
    if no fix has been acquired yet.
    """

    def __init__(self, port: str = "/dev/ttyS0", baud: int = 9600):
        self._lock = threading.Lock()
        self._latest: GPSReading | None = None
        self._ser = serial.Serial(port, baud, timeout=1)
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        logger.info("GPS HAT reader started on %s at %d baud", port, baud)

    def _reader(self) -> None:
        while True:
            try:
                raw = self._ser.readline()
                line = raw.decode("ascii", errors="replace").strip()
                if not line.startswith("$"):
                    continue

                msg = pynmea2.parse(line)

                # GGA carries fix quality and MSL altitude.
                # gps_qual == 0 means no fix; skip those.
                if isinstance(msg, pynmea2.GGA):
                    if not msg.gps_qual or int(msg.gps_qual) == 0:
                        continue
                    lat = msg.latitude
                    lon = msg.longitude
                    elevation = float(msg.altitude) if msg.altitude else 0.0
                    ts = datetime.now(timezone.utc).isoformat()
                    with self._lock:
                        self._latest = GPSReading(
                            lat=lat,
                            lon=lon,
                            timestamp=ts,
                            elevation=elevation,
                        )

            except pynmea2.ParseError:
                pass
            except Exception as exc:
                logger.debug("GPS reader error: %s", exc)

    def get_reading(self) -> GPSReading:
        with self._lock:
            if self._latest is None:
                raise RuntimeError("No GPS fix yet")
            return self._latest
