"""
Regression suite for ALT-02: the Keycloak service-account token must fail closed.

`get_service_token()` used to return the literal "mock-service-token-dev" whenever
Keycloak was unreachable or rejected the credentials, and the caller then went on to
PUT the write to the Core with that bogus bearer. A misconfiguration surfaced as a
business rejection logged at WARNING, instead of as the outage it was.
"""
import unittest
from unittest.mock import patch

from src.app import mcp
from src.config import settings
from src.infrastructure.db_pool import set_db_pool
from src.infrastructure.keycloak_guard import jwks_cache
from src.infrastructure.spring_client import (
    SpringCoreClient, ServiceTokenUnavailableError
)
from tests.fixtures import build_fake_pool, TestKeyring, TEST_AUDIENCE


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("respuesta sin JSON")
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        if self._raises:
            raise self._raises
        return self._response


class TestServiceTokenFailsClosed(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.secret_patch = patch.object(
            settings, "SERVICE_ACCOUNT_CLIENT_SECRET", "un-secreto-de-prueba"
        )
        self.secret_patch.start()
        self.client = SpringCoreClient()

    def tearDown(self):
        self.secret_patch.stop()

    def _patch_http(self, **kwargs):
        return patch(
            "src.infrastructure.spring_client.httpx.AsyncClient",
            _FakeAsyncClient(**kwargs)
        )

    async def test_unreachable_keycloak_raises(self):
        with self._patch_http(raises=OSError("connection refused")):
            with self.assertRaises(ServiceTokenUnavailableError) as cm:
                await self.client.get_service_token()
        self.assertIn("inalcanzable", str(cm.exception))

    async def test_rejected_credentials_raise(self):
        with self._patch_http(response=_FakeResponse(401, text="invalid_client")):
            with self.assertRaises(ServiceTokenUnavailableError) as cm:
                await self.client.get_service_token()
        self.assertIn("401", str(cm.exception))

    async def test_server_error_raises(self):
        with self._patch_http(response=_FakeResponse(503, text="unavailable")):
            with self.assertRaises(ServiceTokenUnavailableError):
                await self.client.get_service_token()

    async def test_unparseable_response_raises(self):
        with self._patch_http(response=_FakeResponse(200, payload=None)):
            with self.assertRaises(ServiceTokenUnavailableError):
                await self.client.get_service_token()

    async def test_missing_access_token_field_raises(self):
        with self._patch_http(response=_FakeResponse(200, payload={"expires_in": 300})):
            with self.assertRaises(ServiceTokenUnavailableError):
                await self.client.get_service_token()

    async def test_no_fallback_token_is_ever_returned(self):
        """The literal that used to be handed to the Core must not appear anywhere."""
        for kwargs in (
            {"raises": OSError("boom")},
            {"response": _FakeResponse(500, text="err")},
        ):
            with self._patch_http(**kwargs):
                try:
                    token = await self.client.get_service_token()
                except ServiceTokenUnavailableError as exc:
                    self.assertNotIn("mock-service-token-dev", str(exc))
                else:
                    self.fail(f"devolvió un token ({token!r}) en lugar de fallar")

    async def test_unconfigured_secret_raises_without_calling_keycloak(self):
        with patch.object(settings, "SERVICE_ACCOUNT_CLIENT_SECRET", ""):
            with self._patch_http(raises=AssertionError("no debería llamarse")):
                with self.assertRaises(ServiceTokenUnavailableError) as cm:
                    await self.client.get_service_token()
        self.assertIn("SERVICE_ACCOUNT_CLIENT_SECRET", str(cm.exception))

    async def test_successful_token_is_returned_and_cached(self):
        response = _FakeResponse(200, payload={"access_token": "tok-123", "expires_in": 600})
        with self._patch_http(response=response):
            first = await self.client.get_service_token()
        self.assertEqual(first, "tok-123")

        # Second call must not hit Keycloak again while the token is fresh.
        with self._patch_http(raises=AssertionError("no debería llamarse")):
            second = await self.client.get_service_token()
        self.assertEqual(second, "tok-123")

    async def test_expired_cache_is_refreshed(self):
        self.client._cached_token = "tok-viejo"
        self.client._token_expires_at = 0.0
        response = _FakeResponse(200, payload={"access_token": "tok-nuevo", "expires_in": 600})
        with self._patch_http(response=response):
            self.assertEqual(await self.client.get_service_token(), "tok-nuevo")


class TestServiceTokenOutageThroughDispatcher(unittest.IsolatedAsyncioTestCase):
    """A token outage must surface as an error, not as a rejection by the Core."""

    @classmethod
    def setUpClass(cls):
        cls.keyring = TestKeyring()

    def setUp(self):
        self.audience_patch = patch.object(settings, "KEYCLOAK_AUDIENCE", TEST_AUDIENCE)
        self.audience_patch.start()
        self.secret_patch = patch.object(settings, "SERVICE_ACCOUNT_CLIENT_SECRET", "secreto")
        self.secret_patch.start()
        self.keyring.install()
        set_db_pool(build_fake_pool())

    def tearDown(self):
        self.audience_patch.stop()
        self.secret_patch.stop()
        jwks_cache.clear()
        set_db_pool(None)

    async def test_jsonrpc_reports_service_token_outage(self):
        from src.domain.sri_validator import calculate_modulo11_check_digit
        first_48 = "111020260109923456780011001002000001234123456781"
        valid_key = first_48 + str(calculate_modulo11_check_digit(first_48))

        erp_bearer = self.keyring.bearer(
            roles=["ERP_WRITER"],
            sub="erp-org1",
            azp=settings.SERVICE_ACCOUNT_CLIENT_ID,
            company_id=5
        )

        with patch(
            "src.infrastructure.spring_client.httpx.AsyncClient",
            _FakeAsyncClient(raises=OSError("connection refused"))
        ):
            resp = await mcp.handle_jsonrpc_request({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "ingest_erp_purchase_order",
                    "arguments": {
                        "company_id": 5,
                        "farmer_id": 22,
                        "parcel_name": "Finca San Antonio",
                        "gross_weight_kg": 100.0,
                        "tare_kg": 2.0,
                        "sri_access_key": valid_key,
                        "erp_internal_id": "ERP-TRANS-77"
                    }
                }
            }, auth_header=erp_bearer)

        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32003)
        self.assertIn("SERVICE_TOKEN_UNAVAILABLE", resp["error"]["message"])
        self.assertNotIn("ACCEPTED", str(resp))


if __name__ == "__main__":
    unittest.main()
