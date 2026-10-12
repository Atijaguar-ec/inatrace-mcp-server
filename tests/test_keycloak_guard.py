"""
Unit tests for Keycloak OIDC Guard and 4-Tier Access Control.
"""
import time
import json
import base64
import unittest

from src.infrastructure.keycloak_guard import (
    verify_keycloak_token,
    check_tier_permission,
    AccessTier
)


def _create_mock_jwt(payload: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip('=')
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    sig = "mock_signature_bytes"
    return f"{h_b64}.{p_b64}.{sig}"


class TestKeycloakGuard(unittest.TestCase):

    def test_anonymous_token_resolves_tier_1(self):
        ctx_none = verify_keycloak_token(None)
        self.assertEqual(ctx_none.tier, AccessTier.TIER_1_PUBLIC)
        self.assertFalse(ctx_none.is_authenticated)

        ctx_empty = verify_keycloak_token("   ")
        self.assertEqual(ctx_empty.tier, AccessTier.TIER_1_PUBLIC)

    def test_malformed_token_fallback(self):
        ctx = verify_keycloak_token("not-a-jwt")
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertFalse(ctx.is_authenticated)

    def test_expired_token(self):
        payload = {
            "sub": "user-expired",
            "exp": time.time() - 3600,
            "realm_access": {"roles": ["INATRACE_TECH_SUPPORT"]}
        }
        token = _create_mock_jwt(payload)
        ctx = verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertFalse(ctx.is_authenticated)

    def test_tier_4_tech_support_role(self):
        payload = {
            "sub": "alvaro-admin",
            "exp": time.time() + 3600,
            "realm_access": {"roles": ["INATRACE_TECH_SUPPORT", "default-roles"]}
        }
        token = _create_mock_jwt(payload)
        ctx = verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_4_SUPPORT)
        self.assertTrue(ctx.is_authenticated)
        self.assertTrue(check_tier_permission(ctx.tier, "diagnose_tenant_health"))
        self.assertTrue(check_tier_permission(ctx.tier, "audit_lot_traceability"))

    def test_tier_3_erp_service_account(self):
        payload = {
            "sub": "erp-service",
            "azp": "service-account-erp-contable",
            "exp": time.time() + 3600,
            "realm_access": {"roles": ["COMPANY_ADMIN"]}
        }
        token = _create_mock_jwt(payload)
        ctx = verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_3_ERP)
        self.assertTrue(check_tier_permission(ctx.tier, "ingest_erp_purchase_order"))
        self.assertFalse(check_tier_permission(ctx.tier, "diagnose_tenant_health"))

    def test_tier_2_customs_passport_token(self):
        payload = {
            "sub": "customs-officer-rotterdam",
            "exp": time.time() + 3600,
            "token_type": "customs_passport",
            "realm_access": {"roles": ["CUSTOMS_INSPECTOR"]}
        }
        token = _create_mock_jwt(payload)
        ctx = verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_2_CUSTOMS)
        self.assertTrue(check_tier_permission(ctx.tier, "verify_eudr_compliance"))
        self.assertTrue(check_tier_permission(ctx.tier, "generate_traces_jsonld"))
        self.assertFalse(check_tier_permission(ctx.tier, "ingest_erp_purchase_order"))

    def test_tier_1_public_permissions(self):
        self.assertTrue(check_tier_permission(AccessTier.TIER_1_PUBLIC, "audit_lot_traceability"))
        self.assertTrue(check_tier_permission(AccessTier.TIER_1_PUBLIC, "diagnose_and_repair_qr"))
        self.assertFalse(check_tier_permission(AccessTier.TIER_1_PUBLIC, "verify_eudr_compliance"))
        self.assertFalse(check_tier_permission(AccessTier.TIER_1_PUBLIC, "ingest_erp_purchase_order"))
        self.assertFalse(check_tier_permission(AccessTier.TIER_1_PUBLIC, "diagnose_tenant_health"))


if __name__ == "__main__":
    unittest.main()
