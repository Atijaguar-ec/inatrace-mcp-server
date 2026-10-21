"""
Configuración del Servidor MCP INATrace mediante Pydantic Settings / Environment.
Carga variables de entorno locales o inyectadas por docker-compose (org.env).
"""
import os
from typing import Optional
from pydantic import Field

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    USE_PYDANTIC_SETTINGS = True
except ImportError:
    from pydantic import BaseModel as BaseSettings
    SettingsConfigDict = None
    USE_PYDANTIC_SETTINGS = False


class Settings(BaseSettings):
    if USE_PYDANTIC_SETTINGS:
        model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Servidor y Transporte
    SERVER_NAME: str = os.getenv("SERVER_NAME", "inatrace-oracle")
    PORT: int = int(os.getenv("PORT", "8000"))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

    # Transporte HTTP (Starlette / Uvicorn)
    # MAX_REQUEST_BYTES acota la memoria que puede consumir una sola petición;
    # los cuerpos JSON-RPC legítimos de este servidor son de pocos kilobytes.
    MAX_REQUEST_BYTES: int = int(os.getenv("MAX_REQUEST_BYTES", str(1024 * 1024)))
    BODY_READ_TIMEOUT_SECONDS: float = float(os.getenv("BODY_READ_TIMEOUT_SECONDS", "15.0"))
    TOOL_TIMEOUT_SECONDS: float = float(os.getenv("TOOL_TIMEOUT_SECONDS", "30.0"))
    KEEP_ALIVE_TIMEOUT_SECONDS: int = int(os.getenv("KEEP_ALIVE_TIMEOUT_SECONDS", "5"))

    # Base de Datos PostgreSQL 16 (Modo Solo Lectura)
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_NAME: str = os.getenv("DB_NAME", "inatrace_unocace_stage")
    DB_USER: str = os.getenv("DB_USER", "inatrace_mcp_ro")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "ro_password_secure")
    DB_POOL_MIN: int = int(os.getenv("DB_POOL_MIN", "2"))
    DB_POOL_MAX: int = int(os.getenv("DB_POOL_MAX", "10"))

    # Backend Core INATrace (Spring Boot) para Escritura Delegada (ERP)
    CORE_API_URL: str = os.getenv("CORE_API_URL", "http://localhost:8080/api")

    # Autenticación Keycloak
    KEYCLOAK_URL: str = os.getenv("KEYCLOAK_URL", "https://auth.unocace.com")
    KEYCLOAK_REALM: str = os.getenv("KEYCLOAK_REALM", "unocace")
    SERVICE_ACCOUNT_CLIENT_ID: str = os.getenv("SERVICE_ACCOUNT_CLIENT_ID", "service-account-erp-contable")
    SERVICE_ACCOUNT_CLIENT_SECRET: str = os.getenv("SERVICE_ACCOUNT_CLIENT_SECRET", "")

    # Verificación de firma RS256 contra el JWKS del realm.
    # KEYCLOAK_AUDIENCE acepta una lista separada por comas; sin valor, ningún
    # token se considera válido (fallo cerrado, no verificación relajada).
    KEYCLOAK_AUDIENCE: str = os.getenv("KEYCLOAK_AUDIENCE", "inatrace-mcp")

    # El issuer que firma los tokens y la URL desde la que se descarga el JWKS no
    # siempre coinciden: en un despliegue tras proxy inverso, Keycloak emite `iss`
    # con su URL pública mientras el servidor sólo lo alcanza por la red interna.
    # Vacíos, ambos se derivan de KEYCLOAK_URL y el comportamiento es el de antes.
    KEYCLOAK_ISSUER: str = os.getenv("KEYCLOAK_ISSUER", "")
    KEYCLOAK_JWKS_URL: str = os.getenv("KEYCLOAK_JWKS_URL", "")
    KEYCLOAK_JWKS_TTL_SECONDS: int = int(os.getenv("KEYCLOAK_JWKS_TTL_SECONDS", "600"))
    KEYCLOAK_JWKS_MIN_REFRESH_SECONDS: int = int(os.getenv("KEYCLOAK_JWKS_MIN_REFRESH_SECONDS", "30"))
    KEYCLOAK_JWKS_TIMEOUT_SECONDS: float = float(os.getenv("KEYCLOAK_JWKS_TIMEOUT_SECONDS", "5.0"))


settings = Settings()
