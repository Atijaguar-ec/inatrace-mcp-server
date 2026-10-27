"""
Regression suite for ALT-01: the HTTP transport.

The hand-rolled `asyncio.start_server` handler parsed HTTP by hand with no request
size limit (`readexactly(content_length)` would happily allocate whatever a client
declared), no read timeouts and no connection limits. It now runs on Starlette, and
these tests pin both the limits and the route contract, so the migration cannot
silently drop an endpoint existing clients depend on.
"""
import json
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from src.app import mcp
from src.config import settings
from src.infrastructure.db_pool import set_db_pool
from src.infrastructure.keycloak_guard import jwks_cache
from tests.fixtures import build_fake_pool, TestKeyring, TEST_AUDIENCE


class TransportTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(mcp.asgi_app)

    def setUp(self):
        set_db_pool(None)

    def tearDown(self):
        set_db_pool(None)


class TestHealthEndpoints(TransportTestCase):

    def test_liveness_reports_inventory(self):
        for path in ("/health", "/health/live"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            body = r.json()
            self.assertEqual(body["status"], "ALIVE")
            self.assertEqual(body["tools_count"], 9)
            self.assertEqual(body["resources_count"], 3)
            self.assertEqual(body["prompts_count"], 2)

    def test_readiness_is_503_without_pool(self):
        r = self.client.get("/health/ready")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "DEGRADED")
        self.assertFalse(r.json()["db_pool_connected"])

    def test_readiness_is_200_with_pool(self):
        set_db_pool(build_fake_pool())
        r = self.client.get("/health/ready")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "READY")


class TestInventoryEndpoints(TransportTestCase):

    def test_tools_listing_on_both_paths(self):
        for path in ("/mcp/tools", "/tools"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            names = [t["name"] for t in r.json()["tools"]]
            self.assertEqual(len(names), 9, path)
            self.assertIn("audit_lot_traceability", names)

    def test_resources_listing_on_both_paths(self):
        for path in ("/mcp/resources", "/resources"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            uris = [x["uri"] for x in r.json()["resources"]]
            self.assertIn("inatrace://catalogs/dpa-ecuador", uris)

    def test_prompts_listing_on_both_paths(self):
        for path in ("/mcp/prompts", "/prompts"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            names = [x["name"] for x in r.json()["prompts"]]
            self.assertIn("auditoria_aduanera_eudr", names)

    def test_link_header_announces_mcp_endpoint(self):
        r = self.client.get("/health/live")
        self.assertIn("modelcontextprotocol", r.headers["link"])

    def test_unknown_path_is_404_json(self):
        r = self.client.get("/no/existe")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "NOT_FOUND")

    def test_sse_endpoint_is_a_real_event_stream(self):
        r = self.client.get("/mcp/sse?lotId=LOT-2026-042")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers["content-type"])
        # A text/event-stream response must not declare a body length.
        self.assertNotIn("content-length", {k.lower() for k in r.headers})
        self.assertIn("LOT-2026-042", r.headers["link"])
        self.assertTrue(r.text.startswith("event: endpoint"))
        payload = json.loads(r.text.split("data: ", 1)[1].strip())
        self.assertEqual(payload["jsonrpcEndpoint"], "/mcp")
        self.assertEqual(payload["lotId"], "LOT-2026-042")


class TestRequestLimits(TransportTestCase):
    """The limits that the hand-rolled server lacked entirely."""

    def test_oversized_declared_body_is_rejected(self):
        payload = b"x" * (settings.MAX_REQUEST_BYTES + 1024)
        r = self.client.post("/mcp", content=payload,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["error"], "REQUEST_TOO_LARGE")

    def test_oversized_chunked_body_is_rejected(self):
        """No Content-Length to trust: the cap must hold while streaming."""
        chunk = b"y" * 65536
        total = settings.MAX_REQUEST_BYTES + (2 * len(chunk))

        def generate():
            sent = 0
            while sent < total:
                yield chunk
                sent += len(chunk)

        r = self.client.post("/mcp", content=generate(),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_lying_content_length_is_rejected(self):
        r = self.client.post("/mcp", content=b"{}",
                             headers={"Content-Length": str(10 ** 12),
                                      "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_malformed_content_length_is_rejected(self):
        r = self.client.post("/mcp", content=b"{}",
                             headers={"Content-Length": "no-soy-un-numero",
                                      "Content-Type": "application/json"})
        self.assertIn(r.status_code, (400, 413))

    def test_body_within_the_limit_is_accepted(self):
        big_but_legal = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                         "params": {"relleno": "z" * 1000}}
        r = self.client.post("/mcp", json=big_but_legal)
        self.assertEqual(r.status_code, 200)
        self.assertIn("result", r.json())

    def test_get_requests_are_not_buffered(self):
        """The guard must not interfere with bodyless methods."""
        r = self.client.get("/health/live")
        self.assertEqual(r.status_code, 200)


class TestJsonRpcOverHttp(TransportTestCase):

    def test_initialize(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(r.status_code, 200)
        res = r.json()["result"]
        self.assertIn("capabilities", res)
        self.assertIn("instructions", res)
        self.assertIn("organización", res["instructions"])
        self.assertIn("token de seguridad", res["instructions"])
        self.assertNotIn("UNOCACE", res["instructions"])
        self.assertNotIn("cooperativa", res["instructions"].lower())

    def test_mcp_overview_get_returns_agent_instructions(self):
        for path in ("/mcp", "/"):
            r = self.client.get(
                path,
                headers={"x-forwarded-proto": "https", "x-forwarded-host": "mcp.example.org"}
            )
            self.assertEqual(r.status_code, 200, path)
            body = r.json()
            self.assertEqual(body["status"], "ALIVE")
            self.assertEqual(body["mcp_endpoint"], "https://mcp.example.org/mcp")
            self.assertIn("instructions_for_ai_agent", body)
            self.assertIn("https://mcp.example.org/mcp", body["instructions_for_ai_agent"])
            self.assertIn("organización", body["question_to_ask_user"])
            self.assertNotIn("UNOCACE", body["instructions_for_ai_agent"])
            self.assertNotIn("cooperativa", body["instructions_for_ai_agent"].lower())
            self.assertEqual(len(body["capabilities_without_token"]), 3)

    def test_jsonrpc_alias_path(self):
        r = self.client.post("/jsonrpc", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["result"]["tools"]), 9)

    def test_unparseable_body_is_a_parse_error(self):
        r = self.client.post("/mcp", content=b"{no es json",
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"]["code"], -32700)

    def test_non_object_body_is_an_invalid_request(self):
        r = self.client.post("/mcp", json=[1, 2, 3])
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_unknown_method(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "no/existe"})
        self.assertEqual(r.json()["error"]["code"], -32601)

    def test_anonymous_tool_call_is_forbidden(self):
        r = self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "diagnose_tenant_health", "arguments": {"company_id": 1}}
        })
        self.assertEqual(r.json()["error"]["code"], -32001)


class TestRestToolRoute(TransportTestCase):
    """Authorization must hold on this transport exactly as on JSON-RPC."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.keyring = TestKeyring()

    def setUp(self):
        super().setUp()
        self.audience_patch = patch.object(settings, "KEYCLOAK_AUDIENCE", TEST_AUDIENCE)
        self.audience_patch.start()
        self.keyring.install()

    def tearDown(self):
        self.audience_patch.stop()
        jwks_cache.clear()
        super().tearDown()

    def test_unknown_tool_is_404(self):
        r = self.client.post("/mcp/tools/no_existe", json={})
        self.assertEqual(r.status_code, 404)

    def test_anonymous_write_is_403(self):
        r = self.client.post("/mcp/tools/ingest_erp_purchase_order", json={
            "company_id": 5, "farmer_id": 1, "parcel_name": "X",
            "gross_weight_kg": 100.0, "tare_kg": 2.0,
            "sri_access_key": "1" * 49, "erp_internal_id": "E1"
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "FORBIDDEN")

    def test_outage_is_503_not_a_verdict(self):
        bearer = self.keyring.bearer(roles=["CUSTOMS_INSPECTOR"], sub="customs")
        r = self.client.post("/mcp/tools/verify_eudr_compliance",
                             json={"stock_order_id": 999999999},
                             headers={"Authorization": bearer})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "DATABASE_UNAVAILABLE")
        self.assertNotIn("RELEASE_CONTAINER_GREEN_CHANNEL", r.text)

    def test_cross_tenant_request_is_403(self):
        bearer = self.keyring.bearer(
            roles=["ERP_WRITER"], sub="erp-org1",
            azp=settings.SERVICE_ACCOUNT_CLIENT_ID, company_id=1
        )
        set_db_pool(build_fake_pool())
        r = self.client.post("/mcp/tools/audit_lot_traceability",
                             json={"lot_identifier": "LOT-TEST-001", "company_id": 2},
                             headers={"Authorization": bearer})
        self.assertEqual(r.status_code, 403)

    def test_authorized_call_succeeds(self):
        bearer = self.keyring.bearer(
            roles=["ERP_WRITER"], sub="erp-org1",
            azp=settings.SERVICE_ACCOUNT_CLIENT_ID, company_id=1
        )
        set_db_pool(build_fake_pool())
        r = self.client.post("/mcp/tools/audit_lot_traceability",
                             json={"lot_identifier": "LOT-TEST-001", "company_id": 1},
                             headers={"Authorization": bearer})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["found"])
        self.assertEqual(r.json()["data_source"], "DATABASE")

    def test_initialize_returns_mcp_session_id_header(self):
        r = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"}
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("mcp-session-id", {k.lower() for k in r.headers})
        session_id = r.headers.get("mcp-session-id")
        self.assertTrue(bool(session_id))
        self.assertEqual(r.json()["result"]["protocolVersion"], "2024-11-05")

    def test_oauth_protected_resource_metadata(self):
        for path in ("/.well-known/oauth-protected-resource", "/mcp/.well-known/oauth-protected-resource"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            data = r.json()
            self.assertIn("authorization_servers", data)
            self.assertTrue(len(data["authorization_servers"]) > 0)
            self.assertIn("scopes_supported", data)

    def test_coop_staff_tier_access_allowed_for_own_company(self):
        bearer = self.keyring.bearer(
            roles=["USER"], sub="tecnico-unocace", company_id=1
        )
        set_db_pool(build_fake_pool())
        r = self.client.post("/mcp/tools/audit_lot_traceability",
                             json={"lot_identifier": "LOT-TEST-001", "company_id": 1, "public_view": False},
                             headers={"Authorization": bearer})
        self.assertEqual(r.status_code, 200)
        # Coop staff is NOT forced to public_view=True
        self.assertNotIn("channel", r.json())  # public view has channel=PUBLIC_B2C

    def test_coop_staff_tier_access_denied_for_another_company(self):
        bearer = self.keyring.bearer(
            roles=["USER"], sub="tecnico-unocace", company_id=1
        )
        set_db_pool(build_fake_pool())
        r = self.client.post("/mcp/tools/audit_lot_traceability",
                             json={"lot_identifier": "LOT-TEST-001", "company_id": 2},
                             headers={"Authorization": bearer})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
