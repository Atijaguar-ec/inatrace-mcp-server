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


class SpringCoreClient:
    """Client for INATrace Spring Boot Core REST API."""

    def __init__(self):
        self.base_url = settings.CORE_API_URL.rstrip('/')
        self.keycloak_url = settings.KEYCLOAK_URL.rstrip('/')
        self.realm = settings.KEYCLOAK_REALM
        self.client_id = settings.SERVICE_ACCOUNT_CLIENT_ID
        self.client_secret = settings.SERVICE_ACCOUNT_CLIENT_SECRET
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    async def get_service_token(self) -> str:
        """
        Retrieves or refreshes an OAuth2 Bearer token from Keycloak using client credentials.
        """
        now = time.time()
        if self._cached_token and now < (self._token_expires_at - 60):
            return self._cached_token

        token_url = f"{self.keycloak_url}/realms/{self.realm}/protocol/openid-connect/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(token_url, data=data)
                if resp.status_code == 200:
                    payload = resp.json()
                    self._cached_token = payload["access_token"]
                    expires_in = payload.get("expires_in", 300)
                    self._token_expires_at = now + expires_in
                    logger.info("Successfully acquired Keycloak service account token.")
                    return self._cached_token
                else:
                    logger.warning(
                        f"Keycloak token endpoint returned {resp.status_code}: {resp.text}"
                    )
                    return "mock-service-token-dev"
        except Exception as exc:
            logger.warning(f"Unable to reach Keycloak at {token_url}: {exc}. Using fallback token.")
            return "mock-service-token-dev"

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
