"""
Regression suite for the three QA blockers closed on the staging branch:

- BLK-02: authorization runs on every call; a missing Authorization header
  resolves to TIER_1_PUBLIC instead of skipping the tier check.
- BLK-03: no tool fabricates data. Without the read-only pool every read path
  raises DatabaseUnavailableError, and an unknown StockOrder is never cleared.
- BLK-04: the privacy gate is a consequence of the resolved tier, not a
  parameter the caller chooses.
"""
import json
import time
import base64
import unittest

from src.app import mcp
from src.infrastructure.db_pool import set_db_pool, DatabaseUnavailableError
from src.infrastructure.keycloak_guard import (
    enforce_tier_arguments, AccessTier
)
from src.tools.audit_tools import (
    execute_audit_lot_traceability, execute_verify_eudr_compliance,
    AuditLotInput, VerifyEudrInput
)
from src.tools.export_tools import (
    execute_generate_traces_jsonld, execute_export_agrocalidad_guia,
    GenerateTracesJsonLdInput, ExportAgrocalidadInput, StockOrderNotFoundError
)
from src.tools.diagnostic_tools import (
    execute_diagnose_tenant_health, DiagnoseTenantHealthInput
)
from src.tools.erp_tools import execute_verify_sri_compliance, VerifySriInput
from tests.fixtures import build_fake_pool


def _unsigned_jwt(payload: dict) -> str:
    """Builds a token for tier-resolution tests. Signature verification is BLK-01, still open."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).decode().rstrip('=')
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f"{header}.{body}.unverified"


class TestFailClosedWithoutDatabase(unittest.IsolatedAsyncioTestCase):
    """BLK-03: every read path must fail closed when the pool is absent."""

    def setUp(self):
        set_db_pool(None)

    async def test_audit_lot_traceability_raises(self):
        with self.assertRaises(DatabaseUnavailableError):
            await execute_audit_lot_traceability(
                AuditLotInput(lot_identifier="LOT-X", company_id=1)
            )

    async def test_verify_eudr_compliance_raises(self):
        with self.assertRaises(DatabaseUnavailableError):
            await execute_verify_eudr_compliance(VerifyEudrInput(stock_order_id=999999999))

    async def test_generate_traces_jsonld_raises(self):
        with self.assertRaises(DatabaseUnavailableError):
            await execute_generate_traces_jsonld(
                GenerateTracesJsonLdInput(stock_order_id=101, operator_ruc="0992345678001")
            )

    async def test_export_agrocalidad_guia_raises(self):
        with self.assertRaises(DatabaseUnavailableError):
            await execute_export_agrocalidad_guia(
                ExportAgrocalidadInput(company_id=1, date_from="2026-10-01", date_to="2026-10-11")
            )

    async def test_diagnose_tenant_health_raises(self):
        with self.assertRaises(DatabaseUnavailableError):
            await execute_diagnose_tenant_health(DiagnoseTenantHealthInput(company_id=1))

    async def test_verify_sri_compliance_raises(self):
        with self.assertRaises(DatabaseUnavailableError):
            await execute_verify_sri_compliance(VerifySriInput(stock_order_id=101))

    async def test_jsonrpc_reports_outage_and_never_clears_a_container(self):
        resp = await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "verify_eudr_compliance",
                "arguments": {"stock_order_id": 999999999}
            }
        }, auth_header=f"Bearer {_unsigned_jwt({'sub': 'customs', 'exp': time.time() + 3600, 'realm_access': {'roles': ['CUSTOMS_INSPECTOR']}})}")

        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32002)
        self.assertIn("DATABASE_UNAVAILABLE", resp["error"]["message"])
        # The outage must not be reported as a compliance verdict.
        self.assertNotIn("RELEASE_CONTAINER_GREEN_CHANNEL", json.dumps(resp))
        self.assertNotIn("COMPLIANT", json.dumps(resp))


class TestNoFabricatedRecordsWithDatabase(unittest.IsolatedAsyncioTestCase):
    """BLK-03: with a pool, missing rows must not be filled in with demo values."""

    def tearDown(self):
        set_db_pool(None)

    async def test_unknown_stock_order_is_held_not_green_channeled(self):
        set_db_pool(build_fake_pool(stock_order=None))
        res = await execute_verify_eudr_compliance(VerifyEudrInput(stock_order_id=999999999))
        self.assertEqual(res["eudr_status"], "STOCK_ORDER_NOT_FOUND")
        self.assertEqual(res["customs_verdict"], "HOLD_CONTAINER_INSPECTION_REQUIRED")
        self.assertEqual(res["total_plots_evaluated"], 0)

    async def test_unknown_stock_order_mints_no_dds(self):
        set_db_pool(build_fake_pool(stock_order=None))
        with self.assertRaises(StockOrderNotFoundError):
            await execute_generate_traces_jsonld(
                GenerateTracesJsonLdInput(stock_order_id=999999999, operator_ruc="0992345678001")
            )

    async def test_empty_agrocalidad_range_yields_no_demo_row(self):
        set_db_pool(build_fake_pool(agrocalidad_rows=[]))
        res = await execute_export_agrocalidad_guia(
            ExportAgrocalidadInput(company_id=1, date_from="2026-10-01", date_to="2026-10-11")
        )
        self.assertEqual(res["total_records"], 0)
        self.assertNotIn("PRODUCTOR COMUNITARIO UNOCACE", res["csv_content"])
        # Headers are still emitted with BOM and semicolons.
        self.assertTrue(res["csv_content"].startswith("﻿"))
        self.assertIn("RUC_OPERADOR;", res["csv_content"])

    async def test_responses_declare_their_data_source(self):
        set_db_pool(build_fake_pool())
        res = await execute_audit_lot_traceability(
            AuditLotInput(lot_identifier="LOT-TEST-001", company_id=1)
        )
        self.assertEqual(res["data_source"], "DATABASE")


class TestAuthorizationIsUnconditional(unittest.IsolatedAsyncioTestCase):
    """BLK-02: omitting the Authorization header must not bypass the tier check."""

    def setUp(self):
        set_db_pool(build_fake_pool())

    def tearDown(self):
        set_db_pool(None)

    async def _call(self, tool_name, arguments, auth_header=None):
        return await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments}
        }, auth_header=auth_header)

    async def test_anonymous_cannot_run_tier4_diagnostics(self):
        resp = await self._call("diagnose_tenant_health", {"company_id": 1})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)
        self.assertIn("FORBIDDEN", resp["error"]["message"])

    async def test_anonymous_cannot_run_tier3_erp_write(self):
        resp = await self._call("ingest_erp_purchase_order", {
            "company_id": 5,
            "farmer_id": 22,
            "parcel_name": "Finca San Antonio",
            "gross_weight_kg": 100.0,
            "tare_kg": 2.0,
            "sri_access_key": "1" * 49,
            "erp_internal_id": "ERP-TRANS-77"
        })
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)

    async def test_anonymous_cannot_run_tier2_regulatory_export(self):
        resp = await self._call("generate_traces_jsonld", {
            "stock_order_id": 101,
            "operator_ruc": "0992345678001"
        })
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)

    async def test_tier4_token_still_reaches_diagnostics(self):
        token = _unsigned_jwt({
            "sub": "soporte-giz",
            "exp": time.time() + 3600,
            "realm_access": {"roles": ["INATRACE_TECH_SUPPORT"]}
        })
        resp = await self._call("diagnose_tenant_health", {"company_id": 1}, auth_header=f"Bearer {token}")
        self.assertNotIn("error", resp)
        self.assertEqual(resp["result"]["structuredContent"]["company_id"], 1)


class TestPrivacyGateDerivedFromTier(unittest.IsolatedAsyncioTestCase):
    """BLK-04: a Tier 1 caller cannot opt out of LOPDP minimization."""

    def setUp(self):
        set_db_pool(build_fake_pool())

    def tearDown(self):
        set_db_pool(None)

    def test_tier1_arguments_are_overridden(self):
        effective, overridden = enforce_tier_arguments(
            AccessTier.TIER_1_PUBLIC,
            "audit_lot_traceability",
            {"lot_identifier": "L1", "company_id": 1, "public_view": False, "include_raw_coordinates": True}
        )
        self.assertTrue(effective["public_view"])
        self.assertFalse(effective["include_raw_coordinates"])
        self.assertIn("public_view", overridden)
        self.assertIn("include_raw_coordinates", overridden)

    def test_higher_tiers_keep_their_arguments(self):
        for tier in (AccessTier.TIER_2_CUSTOMS, AccessTier.TIER_3_ERP, AccessTier.TIER_4_SUPPORT):
            effective, overridden = enforce_tier_arguments(
                tier,
                "audit_lot_traceability",
                {"lot_identifier": "L1", "company_id": 1, "public_view": False, "include_raw_coordinates": True}
            )
            self.assertFalse(effective["public_view"], tier)
            self.assertEqual(overridden, [], tier)

    async def test_anonymous_audit_is_sanitized_despite_public_view_false(self):
        resp = await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "audit_lot_traceability",
                "arguments": {
                    "lot_identifier": "LOT-TEST-001",
                    "company_id": 1,
                    "public_view": False,
                    "include_raw_coordinates": True
                }
            }
        })

        self.assertNotIn("error", resp)
        body = resp["result"]["structuredContent"]
        self.assertEqual(body["channel"], "PUBLIC_B2C")
        self.assertNotIn("participating_plots", body)

        # No farmer identity, plot name or exact coordinate may survive.
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("Carlos", serialized)
        self.assertNotIn("Mendoza", serialized)
        self.assertNotIn("Finca La Providencia", serialized)
        self.assertNotIn("-79.754123", serialized)

    async def test_customs_tier_keeps_the_full_dossier(self):
        token = _unsigned_jwt({
            "sub": "customs-rotterdam",
            "exp": time.time() + 3600,
            "realm_access": {"roles": ["CUSTOMS_INSPECTOR"]}
        })
        resp = await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "audit_lot_traceability",
                "arguments": {"lot_identifier": "LOT-TEST-001", "company_id": 1, "public_view": False}
            }
        }, auth_header=f"Bearer {token}")

        self.assertNotIn("error", resp)
        body = resp["result"]["structuredContent"]
        self.assertIn("participating_plots", body)
        self.assertEqual(body["data_source"], "DATABASE")


if __name__ == "__main__":
    unittest.main()
