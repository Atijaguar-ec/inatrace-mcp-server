"""
Edge Cases and Boundary Conditions Test Suite for INATrace MCP Server.
Covers mathematical Modulo 11 edge cases, EUDR 4.0 ha boundary,
geodesic coordinate precision, and QR routing permutations.
"""
import unittest
from unittest.mock import patch, AsyncMock

from src.domain.sri_validator import (
    calculate_modulo11_check_digit,
    validate_sri_access_key,
    parse_canonical_sri_comment
)
from src.domain.eudr_geometry import (
    evaluate_plot_geometry,
    validate_and_close_polygon
)
from src.domain.traces_ld import format_dpa_code, build_traces_jsonld
from src.domain.privacy import sanitize_for_public_b2c
from src.tools.erp_tools import execute_ingest_erp_purchase_order, IngestErpPurchaseInput
from src.tools.diagnostic_tools import execute_diagnose_and_repair_qr, DiagnoseQrInput
from src.infrastructure.db_pool import set_db_pool
from tests.fixtures import build_fake_pool


class TestEdgeCases(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # QR diagnosis now requires the read-only pool instead of assuming the lot exists.
        set_db_pool(build_fake_pool())

    def tearDown(self):
        set_db_pool(None)

    # --- 1. SRI MODULO 11 BOUNDARY CASES ---
    def test_modulo11_check_digit_special_cases(self):
        # We test that calculate_modulo11_check_digit returns 0 when (11 - rem) == 11,
        # and returns 1 when (11 - rem) == 10.
        # Find 48-digit strings that produce these specific remainders:
        found_zero = False
        found_one = False

        for i in range(100):
            test_seq = f"{i:02d}" + "1" * 46
            chk = calculate_modulo11_check_digit(test_seq)
            if chk == 0:
                found_zero = True
            elif chk == 1:
                found_one = True
            if found_zero and found_one:
                break

        self.assertTrue(found_zero, "Did not encounter remainder producing check digit 0")
        self.assertTrue(found_one, "Did not encounter remainder producing check digit 1")

    def test_canonical_sri_comment_edge_cases(self):
        # Extra spaces or tabs
        self.assertIsNone(parse_canonical_sri_comment("  "))
        # Missing ERP section
        self.assertIsNone(parse_canonical_sri_comment("LC:001-001-000000001|CA:" + "1" * 49))
        # Extra pipes
        self.assertIsNone(parse_canonical_sri_comment("LC:001-001-000000001|CA:" + "1" * 49 + "|ERP:123|EXTRA"))

    # --- 2. EUDR 4.0 HA THRESHOLD & PRECISION BOUNDARIES ---
    def test_eudr_exact_4ha_boundary(self):
        # Exactly 4.0 ha -> centroid allowed
        coords = [(-79.754123, -1.542189)]
        res = evaluate_plot_geometry(coords, plot_size_ha=4.0)
        self.assertEqual(res["type"], "CENTROID")
        self.assertTrue(res["is_valid"])

        # 4.0001 ha -> strictly requires polygon
        res_exceeded = evaluate_plot_geometry(coords, plot_size_ha=4.0001)
        self.assertFalse(res_exceeded["is_valid"])
        self.assertIn("requiere polígono", res_exceeded["error"])

    def test_eudr_centroid_precision_boundary(self):
        # 5 decimals: invalid
        coords_5 = [(-79.75412, -1.54218)]
        res_5 = evaluate_plot_geometry(coords_5, plot_size_ha=2.0)
        self.assertFalse(res_5["is_valid"])

        # 6 decimals: valid
        coords_6 = [(-79.754123, -1.542189)]
        res_6 = evaluate_plot_geometry(coords_6, plot_size_ha=2.0)
        self.assertTrue(res_6["is_valid"])

        # 8 decimals: valid
        coords_8 = [(-79.75412345, -1.54218912)]
        res_8 = evaluate_plot_geometry(coords_8, plot_size_ha=2.0)
        self.assertTrue(res_8["is_valid"])

    def test_polygon_multipoint_closure(self):
        # 5 vertices polygon, open ring
        coords_5 = [
            (-79.7541, -1.5421),
            (-79.7520, -1.5430),
            (-79.7510, -1.5410),
            (-79.7525, -1.5390),
            (-79.7540, -1.5400)
        ]
        closed, enforced = validate_and_close_polygon(coords_5)
        self.assertTrue(enforced)
        self.assertEqual(len(closed), 6)
        self.assertEqual(closed[0], closed[-1])

    # --- 3. DPA ZERO-PADDING & TRACES JSON-LD ---
    def test_format_dpa_code_edge_cases(self):
        self.assertEqual(format_dpa_code("   40250   "), "040250")
        self.assertEqual(format_dpa_code("4"), "000004")
        self.assertEqual(format_dpa_code(0), "000000")

    def test_traces_jsonld_multi_places(self):
        places = [
            {
                "plot_id": 1,
                "geo_id": "EC-AGS-1",
                "dpa_code": "040250",
                "plot_size_ha": 2.0,
                "deforestation_risk": "NONE",
                "geometry": {"type": "Point", "coordinates": [-79.754123, -1.542189]}
            },
            {
                "plot_id": 2,
                "geo_id": "EC-AGS-2",
                "dpa_code": "120550",
                "plot_size_ha": 6.0,
                "deforestation_risk": "NONE",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-79.7541, -1.5421],
                        [-79.7520, -1.5430],
                        [-79.7510, -1.5410],
                        [-79.7541, -1.5421]
                    ]]
                }
            }
        ]

        payload = build_traces_jsonld(
            stock_order_uuid="uuid-multi-1234",
            operator_ruc="0992345678001",
            operator_name="Fortaleza del Valle",
            net_weight_kg=6900.0,
            production_places=places
        )

        self.assertEqual(len(payload["productionPlaces"]), 2)
        self.assertEqual(payload["productionPlaces"][0]["geometry"]["type"], "Point")
        self.assertEqual(payload["productionPlaces"][1]["geometry"]["type"], "Polygon")

    # --- 4. PRIVACY GATE MISSING FIELDS ---
    def test_privacy_gate_empty_or_sparse(self):
        minimal_data = {"lot_identifier": "MIN-01"}
        sanitized = sanitize_for_public_b2c(minimal_data)
        self.assertEqual(sanitized["lot_identifier"], "MIN-01")
        self.assertEqual(sanitized["total_participating_farmers"], 0)
        self.assertEqual(sanitized["regions"], ["Ecuador - Región Litoral"])

    # --- 5. ERP WEIGH-IN BOUNDARIES ---
    async def test_erp_zero_net_weight(self):
        first_48 = "111020260109923456780011001002000001234123456781"
        chk = calculate_modulo11_check_digit(first_48)
        valid_sri_key = first_48 + str(chk)

        params = IngestErpPurchaseInput(
            company_id=5,
            farmer_id=1,
            parcel_name="Finca A",
            gross_weight_kg=50.0,
            tare_kg=50.0,  # Net is 0
            sri_access_key=valid_sri_key,
            erp_internal_id="TRANS-00"
        )
        res = await execute_ingest_erp_purchase_order(params)
        self.assertEqual(res["status"], "VALIDATION_ERROR")
        self.assertEqual(res["error_code"], "INVALID_NET_WEIGHT")


    # --- 6. QR SCANNER URL PERMUTATIONS ---
    async def test_qr_diagnose_varieties(self):
        # Raw UUID
        res_uuid = await execute_diagnose_and_repair_qr(
            DiagnoseQrInput(qr_payload="e0a1b2c3-d4e5-6f7a-8b9c-0d1e2f3a4b5c")
        )
        self.assertTrue(res_uuid["is_uuid_v4"])
        self.assertFalse(res_uuid["is_url"])

        # Numeric sequential ID
        res_num = await execute_diagnose_and_repair_qr(
            DiagnoseQrInput(qr_payload="99214")
        )
        self.assertTrue(res_num["is_numeric_id"])
        self.assertFalse(res_num["is_uuid_v4"])

        # Public delivery receipt URL
        res_pub = await execute_diagnose_and_repair_qr(
            DiagnoseQrInput(qr_payload="https://inatrace.unocace.com/public/delivery-receipt/99214")
        )
        self.assertTrue(res_pub["is_public_route"])
        self.assertEqual(res_pub["extracted_identifier"], "99214")


if __name__ == "__main__":
    unittest.main()
