# Meshtastic Signal Mapper

Maps RF signal strength (SNR/RSSI) from a moving Meshtastic node onto an OpenStreetMap, displayed in a live web UI. Useful for surveying mesh network coverage from a vehicle.

## How It Works

- **Client** (car / Raspberry Pi): reads GPS coordinates and broadcasts them every N seconds as a JSON text message over Meshtastic
- **Server** (base station): receives the packets, extracts GPS + SNR/RSSI/hop metadata, plots points on a Folium/Leaflet map, serves a Flask web UI, and sends an ACK back to the client

Both sides run in Alpine-based Docker containers with their Meshtastic boards connected via USB.

## Architecture

```
[Car]                              [Base Station]
 GPS -> client/main.py             server/main.py -> Flask :5000
        |                                    |
        | Meshtastic (LoRa)                  +-> map_handler.py -> map.html
        |  {lat, lon, messageId, ts}         +-> sessions/*.json
        +---------------------------------->
        <-- ACK {messageId, snr, rssi} -----
```

## Project Structure

```
MeshtasticSignalMapper/
├── client/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── docker-compose.yml        # deploy on car machine
│   ├── main.py                   # GPS sender + ACK handler
│   └── gps_mock.py               # simulated GPS (replace with real hardware)
├── server/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── docker-compose.yml        # deploy on base station
│   ├── main.py                   # Meshtastic receiver, Flask API, ACK sender
│   ├── map_handler.py            # Folium map rendering (thread-safe)
│   └── templates/
│       └── index.html            # web UI
└── docker-compose.dev.yml        # both services on one machine (two USB boards)
```

## Web UI Features

- Live map with colour-coded pins: green (SNR >= 7 dB), orange (>= 3 dB), red (< 3 dB)
- Route polyline connecting all points in order
- Sidebar listing all received messages with SNR, RSSI, hops, coordinates, and timestamp
- Click a message in the sidebar to pan the map to its pin and open its popup
- Named sessions: save each drive as a named session, browse and reload historical sessions
- Delete saved sessions from the UI
- Independent auto-refresh controls for the messages sidebar and the map (Off / 5s / 10s / 30s / 60s), plus manual refresh buttons

## Message Formats

**Client → Server**
```json
{"messageId": "<uuid4>", "lat": 39.059200, "lon": -94.880400, "timestamp": "2026-03-10T18:20:00Z"}
```

**Server → Client (ACK)**
```json
{"messageId": "<uuid4>", "snr": 6.25, "rssi": -90, "ack": true}
```

SNR and RSSI are extracted from the Meshtastic packet metadata (`rxSnr` / `rxRssi`), not from the message body. Hop count is derived from `hopStart - hopLimit`.

## Deployment

### Base Station (Server)

1. Connect your Meshtastic board via USB and confirm the device path (e.g. `ls /dev/ttyACM*`)
2. Edit `server/docker-compose.yml` — update the `devices:` path and `MESHTASTIC_PORT` to match
3. Optionally set `SERVER_NODE_ID` in `client/docker-compose.yml` to the server's node ID (run `meshtastic --info` to find it)

```bash
cd server
docker compose up -d
```

Open `http://<server-ip>:5001` in a browser.

### Client (Car / Raspberry Pi)

1. Connect the Meshtastic board via USB
2. Edit `client/docker-compose.yml` — update the `devices:` path, `MESHTASTIC_PORT`, and `SERVER_NODE_ID`
3. Adjust `GPS_START_LAT` / `GPS_START_LON` for the mock GPS starting location (or replace `gps_mock.py` with real hardware reads)

```bash
cd client
docker compose up -d
```

### Development (Both Services on One Machine)

Requires two Meshtastic boards on `/dev/ttyUSB0` (client) and `/dev/ttyUSB1` (server):

```bash
docker compose -f docker-compose.dev.yml up -d
```

Web UI available at `http://localhost:5000`.

## Environment Variables

### Client

| Variable | Default | Description |
|---|---|---|
| `MESHTASTIC_PORT` | `/dev/ttyUSB0` | Serial device for the Meshtastic board |
| `SEND_INTERVAL` | `10` | Seconds between GPS broadcasts |
| `GPS_START_LAT` | `37.7749` | Mock GPS starting latitude |
| `GPS_START_LON` | `-122.4194` | Mock GPS starting longitude |
| `SERVER_NODE_ID` | *(broadcast)* | Meshtastic node ID of the server (e.g. `!a6961690`) |

### Server

| Variable | Default | Description |
|---|---|---|
| `MESHTASTIC_PORT` | `/dev/ttyUSB0` | Serial device for the Meshtastic board |
| `WEB_PORT` | `5000` | Flask listen port |
| `MAP_OUTPUT` | `/app/static/map.html` | Path for the rendered live map |
| `SESSIONS_DIR` | `/app/static/sessions` | Directory for saved session JSON files |

## GPS Hardware

`client/gps_mock.py` simulates a GPS hat with slow eastward drift. To use real hardware, replace the `GPSMock.get_reading()` implementation with reads from your GPS device (e.g. via `gpsd` or direct serial NMEA parsing).

## Dependencies

| Side | Packages |
|---|---|
| Client | `meshtastic >= 2.3.0`, `pyserial >= 3.5` |
| Server | `meshtastic >= 2.3.0`, `pyserial >= 3.5`, `folium >= 0.14.0`, `flask >= 3.0.0` |

Both run on `python:3.12-alpine`.
