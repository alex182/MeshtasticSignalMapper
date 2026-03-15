import json
import logging
import os
import threading

import folium

logger = logging.getLogger(__name__)

# SNR thresholds (dB) for marker colour
_SNR_GOOD = 7
_SNR_OK = 3

DEFAULT_CENTER = (39.0594, -94.8827)  # Bonner Springs, KS

TILES_LIGHT = "OpenStreetMap"
TILES_DARK  = "CartoDB dark_matter"


def _snr_color(snr: float) -> str:
    if snr >= _SNR_GOOD:
        return "green"
    if snr >= _SNR_OK:
        return "orange"
    return "red"


def render_points_to_file(points: list[dict], output_path: str, tiles: str = TILES_LIGHT) -> None:
    """Render a list of signal points to a Folium map HTML file."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    if points:
        center = (points[-1]["lat"], points[-1]["lon"])
    else:
        center = DEFAULT_CENTER

    m = folium.Map(location=center, zoom_start=14, tiles=tiles)

    if points:
        coords = [(p["lat"], p["lon"]) for p in points]
        folium.PolyLine(coords, color="blue", weight=2.5, opacity=0.8).add_to(m)

        last_idx = len(points) - 1
        coord_map = {}  # point index → [lat, lon] for highlight script
        for idx, pt in enumerate(points):
            color = _snr_color(pt["snr"])
            if idx == 0:
                icon_glyph = "home"
            elif idx == last_idx:
                icon_glyph = "flag"
            else:
                icon_glyph = "map-marker"
            msg_id = pt.get("message_id") or pt.get("messageId", "")
            popup_html = (
                f"<b>Point #{idx + 1}</b><br>"
                f"Lat: {pt['lat']:.6f}<br>"
                f"Lon: {pt['lon']:.6f}<br>"
                f"SNR: {pt['snr']} dB<br>"
                f"RSSI: {pt['rssi']} dBm<br>"
                f"Time: {pt['timestamp']}<br>"
                f"Elevation: {pt['elevation'] * 3.28084:.0f} ft<br>"
                f"ID: {msg_id[:8]}…"
            )
            tooltip_text = f"#{idx + 1} | SNR: {pt['snr']} dB | RSSI: {pt['rssi']} dBm"
            folium.Marker(
                location=(pt["lat"], pt["lon"]),
                icon=folium.Icon(color=color, icon=icon_glyph),
                popup=folium.Popup(popup_html, max_width=260),
                tooltip=folium.Tooltip(tooltip_text),
            ).add_to(m)
            coord_map[idx] = [pt["lat"], pt["lon"]]

        # Inject a postMessage listener so the parent page can highlight a marker.
        # Looks up the Leaflet map by scanning window for an L.Map instance at
        # message-receive time (avoids relying on Folium's variable being on window).
        highlight_script = f"""
(function() {{
  var coordMap = {json.dumps(coord_map)};
  var mapId = '{m.get_name()}';
  function findMap() {{
    if (window[mapId] instanceof L.Map) return window[mapId];
    for (var k in window) {{
      try {{ if (window[k] instanceof L.Map) return window[k]; }} catch(e) {{}}
    }}
    return null;
  }}
  window.addEventListener('message', function(e) {{
    if (!e.data || e.data.type !== 'highlight') return;
    var coords = coordMap[e.data.index];
    if (!coords) return;
    var mapObj = findMap();
    if (!mapObj) return;
    mapObj.panTo(coords, {{animate: true}});
    mapObj.eachLayer(function(layer) {{
      if (layer instanceof L.Marker) {{
        var ll = layer.getLatLng();
        if (Math.abs(ll.lat - coords[0]) < 1e-6 && Math.abs(ll.lng - coords[1]) < 1e-6) {{
          layer.openPopup();
        }}
      }}
    }});
  }});
}})();"""
        m.get_root().script.add_child(folium.Element(highlight_script))

    m.save(output_path)
    logger.debug("Map saved with %d points → %s", len(points), output_path)


class MapHandler:
    """Tracks live session points and renders the active map."""

    def __init__(self, output_path: str, tiles: str = TILES_LIGHT):
        self.output_path = output_path
        self._points: list[dict] = []
        self._tiles = tiles
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def add_point(
        self,
        lat: float,
        lon: float,
        snr: float,
        rssi: int,
        elevation: float,   
        message_id: str,
        timestamp: str,
    ) -> None:
        with self._lock:
            self._points.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "snr": snr,
                    "rssi": rssi,
                    "elevation": elevation,
                    "message_id": message_id,
                    "timestamp": timestamp,
                }
            )
            render_points_to_file(self._points, self.output_path, self._tiles)

    def set_tiles(self, tiles: str) -> None:
        with self._lock:
            self._tiles = tiles
            render_points_to_file(self._points, self.output_path, self._tiles)

    def load_points(self, points: list[dict]) -> None:
        """Bulk-load saved points and render once."""
        with self._lock:
            self._points.extend(points)
            render_points_to_file(self._points, self.output_path, self._tiles)

    def clear(self) -> None:
        """Reset to empty and re-render."""
        with self._lock:
            self._points.clear()
            render_points_to_file(self._points, self.output_path, self._tiles)

    def generate_map(self) -> None:
        with self._lock:
            render_points_to_file(self._points, self.output_path, self._tiles)
