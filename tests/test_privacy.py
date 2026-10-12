"""
Tests for Data Minimization Gate (LOPDP Ecuador Art. 10 / EU GDPR Art. 5).
"""
import unittest
from src.domain.privacy import sanitize_for_public_b2c, apply_privacy_gate


class TestPrivacyGate(unittest.TestCase):

    def setUp(self):
        self.raw_lot_data = {
            "lot_identifier": "LOT-2026-EXP-001",
            "product_name": "Cacao Fino de Aroma",
            "company_name": "UNOCACE",
            "variety": "Nacional / EET-103",
            "organic": True,
            "participating_plots": [
                {
                    "plot_id": 1,
                    "farmer_id": 101,
                    "farmer_name": "Juan Perez Sanchez",
                    "cedula": "0912345678",
                    "phone": "0998765432",
                    "bank_account": "2100456789",
                    "canton": "Vinces",
                    "purchase_price": 120.50,
                    "coordinates": [(-79.754123, -1.542189)]
                },
                {
                    "plot_id": 2,
                    "farmer_id": 102,
                    "farmer_name": "Maria Lopez Alava",
                    "cedula": "1209876543",
                    "phone": "0987654321",
                    "bank_account": "1009876543",
                    "canton": "Palenque",
                    "purchase_price": 115.00,
                    "coordinates": [(-79.800123, -1.450189)]
                }
            ]
        }

    def test_sanitize_for_public_b2c_pii_removal(self):
        sanitized = sanitize_for_public_b2c(self.raw_lot_data)

        # Basic public lot fields present
        self.assertEqual(sanitized["lot_identifier"], "LOT-2026-EXP-001")
        self.assertEqual(sanitized["product"], "Cacao Fino de Aroma")
        self.assertEqual(sanitized["cooperative"], "UNOCACE")
        self.assertEqual(sanitized["total_participating_farmers"], 2)
        self.assertEqual(sanitized["total_participating_plots"], 2)
        self.assertIn("Vinces", sanitized["regions"])
        self.assertIn("Palenque", sanitized["regions"])

        # PII fields strictly stripped from output
        sanitized_str = str(sanitized)
        self.assertNotIn("Juan Perez", sanitized_str)
        self.assertNotIn("Maria Lopez", sanitized_str)
        self.assertNotIn("0912345678", sanitized_str)
        self.assertNotIn("1209876543", sanitized_str)
        self.assertNotIn("2100456789", sanitized_str)
        self.assertNotIn("120.50", sanitized_str)

    def test_apply_privacy_gate_routing(self):
        # Public channel strips PII
        public_res = apply_privacy_gate(self.raw_lot_data, is_public_channel=True)
        self.assertEqual(public_res["channel"], "PUBLIC_B2C")

        # Regulatory channel preserves technical dossier
        reg_res = apply_privacy_gate(self.raw_lot_data, is_public_channel=False)
        self.assertIn("participating_plots", reg_res)
        self.assertEqual(len(reg_res["participating_plots"]), 2)

    def test_sanitize_for_public_b2c_non_compliant_eudr(self):
        non_compliant_data = dict(self.raw_lot_data)
        non_compliant_data["eudr_summary"] = {"overall_status": "NON_COMPLIANT"}
        sanitized = sanitize_for_public_b2c(non_compliant_data)

        self.assertEqual(sanitized["eudr_status"], "NON_COMPLIANT_RISK_DETECTED")
        self.assertEqual(sanitized["deforestation_risk"], "RISK_DETECTED")


if __name__ == "__main__":

    unittest.main()
