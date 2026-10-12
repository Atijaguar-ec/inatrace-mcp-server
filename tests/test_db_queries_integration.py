"""
Integration Tests for Database Query Pathways in MCP Tools using Mock Pool.
Exercises the 'else' branch (pool is present) to verify that queries and data mapping
execute without schema or syntax exceptions.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.infrastructure.db_pool import set_db_pool
from src.tools.audit_tools import execute_audit_lot_traceability, execute_verify_eudr_compliance, AuditLotInput, VerifyEudrInput
from src.tools.export_tools import execute_generate_traces_jsonld, GenerateTracesJsonLdInput
from src.tools.diagnostic_tools import execute_diagnose_tenant_health, execute_diagnose_and_repair_qr, DiagnoseTenantHealthInput, DiagnoseQrInput
from src.domain.dag_engine import build_lot_dag


class TestDbQueriesIntegration(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_conn = MagicMock()
        self.mock_conn.fetch = AsyncMock()
        self.mock_conn.fetchrow = AsyncMock()
        self.mock_conn.fetchval = AsyncMock()

        self.mock_pool = MagicMock()
        # Context manager for pool.acquire()
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=self.mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        self.mock_pool.acquire.return_value = acquire_ctx

        set_db_pool(self.mock_pool)

    def tearDown(self):
        set_db_pool(None)

    async def test_build_lot_dag_db_path(self):
        self.mock_conn.fetch.side_effect = [
            # 1. DAG_RECURSIVE_QUERY
            [
                {
                    "stock_order_id": 100,
                    "lot_code": "EXP-01",
                    "qrcodetag": "uuid-1234",
                    "amount": 13800.0,
                    "status": "ACTIVE",
                    "facility_id": 1,
                    "company_id": 1,
                    "producerusercustomer_id": 20,
                    "parcellot": "Finca San Jose",
                    "comments": "LC:001-002-000001234|CA:1110202601099234567800110010020000012341234567812|ERP:ERP-1",
                    "processingorder_id": 50,
                    "level": 1
                }
            ],
            # 2. plots_query
            [
                {
                    "id": 10,
                    "plotname": "Finca San Jose",
                    "size": 3.0,
                    "geoid": "EC-AGS-10",
                    "cocoavariety": "Nacional",
                    "productionestimate": 35.0,
                    "farmer_id": 20,
                    "name": "Pedro",
                    "surname": "Alvarado"
                }
            ],
            # 3. coords_rows
            [
                {"plot_id": 10, "latitude": -1.542189, "longitude": -79.754123}
            ],
            # 4. deforest_rows
            [
                {
                    "plot_id": 10,
                    "status": "COMPLIANT",
                    "riskpcrop": "low",
                    "indicatordisturbanceafter2020": "0"
                }
            ]
        ]

        res = await build_lot_dag(self.mock_conn, "uuid-1234", 1)
        self.assertTrue(res["found"])
        self.assertEqual(res["lot_identifier"], "uuid-1234")
        self.assertEqual(len(res["participating_plots"]), 1)
        self.assertEqual(res["eudr_summary"]["overall_status"], "COMPLIANT")

    async def test_execute_verify_eudr_compliance_db_path(self):
        # mock stockorder fetchrow
        self.mock_conn.fetchrow.return_value = {"company_id": 1}

        # build_lot_dag queries inside
        self.mock_conn.fetch.side_effect = [
            # DAG_RECURSIVE_QUERY
            [
                {
                    "stock_order_id": 100,
                    "lot_code": "EXP-01",
                    "amount": 13800.0,
                    "producerusercustomer_id": 20,
                    "level": 1
                }
            ],
            # plots_query
            [
                {
                    "id": 10,
                    "plotname": "Finca San Jose",
                    "size": 3.0,
                    "geoid": "EC-AGS-10",
                    "farmer_id": 20,
                    "name": "Pedro",
                    "surname": "Alvarado"
                }
            ],
            # coords_rows
            [
                {"plot_id": 10, "latitude": -1.542189, "longitude": -79.754123}
            ],
            # deforest_rows
            [
                {
                    "plot_id": 10,
                    "status": "COMPLIANT",
                    "riskpcrop": "low",
                    "indicatordisturbanceafter2020": "0"
                }
            ]
        ]

        res = await execute_verify_eudr_compliance(VerifyEudrInput(stock_order_id=100))
        self.assertEqual(res["stock_order_id"], 100)
        self.assertEqual(res["eudr_status"], "COMPLIANT")
        self.assertEqual(res["customs_verdict"], "RELEASE_CONTAINER_GREEN_CHANNEL")

    async def test_execute_generate_traces_jsonld_db_path(self):
        self.mock_conn.fetchrow.return_value = {
            "qrcodetag": "a1b2c3d4-0000-0000-0000-000000000001",
            "totalquantity": 6900.0,
            "identifier": "EXP-LOT-01",
            "company_id": 1
        }

        # DAG resolution queries
        self.mock_conn.fetch.side_effect = [
            [
                {
                    "stock_order_id": 200,
                    "lot_code": "EXP-LOT-01",
                    "amount": 6900.0,
                    "producerusercustomer_id": 5,
                    "level": 1
                }
            ],
            [
                {
                    "id": 50,
                    "plotname": "Finca Test",
                    "size": 2.5,
                    "geoid": "GEO-50",
                    "farmer_id": 5,
                    "name": "Luis",
                    "surname": "Perez"
                }
            ],
            [
                {"plot_id": 50, "latitude": -1.542189, "longitude": -79.754123}
            ],
            [
                {
                    "plot_id": 50,
                    "status": "COMPLIANT",
                    "riskpcrop": "low",
                    "indicatordisturbanceafter2020": "0"
                }
            ]
        ]

        res = await execute_generate_traces_jsonld(
            GenerateTracesJsonLdInput(
                stock_order_id=200,
                operator_ruc="0992345678001"
            )
        )
        self.assertEqual(res["@type"], "eudr:DueDiligenceStatement")
        self.assertEqual(len(res["productionPlaces"]), 1)
        self.assertEqual(res["commodity"]["netMassKg"], 6900.0)

    async def test_execute_diagnose_tenant_health_db_path(self):
        # 1. PM-033 count
        self.mock_conn.fetchval.side_effect = [
            1,   # owners_count = 1 (healthy)
            0    # unpadded_dpa count = 0 (healthy)
        ]
        # 2. PM-036 qr_actions
        self.mock_conn.fetch.return_value = [
            {"id": 1, "inputsemiproduct_id": 3}  # 3 is Cacao Seco (healthy)
        ]
        # 4. company configuration
        self.mock_conn.fetchrow.return_value = {
            "configuration": '{"enableDeliveryReceipt": true, "enablePublicDeliveryReceipt": true}'
        }

        res = await execute_diagnose_tenant_health(DiagnoseTenantHealthInput(company_id=1))
        self.assertTrue(res["healthy"])
        self.assertEqual(res["issues_count"], 0)


if __name__ == "__main__":
    unittest.main()
