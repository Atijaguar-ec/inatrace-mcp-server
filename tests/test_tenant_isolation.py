"""
Regression suite for BLK-05: multi-tenant isolation.

Before the fix, `company_id` was an argument the caller chose and the token's own
`company_id` claim was never compared against it: an ERP account for organization 1
could read organization 2's data by changing a number (horizontal IDOR, OWASP
API1:2023). Tools that take a stock_order_id were worse, since the tenant was
resolved from the database and never checked at all.

Two layers are covered here:
- the dispatcher, for the `company_id` argument;
- the tool layer, for owners only known after a database lookup.
"""
import unittest
from unittest.mock import patch

from src.app import mcp
from src.config import settings
from src.infrastructure.db_pool import set_db_pool
from src.infrastructure.keycloak_guard import (
    enforce_tenant_isolation, tenant_scope_for, AuthContext, AccessTier, jwks_cache
)
from src.infrastructure.auth_scope import (
    set_tenant_scope, reset_tenant_scope, assert_tenant_access, TenantScope
)
from src.tools.audit_tools import execute_verify_eudr_compliance, VerifyEudrInput
from src.tools.erp_tools import execute_verify_sri_compliance, VerifySriInput
from src.tools.export_tools import (
    execute_generate_traces_jsonld, GenerateTracesJsonLdInput
)
from tests.fixtures import build_fake_pool, TestKeyring, TEST_AUDIENCE


def erp_context(company_id=1):
    return AuthContext(
        tier=AccessTier.TIER_3_ERP,
        user_id="erp-service",
        client_id=settings.SERVICE_ACCOUNT_CLIENT_ID,
        roles=["ERP_WRITER"],
        company_id=company_id,
        is_authenticated=True
    )


class TestTenantArgumentIsolation(unittest.TestCase):
    """The company_id argument must match the token's own organization."""

    def test_matching_company_is_allowed(self):
        enforce_tenant_isolation(erp_context(1), "audit_lot_traceability", {"company_id": 1})

    def test_other_company_is_denied(self):
        with self.assertRaises(PermissionError) as cm:
            enforce_tenant_isolation(erp_context(1), "audit_lot_traceability", {"company_id": 2})
        self.assertIn("organización 1", str(cm.exception))

    def test_erp_token_without_company_claim_is_denied(self):
        ctx = erp_context(None)
        with self.assertRaises(PermissionError) as cm:
            enforce_tenant_isolation(ctx, "ingest_erp_purchase_order", {"company_id": 1})
        self.assertIn("company_id", str(cm.exception))

    def test_non_numeric_company_argument_is_denied(self):
        with self.assertRaises(PermissionError):
            enforce_tenant_isolation(
                erp_context(1), "audit_lot_traceability", {"company_id": "1; DROP TABLE"}
            )

    def test_support_tier_may_cross_tenants(self):
        ctx = AuthContext(
            tier=AccessTier.TIER_4_SUPPORT, user_id="soporte",
            company_id=1, is_authenticated=True
        )
        enforce_tenant_isolation(ctx, "diagnose_tenant_health", {"company_id": 99})

    def test_customs_passport_without_company_claim_is_not_scoped(self):
        ctx = AuthContext(
            tier=AccessTier.TIER_2_CUSTOMS, user_id="customs",
            company_id=None, is_authenticated=True
        )
        enforce_tenant_isolation(ctx, "verify_eudr_compliance", {"company_id": 7})

    def test_scope_marks_support_as_cross_tenant(self):
        support = AuthContext(
            tier=AccessTier.TIER_4_SUPPORT, user_id="soporte", is_authenticated=True
        )
        self.assertTrue(tenant_scope_for(support).cross_tenant_allowed)
        self.assertFalse(tenant_scope_for(erp_context(1)).cross_tenant_allowed)


class TestTenantScopePropagation(unittest.TestCase):
    """assert_tenant_access reads the scope installed for the current task."""

    def tearDown(self):
        set_tenant_scope(None)

    def test_no_scope_enforces_nothing(self):
        set_tenant_scope(None)
        assert_tenant_access(42, "recurso")

    def test_scope_denies_other_company(self):
        token = set_tenant_scope(TenantScope(tier="TIER_3_ERP", user_id="erp", company_id=1))
        try:
            with self.assertRaises(PermissionError):
                assert_tenant_access(2, "StockOrder 500")
            assert_tenant_access(1, "StockOrder 500")
        finally:
            reset_tenant_scope(token)

    def test_cross_tenant_scope_allows_anything(self):
        token = set_tenant_scope(TenantScope(
            tier="TIER_4_SUPPORT", user_id="soporte", company_id=1, cross_tenant_allowed=True
        ))
        try:
            assert_tenant_access(999, "StockOrder 500")
        finally:
            reset_tenant_scope(token)


class TestDatabaseResolvedTenantIsolation(unittest.IsolatedAsyncioTestCase):
    """
    Tools addressed by stock_order_id: the owner is only known after the lookup, so
    the check lives in the tool layer.
    """

    def setUp(self):
        # Fake StockOrder 500 belongs to organization 2.
        set_db_pool(build_fake_pool(stock_order={
            "qrcodetag": "aaaaaaaa-0000-0000-0000-000000000002",
            "totalquantity": 6900.0,
            "identifier": "LOT-ORG2",
            "company_id": 2,
            "comments": "LC:001-002-000001234|CA:1110202601099234567800110010020000012341234567812|ERP:ERP-2"
        }))
        # Caller is the ERP of organization 1.
        self.scope_token = set_tenant_scope(
            TenantScope(tier="TIER_3_ERP", user_id="erp-org1", company_id=1)
        )

    def tearDown(self):
        reset_tenant_scope(self.scope_token)
        set_db_pool(None)

    async def test_eudr_audit_of_another_tenants_lot_is_denied(self):
        with self.assertRaises(PermissionError):
            await execute_verify_eudr_compliance(VerifyEudrInput(stock_order_id=500))

    async def test_sri_audit_of_another_tenants_lot_is_denied(self):
        with self.assertRaises(PermissionError):
            await execute_verify_sri_compliance(VerifySriInput(stock_order_id=500))

    async def test_dds_for_another_tenants_lot_is_denied(self):
        with self.assertRaises(PermissionError):
            await execute_generate_traces_jsonld(
                GenerateTracesJsonLdInput(stock_order_id=500, operator_ruc="0992345678001")
            )


class TestTenantIsolationThroughDispatcher(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a signed ERP token for organization 1 cannot reach organization 2."""

    @classmethod
    def setUpClass(cls):
        cls.keyring = TestKeyring()

    def setUp(self):
        self.audience_patch = patch.object(settings, "KEYCLOAK_AUDIENCE", TEST_AUDIENCE)
        self.audience_patch.start()
        self.keyring.install()
        set_db_pool(build_fake_pool())

    def tearDown(self):
        self.audience_patch.stop()
        jwks_cache.clear()
        set_db_pool(None)

    def erp_bearer(self, company_id=1):
        return self.keyring.bearer(
            roles=["ERP_WRITER"],
            sub="erp-org1",
            azp=settings.SERVICE_ACCOUNT_CLIENT_ID,
            company_id=company_id
        )

    async def call(self, tool_name, arguments, auth_header):
        return await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments}
        }, auth_header=auth_header)

    async def test_erp_cannot_audit_another_organization(self):
        resp = await self.call(
            "audit_lot_traceability",
            {"lot_identifier": "LOT-TEST-001", "company_id": 2},
            self.erp_bearer(company_id=1)
        )
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)
        self.assertIn("organización 1", resp["error"]["message"])

    async def test_erp_can_audit_its_own_organization(self):
        resp = await self.call(
            "audit_lot_traceability",
            {"lot_identifier": "LOT-TEST-001", "company_id": 1},
            self.erp_bearer(company_id=1)
        )
        self.assertNotIn("error", resp)
        self.assertTrue(resp["result"]["structuredContent"]["found"])

    async def test_erp_cannot_write_into_another_organization(self):
        resp = await self.call("ingest_erp_purchase_order", {
            "company_id": 2,
            "farmer_id": 22,
            "parcel_name": "Finca San Antonio",
            "gross_weight_kg": 100.0,
            "tare_kg": 2.0,
            "sri_access_key": "1" * 49,
            "erp_internal_id": "ERP-TRANS-77"
        }, self.erp_bearer(company_id=1))
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)


if __name__ == "__main__":
    unittest.main()
