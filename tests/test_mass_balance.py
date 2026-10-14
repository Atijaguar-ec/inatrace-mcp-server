"""
Unit Tests for Biological Mass Balance & Anti-Laundering Engine (mass_balance.py).
Validates ISO 22005:2007 and MAG Acuerdo Ministerial 023 physical limits.
"""
import unittest
from src.domain.mass_balance import evaluate_lot_mass_balance


class TestMassBalance(unittest.TestCase):

    def test_mass_balance_ok_within_plot_capacity(self):
        # 100 qq estimate = 10,000 lbs = 4,535.92 kg max annual capacity
        lineage_rows = [
            {"stock_order_id": 1, "amount": 2000.0, "level": 1},
            {"stock_order_id": 2, "amount": 2100.0, "producerusercustomer_id": 10, "level": 2}
        ]
        plots = [
            {"plot_id": 10, "size_ha": 5.0, "production_estimate_qq": 100.0}
        ]
        res = evaluate_lot_mass_balance(lineage_rows, plots)
        self.assertEqual(res["status"], "MASS_BALANCE_OK")
        self.assertTrue(res["is_compliant"])
        self.assertEqual(res["anomalies_count"], 0)

    def test_mass_balance_exceeds_annual_quota_anti_laundering(self):
        # 20 qq estimate = 2,000 lbs = 907.18 kg max capacity, but lot exports 5,000 kg
        lineage_rows = [
            {"stock_order_id": 1, "amount": 5000.0, "level": 1}
        ]
        plots = [
            {"plot_id": 10, "size_ha": 1.5, "production_estimate_qq": 20.0}
        ]
        res = evaluate_lot_mass_balance(lineage_rows, plots)
        self.assertEqual(res["status"], "MASS_BALANCE_EXCEEDED")
        self.assertFalse(res["is_compliant"])
        self.assertGreaterEqual(res["anomalies_count"], 1)
        codes = [a["code"] for a in res["anomalies"]]
        self.assertIn("ISO22005_QUOTA_CAPACITY_EXCEEDED", codes)

    def test_mass_balance_transformation_inflation_detected(self):
        # Input deliveries = 1,000 kg, but output export lot = 3,500 kg (mass created out of nowhere)
        lineage_rows = [
            {"stock_order_id": 1, "amount": 3500.0, "level": 1},
            {"stock_order_id": 2, "amount": 1000.0, "producerusercustomer_id": 10, "level": 2}
        ]
        plots = [
            {"plot_id": 10, "size_ha": 10.0, "production_estimate_qq": 200.0}
        ]
        res = evaluate_lot_mass_balance(lineage_rows, plots)
        self.assertEqual(res["status"], "MASS_BALANCE_EXCEEDED")
        self.assertFalse(res["is_compliant"])
        codes = [a["code"] for a in res["anomalies"]]
        self.assertIn("MAG023_TRANSFORMATION_INFLATION", codes)


if __name__ == "__main__":
    unittest.main()
