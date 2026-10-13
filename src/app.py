"""
INATrace MCP Server (FastMCP + AsyncPG + Pydantic v2 + Shapely)
Oracle for Retrospective Traceability, EUDR/LOPDP Regulatory Compliance,
and Institutional Scale/ERP Interoperability (Ecuador - European Union).
"""
import sys
import logging
from typing import Dict, Any, Optional

from src.config import settings
from src.infrastructure.db_pool import init_db_pool, close_db_pool
from src.tools.audit_tools import (
    AuditLotInput, VerifyEudrInput,
    execute_audit_lot_traceability, execute_verify_eudr_compliance
)
from src.tools.export_tools import (
    GenerateTracesJsonLdInput, ExportAgrocalidadInput,
    execute_generate_traces_jsonld, execute_export_agrocalidad_guia
)
from src.tools.erp_tools import (
    IngestErpPurchaseInput, SyncFarmerProfileInput, VerifySriInput,
    execute_ingest_erp_purchase_order, execute_sync_farmer_profile, execute_verify_sri_compliance
)
from src.tools.diagnostic_tools import (
    DiagnoseTenantHealthInput, DiagnoseQrInput,
    execute_diagnose_tenant_health, execute_diagnose_and_repair_qr
)
from src.infrastructure.keycloak_guard import (
    verify_keycloak_token, check_tier_permission, AccessTier, AuthContext
)


# Structured Logging according to ISO/IEC 27001
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("inatrace-mcp")

# Unified FastMCP Server with built-in HTTP/SSE & Health endpoints (0.0.0.0:PORT)
try:
    from mcp.server.fastmcp import FastMCP as _SDKFastMCP
    IS_FASTMCP_INSTALLED = True
except ImportError:
    _SDKFastMCP = None
    IS_FASTMCP_INSTALLED = False


class INATraceFastMCP:
    """
    Production-grade FastMCP orchestrator supporting both MCP SDK registration
    and standalone async HTTP/SSE + Healthcheck endpoints on PORT (default 8000).
    """
    def __init__(self, name: str):
        self.name = name
        self.tools: Dict[str, Any] = {}
        self._startup_handler = None
        self._shutdown_handler = None
        self._sdk = _SDKFastMCP(name) if _SDKFastMCP is not None else None

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            if self._sdk is not None:
                try:
                    self._sdk.tool()(fn)
                except Exception:
                    pass
            return fn
        return decorator

    def on_startup(self, fn):
        self._startup_handler = fn
        return fn

    def on_shutdown(self, fn):
        self._shutdown_handler = fn
        return fn

    async def _handle_http_client(self, reader, writer):
        import json as _json
        from urllib.parse import urlparse, parse_qs
        from src.infrastructure.db_pool import get_db_pool

        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            parts = request_line.decode("utf-8", errors="ignore").strip().split(" ")
            method = parts[0] if len(parts) > 0 else "GET"
            raw_path = parts[1] if len(parts) > 1 else "/"
            parsed_url = urlparse(raw_path)
            path = parsed_url.path

            headers: Dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                hline = line.decode("utf-8", errors="ignore").strip()
                if ":" in hline:
                    k, v = hline.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_length = int(headers.get("content-length", "0"))
            body_bytes = await reader.readexactly(content_length) if content_length > 0 else b""

            def _send_json(status_code: int, status_text: str, payload: Dict[str, Any]):
                body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
                resp = (
                    f"HTTP/1.1 {status_code} {status_text}\r\n"
                    f"Content-Type: application/json; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Link: </mcp/sse>; rel=\"modelcontextprotocol\"\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode("utf-8") + body
                writer.write(resp)

            if method == "GET" and path in ("/health/live", "/health"):
                _send_json(200, "OK", {
                    "status": "ALIVE",
                    "server": self.name,
                    "tools_count": len(self.tools),
                    "whisp_source": "plotdeforestationanalysis"
                })
            elif method == "GET" and path == "/health/ready":
                pool = get_db_pool()
                _send_json(200 if pool else 503, "OK" if pool else "Service Unavailable", {
                    "status": "READY" if pool else "DEGRADED",
                    "db_pool_connected": pool is not None,
                    "whisp_table": "plotdeforestationanalysis"
                })
            elif method == "GET" and path in ("/mcp/tools", "/tools"):
                _send_json(200, "OK", {
                    "server": self.name,
                    "tools": [
                        {"name": k, "description": (fn.__doc__ or "").strip()}
                        for k, fn in self.tools.items()
                    ]
                })
            elif method == "GET" and path == "/mcp/sse":
                qs = parse_qs(parsed_url.query)
                lot_id = qs.get("lotId", ["unknown"])[0]
                event_payload = _json.dumps({
                    "protocol": "mcp/1.0",
                    "server": self.name,
                    "lotId": lot_id,
                    "availableTools": list(self.tools.keys())
                })
                sse_body = f"event: endpoint\ndata: {event_payload}\n\n".encode("utf-8")
                resp = (
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: text/event-stream\r\n"
                    f"Cache-Control: no-cache\r\n"
                    f"Link: </mcp/sse?lotId={lot_id}>; rel=\"modelcontextprotocol\"\r\n"
                    f"Content-Length: {len(sse_body)}\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode("utf-8") + sse_body
                writer.write(resp)
            elif method == "POST" and (path.startswith("/mcp/tools/") or path.startswith("/tools/")):
                tool_name = path.rstrip("/").split("/")[-1]
                if tool_name not in self.tools:
                    _send_json(404, "Not Found", {"error": f"Tool '{tool_name}' not found"})
                else:
                    auth_header = headers.get("authorization")
                    try:
                        if auth_header:
                            authorize_request(auth_header, tool_name)
                        params = _json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                        result = await self.tools[tool_name](**params)
                        _send_json(200, "OK", result)
                    except PermissionError as pe:
                        _send_json(403, "Forbidden", {"error": "FORBIDDEN", "message": str(pe)})
                    except Exception as ex:
                        _send_json(400, "Bad Request", {"error": "EXECUTION_ERROR", "message": str(ex)})
            else:
                _send_json(404, "Not Found", {"error": "NOT_FOUND", "path": path})

            await writer.drain()
        except Exception as exc:
            logger.error(f"HTTP handler error: {exc}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def serve_forever(self):
        import asyncio
        if self._startup_handler:
            await self._startup_handler()
        server = await asyncio.start_server(self._handle_http_client, "0.0.0.0", settings.PORT)
        logger.info(
            f"INATrace MCP Server '{self.name}' listening on 0.0.0.0:{settings.PORT} "
            f"with {len(self.tools)} canonical tools."
        )
        try:
            async with server:
                await server.serve_forever()
        finally:
            if self._shutdown_handler:
                await self._shutdown_handler()

    def run(self):
        import asyncio
        try:
            asyncio.run(self.serve_forever())
        except KeyboardInterrupt:
            logger.info("Server stopped by signal.")


mcp = INATraceFastMCP(settings.SERVER_NAME)


def authorize_request(bearer_token: Optional[str], tool_name: str) -> AuthContext:
    """
    Enforces ISO/IEC 27001 Multi-Tier Governance and Authorization.
    Returns AuthContext or raises PermissionError if access tier is insufficient.
    """
    auth_ctx = verify_keycloak_token(bearer_token)
    if not check_tier_permission(auth_ctx.tier, tool_name):
        raise PermissionError(
            f"Access denied: Tool '{tool_name}' requires higher privileges than tier '{auth_ctx.tier.value}'."
        )
    return auth_ctx



# --- LIFECYCLE HOOKS ---
@mcp.on_startup
async def startup():
    logger.info("Initializing INATrace MCP Server...")
    await init_db_pool()
    logger.info("Server startup complete. Ready for tool requests.")


@mcp.on_shutdown
async def shutdown():
    logger.info("Shutting down INATrace MCP Server...")
    await close_db_pool()
    logger.info("Server shutdown complete.")


# --- CANONICAL TOOLS REGISTRATION ---

# Tool 1: audit_lot_traceability
@mcp.tool()
async def audit_lot_traceability(
    lot_identifier: str,
    company_id: int,
    include_plots: bool = True,
    public_view: bool = False
) -> Dict[str, Any]:
    """
    Reconstruye retrospectivamente el Grafo DAG completo desde el saco de 69kg
    a través de fermentación y secado hasta cada parcela física y productor.
    Si public_view=True, aplica el filtro de minimización LOPDP eliminando toda PII.
    """
    params = AuditLotInput(
        lot_identifier=lot_identifier,
        company_id=company_id,
        include_plots=include_plots,
        public_view=public_view
    )
    return await execute_audit_lot_traceability(params)


# Tool 2: verify_eudr_compliance
@mcp.tool()
async def verify_eudr_compliance(
    stock_order_id: int,
    strict_polygon_closure: bool = True
) -> Dict[str, Any]:
    """
    Audita el cumplimiento del Reglamento de Deforestación de la UE (EUDR 2023/1115):
    - Cierre de anillo topológico WGS84 para predios > 4.0 ha.
    - Precisión de centroide (>= 6 decimales) para predios <= 4.0 ha.
    - Dictamen de corte de no deforestación posterior al 31/12/2020.
    """
    params = VerifyEudrInput(
        stock_order_id=stock_order_id,
        strict_polygon_closure=strict_polygon_closure
    )
    return await execute_verify_eudr_compliance(params)


# Tool 3: generate_traces_jsonld
@mcp.tool()
async def generate_traces_jsonld(
    stock_order_id: int,
    operator_ruc: str,
    issuing_authority: str = "AGROCALIDAD-EC",
    operator_name: str = "UNOCACE"
) -> Dict[str, Any]:
    """
    Genera el payload semántico oficial en estándar JSON-LD según el esquema de la
    Comisión Europea (DG SANTE / TRACES-NT) para la Declaración de Debida Diligencia (DDS).
    Incorpora códigos territoriales DPA INEC de 6 dígitos con zero-padding estricto.
    """
    params = GenerateTracesJsonLdInput(
        stock_order_id=stock_order_id,
        operator_ruc=operator_ruc,
        issuing_authority=issuing_authority,
        operator_name=operator_name
    )
    return await execute_generate_traces_jsonld(params)


# Tool 4: export_agrocalidad_guia
@mcp.tool()
async def export_agrocalidad_guia(
    company_id: int,
    date_from: str,
    date_to: str
) -> Dict[str, Any]:
    """
    Emite el reporte regulatorio oficial para el sistema GUIA de Agrocalidad
    en formato CSV delimitado por punto y coma (;) con codificación UTF-8 BOM.
    """
    params = ExportAgrocalidadInput(
        company_id=company_id,
        date_from=date_from,
        date_to=date_to
    )
    return await execute_export_agrocalidad_guia(params)


# Tool 5: verify_sri_compliance
@mcp.tool()
async def verify_sri_compliance(
    stock_order_id: int
) -> Dict[str, Any]:
    """
    Audita la clave de acceso de 49 dígitos del SRI en el campo comments de la compra,
    validando el algoritmo matemático de Módulo 11 para deducibilidad tributaria.
    """
    params = VerifySriInput(stock_order_id=stock_order_id)
    return await execute_verify_sri_compliance(params)


# Tool 6: ingest_erp_purchase_order
@mcp.tool()
async def ingest_erp_purchase_order(
    company_id: int,
    farmer_id: int,
    parcel_name: str,
    gross_weight_kg: float,
    tare_kg: float,
    sri_access_key: str,
    erp_internal_id: str
) -> Dict[str, Any]:
    """
    Ingiere de forma segura una liquidación de compra de báscula/ERP contable:
    - Valida el Módulo 11 de la clave de acceso del SRI.
    - Convierte el peso neto de kg a Libras (PESOLIBRA = round(kg * 2.20462, 2)).
    - Delega la escritura transaccional al Core API de Spring Boot autenticado con Keycloak.
    """
    params = IngestErpPurchaseInput(
        company_id=company_id,
        farmer_id=farmer_id,
        parcel_name=parcel_name,
        gross_weight_kg=gross_weight_kg,
        tare_kg=tare_kg,
        sri_access_key=sri_access_key,
        erp_internal_id=erp_internal_id
    )
    return await execute_ingest_erp_purchase_order(params)


# Tool 7: sync_farmer_profile
@mcp.tool()
async def sync_farmer_profile(
    company_id: int,
    farmer_cedula: str,
    first_name: str,
    last_name: str,
    phone: str = "",
    canton_dpa_code: str = "120550",
    has_smartphone: bool = False
) -> Dict[str, Any]:
    """
    Sincroniza el catálogo maestro de socios agricultores desde el ERP hacia INATrace,
    delegando en el endpoint oficial de Spring Boot con control de consentimiento LOPDP.
    """
    params = SyncFarmerProfileInput(
        company_id=company_id,
        farmer_cedula=farmer_cedula,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        canton_dpa_code=canton_dpa_code,
        has_smartphone=has_smartphone
    )
    return await execute_sync_farmer_profile(params)


# Tool 8: diagnose_tenant_health
@mcp.tool()
async def diagnose_tenant_health(
    company_id: int
) -> Dict[str, Any]:
    """
    Scanner forense automático que audita la base de datos de la organización
    en busca de anomalías documentadas en los Post-Mortems de producción:
    - PM-033: Pérdida del rol OWNER en productcompany.
    - PM-036: Semiproducto de baba en acción GENERATE_QR_CODE.
    - PM-037: Cláusulas ORDER BY en conteos paginados Torpedo.
    - PM-040: Códigos DPA INEC truncados sin zero-padding.
    - Banderas de configuración multi-tenant.
    """
    params = DiagnoseTenantHealthInput(company_id=company_id)
    return await execute_diagnose_tenant_health(params)


# Tool 9: diagnose_and_repair_qr
@mcp.tool()
async def diagnose_and_repair_qr(
    qr_payload: str,
    company_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Oráculo de diagnóstico ante fallos en escaneo de códigos QR en puerto o báscula:
    Diferencia entre 403 Forbidden (falta bandera de acceso público) y 404 Not Found,
    e indica la acción correctiva inmediata.
    """
    params = DiagnoseQrInput(qr_payload=qr_payload, company_id=company_id)
    return await execute_diagnose_and_repair_qr(params)


if __name__ == "__main__":
    mcp.run()
