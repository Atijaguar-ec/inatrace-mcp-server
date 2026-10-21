"""
Tenant scope propagation for the duration of a single tool call.

A tool receives its arguments, not the caller's identity, so the owning
organization of a row read from the database cannot be checked at the dispatcher.
This module carries the authenticated caller's tenant scope through the async call
chain (contextvars, so concurrent requests never see each other's scope) and gives
the tool layer `assert_tenant_access()` to check it at the point where the owning
company_id becomes known.

Deliberately free of Keycloak imports: keycloak_guard builds the scope, tools only
read it.
"""
import logging
from dataclasses import dataclass
from contextvars import ContextVar, Token
from typing import Optional

logger = logging.getLogger("inatrace-mcp.tenant")


@dataclass(frozen=True)
class TenantScope:
    """The authenticated caller's tenant boundary for one tool call."""
    tier: str
    user_id: str
    company_id: Optional[int] = None
    cross_tenant_allowed: bool = False


_scope: ContextVar[Optional[TenantScope]] = ContextVar("inatrace_tenant_scope", default=None)


def set_tenant_scope(scope: Optional[TenantScope]) -> Token:
    """Installs the scope for the current task. Keep the token and reset it in a finally block."""
    return _scope.set(scope)


def reset_tenant_scope(token: Token) -> None:
    _scope.reset(token)


def current_tenant_scope() -> Optional[TenantScope]:
    return _scope.get()


def assert_tenant_access(company_id: Optional[int], resource: str = "recurso") -> None:
    """
    Verifies that the resolved owner of a resource is inside the caller's tenant boundary.

    Raises PermissionError on a cross-tenant read. No scope installed means the call
    did not come through an authorizing dispatcher (direct call, or a unit test), so
    nothing is enforced; the dispatcher is what guarantees a scope is present.
    """
    scope = _scope.get()
    if scope is None or scope.cross_tenant_allowed:
        return
    if scope.company_id is None or company_id is None:
        return
    if int(company_id) != int(scope.company_id):
        logger.warning(
            f"Acceso multi-tenant denegado: '{scope.user_id}' (tier {scope.tier}, "
            f"organización {scope.company_id}) intentó leer {resource} de la "
            f"organización {company_id}."
        )
        raise PermissionError(
            f"Access denied: {resource} pertenece a la organización {company_id} y "
            f"el token está limitado a la organización {scope.company_id}."
        )
