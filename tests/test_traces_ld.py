"""
Tests for canonical JSON-LD TRACES-NT serializer (DG SANTE / EUDR).
"""
import unittest
from datetime import datetime, timezone
from src.domain.traces_ld import format_dpa_code, build_traces_jsonld


class TestTracesLd(unittest.TestCase):

    def test_format_dpa_code_padding(self):
        # Must zero-pad to exactly 6 digits
        self.assertEqual(format_dpa_code("40250"), "040250")
        self.assertEqual(format_dpa_code("120550"), "120550")
        self.assertEqual(format_dpa_code(40250), "040250")
        self.assertEqual(format_dpa_code(None), "000000")
        self.assertEqual(format_dpa_code(""), "000000")

    def test_build_traces_jsonld_structure(self):
        places = [{
            "plot_id": 42,
            "geo_id": "EC-AGS-9912",
            "dpa_code": "040250",
            "plot_size_ha": 3.8,
            "deforestation_risk": "NONE",
            "geometry": {
                "type": "Point",
                "coordinates": [-79.754123, -1.542189]
            }
        }]

        ts = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
        payload = build_traces_jsonld(
            stock_order_uuid="88888888-4444-4444-a1b2-cccccccccccc",
            operator_ruc="0992345678001",
            operator_name="UNOCACE",
            net_weight_kg=13800.0,
            production_places=places,
            created_at=ts
        )

        # Context & Type
        self.assertIn("@context", payload)
        self.assertEqual(payload["@type"], "eudr:DueDiligenceStatement")
        self.assertEqual(payload["statementIdentifier"], "urn:uuid:88888888-4444-4444-a1b2-cccccccccccc")

        # Operator
        self.assertEqual(payload["operator"]["identifier"], "0992345678001")
        self.assertEqual(payload["operator"]["name"], "UNOCACE")

        # Commodity
        self.assertEqual(payload["commodity"]["hsCode"], "18010000")
        self.assertEqual(payload["commodity"]["netMassKg"], 13800.0)

        # Places & GeoJSON
        self.assertEqual(len(payload["productionPlaces"]), 1)
        p = payload["productionPlaces"][0]
        self.assertEqual(p["dpaCode"], "040250")
        self.assertEqual(p["countryOfProduction"], "EC")
        self.assertEqual(p["geometry"]["type"], "Point")
        self.assertEqual(p["geometry"]["coordinates"], [-79.754123, -1.542189])

        # Declaration
        self.assertTrue(payload["complianceDeclaration"]["isDeforestationFree"])
        self.assertEqual(payload["complianceDeclaration"]["verifiedAgainstCutoffDate"], "2020-12-31")


if __name__ == "__main__":
    unittest.main()
