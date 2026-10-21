"""
Spring Boot Core API Client for Delegated Transactional Writes (CQRS Architecture).
Handles Keycloak Client Credentials authentication and proxies writes to Spring Boot
preserving Hibernate Envers audit trails and annual quota validations.
"""
import logging
import time
from typing import Dict, Any, Optional
import httpx
from src.config import settings

logger = logging.getLogger("inatrace-mcp.spring")


class ServiceTokenUnavailableError(RuntimeError):
    """
    Raised when no Keycloak service-account token can be obtained.

    The write path must fail closed: delegating a transactional write to the Core
    with an invalid bearer masks a misconfiguration behind what looks like a
    business rejection, and leaves the operator guessing.
    """

    def __init__(self, reason: str):
        super().__init__(
            f"No se pudo obtener el token de la cuenta de servicio de Keycloak: {reason}. "
            f"No se delega ninguna escritura al Core sin credencial válida."
        )
        self.reason = reason


class SpringCoreClient:
    """Client for INATrace Spring Boot Core REST API."""

    def __init__(self):
        self.base_url = settings.CORE_API_URL.rstrip('/')
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def keycloak_url(self) -> str:
        return settings.KEYCLOAK_URL.rstrip('/')

    @property
    def realm(self) -> str:
        return settings.KEYCLOAK_REALM

    @property
    def client_id(self) -> str:
        return settings.SERVICE_ACCOUNT_CLIENT_ID

    async def get_service_token(self) -> str:
        """
        Retrieves or refreshes an OAuth2 Bearer token from Keycloak using client credentials.

        Raises ServiceTokenUnavailableError if Keycloak is unreachable, rejects the
        credentials, or no client secret is configured.
        """
        now = time.time()
        if self._cached_token and now < (self._token_expires_at - 60):
            return self._cached_token

        client_secret = settings.SERVICE_ACCOUNT_CLIENT_SECRET
        if not client_secret:
            logger.error(
                "SERVICE_ACCOUNT_CLIENT_SECRET está vacío: no se puede autenticar la "
                "cuenta de servicio contra Keycloak."
            )
            raise ServiceTokenUnavailableError("SERVICE_ACCOUNT_CLIENT_SECRET no configurado")

        token_url = f"{self.keycloak_url}/realms/{self.realm}/protocol/openid-connect/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": client_secret,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(token_url, data=data)
        except Exception as exc:
            logger.error(f"Keycloak inalcanzable en {token_url}: {exc}")
            raise ServiceTokenUnavailableError(f"Keycloak inalcanzable en {token_url}: {exc}") from exc

        if resp.status_code != 200:
            logger.error(
                f"El endpoint de token de Keycloak devolvió {resp.status_code} para el "
                f"cliente '{self.client_id}'."
            )
            raise ServiceTokenUnavailableError(
                f"Keycloak respondió {resp.status_code} al solicitar el token de "
                f"'{self.client_id}'"
            )

        try:
            payload = resp.json()
            access_token = payload["access_token"]
        except Exception as exc:
            logger.error(f"Respuesta de token de Keycloak ilegible: {exc}")
            raise ServiceTokenUnavailableError(f"respuesta de token ilegible: {exc}") from exc

        self._cached_token = access_token
        self._token_expires_at = now + payload.get("expires_in", 300)
        logger.info("Token de cuenta de servicio de Keycloak obtenido correctamente.")
        return access_token

    async def put_stock_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Delegates purchase order ingestion to Spring Boot:
        PUT /api/chain/stock-order
        """
        token = await self.get_service_token()
        url = f"{self.base_url}/chain/stock-order"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.put(url, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    return {"status": "SUCCESS", "data": resp.json()}
                return {
                    "status": "ERROR",
                    "code": resp.status_code,
                    "message": resp.text
                }
        except Exception as exc:
            logger.error(f"Error calling Spring Boot {url}: {exc}")
            return {
                "status": "CONNECTION_FAILED",
                "message": f"Could not reach Spring Boot Core API: {str(exc)}"
            }

    async def post_user_customer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Delegates farmer registration to Spring Boot:
        POST /api/company/userCustomers
        """
        token = await self.get_service_token()
        url = f"{self.base_url}/company/userCustomers"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    return {"status": "SUCCESS", "data": resp.json()}
                return {
                    "status": "ERROR",
                    "code": resp.status_code,
                    "message": resp.text
                }
        except Exception as exc:
            logger.error(f"Error calling Spring Boot {url}: {exc}")
            return {
                "status": "CONNECTION_FAILED",
                "message": f"Could not reach Spring Boot Core API: {str(exc)}"
            }


spring_client = SpringCoreClient()
