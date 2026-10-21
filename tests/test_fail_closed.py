"""
Regression suite for the QA blockers closed on the staging branch:

- BLK-02: authorization runs on every call; a missing Authorization header
  resolves to TIER_1_PUBLIC instead of skipping the tier check.
- BLK-03: no tool fabricates data. Without the read-only pool every read path
  raises DatabaseUnavailableError, and an unknown StockOrder is never cleared.
- BLK-04: the privacy gate is a consequence of the resolved tier, not a
  parameter the caller chooses.

Tenant isolation (BLK-05) has its own module, test_tenant_isolation.py.
"""
import json
import unittest
from unittest.mock import patch

from src.app import mcp
from src.config import settings
from src.infrastructure.db_pool import set_db_pool, DatabaseUnavailableError
from src.infrastructure.keycloak_guard import (
    enforce_tier_arguments, jwks_cache, AccessTier
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
from tests.fixtures import build_fake_pool, TestKeyring, TEST_AUDIENCE


class AuthenticatedTestCase(unittest.IsolatedAsyncioTestCase):
    """Seeds the JWKS cache with a test keypair so signed tokens verify."""

    @classmethod
    def setUpClass(cls):
        cls.keyring = TestKeyring()

    def setUp(self):
        self.audience_patch = patch.object(settings, "KEYCLOAK_AUDIENCE", TEST_AUDIENCE)
        self.audience_patch.start()
        self.keyring.install()

    def tearDown(self):
        self.audience_patch.stop()
        jwks_cache.clear()
        set_db_pool(None)

    def customs_bearer(self, **kwargs):
        return self.keyring.bearer(roles=["CUSTOMS_INSPECTOR"], sub="customs-rotterdam", **kwargs)

    def support_bearer(self, **kwargs):
        return self.keyring.bearer(roles=["INATRACE_TECH_SUPPORT"], sub="soporte-giz", **kwargs)

    async def call(self, tool_name, arguments, auth_header=None, req_id=1):
        return await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments}
        }, auth_header=auth_header)


class TestFailClosedWithoutDatabase(AuthenticatedTestCase):
    """BLK-03: every read path must fail closed when the pool is absent."""

    def setUp(self):
        super().setUp()
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
        resp = await self.call(
            "verify_eudr_compliance",
            {"stock_order_id": 999999999},
            auth_header=self.customs_bearer()
        )
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32002)
        self.assertIn("DATABASE_UNAVAILABLE", resp["error"]["message"])
        # The outage must not be reported as a compliance verdict.
        self.assertNotIn("RELEASE_CONTAINER_GREEN_CHANNEL", json.dumps(resp))
        self.assertNotIn("COMPLIANT", json.dumps(resp))


class TestNoFabricatedRecordsWithDatabase(AuthenticatedTestCase):
    """BLK-03: with a pool, missing rows must not be filled in with demo values."""

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

    async def test_unknown_stock_order_reports_no_sri_verdict(self):
        set_db_pool(build_fake_pool(stock_order=None))
        res = await execute_verify_sri_compliance(VerifySriInput(stock_order_id=999999999))
        self.assertEqual(res["sri_status"], "STOCK_ORDER_NOT_FOUND")
        self.assertFalse(res["is_valid"])

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


class TestAuthorizationIsUnconditional(AuthenticatedTestCase):
    """BLK-02: omitting the Authorization header must not bypass the tier check."""

    def setUp(self):
        super().setUp()
        set_db_pool(build_fake_pool())

    async def test_anonymous_cannot_run_tier4_diagnostics(self):
        resp = await self.call("diagnose_tenant_health", {"company_id": 1})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)
        self.assertIn("FORBIDDEN", resp["error"]["message"])

    async def test_anonymous_cannot_run_tier3_erp_write(self):
        resp = await self.call("ingest_erp_purchase_order", {
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
        resp = await self.call("generate_traces_jsonld", {
            "stock_order_id": 101,
            "operator_ruc": "0992345678001"
        })
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)

    async def test_forged_token_cannot_run_tier4_diagnostics(self):
        """BLK-01 + BLK-02 together: a forged token is Tier 1, so it is forbidden."""
        from tests.fixtures import unsigned_token
        import time as _time
        forged = unsigned_token({
            "sub": "atacante",
            "iss": "https://auth.unocace.com/realms/unocace",
            "aud": TEST_AUDIENCE,
            "exp": _time.time() + 9999,
            "realm_access": {"roles": ["ADMIN"]}
        })
        resp = await self.call(
            "diagnose_tenant_health", {"company_id": 1}, auth_header=f"Bearer {forged}"
        )
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)
        self.assertNotIn("HEALTHY", json.dumps(resp))

    async def test_tier4_token_still_reaches_diagnostics(self):
        resp = await self.call(
            "diagnose_tenant_health", {"company_id": 1}, auth_header=self.support_bearer()
        )
        self.assertNotIn("error", resp)
        self.assertEqual(resp["result"]["structuredContent"]["company_id"], 1)


class TestPrivacyGateDerivedFromTier(AuthenticatedTestCase):
    """BLK-04: a Tier 1 caller cannot opt out of LOPDP minimization."""

    def setUp(self):
        super().setUp()
        set_db_pool(build_fake_pool())

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
        resp = await self.call("audit_lot_traceability", {
            "lot_identifier": "LOT-TEST-001",
            "company_id": 1,
            "public_view": False,
            "include_raw_coordinates": True
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
        resp = await self.call(
            "audit_lot_traceability",
            {"lot_identifier": "LOT-TEST-001", "company_id": 1, "public_view": False},
            auth_header=self.customs_bearer()
        )
        self.assertNotIn("error", resp)
        body = resp["result"]["structuredContent"]
        self.assertIn("participating_plots", body)
        self.assertEqual(body["data_source"], "DATABASE")


if __name__ == "__main__":
    unittest.main()
