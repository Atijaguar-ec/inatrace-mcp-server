"""
Pruebas del Tier 2B (TIER_2B_COOP_STAFF) — técnicos de campo de la cooperativa.

El tier se añadió en `de2221d` para que el personal de UNOCACE vea el grafo DAG
completo: nombres de socios, áreas y coordenadas sin censura. Se incorporó sin una
sola aserción, y el informe declaraba que restringía «datos de liquidación contable
sensible» sin que existiera tal filtro.

Estas pruebas fijan las dos mitades del contrato:
  1. Lo que Tier 2B SÍ debe ver: el expediente operativo íntegro, con PII.
  2. Lo que NO debe ver: las liquidaciones fiscales del SRI (`sri_settlements`),
     que llevan clave de acceso, código de liquidación e id interno del ERP.

Contraste deliberado con Tier 2 (aduanas), que sí necesita la conciliación fiscal.
"""
import json
import unittest
from unittest.mock import patch

from src.app import mcp
from src.config import settings
from src.domain.privacy import redact_settlement_data
from src.infrastructure.db_pool import set_db_pool
from src.infrastructure.keycloak_guard import (
    tenant_scope_for, enforce_tenant_isolation, check_tier_permission,
    AuthContext, AccessTier, jwks_cache
)
from tests.fixtures import build_fake_pool, TestKeyring, TEST_AUDIENCE


def coop_context(company_id=1):
    return AuthContext(
        tier=AccessTier.TIER_2B_COOP_STAFF,
        user_id="tecnico-campo",
        roles=["FIELD_TECHNICIAN"],
        company_id=company_id,
        is_authenticated=True
    )


class TestSettlementRedaction(unittest.TestCase):
    """La función de redacción, aislada."""

    def setUp(self):
        self.dossier = {
            "found": True,
            "lot_identifier": "785",
            "participating_plots": [{
                "plot_id": 1,
                "plot_name": "Finca San José",
                "farmer_name": "Pedro Alvarado",
                "size_ha": 3.0,
                "geometry": {"coordinates": [[-79.754123, -1.542189]]}
            }],
            "sri_settlements": [{
                "liquidation_code": "001-002-000001234",
                "access_key": "1" * 49,
                "erp_internal_id": "ERP-1",
                "is_valid_modulo11": True
            }],
            "mass_balance": {"status": "OK", "lot_weight_kg": 13800.0},
            "eudr_summary": {"overall_status": "COMPLIANT"}
        }

    def test_removes_fiscal_settlements(self):
        out = redact_settlement_data(self.dossier)
        self.assertNotIn("sri_settlements", out)
        serialized = json.dumps(out)
        self.assertNotIn("001-002-000001234", serialized)
        self.assertNotIn("1" * 49, serialized)
        self.assertNotIn("ERP-1", serialized)

    def test_keeps_the_operational_dossier(self):
        """El técnico de campo necesita identidad del socio, parcela y geometría."""
        out = redact_settlement_data(self.dossier)
        plot = out["participating_plots"][0]
        self.assertEqual(plot["farmer_name"], "Pedro Alvarado")
        self.assertEqual(plot["plot_name"], "Finca San José")
        self.assertIn("coordinates", plot["geometry"])
        self.assertIn("mass_balance", out)
        self.assertIn("eudr_summary", out)

    def test_declares_the_redaction(self):
        """La respuesta debe decir que se redactó, no callarlo."""
        out = redact_settlement_data(self.dossier)
        self.assertIn("settlement_data_redacted", out)
        self.assertTrue(out["settlement_data_redacted"])

    def test_does_not_mutate_the_input(self):
        redact_settlement_data(self.dossier)
        self.assertIn("sri_settlements", self.dossier)

    def test_is_idempotent_without_settlements(self):
        del self.dossier["sri_settlements"]
        out = redact_settlement_data(self.dossier)
        self.assertNotIn("sri_settlements", out)
        self.assertTrue(out["settlement_data_redacted"])


class TestCoopStaffPolicy(unittest.TestCase):
    """La política del tier, sin red ni base de datos."""

    def test_scope_marks_coop_staff_for_redaction(self):
        self.assertTrue(tenant_scope_for(coop_context()).redact_settlements)

    def test_other_tiers_keep_settlement_data(self):
        for tier in (AccessTier.TIER_2_CUSTOMS, AccessTier.TIER_3_ERP, AccessTier.TIER_4_SUPPORT):
            ctx = AuthContext(tier=tier, user_id="x", company_id=1, is_authenticated=True)
            self.assertFalse(tenant_scope_for(ctx).redact_settlements, tier)

    def test_coop_staff_is_tenant_scoped(self):
        """No es exento: un técnico de la organización 1 no mira la 2."""
        enforce_tenant_isolation(coop_context(1), "audit_lot_traceability", {"company_id": 1})
        with self.assertRaises(PermissionError):
            enforce_tenant_isolation(coop_context(1), "audit_lot_traceability", {"company_id": 2})

    def test_coop_staff_token_without_company_claim_is_denied(self):
        with self.assertRaises(PermissionError):
            enforce_tenant_isolation(coop_context(None), "audit_lot_traceability", {"company_id": 1})

    def test_coop_staff_tool_matrix(self):
        permitted = ["audit_lot_traceability", "verify_eudr_compliance",
                     "export_agrocalidad_guia", "verify_sri_compliance",
                     "diagnose_and_repair_qr"]
        for tool in permitted:
            self.assertTrue(check_tier_permission(AccessTier.TIER_2B_COOP_STAFF, tool), tool)
        for tool in ("diagnose_tenant_health", "ingest_erp_purchase_order",
                     "sync_farmer_profile", "generate_traces_jsonld"):
            self.assertFalse(check_tier_permission(AccessTier.TIER_2B_COOP_STAFF, tool), tool)


class TestCoopStaffThroughDispatcher(unittest.IsolatedAsyncioTestCase):
    """Extremo a extremo con un token firmado de verdad."""

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

    async def call(self, tool, arguments, bearer):
        return await mcp.handle_jsonrpc_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}
        }, auth_header=bearer)

    def coop_bearer(self, company_id=1):
        return self.keyring.bearer(
            roles=["FIELD_TECHNICIAN"], sub="tecnico-unocace", company_id=company_id
        )

    async def test_coop_staff_sees_pii_but_not_settlements(self):
        resp = await self.call(
            "audit_lot_traceability",
            {"lot_identifier": "LOT-TEST-001", "company_id": 1},
            self.coop_bearer()
        )
        self.assertNotIn("error", resp)
        body = resp["result"]["structuredContent"]

        # Ve el expediente operativo, no la vista pública.
        self.assertNotEqual(body.get("channel"), "PUBLIC_B2C")
        self.assertIn("participating_plots", body)
        self.assertEqual(body["participating_plots"][0]["farmer_name"], "Carlos Mendoza")

        # No ve las liquidaciones fiscales.
        self.assertNotIn("sri_settlements", body)
        self.assertTrue(body.get("settlement_data_redacted"))

    async def test_customs_still_sees_settlements(self):
        """Contraste: aduanas necesita la conciliación fiscal."""
        bearer = self.keyring.bearer(roles=["CUSTOMS_INSPECTOR"], sub="aduana")
        resp = await self.call(
            "audit_lot_traceability",
            {"lot_identifier": "LOT-TEST-001", "company_id": 1},
            bearer
        )
        self.assertNotIn("error", resp)
        body = resp["result"]["structuredContent"]
        self.assertIn("sri_settlements", body)
        self.assertNotIn("settlement_data_redacted", body)

    async def test_coop_staff_cannot_cross_tenant(self):
        resp = await self.call(
            "audit_lot_traceability",
            {"lot_identifier": "LOT-TEST-001", "company_id": 2},
            self.coop_bearer(company_id=1)
        )
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)

    async def test_coop_staff_cannot_reach_tier4_diagnostics(self):
        resp = await self.call("diagnose_tenant_health", {"company_id": 1}, self.coop_bearer())
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)


if __name__ == "__main__":
    unittest.main()
