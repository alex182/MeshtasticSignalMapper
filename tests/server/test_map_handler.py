import os
import tempfile
import threading
import unittest

from server.map_handler import MapHandler, _snr_color, render_points_to_file

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _point(lat=37.7, lon=-122.4, snr=5.0, rssi=-90, elevation=900.0,
           message_id="msg-1", timestamp="2024-01-01T00:00:00+00:00") -> dict:
    return dict(lat=lat, lon=lon, snr=snr, rssi=rssi, elevation=elevation,
                message_id=message_id, timestamp=timestamp)


def _pending_point(lat=37.7, lon=-122.4, elevation=900.0,
                   message_id="msg-p", timestamp="2024-01-01T00:00:00+00:00") -> dict:
    return dict(lat=lat, lon=lon, snr=None, rssi=None, elevation=elevation,
                message_id=message_id, timestamp=timestamp, pending=True)


# ---------------------------------------------------------------------------
# Tests: _snr_color
# ---------------------------------------------------------------------------

class TestSnrColor(unittest.TestCase):
    def test_good_snr_is_green(self):
        self.assertEqual(_snr_color(7), "green")
        self.assertEqual(_snr_color(10), "green")

    def test_ok_snr_is_orange(self):
        self.assertEqual(_snr_color(3), "orange")
        self.assertEqual(_snr_color(6.9), "orange")

    def test_bad_snr_is_red(self):
        self.assertEqual(_snr_color(2.9), "red")
        self.assertEqual(_snr_color(-10), "red")
        self.assertEqual(_snr_color(0), "red")


# ---------------------------------------------------------------------------
# Tests: render_points_to_file
# ---------------------------------------------------------------------------

class TestRenderPointsToFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output = os.path.join(self.tmpdir, "map.html")

    def test_creates_html_file(self):
        render_points_to_file([], self.output)
        self.assertTrue(os.path.exists(self.output))

    def test_output_is_valid_html(self):
        render_points_to_file([_point()], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("<html", content.lower())

    def test_empty_points_renders_default_center(self):
        render_points_to_file([], self.output)
        with open(self.output) as f:
            content = f.read()
        # Default center coords should appear somewhere in the output
        self.assertIn("39.0594", content)

    def test_single_point_centers_on_that_point(self):
        render_points_to_file([_point(lat=51.5, lon=-0.1)], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("51.5", content)
        self.assertIn("-0.1", content)

    def test_popup_contains_snr_and_rssi(self):
        render_points_to_file([_point(snr=8.0, rssi=-75)], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("8.0", content)
        self.assertIn("-75", content)

    def test_popup_contains_elevation(self):
        render_points_to_file([_point(elevation=1234.0)], self.output)
        with open(self.output) as f:
            content = f.read()
        # 1234.0 m * 3.28084 = 4049 ft
        self.assertIn("4049", content)

    def test_creates_parent_dirs_if_missing(self):
        nested_output = os.path.join(self.tmpdir, "a", "b", "map.html")
        render_points_to_file([], nested_output)
        self.assertTrue(os.path.exists(nested_output))

    def test_highlight_script_injected_with_multiple_points(self):
        points = [_point(message_id=f"id-{i}") for i in range(3)]
        render_points_to_file(points, self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("coordMap", content)
        self.assertIn("highlight", content)

    def test_message_id_truncated_in_popup(self):
        render_points_to_file([_point(message_id="abcdefgh12345678")], self.output)
        with open(self.output) as f:
            content = f.read()
        # Only first 8 chars shown
        self.assertIn("abcdefgh", content)

    def test_pending_point_renders_gray(self):
        render_points_to_file([_pending_point()], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("gray", content)

    def test_pending_point_popup_shows_pending_status(self):
        render_points_to_file([_pending_point()], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("Pending", content)

    def test_pending_point_popup_omits_snr(self):
        render_points_to_file([_pending_point()], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertNotIn("SNR: None", content)

    def test_heatmap_rendered_for_acked_points(self):
        render_points_to_file(
            [_point(), _point(lat=37.8, message_id="msg-2")], self.output
        )
        with open(self.output) as f:
            content = f.read()
        self.assertIn("heatLayer", content)

    def test_heatmap_not_rendered_when_no_acked_points(self):
        render_points_to_file([_pending_point()], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertNotIn("heatLayer", content)

    def test_layer_control_rendered(self):
        render_points_to_file([_point()], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("L.control.layers", content)

    def test_getview_setview_script_injected(self):
        render_points_to_file([_point()], self.output)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("getView", content)
        self.assertIn("setView", content)


# ---------------------------------------------------------------------------
# Tests: MapHandler
# ---------------------------------------------------------------------------

class TestMapHandler(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output = os.path.join(self.tmpdir, "map.html")
        self.handler = MapHandler(self.output)

    def test_init_creates_output_dir(self):
        self.assertTrue(os.path.isdir(os.path.dirname(self.output)))

    def test_add_point_writes_file(self):
        self.handler.add_point(lat=1.0, lon=2.0, snr=5.0, rssi=-80,
                               elevation=100.0, message_id="x", timestamp="ts")
        self.assertTrue(os.path.exists(self.output))

    def test_add_point_accumulates(self):
        for i in range(3):
            self.handler.add_point(lat=float(i), lon=float(i), snr=5.0,
                                   rssi=-80, elevation=0.0,
                                   message_id=f"id-{i}", timestamp="ts")
        self.assertEqual(len(self.handler._points), 3)

    def test_clear_empties_points(self):
        self.handler.add_point(lat=1.0, lon=2.0, snr=5.0, rssi=-80,
                               elevation=0.0, message_id="x", timestamp="ts")
        self.handler.clear()
        self.assertEqual(len(self.handler._points), 0)

    def test_clear_rewrites_file(self):
        self.handler.add_point(lat=1.0, lon=2.0, snr=5.0, rssi=-80,
                               elevation=0.0, message_id="x", timestamp="ts")
        mtime_before = os.path.getmtime(self.output)
        import time; time.sleep(0.01)
        self.handler.clear()
        mtime_after = os.path.getmtime(self.output)
        self.assertGreater(mtime_after, mtime_before)

    def test_load_points_bulk_adds(self):
        pts = [_point(message_id=f"id-{i}") for i in range(5)]
        self.handler.load_points(pts)
        self.assertEqual(len(self.handler._points), 5)

    def test_generate_map_writes_file(self):
        self.handler.generate_map()
        self.assertTrue(os.path.exists(self.output))

    def test_thread_safety(self):
        errors = []

        def add_points():
            try:
                for i in range(10):
                    self.handler.add_point(lat=float(i), lon=float(i), snr=5.0,
                                           rssi=-80, elevation=0.0,
                                           message_id=f"t-{i}", timestamp="ts")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_points) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.handler._points), 40)

    def test_add_pending_point_stores_pending_flag(self):
        self.handler.add_pending_point(
            lat=1.0, lon=2.0, elevation=0.0, message_id="p1", timestamp="ts"
        )
        self.assertEqual(len(self.handler._points), 1)
        self.assertTrue(self.handler._points[0]["pending"] is True)
        self.assertIsNone(self.handler._points[0]["snr"])

    def test_add_pending_point_writes_file(self):
        self.handler.add_pending_point(
            lat=1.0, lon=2.0, elevation=0.0, message_id="p1", timestamp="ts"
        )
        self.assertTrue(os.path.exists(self.output))

    def test_ack_point_updates_pending_flag(self):
        self.handler.add_pending_point(
            lat=1.0, lon=2.0, elevation=0.0, message_id="p1", timestamp="ts"
        )
        self.handler.ack_point(message_id="p1", snr=7.0, rssi=-80)
        self.assertFalse(self.handler._points[0]["pending"])
        self.assertEqual(self.handler._points[0]["snr"], 7.0)

    def test_ack_point_unknown_id_is_noop(self):
        self.handler.ack_point(message_id="nonexistent", snr=7.0, rssi=-80)
        self.assertEqual(len(self.handler._points), 0)

    def test_replace_points_swaps_all(self):
        for i in range(3):
            self.handler.add_point(
                lat=float(i), lon=float(i), snr=5.0, rssi=-80,
                elevation=0.0, message_id=f"old-{i}", timestamp="ts"
            )
        self.handler.replace_points([_point(message_id="new")])
        self.assertEqual(len(self.handler._points), 1)
        self.assertEqual(self.handler._points[0]["message_id"], "new")

    def test_replace_points_with_empty_clears(self):
        for i in range(3):
            self.handler.add_point(
                lat=float(i), lon=float(i), snr=5.0, rssi=-80,
                elevation=0.0, message_id=f"old-{i}", timestamp="ts"
            )
        self.handler.replace_points([])
        self.assertEqual(len(self.handler._points), 0)


if __name__ == "__main__":
    unittest.main()
