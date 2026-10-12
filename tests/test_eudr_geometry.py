"""
Tests for EUDR Geodesic Topology and Geometry Engine (WGS84 EPSG:4326).
"""
import unittest
from src.domain.eudr_geometry import (
    evaluate_plot_geometry,
    validate_and_close_polygon
)


class TestEudrGeometry(unittest.TestCase):

    def test_centroid_valid_precision(self):
        # Small plot <= 4.0 ha with 6+ decimal places
        coords = [(-79.754123, -1.542189)]
        res = evaluate_plot_geometry(coords, plot_size_ha=2.5)

        self.assertEqual(res["type"], "CENTROID")
        self.assertTrue(res["is_valid"])
        self.assertIsNone(res["error"])
        self.assertFalse(res["closed_ring_enforced"])

    def test_centroid_insufficient_precision(self):
        # Small plot <= 4.0 ha with only 3 decimals
        coords = [(-79.754, -1.542)]
        res = evaluate_plot_geometry(coords, plot_size_ha=2.5)

        self.assertEqual(res["type"], "CENTROID")
        self.assertFalse(res["is_valid"])
        self.assertIn("6 decimales", res["error"])

    def test_large_plot_centroid_rejected(self):
        # Large plot > 4.0 ha requires closed polygon, single point rejected
        coords = [(-79.754123, -1.542189)]
        res = evaluate_plot_geometry(coords, plot_size_ha=5.5)

        self.assertFalse(res["is_valid"])
        self.assertIn("requiere polígono", res["error"])

    def test_polygon_closed_ring_enforcement(self):
        # 3 points provided without explicit closure: first point should be auto-appended
        coords = [(-79.7541, -1.5421), (-79.7520, -1.5430), (-79.7510, -1.5410)]
        res = evaluate_plot_geometry(coords, plot_size_ha=6.0)

        self.assertEqual(res["type"], "POLYGON")
        self.assertTrue(res["is_valid"])
        self.assertTrue(res["closed_ring_enforced"])
        self.assertEqual(len(res["coordinates"]), 4)
        self.assertEqual(res["coordinates"][0], res["coordinates"][-1])
        self.assertGreater(res["calculated_area_ha"], 0.0)

    def test_polygon_already_closed(self):
        coords = [
            (-79.7541, -1.5421),
            (-79.7520, -1.5430),
            (-79.7510, -1.5410),
            (-79.7541, -1.5421)
        ]
        res = evaluate_plot_geometry(coords, plot_size_ha=6.0)

        self.assertEqual(res["type"], "POLYGON")
        self.assertTrue(res["is_valid"])
        self.assertFalse(res["closed_ring_enforced"])
        self.assertEqual(len(res["coordinates"]), 4)

    def test_polygon_too_few_points(self):
        coords = [(-79.7541, -1.5421), (-79.7520, -1.5430)]
        res = evaluate_plot_geometry(coords, plot_size_ha=6.0)

        self.assertEqual(res["type"], "POLYGON")
        self.assertFalse(res["is_valid"])
        self.assertIn("mínimo 3 vértices", res["error"])

    def test_centroid_string_coordinates_with_trailing_zero(self):
        # 6 decimals with trailing zero passed as string preserved
        coords = [("-79.754120", "-1.542180")]
        res = evaluate_plot_geometry(coords, plot_size_ha=2.5)
        self.assertEqual(res["type"], "CENTROID")
        self.assertTrue(res["is_valid"])

    def test_inverted_coordinates_auto_normalized(self):
        # Passed as (lat, lon) where lat is ~ -1.5 and lon is ~ -79.75
        coords = [(-1.542189, -79.754123)]
        res = evaluate_plot_geometry(coords, plot_size_ha=2.5)
        self.assertTrue(res["is_valid"])
        # Coordinates should be auto-normalized to (lon, lat)
        lon, lat = res["coordinates"][0]
        self.assertAlmostEqual(lon, -79.754123, places=5)
        self.assertAlmostEqual(lat, -1.542189, places=5)

    def test_empty_coordinates(self):
        res = evaluate_plot_geometry([], plot_size_ha=1.0)
        self.assertFalse(res["is_valid"])
        self.assertEqual(res["type"], "NONE")



if __name__ == "__main__":
    unittest.main()
