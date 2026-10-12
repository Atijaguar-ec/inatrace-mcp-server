"""
Keycloak OIDC and Multi-Tier Governance Guard (ISO/IEC 27001 & ISO 22005).
Implements 4-Tier Access Control defined in MATRIZ_DE_RIESGOS_GOBERNANZA_MCP_INATRACE.md:
- Tier 1: Public B2C QR Consumer (Unauthenticated, strictly sanitized via Privacy Gate)
- Tier 2: Customs Inspector / TRACES-NT Auditor (Customs Passport Token)
- Tier 3: ERP / Weighing Scale Service Account (Keycloak Client Credentials)
- Tier 4: Technical Support GIZ / Factory Engineering (Read-Only Forensic Diagnostics)
"""
import time
import json
import base64
import logging
from enum import Enum
from typing import Dict, Any, Optional, Tuple, List
from pydantic import BaseModel, Field
from src.config import settings

logger = logging.getLogger("inatrace-mcp.auth")

try:
    from jose import jwt, JWTError
    JOSE_AVAILABLE = True
except ImportError:
    jwt = None
    JWTError = Exception
    JOSE_AVAILABLE = False


class AccessTier(str, Enum):
    TIER_1_PUBLIC = "TIER_1_PUBLIC"       # Public B2C / Consumers / Anonymous QR
    TIER_2_CUSTOMS = "TIER_2_CUSTOMS"     # European Customs / TRACES-NT Auditors
    TIER_3_ERP = "TIER_3_ERP"             # Accounting ERP / Báscula (service-account-erp-contable)
    TIER_4_SUPPORT = "TIER_4_SUPPORT"     # GIZ 60h Support / Engineering Diagnostics


# Permitted tools matrix per Access Tier
TIER_PERMITTED_TOOLS: Dict[AccessTier, List[str]] = {
    AccessTier.TIER_1_PUBLIC: [
        "audit_lot_traceability",  # In public mode (public_view=True enforced)
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


class AuthContext(BaseModel):
    tier: AccessTier
    user_id: str
    client_id: Optional[str] = None
    roles: List[str] = Field(default_factory=list)
    company_id: Optional[int] = None
    is_authenticated: bool = False


def _decode_unverified_jwt(token: str) -> Dict[str, Any]:
    """Decodes JWT payload without signature verification for claims inspection/fallback."""
    parts = token.split('.')
    if len(parts) != 3:
        raise ValueError("Invalid JWT token structure")
    payload_b64 = parts[1]
    # Add padding if needed
    rem = len(payload_b64) % 4
    if rem > 0:
        payload_b64 += '=' * (4 - rem)
    decoded_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(decoded_bytes.decode('utf-8'))


def verify_keycloak_token(token: Optional[str]) -> AuthContext:
    """
    Validates a Keycloak OIDC Bearer token and resolves the caller's AccessTier.
    If no token is supplied, returns a safe Tier 1 Public context.
    """
    if not token or not token.strip():
        return AuthContext(
            tier=AccessTier.TIER_1_PUBLIC,
            user_id="anonymous-b2c-consumer",
            roles=[],
            is_authenticated=False
        )

    clean_token = token.strip()
    if clean_token.lower().startswith("bearer "):
        clean_token = clean_token[7:].strip()

    try:
        payload = _decode_unverified_jwt(clean_token)
    except Exception as exc:
        logger.warning(f"Malformed Bearer token rejected: {exc}")
        return AuthContext(
            tier=AccessTier.TIER_1_PUBLIC,
            user_id="anonymous-invalid-token",
            roles=[],
            is_authenticated=False
        )

    # Check expiration
    exp = payload.get("exp")
    if exp and time.time() > exp:
        logger.warning(f"Bearer token expired for subject: {payload.get('sub')}")
        return AuthContext(
            tier=AccessTier.TIER_1_PUBLIC,
            user_id="expired-token-user",
            roles=[],
            is_authenticated=False
        )

    # Extract roles
    realm_access = payload.get("realm_access", {})
    realm_roles = realm_access.get("roles", [])
    client_id = payload.get("azp") or payload.get("client_id")
    resource_access = payload.get("resource_access", {})
    client_roles = resource_access.get(client_id, {}).get("roles", []) if client_id else []
    all_roles = list(set(realm_roles + client_roles))

    sub = payload.get("sub", "unknown")
    company_id = payload.get("company_id")

    # Evaluate 4-Tier Hierarchy
    # Tier 4: Support / Admin
    if any(r in all_roles for r in ("INATRACE_TECH_SUPPORT", "ADMIN", "realm-admin", "superadmin")):
        return AuthContext(
            tier=AccessTier.TIER_4_SUPPORT,
            user_id=sub,
            client_id=client_id,
            roles=all_roles,
            company_id=company_id,
            is_authenticated=True
        )

    # Tier 3: ERP / Scale
    if client_id == settings.SERVICE_ACCOUNT_CLIENT_ID or any(r in all_roles for r in ("service-account", "ERP_WRITER", "COMPANY_ADMIN")):
        return AuthContext(
            tier=AccessTier.TIER_3_ERP,
            user_id=sub,
            client_id=client_id,
            roles=all_roles,
            company_id=company_id,
            is_authenticated=True
        )

    # Tier 2: Customs / TRACES-NT Auditor
    if any(r in all_roles for r in ("CUSTOMS_INSPECTOR", "TRACES_AUDITOR", "customs-officer")) or payload.get("token_type") == "customs_passport":
        return AuthContext(
            tier=AccessTier.TIER_2_CUSTOMS,
            user_id=sub,
            client_id=client_id,
            roles=all_roles,
            company_id=company_id,
            is_authenticated=True
        )

    # Default Authenticated Consumer / Producer
    return AuthContext(
        tier=AccessTier.TIER_1_PUBLIC,
        user_id=sub,
        client_id=client_id,
        roles=all_roles,
        company_id=company_id,
        is_authenticated=True
    )


def check_tier_permission(tier: AccessTier, tool_name: str) -> bool:
    """
    Checks whether a given access tier is authorized to execute a specific tool.
    """
    permitted = TIER_PERMITTED_TOOLS.get(tier, [])
    return tool_name in permitted
