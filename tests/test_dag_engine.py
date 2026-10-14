"""
Tests for Retrospective Traceability DAG Engine.
"""
import unittest
from src.domain.dag_engine import process_dag_results
from src.domain.sri_validator import calculate_modulo11_check_digit


class TestDagEngine(unittest.TestCase):

    def setUp(self):
        first_48 = "111020260109923456780011001002000001234123456781"
        chk = calculate_modulo11_check_digit(first_48)
        self.access_key = first_48 + str(chk)

    def test_process_dag_results_multi_level(self):
        # 3 levels of processing:
        # Level 1: Export sacks (69kg)
        # Level 2: Fermentation / Drying batch
        # Level 3: Farmer raw cacao delivery
        lineage_rows = [
            {
                "stock_order_id": 500,
                "lot_code": "EXP-2026-001",
                "qrcodetag": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
                "amount": 13800.0,
                "status": "ACTIVE",
                "processingorder_id": 200,
                "level": 1
            },
            {
                "stock_order_id": 401,
                "lot_code": "SEC-BATCH-10",
                "amount": 14000.0,
                "status": "CONSUMED",
                "processingorder_id": 190,
                "level": 2
            },
            {
                "stock_order_id": 301,
                "lot_code": "PUR-001",
                "amount": 1500.0,
                "status": "CONSUMED",
                "producerusercustomer_id": 10,
                "parcellot": "Finca Buena Vista",
                "comments": f"LC:001-002-000001234|CA:{self.access_key}|ERP:ERP-101",
                "level": 3
            }
        ]

        plots_data = [{
            "id": 100,
            "plotname": "Finca Buena Vista",
            "size": 3.8,
            "geoid": "EC-AGS-100",
            "cocoavariety": "Nacional Fino",
            "productionestimate": 45.0,
            "farmer_id": 10,
            "name": "Segundo",
            "surname": "Zambrano",
            "canton": "Vinces",
            "dpa_code": "120550"
        }]

        coords_data = [
            {"plot_id": 100, "latitude": -1.542189, "longitude": -79.754123}
        ]

        deforest_data = [{
            "plot_id": 100,
            "status": "COMPLIANT",
            "riskpcrop": "low",
            "indicatordisturbanceafter2020": "0"
        }]

        res = process_dag_results(
            lot_identifier="a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
            company_id=1,
            lineage_rows=lineage_rows,
            plots_data=plots_data,
            coordinates_data=coords_data,
            deforestation_data=deforest_data
        )

        self.assertTrue(res["found"])
        self.assertEqual(res["depth_levels"], 3)
        self.assertEqual(res["total_nodes"], 3)
        self.assertEqual(res["participating_plots_count"], 1)

        # Plot details
        plot = res["participating_plots"][0]
        self.assertEqual(plot["plot_name"], "Finca Buena Vista")
        self.assertEqual(plot["geometry"]["type"], "CENTROID")
        self.assertTrue(plot["geometry"]["is_valid"])

        # SRI settlement
        self.assertEqual(len(res["sri_settlements"]), 1)
        self.assertEqual(res["sri_settlements"][0]["access_key"], self.access_key)
        self.assertTrue(res["sri_settlements"][0]["is_valid_modulo11"])

        # EUDR Overall
        self.assertEqual(res["eudr_summary"]["overall_status"], "COMPLIANT")

    def test_process_dag_results_not_found(self):
        res = process_dag_results(
            lot_identifier="NON-EXISTENT",
            company_id=1,
            lineage_rows=[],
            plots_data=[],
            coordinates_data=[],
            deforestation_data=[]
        )
        self.assertFalse(res["found"])
        self.assertIn("No se encontró", res["message"])

    def test_process_dag_results_deforestation_violation(self):
        lineage_rows = [{
            "stock_order_id": 500,
            "lot_code": "EXP-NON-COMPLIANT",
            "amount": 500.0,
            "producerusercustomer_id": 10,
            "level": 1
        }]
        plots_data = [{
            "id": 100,
            "plotname": "Finca Deforestada",
            "size": 2.0,
            "farmer_id": 10,
            "canton": "Vinces"
        }]
        coords_data = [{"plot_id": 100, "latitude": -1.542100, "longitude": -79.754100}]
        # Perturbation post-2020 detected
        deforest_data = [{
            "plot_id": 100,
            "status": "NON_COMPLIANT",
            "riskpcrop": "high",
            "indicatordisturbanceafter2020": "high_loss"
        }]

        res = process_dag_results(
            lot_identifier="EXP-NON-COMPLIANT",
            company_id=1,
            lineage_rows=lineage_rows,
            plots_data=plots_data,
            coordinates_data=coords_data,
            deforestation_data=deforest_data
        )

        self.assertEqual(res["eudr_summary"]["overall_status"], "NON_COMPLIANT")
        self.assertFalse(res["participating_plots"][0]["deforestation_analysis"]["is_eudr_compliant"])

    def test_process_dag_results_no_plots_marked_non_compliant(self):
        lineage_rows = [{
            "stock_order_id": 500,
            "lot_code": "EXP-WITHOUT-PLOTS",
            "amount": 500.0,
            "level": 1
        }]
        res = process_dag_results(
            lot_identifier="EXP-WITHOUT-PLOTS",
            company_id=1,
            lineage_rows=lineage_rows,
            plots_data=[],
            coordinates_data=[],
            deforestation_data=[]
        )
        self.assertTrue(res["found"])
        self.assertEqual(res["participating_plots_count"], 0)
        self.assertEqual(res["eudr_summary"]["overall_status"], "NON_COMPLIANT")

    def test_exact_parcellot_filtering_excludes_unrelated_farmer_plots(self):
        # Farmer 10 owns two plots: "Finca San Antonio" (compliant) and "Finca El Paraíso" (deforested).
        # The delivery StockOrder specifies parcellot="Finca San Antonio".
        lineage_rows = [{
            "stock_order_id": 601,
            "lot_code": "LOT-FV-001",
            "amount": 400.0,
            "producerusercustomer_id": 10,
            "parcellot": "Finca San Antonio",
            "level": 1
        }]
        plots_data = [
            {
                "id": 101,
                "plotname": "Finca San Antonio",
                "size": 2.5,
                "productionestimate": 40.0,
                "farmer_id": 10,
                "name": "Pedro",
                "surname": "Alcívar"
            },
            {
                "id": 102,
                "plotname": "Finca El Paraíso",
                "size": 3.0,
                "productionestimate": 30.0,
                "farmer_id": 10,
                "name": "Pedro",
                "surname": "Alcívar"
            }
        ]
        coords_data = [
            {"plot_id": 101, "latitude": -1.542189, "longitude": -79.754123},
            {"plot_id": 102, "latitude": -1.550000, "longitude": -79.760000}
        ]
        deforest_data = [
            {"plot_id": 101, "status": "VERIFIED", "riskpcrop": "low", "indicatordisturbanceafter2020": "0"},
            {"plot_id": 102, "status": "NON_COMPLIANT", "riskpcrop": "high", "indicatordisturbanceafter2020": "severe"}
        ]

        res = process_dag_results(
            lot_identifier="LOT-FV-001",
            company_id=5,
            lineage_rows=lineage_rows,
            plots_data=plots_data,
            coordinates_data=coords_data,
            deforestation_data=deforest_data
        )
        self.assertEqual(res["participating_plots_count"], 1)
        self.assertEqual(res["participating_plots"][0]["plot_name"], "Finca San Antonio")
        self.assertEqual(res["eudr_summary"]["overall_status"], "COMPLIANT")

    def test_whisp_tristate_pending_analysis_and_tokenomics(self):
        lineage_rows = [{
            "stock_order_id": 784,
            "lot_code": "LOT-STAGE-784",
            "amount": 300.0,
            "producerusercustomer_id": 10,
            "level": 1
        }]
        plots_data = [{
            "id": 740,
            "plotname": "Finca Santa Rosa",
            "size": 2.2,
            "productionestimate": 50.0,
            "farmer_id": 10
        }]
        coords_data = [{"plot_id": 740, "latitude": -1.542189, "longitude": -79.754123}]

        # Empty deforestation_data with require_completed_whisp=True and include_raw_coordinates=False
        res = process_dag_results(
            lot_identifier="784",
            company_id=1,
            lineage_rows=lineage_rows,
            plots_data=plots_data,
            coordinates_data=coords_data,
            deforestation_data=[],
            include_raw_coordinates=False,
            require_completed_whisp=True
        )
        plot = res["participating_plots"][0]
        self.assertEqual(plot["deforestation_analysis"]["status"], "PENDING_WHISP_ANALYSIS")
        self.assertEqual(plot["deforestation_analysis"]["whisp_verification_state"], "PENDING_WHISP_ANALYSIS")
        self.assertEqual(res["eudr_summary"]["overall_status"], "PENDING_WHISP_ANALYSIS")
        self.assertEqual(res["eudr_summary"]["strict_whisp_status"], "PENDING_WHISP_ANALYSIS")
        # Tokenomics check: raw coordinates omitted, centroid and bounding_box present
        self.assertNotIn("coordinates", plot["geometry"])
        self.assertIn("centroid", plot["geometry"])
        self.assertIn("bounding_box", plot["geometry"])


if __name__ == "__main__":
    unittest.main()

