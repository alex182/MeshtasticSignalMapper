import os
import tempfile
import threading
import unittest

from server.map_handler import MapHandler, _snr_color, render_points_to_file

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _point(lat=37.7, lon=-122.4, snr=5.0, rssi=-90,
           message_id="msg-1", timestamp="2024-01-01T00:00:00+00:00") -> dict:
    return dict(lat=lat, lon=lon, snr=snr, rssi=rssi,
                message_id=message_id, timestamp=timestamp)


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
        self.assertIn("abcdefgh", content)

    def test_dark_tiles_used_when_specified(self):
        from server.map_handler import TILES_DARK
        render_points_to_file([], self.output, tiles=TILES_DARK)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("cartocdn.com", content)


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
                               message_id="x", timestamp="ts")
        self.assertTrue(os.path.exists(self.output))

    def test_add_point_accumulates(self):
        for i in range(3):
            self.handler.add_point(lat=float(i), lon=float(i), snr=5.0,
                                   rssi=-80, message_id=f"id-{i}", timestamp="ts")
        self.assertEqual(len(self.handler._points), 3)

    def test_clear_empties_points(self):
        self.handler.add_point(lat=1.0, lon=2.0, snr=5.0, rssi=-80,
                               message_id="x", timestamp="ts")
        self.handler.clear()
        self.assertEqual(len(self.handler._points), 0)

    def test_clear_rewrites_file(self):
        self.handler.add_point(lat=1.0, lon=2.0, snr=5.0, rssi=-80,
                               message_id="x", timestamp="ts")
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

    def test_set_tiles_rerenders_map(self):
        from server.map_handler import TILES_DARK
        self.handler.add_point(lat=1.0, lon=2.0, snr=5.0, rssi=-80,
                               message_id="x", timestamp="ts")
        self.handler.set_tiles(TILES_DARK)
        with open(self.output) as f:
            content = f.read()
        self.assertIn("cartocdn.com", content)

    def test_thread_safety(self):
        errors = []

        def add_points():
            try:
                for i in range(10):
                    self.handler.add_point(lat=float(i), lon=float(i), snr=5.0,
                                           rssi=-80, message_id=f"t-{i}", timestamp="ts")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_points) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.handler._points), 40)


if __name__ == "__main__":
    unittest.main()
