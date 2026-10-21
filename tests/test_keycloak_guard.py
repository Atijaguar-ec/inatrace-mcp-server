"""
Unit tests for the Keycloak OIDC Guard, RS256 signature verification and 4-Tier
Access Control.

Before BLK-01 was closed this file asserted the vulnerability as expected
behaviour: it built tokens with `sig = "mock_signature_bytes"` and required them to
resolve to TIER_4_SUPPORT. Tokens are now signed with a real RSA key whose public
half is seeded into the guard's JWKS cache, and every unverifiable variant must come
back as an unauthenticated Tier 1 context.
"""
import time
import unittest
from unittest.mock import patch

from src.config import settings
from src.infrastructure.keycloak_guard import (
    verify_keycloak_token,
    resolve_tier_from_claims,
    check_tier_permission,
    accepted_audiences,
    expected_issuer,
    jwks_cache,
    AccessTier
)
from tests.fixtures import TestKeyring, unsigned_token, TEST_ISSUER, TEST_AUDIENCE


class TestKeycloakGuard(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        # One keypair for the whole class: RSA generation is the slow part.
        cls.keyring = TestKeyring()
        cls.foreign = TestKeyring(kid="inatrace-test-key")  # same kid, different key

    def setUp(self):
        self.audience_patch = patch.object(settings, "KEYCLOAK_AUDIENCE", TEST_AUDIENCE)
        self.audience_patch.start()
        self.keyring.install()

    def tearDown(self):
        self.audience_patch.stop()
        jwks_cache.clear()

    # --- 1. ANONYMOUS AND MALFORMED INPUT ---

    async def test_anonymous_token_resolves_tier_1(self):
        ctx_none = await verify_keycloak_token(None)
        self.assertEqual(ctx_none.tier, AccessTier.TIER_1_PUBLIC)
        self.assertFalse(ctx_none.is_authenticated)
        self.assertEqual(ctx_none.rejection_reason, "NO_TOKEN")

        ctx_empty = await verify_keycloak_token("   ")
        self.assertEqual(ctx_empty.tier, AccessTier.TIER_1_PUBLIC)
        self.assertFalse(ctx_empty.is_authenticated)

    async def test_malformed_token_is_rejected(self):
        ctx = await verify_keycloak_token("not-a-jwt")
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertFalse(ctx.is_authenticated)
        self.assertEqual(ctx.rejection_reason, "MALFORMED_HEADER")

    # --- 2. SIGNATURE VERIFICATION (BLK-01) ---

    async def test_forged_signature_does_not_escalate(self):
        """The exact attack that granted Tier 4 before the fix."""
        forged = unsigned_token({
            "sub": "atacante",
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "exp": time.time() + 9999,
            "realm_access": {"roles": ["ADMIN"]}
        })
        ctx = await verify_keycloak_token(f"Bearer {forged}")
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertFalse(ctx.is_authenticated)
        self.assertEqual(ctx.rejection_reason, "INVALID_SIGNATURE")
        self.assertFalse(check_tier_permission(ctx.tier, "diagnose_tenant_health"))

    async def test_signature_from_another_key_is_rejected(self):
        token = self.foreign.token(roles=["ADMIN"], sub="atacante")
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertEqual(ctx.rejection_reason, "INVALID_SIGNATURE")

    async def test_alg_none_is_rejected(self):
        token = unsigned_token({
            "sub": "atacante",
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "exp": time.time() + 9999,
            "realm_access": {"roles": ["ADMIN"]}
        }, alg="none")
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertEqual(ctx.rejection_reason, "ALGORITHM_NOT_ALLOWED:none")

    async def test_hs256_confusion_is_rejected(self):
        token = unsigned_token({
            "sub": "atacante",
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "exp": time.time() + 9999,
            "realm_access": {"roles": ["ADMIN"]}
        }, alg="HS256")
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.rejection_reason, "ALGORITHM_NOT_ALLOWED:HS256")

    async def test_unknown_kid_is_rejected(self):
        token = self.keyring.token(roles=["ADMIN"], kid="clave-que-no-existe")
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertEqual(ctx.rejection_reason, "UNKNOWN_KID")

    async def test_empty_jwks_rejects_everything(self):
        jwks_cache.clear()
        with patch.object(jwks_cache, "_refresh", return_value=None):
            ctx = await verify_keycloak_token(self.keyring.token(roles=["ADMIN"]))
        self.assertEqual(ctx.rejection_reason, "UNKNOWN_KID")

    # --- 3. CLAIM VALIDATION: ISSUER, AUDIENCE, EXPIRY ---

    async def test_expired_token_is_rejected(self):
        token = self.keyring.token(roles=["INATRACE_TECH_SUPPORT"], expires_in=-3600)
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertFalse(ctx.is_authenticated)
        self.assertEqual(ctx.rejection_reason, "EXPIRED")

    async def test_foreign_issuer_is_rejected(self):
        token = self.keyring.token(roles=["ADMIN"], issuer="https://evil.example.com/realms/otro")
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertEqual(ctx.rejection_reason, "INVALID_CLAIMS")

    async def test_wrong_audience_is_rejected(self):
        token = self.keyring.token(roles=["ADMIN"], audience="otro-servicio")
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertEqual(ctx.rejection_reason, "INVALID_AUDIENCE")

    async def test_missing_audience_claim_is_rejected(self):
        token = self.keyring.token(roles=["ADMIN"], audience=None)
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.rejection_reason, "INVALID_AUDIENCE")

    async def test_audience_list_is_accepted_when_it_contains_ours(self):
        token = self.keyring.token(roles=["ADMIN"], audience=["account", TEST_AUDIENCE])
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_4_SUPPORT)
        self.assertTrue(ctx.is_authenticated)

    async def test_unconfigured_audience_rejects_every_token(self):
        """Misconfiguration must close the gate, not relax verification."""
        with patch.object(settings, "KEYCLOAK_AUDIENCE", ""):
            ctx = await verify_keycloak_token(self.keyring.token(roles=["ADMIN"]))
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertEqual(ctx.rejection_reason, "AUDIENCE_NOT_CONFIGURED")

    async def test_missing_jwt_library_rejects_every_token(self):
        with patch("src.infrastructure.keycloak_guard.JOSE_AVAILABLE", False):
            ctx = await verify_keycloak_token(self.keyring.token(roles=["ADMIN"]))
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertEqual(ctx.rejection_reason, "JWT_LIBRARY_MISSING")

    def test_issuer_and_jwks_url_derive_from_settings(self):
        self.assertEqual(
            expected_issuer(),
            f"{settings.KEYCLOAK_URL.rstrip('/')}/realms/{settings.KEYCLOAK_REALM}"
        )
        self.assertEqual(accepted_audiences(), [TEST_AUDIENCE])

    # --- 4. TIER RESOLUTION FROM VERIFIED TOKENS ---

    async def test_tier_4_tech_support_role(self):
        token = self.keyring.token(
            roles=["INATRACE_TECH_SUPPORT", "default-roles"],
            sub="alvaro-admin"
        )
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_4_SUPPORT)
        self.assertTrue(ctx.is_authenticated)
        self.assertTrue(check_tier_permission(ctx.tier, "diagnose_tenant_health"))
        self.assertTrue(check_tier_permission(ctx.tier, "audit_lot_traceability"))

    async def test_tier_3_erp_service_account(self):
        token = self.keyring.token(
            roles=["COMPANY_ADMIN"],
            sub="erp-service",
            azp=settings.SERVICE_ACCOUNT_CLIENT_ID,
            company_id=1
        )
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_3_ERP)
        self.assertEqual(ctx.company_id, 1)
        self.assertTrue(check_tier_permission(ctx.tier, "ingest_erp_purchase_order"))
        self.assertFalse(check_tier_permission(ctx.tier, "diagnose_tenant_health"))

    async def test_tier_2_customs_passport_token(self):
        token = self.keyring.token(
            roles=["CUSTOMS_INSPECTOR"],
            sub="customs-officer-rotterdam",
            token_type="customs_passport"
        )
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_2_CUSTOMS)
        self.assertTrue(check_tier_permission(ctx.tier, "verify_eudr_compliance"))
        self.assertTrue(check_tier_permission(ctx.tier, "generate_traces_jsonld"))
        self.assertFalse(check_tier_permission(ctx.tier, "ingest_erp_purchase_order"))

    async def test_authenticated_consumer_stays_tier_1(self):
        token = self.keyring.token(roles=["default-roles"], sub="socio-productor")
        ctx = await verify_keycloak_token(token)
        self.assertEqual(ctx.tier, AccessTier.TIER_1_PUBLIC)
        self.assertTrue(ctx.is_authenticated)

    def test_tier_1_public_permissions(self):
        self.assertTrue(check_tier_permission(AccessTier.TIER_1_PUBLIC, "audit_lot_traceability"))
        self.assertTrue(check_tier_permission(AccessTier.TIER_1_PUBLIC, "diagnose_and_repair_qr"))
        self.assertFalse(check_tier_permission(AccessTier.TIER_1_PUBLIC, "verify_eudr_compliance"))
        self.assertFalse(check_tier_permission(AccessTier.TIER_1_PUBLIC, "ingest_erp_purchase_order"))
        self.assertFalse(check_tier_permission(AccessTier.TIER_1_PUBLIC, "diagnose_tenant_health"))

    def test_non_numeric_company_claim_is_ignored(self):
        ctx = resolve_tier_from_claims({
            "sub": "erp",
            "azp": settings.SERVICE_ACCOUNT_CLIENT_ID,
            "company_id": "no-soy-un-numero",
            "realm_access": {"roles": []}
        })
        self.assertIsNone(ctx.company_id)


if __name__ == "__main__":
    unittest.main()
