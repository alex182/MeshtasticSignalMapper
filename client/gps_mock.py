import random
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class GPSReading:
    lat: float
    lon: float
    timestamp: str
    elevation: float


class GPSMock:
    """
    Simulates a GPS hat by generating coordinates that drift along a route.
    Replace this class with real GPS hardware reads when available.
    """

    def __init__(self, start_lat: float = 37.7749, start_lon: float = -122.4194):
        self.lat = start_lat
        self.lon = start_lon

    def get_reading(self) -> GPSReading:
        # Simulate slow eastward movement with small random noise
        self.lat += random.uniform(-0.0002, 0.0002)
        self.lon += 0.0003 + random.uniform(-0.0001, 0.0001)

        return GPSReading(
            lat=round(self.lat, 6),
            lon=round(self.lon, 6),
            timestamp=datetime.now(timezone.utc).isoformat(),
            elevation=round(random.uniform(800, 1050),10)
        )
