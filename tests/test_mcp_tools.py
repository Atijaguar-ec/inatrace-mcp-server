"""
Integration Tests for INATrace FastMCP Server Tools.
Tests all 9 canonical MCP tools asynchronously.
"""
import unittest
from unittest.mock import patch, AsyncMock
from src.app import (
    audit_lot_traceability,
    verify_eudr_compliance,
    generate_traces_jsonld,
    export_agrocalidad_guia,
    verify_sri_compliance,
    ingest_erp_purchase_order,
    sync_farmer_profile,
    diagnose_tenant_health,
    diagnose_and_repair_qr
)
from src.domain.sri_validator import calculate_modulo11_check_digit


class TestMcpTools(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        first_48 = "111020260109923456780011001002000001234123456781"
        chk = calculate_modulo11_check_digit(first_48)
        self.valid_sri_key = first_48 + str(chk)

        self.patcher_put = patch('src.tools.erp_tools.spring_client.put_stock_order', new_callable=AsyncMock)
        self.mock_put = self.patcher_put.start()
        self.mock_put.return_value = {"status": "SUCCESS", "id": 999}

        self.patcher_post = patch('src.tools.erp_tools.spring_client.post_user_customer', new_callable=AsyncMock)
        self.mock_post = self.patcher_post.start()
        self.mock_post.return_value = {"status": "SUCCESS", "id": 888}

    def tearDown(self):
        self.patcher_put.stop()
        self.patcher_post.stop()

    async def test_audit_lot_traceability_dossier(self):
        res = await audit_lot_traceability(
            lot_identifier="LOT-TEST-001",
            company_id=1,
            include_plots=True,
            public_view=False
        )
        self.assertTrue(res.get("found"))
        self.assertEqual(res.get("lot_identifier"), "LOT-TEST-001")
        self.assertIn("participating_plots", res)
        self.assertIn("eudr_summary", res)

    async def test_audit_lot_traceability_privacy_sanitized(self):
        res = await audit_lot_traceability(
            lot_identifier="LOT-TEST-001",
            company_id=1,
            include_plots=True,
            public_view=True
        )
        self.assertEqual(res.get("channel"), "PUBLIC_B2C")
        self.assertEqual(res.get("eudr_status"), "COMPLIANT_ZERO_DEFORESTATION")
        self.assertNotIn("participating_plots", res)
        self.assertIn("total_participating_farmers", res)

    async def test_verify_eudr_compliance(self):
        res = await verify_eudr_compliance(stock_order_id=101)
        self.assertEqual(res.get("eudr_status"), "COMPLIANT")
        self.assertEqual(res.get("deforestation_cutoff_verified"), "2020-12-31")
        self.assertEqual(res.get("customs_verdict"), "RELEASE_CONTAINER_GREEN_CHANNEL")

    async def test_generate_traces_jsonld(self):
        res = await generate_traces_jsonld(
            stock_order_id=101,
            operator_ruc="0992345678001",
            issuing_authority="AGROCALIDAD-EC",
            operator_name="UNOCACE"
        )
        self.assertEqual(res.get("@type"), "eudr:DueDiligenceStatement")
        self.assertEqual(res.get("commodity", {}).get("hsCode"), "18010000")
        self.assertGreater(len(res.get("productionPlaces", [])), 0)

    async def test_export_agrocalidad_guia(self):
        res = await export_agrocalidad_guia(
            company_id=1,
            date_from="2026-10-01",
            date_to="2026-10-11"
        )
        self.assertEqual(res.get("status"), "GENERATED")
        self.assertEqual(res.get("delimiter"), ";")
        csv_str = res.get("csv_content", "")
        # Must start with UTF-8 BOM (\uFEFF)
        self.assertTrue(csv_str.startswith("\uFEFF"))
        self.assertIn("RUC_OPERADOR;CODIGO_SITIO_GUIA;", csv_str)

    async def test_ingest_erp_purchase_order_valid(self):
        res = await ingest_erp_purchase_order(
            company_id=5,
            farmer_id=22,
            parcel_name="Finca San Antonio",
            gross_weight_kg=100.0,
            tare_kg=2.0,
            sri_access_key=self.valid_sri_key,
            erp_internal_id="ERP-TRANS-77"
        )
        self.assertEqual(res.get("status"), "ACCEPTED")
        self.assertTrue(res.get("sri_modulo11_valid"))
        # 98 kg * 2.20462 = 216.05 lbs
        self.assertEqual(res.get("net_weight_lbs"), 216.05)
        self.assertIn("LC:001-002-000001234|CA:", res.get("sri_comment"))

    async def test_ingest_erp_purchase_order_invalid_sri_key(self):
        invalid_key = self.valid_sri_key[:48] + str((int(self.valid_sri_key[48]) + 1) % 10)
        res = await ingest_erp_purchase_order(
            company_id=5,
            farmer_id=22,
            parcel_name="Finca San Antonio",
            gross_weight_kg=100.0,
            tare_kg=2.0,
            sri_access_key=invalid_key,
            erp_internal_id="ERP-TRANS-77"
        )
        self.assertEqual(res.get("status"), "VALIDATION_ERROR")
        self.assertEqual(res.get("error_code"), "SRI_MODULO_11_INVALID")
        self.assertFalse(res.get("is_valid"))

    async def test_ingest_erp_purchase_order_core_rejected(self):
        self.mock_put.return_value = {"status": "ERROR", "code": 400, "message": "QUOTA_EXCEEDED"}
        res = await ingest_erp_purchase_order(
            company_id=5,
            farmer_id=22,
            parcel_name="Finca San Antonio",
            gross_weight_kg=100.0,
            tare_kg=2.0,
            sri_access_key=self.valid_sri_key,
            erp_internal_id="ERP-TRANS-77"
        )
        self.assertEqual(res.get("status"), "REJECTED_BY_CORE")
        self.assertEqual(res.get("action"), "DELEGATION_FAILED")
        self.assertEqual(res.get("core_response", {}).get("code"), 400)

    async def test_sync_farmer_profile(self):
        res = await sync_farmer_profile(
            company_id=5,
            farmer_cedula="0923456789",
            first_name="Guillermo",
            last_name="Mora",
            canton_dpa_code="120550"
        )
        self.assertEqual(res.get("status"), "ACCEPTED")
        self.assertEqual(res.get("farmer_name"), "Guillermo Mora")
        # Ensure farmerCompanyInternalId was properly transmitted to Spring Boot
        sent_payload = self.mock_post.call_args[0][0]
        self.assertEqual(sent_payload["farmerCompanyInternalId"], "0923456789")

    async def test_sync_farmer_profile_core_rejected(self):
        self.mock_post.return_value = {"status": "ERROR", "code": 409, "message": "FARMER_ALREADY_EXISTS"}
        res = await sync_farmer_profile(
            company_id=5,
            farmer_cedula="0923456789",
            first_name="Guillermo",
            last_name="Mora",
            canton_dpa_code="120550"
        )
        self.assertEqual(res.get("status"), "REJECTED_BY_CORE")
        self.assertEqual(res.get("action"), "DELEGATION_FAILED")
        self.assertEqual(res.get("core_response", {}).get("code"), 409)


    async def test_verify_sri_compliance(self):
        res = await verify_sri_compliance(stock_order_id=101)
        self.assertEqual(res.get("stock_order_id"), 101)
        self.assertIn("sri_status", res)

    async def test_diagnose_tenant_health(self):
        res = await diagnose_tenant_health(company_id=1)
        self.assertEqual(res.get("company_id"), 1)
        self.assertIn("post_mortems_verified", res)
        self.assertIn("PM-033", res.get("post_mortems_verified"))
        self.assertIn("PM-036", res.get("post_mortems_verified"))
        self.assertIn("PM-037", res.get("post_mortems_verified"))
        self.assertIn("PM-040", res.get("post_mortems_verified"))

    async def test_diagnose_and_repair_qr_public_route(self):
        res = await diagnose_and_repair_qr(
            qr_payload="https://inatrace.unocace.com/public/delivery-receipt/450"
        )
        self.assertTrue(res.get("is_public_route"))
        self.assertEqual(res.get("extracted_identifier"), "450")
        self.assertEqual(res.get("verdict"), "OK")

    async def test_diagnose_and_repair_qr_protected_route(self):
        res = await diagnose_and_repair_qr(
            qr_payload="https://inatrace.unocace.com/company/my-stock/450/view"
        )
        self.assertTrue(res.get("is_protected_route"))
        self.assertEqual(res.get("verdict"), "403_FORBIDDEN")
        self.assertIn("enablePublicDeliveryReceipt", res.get("recommendation"))

    async def test_mcp_jsonrpc_resources_and_prompts(self):
        from src.app import mcp

        # 1. initialize
        init_res = await mcp.handle_jsonrpc_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertIn("capabilities", init_res["result"])
        self.assertIn("resources", init_res["result"]["capabilities"])
        self.assertIn("prompts", init_res["result"]["capabilities"])

        # 2. resources/list & resources/read
        r_list = await mcp.handle_jsonrpc_request({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        uris = [r["uri"] for r in r_list["result"]["resources"]]
        self.assertIn("inatrace://catalogs/dpa-ecuador", uris)
        self.assertIn("inatrace://regulations/eudr-2023-1115", uris)
        self.assertIn("inatrace://schemas/sri-modulo11", uris)

        r_read = await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "inatrace://catalogs/dpa-ecuador"}
        })
        self.assertIn("120550", r_read["result"]["contents"][0]["text"])

        # 3. prompts/list & prompts/get
        p_list = await mcp.handle_jsonrpc_request({"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
        p_names = [p["name"] for p in p_list["result"]["prompts"]]
        self.assertIn("auditoria_aduanera_eudr", p_names)
        self.assertIn("diagnostico_forense_soporte_giz", p_names)

        p_get = await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "prompts/get",
            "params": {"name": "auditoria_aduanera_eudr", "arguments": {"lot_identifier": "784", "company_id": 1}}
        })
        self.assertIn("messages", p_get["result"])


if __name__ == "__main__":
    unittest.main()
