"""
Keycloak OIDC and Multi-Tier Governance Guard (ISO/IEC 27001 & ISO 22005).

Implements 4-Tier Access Control defined in MATRIZ_DE_RIESGOS_GOBERNANZA_MCP_INATRACE.md:
- Tier 1: Public B2C QR Consumer (Unauthenticated, strictly sanitized via Privacy Gate)
- Tier 2: Customs Inspector / TRACES-NT Auditor (Customs Passport Token)
- Tier 3: ERP / Weighing Scale Service Account (Keycloak Client Credentials)
- Tier 4: Technical Support GIZ / Factory Engineering (Read-Only Forensic Diagnostics)

A token's tier comes only from claims whose RS256 signature was verified against the
realm's published JWKS, with the issuer and audience checked. Anything that cannot be
verified resolves to an unauthenticated Tier 1 context, never to a higher tier.
"""
import time
import asyncio
import logging
from enum import Enum
from typing import Dict, Any, Optional, Tuple, List
import httpx
from pydantic import BaseModel, Field
from src.config import settings
from src.infrastructure.auth_scope import TenantScope

logger = logging.getLogger("inatrace-mcp.auth")

try:
    from jose import jwt
    from jose.exceptions import JWTError, JWTClaimsError, ExpiredSignatureError
    JOSE_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency is declared in pyproject.toml
    jwt = None
    JOSE_AVAILABLE = False

    class JWTError(Exception):
        pass

    class JWTClaimsError(JWTError):
        pass

    class ExpiredSignatureError(JWTError):
        pass


ALLOWED_ALGORITHMS = ["RS256"]


class AccessTier(str, Enum):
    TIER_1_PUBLIC = "TIER_1_PUBLIC"       # Public B2C / Consumers / Anonymous QR
    TIER_2_CUSTOMS = "TIER_2_CUSTOMS"     # European Customs / TRACES-NT Auditors
    TIER_2B_COOP_STAFF = "TIER_2B_COOP_STAFF" # UNOCACE Field Technicians / Cooperative Internal Staff
    TIER_3_ERP = "TIER_3_ERP"             # Accounting ERP / Báscula (service-account-erp-contable)
    TIER_4_SUPPORT = "TIER_4_SUPPORT"     # GIZ 60h Support / Engineering Diagnostics


# Permitted tools per Access Tier
TIER_PERMITTED_TOOLS: Dict[AccessTier, List[str]] = {
    AccessTier.TIER_1_PUBLIC: [
        "audit_lot_traceability",  # public_view=True imposed by TIER_FORCED_ARGUMENTS
        "diagnose_and_repair_qr"
    ],
    AccessTier.TIER_2_CUSTOMS: [
        "audit_lot_traceability",
        "verify_eudr_compliance",
        "generate_traces_jsonld",
        "export_agrocalidad_guia",
        "verify_sri_compliance",
        "diagnose_and_repair_qr"
    ],
    AccessTier.TIER_2B_COOP_STAFF: [
        "audit_lot_traceability",
        "verify_eudr_compliance",
        "export_agrocalidad_guia",
        "verify_sri_compliance",
        "diagnose_and_repair_qr"
    ],
    AccessTier.TIER_3_ERP: [
        "ingest_erp_purchase_order",
        "sync_farmer_profile",
        "verify_sri_compliance",
        "audit_lot_traceability"
    ],
    AccessTier.TIER_4_SUPPORT: [
        "audit_lot_traceability",
        "verify_eudr_compliance",
        "generate_traces_jsonld",
        "export_agrocalidad_guia",
        "verify_sri_compliance",
        "ingest_erp_purchase_order",
        "sync_farmer_profile",
        "diagnose_tenant_health",
        "diagnose_and_repair_qr"
    ]
}

# Arguments the server imposes on a tool call regardless of what the caller asked for.
# The privacy posture of a response is a consequence of the resolved tier, never a
# parameter the client gets to choose (LOPDP Art. 10 / GDPR Art. 5 data minimization).
TIER_FORCED_ARGUMENTS: Dict[AccessTier, Dict[str, Dict[str, Any]]] = {
    AccessTier.TIER_1_PUBLIC: {
        "audit_lot_traceability": {
            "public_view": True,
            "include_raw_coordinates": False
        }
    }
}

# Tier 4 support performs cross-tenant forensics by mandate; Tier 1 is anonymous and
# only ever receives sanitized output, so neither is tenant-scoped.
TIERS_EXEMPT_FROM_TENANT_CHECK = frozenset({
    AccessTier.TIER_4_SUPPORT,
    AccessTier.TIER_1_PUBLIC
})

# The ERP/scale write path and Cooperative staff path must carry their organization in the token.
TIERS_REQUIRING_TENANT_CLAIM = frozenset({AccessTier.TIER_3_ERP, AccessTier.TIER_2B_COOP_STAFF})

TENANT_ARGUMENT = "company_id"


class AuthContext(BaseModel):
    tier: AccessTier
    user_id: str
    client_id: Optional[str] = None
    roles: List[str] = Field(default_factory=list)
    company_id: Optional[int] = None
    is_authenticated: bool = False
    rejection_reason: Optional[str] = None


def _derived_realm_url() -> str:
    return f"{settings.KEYCLOAK_URL.rstrip('/')}/realms/{settings.KEYCLOAK_REALM}"


def expected_issuer() -> str:
    """
    The only issuer accepted in the 'iss' claim.

    `KEYCLOAK_ISSUER` overrides the value derived from `KEYCLOAK_URL` because the two
    differ whenever Keycloak sits behind a reverse proxy: it signs tokens with its
    public URL while this server reaches it only on the internal network. Deriving
    both from one variable made either the signature fetch or the issuer check fail.
    """
    explicit = (settings.KEYCLOAK_ISSUER or "").strip()
    return explicit.rstrip('/') if explicit else _derived_realm_url()


def accepted_audiences() -> List[str]:
    """Audiences accepted in the 'aud' claim. Empty means no token can be accepted."""
    raw = settings.KEYCLOAK_AUDIENCE or ""
    return [a.strip() for a in raw.split(",") if a.strip()]


def jwks_url() -> str:
    """
    Where the realm's signing keys are fetched from.

    Deliberately independent of `expected_issuer()`: this may be an internal address
    unreachable from outside, while the issuer stays the public one that appears in
    the tokens. `KEYCLOAK_JWKS_URL` is taken verbatim when set.
    """
    explicit = (settings.KEYCLOAK_JWKS_URL or "").strip()
    return explicit if explicit else f"{_derived_realm_url()}/protocol/openid-connect/certs"


class JwksCache:
    """
    In-process cache of the realm's signing keys, keyed by 'kid'.

    Refreshes when the TTL expires or an unknown kid appears (key rotation), throttled
    so a burst of tokens with a bogus kid cannot turn into a burst of requests to
    Keycloak. A failed refresh keeps the previous keys rather than opening the gate.
    """

    def __init__(self):
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._fetched_at: float = 0.0
        self._last_attempt: float = 0.0
        self._lock = asyncio.Lock()

    def _is_fresh(self) -> bool:
        return bool(self._keys) and (time.time() - self._fetched_at) < settings.KEYCLOAK_JWKS_TTL_SECONDS

    async def get_key(self, kid: str) -> Optional[Dict[str, Any]]:
        if self._is_fresh() and kid in self._keys:
            return self._keys[kid]
        await self._refresh()
        return self._keys.get(kid)

    async def _refresh(self) -> None:
        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if self._is_fresh():
                return
            now = time.time()
            if self._keys and (now - self._last_attempt) < settings.KEYCLOAK_JWKS_MIN_REFRESH_SECONDS:
                return
            self._last_attempt = now
            url = jwks_url()
            try:
                async with httpx.AsyncClient(timeout=settings.KEYCLOAK_JWKS_TIMEOUT_SECONDS) as client:
                    resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
                keys = {
                    k["kid"]: k
                    for k in payload.get("keys", [])
                    if k.get("kid") and k.get("kty") == "RSA"
                }
                if not keys:
                    logger.error(f"El JWKS de {url} no contiene claves RSA utilizables.")
                    return
                self._keys = keys
                self._fetched_at = now
                logger.info(f"JWKS actualizado desde {url}: {len(keys)} clave(s) de firma.")
            except Exception as exc:
                logger.error(
                    f"No se pudo obtener el JWKS de {url}: {exc}. "
                    f"Se conservan {len(self._keys)} clave(s) previas; los tokens que no "
                    f"puedan verificarse se degradan a Tier 1."
                )

    @property
    def key_count(self) -> int:
        """Signing keys currently cached. Zero means no token can be verified."""
        return len(self._keys)

    def clear(self) -> None:
        """Test and lifecycle helper: drops cached keys."""
        self._keys = {}
        self._fetched_at = 0.0
        self._last_attempt = 0.0

    def seed(self, keys: List[Dict[str, Any]]) -> None:
        """Test helper: installs keys without contacting Keycloak."""
        self._keys = {k["kid"]: k for k in keys if k.get("kid")}
        self._fetched_at = time.time()


jwks_cache = JwksCache()


def _anonymous(reason: str, user_id: str = "anonymous-b2c-consumer") -> AuthContext:
    """Fail-closed context: unverifiable input never yields more than public access."""
    return AuthContext(
        tier=AccessTier.TIER_1_PUBLIC,
        user_id=user_id,
        roles=[],
        is_authenticated=False,
        rejection_reason=reason
    )


def resolve_tier_from_claims(claims: Dict[str, Any]) -> AuthContext:
    """
    Maps verified claims onto the 4-Tier hierarchy.

    Only ever called with claims whose signature, issuer, audience and expiry have
    already been verified.
    """
    realm_roles = (claims.get("realm_access") or {}).get("roles", [])
    client_id = claims.get("azp") or claims.get("client_id")
    resource_access = claims.get("resource_access") or {}
    client_roles = (resource_access.get(client_id) or {}).get("roles", []) if client_id else []
    all_roles = sorted(set(realm_roles + client_roles))

    sub = claims.get("sub", "unknown")
    raw_company = claims.get("company_id")
    try:
        company_id = int(raw_company) if raw_company is not None else None
    except (TypeError, ValueError):
        logger.warning(f"Claim company_id no numérico ({raw_company!r}) en el token de {sub}; se ignora.")
        company_id = None

    def ctx(tier: AccessTier) -> AuthContext:
        return AuthContext(
            tier=tier,
            user_id=sub,
            client_id=client_id,
            roles=all_roles,
            company_id=company_id,
            is_authenticated=True
        )

    # Tier 4: Support / Admin
    if any(r in all_roles for r in ("INATRACE_TECH_SUPPORT", "ADMIN", "realm-admin", "superadmin")):
        return ctx(AccessTier.TIER_4_SUPPORT)

    # Tier 3: ERP / Scale
    if client_id == settings.SERVICE_ACCOUNT_CLIENT_ID or any(
        r in all_roles for r in ("service-account", "ERP_WRITER", "COMPANY_ADMIN")
    ):
        return ctx(AccessTier.TIER_3_ERP)

    # Tier 2: Customs / TRACES-NT Auditor
    if any(r in all_roles for r in ("CUSTOMS_INSPECTOR", "TRACES_AUDITOR", "customs-officer")) \
            or claims.get("token_type") == "customs_passport":
        return ctx(AccessTier.TIER_2_CUSTOMS)

    # Tier 2B: Cooperative Internal Staff / Field Technician
    if any(r in all_roles for r in ("FIELD_TECHNICIAN", "COOP_STAFF", "cooperative-technician", "TECNICO_CAMPO")) \
            or (company_id is not None and any(r in all_roles for r in ("USER", "coop-user"))):
        return ctx(AccessTier.TIER_2B_COOP_STAFF)

    # Authenticated consumer / producer: public tier, but identified.
    return ctx(AccessTier.TIER_1_PUBLIC)


def _check_audience(claims: Dict[str, Any], audiences: List[str]) -> bool:
    """Audience is validated here so several accepted values need only one decode."""
    aud = claims.get("aud")
    if aud is None:
        return False
    token_auds = [aud] if isinstance(aud, str) else list(aud)
    return any(a in audiences for a in token_auds)


async def verify_keycloak_token(token: Optional[str]) -> AuthContext:
    """
    Verifies a Keycloak OIDC Bearer token and resolves the caller's AccessTier.

    Enforces, in order: RS256 only, a kid present in the realm's JWKS, a valid
    signature, the realm issuer, an accepted audience, and expiry. Any failure
    returns an unauthenticated Tier 1 context with the reason recorded.
    """
    if not token or not token.strip():
        return _anonymous("NO_TOKEN")

    clean_token = token.strip()
    if clean_token.lower().startswith("bearer "):
        clean_token = clean_token[7:].strip()
    if not clean_token:
        return _anonymous("EMPTY_BEARER")

    if not JOSE_AVAILABLE:
        logger.error(
            "python-jose no está instalado: no es posible verificar firmas JWT. "
            "Todo token se degrada a Tier 1."
        )
        return _anonymous("JWT_LIBRARY_MISSING", "unverifiable-token")

    audiences = accepted_audiences()
    if not audiences:
        logger.error(
            "KEYCLOAK_AUDIENCE no está configurado: no se puede validar el claim 'aud', "
            "por lo que ningún token se acepta."
        )
        return _anonymous("AUDIENCE_NOT_CONFIGURED", "unverifiable-token")

    try:
        header = jwt.get_unverified_header(clean_token)
    except Exception as exc:
        logger.warning(f"Cabecera JWT ilegible, token rechazado: {exc}")
        return _anonymous("MALFORMED_HEADER", "anonymous-invalid-token")

    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        logger.warning(f"Algoritmo '{alg}' no permitido (solo {ALLOWED_ALGORITHMS}); token rechazado.")
        return _anonymous(f"ALGORITHM_NOT_ALLOWED:{alg}", "anonymous-invalid-token")

    kid = header.get("kid")
    if not kid:
        logger.warning("Token sin 'kid': no se puede localizar la clave de firma; rechazado.")
        return _anonymous("MISSING_KID", "anonymous-invalid-token")

    signing_key = await jwks_cache.get_key(kid)
    if signing_key is None:
        logger.warning(f"'kid' {kid} desconocido en el JWKS de {jwks_url()}; token rechazado.")
        return _anonymous("UNKNOWN_KID", "anonymous-invalid-token")

    try:
        claims = jwt.decode(
            clean_token,
            signing_key,
            algorithms=ALLOWED_ALGORITHMS,
            issuer=expected_issuer(),
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                # Checked below so that several accepted audiences cost one decode.
                "verify_aud": False,
                "require_exp": True
            }
        )
    except ExpiredSignatureError:
        logger.warning("Token expirado; rechazado.")
        return _anonymous("EXPIRED", "expired-token-user")
    except JWTClaimsError as exc:
        logger.warning(f"Claims inválidos (issuer/exp), token rechazado: {exc}")
        return _anonymous("INVALID_CLAIMS", "anonymous-invalid-token")
    except JWTError as exc:
        logger.warning(f"Firma JWT inválida, token rechazado: {exc}")
        return _anonymous("INVALID_SIGNATURE", "anonymous-invalid-token")

    if not _check_audience(claims, audiences):
        logger.warning(
            f"Audiencia {claims.get('aud')!r} no aceptada (se esperaba una de {audiences}); "
            f"token rechazado."
        )
        return _anonymous("INVALID_AUDIENCE", "anonymous-invalid-token")

    return resolve_tier_from_claims(claims)


def check_tier_permission(tier: AccessTier, tool_name: str) -> bool:
    """
    Checks whether a given access tier is authorized to execute a specific tool.
    """
    permitted = TIER_PERMITTED_TOOLS.get(tier, [])
    return tool_name in permitted


def enforce_tier_arguments(
    tier: AccessTier,
    tool_name: str,
    arguments: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Applies the server-side argument policy for the resolved tier.

    A Tier 1 public consumer cannot opt out of the privacy gate by passing
    public_view=False or include_raw_coordinates=True: those arguments are
    overwritten here, before the tool ever sees them.

    Returns: (effective_arguments, list_of_overridden_argument_names)
    """
    forced = TIER_FORCED_ARGUMENTS.get(tier, {}).get(tool_name)
    if not forced:
        return dict(arguments), []

    effective = dict(arguments)
    overridden: List[str] = []
    for key, value in forced.items():
        if effective.get(key) != value:
            overridden.append(key)
        effective[key] = value

    if overridden:
        logger.info(
            f"Política de tier {tier.value} aplicada sobre '{tool_name}': "
            f"argumentos forzados {overridden}."
        )
    return effective, overridden


def tenant_scope_for(auth_ctx: AuthContext) -> TenantScope:
    """Builds the tenant boundary carried through the tool call."""
    return TenantScope(
        tier=auth_ctx.tier.value,
        user_id=auth_ctx.user_id,
        company_id=auth_ctx.company_id,
        cross_tenant_allowed=auth_ctx.tier in TIERS_EXEMPT_FROM_TENANT_CHECK,
        redact_settlements=auth_ctx.tier == AccessTier.TIER_2B_COOP_STAFF
    )


def enforce_tenant_isolation(
    auth_ctx: AuthContext,
    tool_name: str,
    arguments: Dict[str, Any]
) -> None:
    """
    Rejects a request for an organization outside the token's tenant boundary.

    Covers the company_id argument; resources whose owner is only known after a
    database lookup are checked in the tool layer via assert_tenant_access().

    Raises PermissionError on a cross-tenant request.
    """
    if auth_ctx.tier in TIERS_EXEMPT_FROM_TENANT_CHECK:
        return

    if auth_ctx.company_id is None:
        if auth_ctx.tier in TIERS_REQUIRING_TENANT_CLAIM:
            logger.warning(
                f"Token de tier {auth_ctx.tier.value} sin claim company_id: "
                f"'{tool_name}' denegado."
            )
            raise PermissionError(
                f"Access denied: el tier '{auth_ctx.tier.value}' exige el claim "
                f"'company_id' en el token para ejecutar '{tool_name}'."
            )
        # A customs passport is not scoped to a single cooperative.
        logger.info(
            f"Token de tier {auth_ctx.tier.value} sin claim company_id; "
            f"'{tool_name}' no se limita a una organización."
        )
        return

    requested = arguments.get(TENANT_ARGUMENT)
    if requested is None:
        return

    try:
        requested_id = int(requested)
    except (TypeError, ValueError):
        raise PermissionError(
            f"Access denied: '{TENANT_ARGUMENT}' debe ser numérico, se recibió {requested!r}."
        )

    if requested_id != auth_ctx.company_id:
        logger.warning(
            f"Aislamiento multi-tenant: '{auth_ctx.user_id}' (organización "
            f"{auth_ctx.company_id}) solicitó '{tool_name}' sobre la organización "
            f"{requested_id}."
        )
        raise PermissionError(
            f"Access denied: el token está limitado a la organización "
            f"{auth_ctx.company_id} y la petición apunta a la {requested_id}."
        )
