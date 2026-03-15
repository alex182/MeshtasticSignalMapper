# Meshtastic Signal Mapper

Maps RF signal strength (SNR/RSSI) from a moving Meshtastic node onto an OpenStreetMap, displayed in a live web UI. Useful for surveying mesh network coverage from a vehicle.

## How It Works

- **Client** (car / Raspberry Pi): reads GPS coordinates and broadcasts them every N seconds as a JSON text message over Meshtastic. Receives ACKs from the server containing SNR/RSSI, tracking round-trip time per message.
- **Server** (base station): receives the packets, extracts GPS + SNR/RSSI/hop metadata, plots points on a Folium/Leaflet map, serves a Flask web UI, and sends an ACK back to the client.

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
        Flask :5002 (client web UI)
```

## Project Structure

```
MeshtasticSignalMapper/
├── client/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── docker-compose.yml        # deploy on car machine
│   ├── main.py                   # GPS sender + ACK handler + Flask web UI
│   ├── map_handler.py            # Folium map rendering (thread-safe)
│   ├── gps_mock.py               # simulated GPS with eastward drift
│   └── gps_hat.py                # Adafruit Ultimate GPS HAT (UART/NMEA)
├── server/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── docker-compose.yml        # deploy on base station
│   ├── main.py                   # Meshtastic receiver, Flask API, ACK sender
│   ├── map_handler.py            # Folium map rendering (thread-safe)
│   └── templates/
│       └── index.html            # web UI
├── tests/
│   ├── client/
│   │   ├── test_main.py
│   │   └── test_gps_hat.py
│   └── server/
│       ├── test_main.py
│       └── test_map_handler.py
└── docker-compose.dev.yml        # both services on one machine (two USB boards)
```

## Web UI Features

Both the client and server have a web UI with a shared layout: header with role badge, collapsible/expandable sidebar, and a live Folium map.

### Server (`http://<server-ip>:5000`)

- Live map with colour-coded pins: green (SNR ≥ 7 dB), orange (≥ 3 dB), red (< 3 dB)
- Route polyline connecting all points in order
- Sidebar with all received messages: SNR, RSSI, hop count, coordinates, timestamp
- Click a message to pan the map to its pin and open the popup
- Named sessions: save each drive, browse and reload historical sessions, delete old sessions
- Table view toggle for compact message listing
- Dark/Light map tile toggle (OpenStreetMap ↔ CartoDB Dark Matter)
- Independent auto-refresh controls for sidebar and map (Off / 5s / 10s / 30s / 60s)

### Client (`http://<client-ip>:5002`)

- Live map with the same colour-coded pins and route polyline
- Sidebar showing all sent messages with status (Pending / ACKed), SNR, RSSI, sent time, ACK time, and round-trip time
- Filter messages by ACKed or Pending status
- Table view toggle for compact message listing
- Target node selector: dropdown of live mesh peers — leave blank to auto-scan until a node appears
- Dark/Light map tile toggle
- Sidebar collapse and full-width expand

## Message Formats

**Client → Server**
```json
{"messageId": "<uuid4>", "lat": 39.059200, "lon": -94.880400, "elevation": 312.4, "timestamp": "2026-03-10T18:20:00Z"}
```

**Server → Client (ACK)**
```json
{"messageId": "<uuid4>", "snr": 6.25, "rssi": -90, "elevation": 312.4, "ack": true}
```

SNR and RSSI are extracted from Meshtastic packet metadata (`rxSnr` / `rxRssi`), not from the message body. Hop count is derived from `hopStart - hopLimit`.

## Deployment

### Base Station (Server)

1. Connect your Meshtastic board via USB and confirm the device path (e.g. `ls /dev/ttyACM*`)
2. Edit `server/docker-compose.yml` — update the `devices:` path and `MESHTASTIC_PORT`

```bash
cd server
docker compose up -d
```

Open `http://<server-ip>:5000` in a browser.

### Client (Car / Raspberry Pi)

1. Connect the Meshtastic board via USB
2. Edit `client/docker-compose.yml` — update the `devices:` path and `MESHTASTIC_PORT`
3. Set `GPS_SOURCE` to `hat` (Adafruit GPS HAT) or `mock` (simulated). See [GPS Hardware](#gps-hardware) below.
4. Set `SERVER_NODE_ID` to the server's node ID (run `meshtastic --info`), or leave it empty (`""`) to auto-scan for nodes from the web UI

```bash
cd client
docker compose up -d
```

Open `http://<client-ip>:5002` in a browser. If `SERVER_NODE_ID` is blank, the target node dropdown will auto-scan and populate as nodes are discovered on the mesh.

### Development (Both Services on One Machine)

Requires two Meshtastic boards on `/dev/ttyUSB0` (client) and `/dev/ttyUSB1` (server):

```bash
docker compose -f docker-compose.dev.yml up -d
```

## Environment Variables

### Client

| Variable | Default | Description |
|---|---|---|
| `MESHTASTIC_PORT` | `/dev/ttyUSB0` | Serial device for the Meshtastic board |
| `SEND_INTERVAL` | `10` | Seconds between GPS broadcasts |
| `GPS_SOURCE` | `mock` | GPS source: `hat` (Adafruit GPS HAT) or `mock` (simulated) |
| `GPS_SERIAL_PORT` | `/dev/ttyS0` | Serial device for the GPS HAT (used when `GPS_SOURCE=hat`) |
| `GPS_BAUD_RATE` | `9600` | Baud rate for the GPS HAT serial port |
| `GPS_START_LAT` | `37.7749` | Mock GPS starting latitude (used when `GPS_SOURCE=mock`) |
| `GPS_START_LON` | `-122.4194` | Mock GPS starting longitude (used when `GPS_SOURCE=mock`) |
| `SERVER_NODE_ID` | *(broadcast)* | Meshtastic node ID of the server (e.g. `!a6961690`). Leave empty to auto-scan from the UI. |
| `WEB_PORT` | `5002` | Flask listen port for the client web UI |

### Server

| Variable | Default | Description |
|---|---|---|
| `MESHTASTIC_PORT` | `/dev/ttyUSB0` | Serial device for the Meshtastic board |
| `WEB_PORT` | `5000` | Flask listen port |
| `MAP_OUTPUT` | `/app/static/map.html` | Path for the rendered live map |
| `SESSIONS_DIR` | `/app/static/sessions` | Directory for saved session JSON files |

## GPS Hardware

Two GPS sources are supported, selected via the `GPS_SOURCE` environment variable:

### `GPS_SOURCE=mock` (default)

`client/gps_mock.py` simulates a moving vehicle with slow eastward drift. Useful for development without hardware. Configure the starting coordinates with `GPS_START_LAT` / `GPS_START_LON`.

### `GPS_SOURCE=hat` — Adafruit Ultimate GPS HAT

`client/gps_hat.py` reads real position from the [Adafruit Ultimate GPS HAT](https://www.adafruit.com/product/2324) connected to the Raspberry Pi's UART pins. It parses NMEA `GGA` sentences for latitude, longitude, and MSL altitude.

**Hardware setup:**
1. Attach the GPS HAT to the Pi's GPIO header
2. Enable the UART in `/boot/config.txt`:
   ```
   enable_uart=1
   dtoverlay=disable-bt
   ```
3. Reboot — the GPS will appear as `/dev/ttyS0`
4. In `client/docker-compose.yml`, set `GPS_SOURCE: "hat"` and ensure `/dev/ttyS0` is in `devices:`

The HAT reader runs in a background thread and `send_location()` will skip transmission (with a warning) until the first satellite fix is acquired.

## Testing

```bash
python3 -m pytest tests/ -v
```

Tests cover Meshtastic packet handling, ACK flow, session management, Flask routes, map rendering, GPS HAT NMEA parsing, and thread safety. Heavy dependencies (meshtastic, pubsub, serial) are stubbed at the module level so no hardware is required.

## Dependencies

| Side | Packages |
|---|---|
| Client | `meshtastic >= 2.3.0`, `pyserial >= 3.5`, `pynmea2 >= 1.18`, `flask >= 3.0`, `folium >= 0.14` |
| Server | `meshtastic >= 2.3.0`, `pyserial >= 3.5`, `flask >= 3.0`, `folium >= 0.14` |

Both run on `python:3.12-alpine`.
