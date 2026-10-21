"""
Shared test fixtures for INATrace MCP Server.

Two things live here:

- FakeConnection / FakePool: answer by inspecting the SQL text instead of by call
  order, so a test only overrides the row sets it cares about. Needed because the
  tools no longer fabricate data when the pool is missing.
- TestKeyring: a real RSA keypair plus the matching JWKS entry, so authentication
  tests sign genuine RS256 tokens and seed the guard's JWKS cache. Signature
  verification cannot be tested with hand-assembled strings.
"""
import base64
import json
import time
from typing import Dict, Any, List, Optional


DEFAULT_LINEAGE = [{
    "stock_order_id": 101,
    "lot_code": "LOT-TEST-001",
    "qrcodetag": "00000000-0000-0000-0000-000000000000",
    "amount": 13800.0,
    "status": "ACTIVE",
    "facility_id": 1,
    "company_id": 1,
    "producerusercustomer_id": 10,
    "parcellot": "Finca La Providencia",
    "comments": "LC:001-002-000001234|CA:1110202601099234567800110010020000012341234567812|ERP:ERP-8841",
    "processingorder_id": 45,
    "level": 1
}]

DEFAULT_PLOTS = [{
    "id": 1,
    "plotname": "Finca La Providencia",
    "size": 3.5,
    "geoid": "EC-AGS-7721",
    "cocoavariety": "Nacional Cacao Fino de Aroma",
    "productionestimate": 50.0,
    "farmer_id": 10,
    "name": "Carlos",
    "surname": "Mendoza"
}]

DEFAULT_COORDS = [
    {"plot_id": 1, "latitude": -1.542189, "longitude": -79.754123}
]

DEFAULT_DEFORESTATION = [{
    "plot_id": 1,
    "status": "COMPLIANT",
    "riskpcrop": "low",
    "indicatordisturbanceafter2020": "0"
}]

DEFAULT_STOCK_ORDER = {
    "qrcodetag": "00000000-0000-0000-0000-000000000000",
    "totalquantity": 13800.0,
    "identifier": "LOT-TEST-001",
    "company_id": 1,
    "comments": "LC:001-002-000001234|CA:1110202601099234567800110010020000012341234567812|ERP:ERP-8841"
}

DEFAULT_AGROCALIDAD_ROWS = [{
    "ruc_operador": "0992345678001",
    "codigo_sitio_guia": "GUIA-SITIO-0012",
    "codigo_area_guia": "AREA-ACOPIO-01",
    "fecha_recepcion": "2026-10-01",
    "codigo_dpa_inec": "120550",
    "provincia": "LOS RIOS",
    "canton": "VINCES",
    "parroquia": "VINCES",
    "nombre_productor": "PRODUCTOR COMUNITARIO UNOCACE",
    "tipo_producto": "CACAO EN BABA",
    "peso_bruto_kg": "1500.00",
    "tara_kg": "50.00",
    "peso_neto_kg": "1450.00",
    "numero_sacos": "21",
    "destino_movilizacion": "PLANTA CENTRAL BENEFICIO",
    "estado_fitosanitario": "CONFORME_SIN_PLAGAS"
}]


class _Unset:
    """Sentinel so that an explicit None (row absent) differs from "use the default"."""


UNSET = _Unset()


class FakeConnection:
    """
    Minimal asyncpg-compatible connection that routes on SQL text.
    Pass row sets to the constructor to override any default.
    """

    def __init__(
        self,
        lineage: Optional[List[Dict[str, Any]]] = None,
        plots: Optional[List[Dict[str, Any]]] = None,
        coords: Optional[List[Dict[str, Any]]] = None,
        deforestation: Optional[List[Dict[str, Any]]] = None,
        stock_order: Any = UNSET,
        company_configuration: Optional[Dict[str, Any]] = None,
        agrocalidad_rows: Optional[List[Dict[str, Any]]] = None,
        qr_actions: Optional[List[Dict[str, Any]]] = None,
        owners_count: int = 1,
        unpadded_dpa_count: int = 0
    ):
        self.lineage = DEFAULT_LINEAGE if lineage is None else lineage
        self.plots = DEFAULT_PLOTS if plots is None else plots
        self.coords = DEFAULT_COORDS if coords is None else coords
        self.deforestation = DEFAULT_DEFORESTATION if deforestation is None else deforestation
        self.stock_order = DEFAULT_STOCK_ORDER if isinstance(stock_order, _Unset) else stock_order
        self.company_configuration = company_configuration if company_configuration is not None else {
            "enableDeliveryReceipt": True,
            "enablePublicDeliveryReceipt": True,
            "enableQuotaBalance": True,
            "quotaAlertThresholdPercent": 80
        }
        self.agrocalidad_rows = DEFAULT_AGROCALIDAD_ROWS if agrocalidad_rows is None else agrocalidad_rows
        self.qr_actions = [{"id": 1, "inputsemiproduct_id": 3}] if qr_actions is None else qr_actions
        self.owners_count = owners_count
        self.unpadded_dpa_count = unpadded_dpa_count
        self.queries: List[str] = []

    async def fetch(self, query: str, *args) -> List[Dict[str, Any]]:
        self.queries.append(query)
        q = " ".join(query.split())
        if "WITH RECURSIVE" in q:
            return self.lineage
        if "FROM plot pl" in q:
            return self.plots
        if "FROM plotcoordinate" in q:
            return self.coords
        if "FROM plotdeforestationanalysis" in q:
            return self.deforestation
        if "FROM processingaction" in q:
            return self.qr_actions
        if "bi.rpt_cacao_agrocalidad_guia" in q:
            return self.agrocalidad_rows
        return []

    async def fetchrow(self, query: str, *args) -> Optional[Dict[str, Any]]:
        self.queries.append(query)
        q = " ".join(query.split())
        if "FROM stockorder" in q:
            return self.stock_order
        if "FROM company" in q:
            return {"configuration": self.company_configuration}
        return None

    async def fetchval(self, query: str, *args) -> Any:
        self.queries.append(query)
        q = " ".join(query.split())
        if "comments FROM stockorder" in q:
            return (self.stock_order or {}).get("comments")
        if "productcompany" in q:
            return self.owners_count
        if "FROM usercustomer" in q:
            return self.unpadded_dpa_count
        if q.startswith("SELECT 1"):
            return 1
        return None


class _AcquireContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakePool:
    """asyncpg-compatible pool wrapper around a single FakeConnection."""

    def __init__(self, conn: FakeConnection):
        self.conn = conn

    def acquire(self):
        return _AcquireContext(self.conn)

    async def close(self):
        return None


def build_fake_pool(**kwargs) -> FakePool:
    """Builds a FakePool whose connection uses the supplied row-set overrides."""
    return FakePool(FakeConnection(**kwargs))


# --- Authentication fixtures (BLK-01) ---

TEST_ISSUER = "https://auth.unocace.com/realms/unocace"
TEST_AUDIENCE = "inatrace-mcp"


class TestKeyring:
    """
    A real RSA keypair that signs tokens and exposes the matching JWKS entry.

    Use `keyring.install()` to seed the guard's JWKS cache, then `keyring.token(...)`
    to mint a genuinely signed token. `foreign` builds a second keyring whose tokens
    carry a valid structure but a signature the server cannot verify.
    """

    def __init__(self, kid: str = "inatrace-test-key"):
        from jose import jwk
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        self.kid = kid
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()
        ).decode()
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

        raw_jwk = jwk.construct(public_pem, algorithm="RS256").to_dict()
        self.jwk = {
            k: (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw_jwk.items()
        }
        self.jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})

    def install(self) -> None:
        """Seeds the guard's JWKS cache with this keyring's public key."""
        from src.infrastructure.keycloak_guard import jwks_cache
        jwks_cache.seed([self.jwk])

    def token(
        self,
        roles: Optional[List[str]] = None,
        sub: str = "test-subject",
        company_id: Optional[int] = None,
        audience: Any = TEST_AUDIENCE,
        issuer: str = TEST_ISSUER,
        expires_in: int = 3600,
        azp: Optional[str] = None,
        token_type: Optional[str] = None,
        kid: Optional[str] = None,
        extra_claims: Optional[Dict[str, Any]] = None
    ) -> str:
        from jose import jwt

        claims: Dict[str, Any] = {
            "sub": sub,
            "iss": issuer,
            "exp": int(time.time()) + expires_in,
            "iat": int(time.time()),
            "realm_access": {"roles": roles or []}
        }
        if audience is not None:
            claims["aud"] = audience
        if company_id is not None:
            claims["company_id"] = company_id
        if azp is not None:
            claims["azp"] = azp
        if token_type is not None:
            claims["token_type"] = token_type
        if extra_claims:
            claims.update(extra_claims)

        return jwt.encode(
            claims,
            self.private_pem,
            algorithm="RS256",
            headers={"kid": kid or self.kid}
        )

    def bearer(self, **kwargs) -> str:
        return f"Bearer {self.token(**kwargs)}"


def unsigned_token(payload: Dict[str, Any], alg: str = "RS256") -> str:
    """
    Hand-assembled token with a bogus signature: the exact shape that escalated to
    Tier 4 before BLK-01 was closed. Must now be rejected.
    """

    def b64(obj: Dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    header = b64({"alg": alg, "typ": "JWT", "kid": "inatrace-test-key"})
    body = b64(payload)
    # The signature segment has to be well-formed base64url, or the token is
    # rejected as malformed before the signature is ever checked. A real forgery
    # looks like a signature; it just is not one.
    fake_signature = base64.urlsafe_b64encode(b"\x00" * 256).decode().rstrip("=")
    return f"{header}.{body}.{fake_signature}"
