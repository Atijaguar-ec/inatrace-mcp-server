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


settings = Settings()
