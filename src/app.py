"""
INATrace MCP Server (FastMCP + AsyncPG + Pydantic v2 + Shapely)
Oracle for Retrospective Traceability, EUDR/LOPDP Regulatory Compliance,
and Institutional Scale/ERP Interoperability (Ecuador - European Union).
"""
import sys
import logging
from typing import Dict, Any, Optional

from src.config import settings
from src.infrastructure.db_pool import init_db_pool, close_db_pool, DatabaseUnavailableError
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
    verify_keycloak_token, check_tier_permission, enforce_tier_arguments,
    AccessTier, AuthContext
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
    Production-grade FastMCP orchestrator supporting:
    - MCP Tools (@mcp.tool)
    - MCP Resources (@mcp.resource)
    - MCP Prompts (@mcp.prompt)
    - JSON-RPC 2.0 over HTTP (POST /mcp) + REST (/mcp/tools, /mcp/resources, /mcp/prompts) + SSE (/mcp/sse)
    """
    def __init__(self, name: str):
        self.name = name
        self.tools: Dict[str, Any] = {}
        self.resources: Dict[str, Dict[str, Any]] = {}
        self.prompts: Dict[str, Dict[str, Any]] = {}
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

    def resource(self, uri: str, name: Optional[str] = None, description: Optional[str] = None, mime_type: str = "application/json"):
        def decorator(fn):
            self.resources[uri] = {
                "uri": uri,
                "name": name or fn.__name__,
                "description": description or (fn.__doc__ or "").strip(),
                "mimeType": mime_type,
                "handler": fn
            }
            if self._sdk is not None:
                try:
                    self._sdk.resource(uri)(fn)
                except Exception:
                    pass
            return fn
        return decorator

    def prompt(self, name: Optional[str] = None, description: Optional[str] = None):
        def decorator(fn):
            p_name = name or fn.__name__
            self.prompts[p_name] = {
                "name": p_name,
                "description": description or (fn.__doc__ or "").strip(),
                "handler": fn
            }
            if self._sdk is not None and hasattr(self._sdk, "prompt"):
                try:
                    self._sdk.prompt()(fn)
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

    async def handle_jsonrpc_request(
        self,
        rpc_req: Dict[str, Any],
        auth_header: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Standard MCP JSON-RPC 2.0 dispatcher supporting initialize, tools/list,
        tools/call, resources/list, resources/read, prompts/list, prompts/get.
        """
        import inspect
        import json as _json

        req_id = rpc_req.get("id")
        method = rpc_req.get("method", "")
        params = rpc_req.get("params") or {}

        def _ok(result_obj: Any) -> Dict[str, Any]:
            return {"jsonrpc": "2.0", "id": req_id, "result": result_obj}

        def _err(code: int, message: str) -> Dict[str, Any]:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

        try:
            if method == "initialize":
                return _ok({
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": self.name, "version": "2.0.0"},
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False}
                    }
                })
            elif method == "tools/list":
                return _ok({
                    "tools": [
                        {"name": k, "description": (fn.__doc__ or "").strip()}
                        for k, fn in self.tools.items()
                    ]
                })
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                if tool_name not in self.tools:
                    return _err(-32601, f"Tool '{tool_name}' not found")
                # Authorization is unconditional: a missing header resolves to
                # TIER_1_PUBLIC, it does not skip the check.
                auth_ctx = authorize_request(auth_header, tool_name)
                arguments, _ = enforce_tier_arguments(auth_ctx.tier, tool_name, arguments)
                res = await self.tools[tool_name](**arguments)
                return _ok({
                    "content": [{"type": "text", "text": _json.dumps(res, ensure_ascii=False)}],
                    "structuredContent": res,
                    "isError": False
                })
            elif method == "resources/list":
                return _ok({
                    "resources": [
                        {
                            "uri": r["uri"],
                            "name": r["name"],
                            "description": r["description"],
                            "mimeType": r["mimeType"]
                        }
                        for r in self.resources.values()
                    ]
                })
            elif method == "resources/read":
                uri = params.get("uri")
                if uri not in self.resources:
                    return _err(-32602, f"Resource '{uri}' not found")
                r_meta = self.resources[uri]
                fn = r_meta["handler"]
                val = await fn() if inspect.iscoroutinefunction(fn) else fn()
                text_val = _json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
                return _ok({
                    "contents": [{
                        "uri": uri,
                        "mimeType": r_meta["mimeType"],
                        "text": text_val
                    }]
                })
            elif method == "prompts/list":
                return _ok({
                    "prompts": [
                        {"name": p["name"], "description": p["description"]}
                        for p in self.prompts.values()
                    ]
                })
            elif method == "prompts/get":
                p_name = params.get("name")
                p_args = params.get("arguments") or {}
                if p_name not in self.prompts:
                    return _err(-32602, f"Prompt '{p_name}' not found")
                fn = self.prompts[p_name]["handler"]
                val = await fn(**p_args) if inspect.iscoroutinefunction(fn) else fn(**p_args)
                return _ok(val)
            else:
                return _err(-32601, f"Method '{method}' not supported")
        except PermissionError as pe:
            return _err(-32001, f"FORBIDDEN: {pe}")
        except DatabaseUnavailableError as due:
            return _err(-32002, f"DATABASE_UNAVAILABLE: {due}")
        except Exception as ex:
            return _err(-32603, f"INTERNAL_ERROR: {ex}")

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
                    "resources_count": len(self.resources),
                    "prompts_count": len(self.prompts),
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
            elif method == "GET" and path in ("/mcp/resources", "/resources"):
                _send_json(200, "OK", {
                    "server": self.name,
                    "resources": [
                        {
                            "uri": r["uri"],
                            "name": r["name"],
                            "description": r["description"],
                            "mimeType": r["mimeType"]
                        }
                        for r in self.resources.values()
                    ]
                })
            elif method == "GET" and path in ("/mcp/prompts", "/prompts"):
                _send_json(200, "OK", {
                    "server": self.name,
                    "prompts": [
                        {"name": p["name"], "description": p["description"]}
                        for p in self.prompts.values()
                    ]
                })
            elif method == "GET" and path == "/mcp/sse":
                qs = parse_qs(parsed_url.query)
                lot_id = qs.get("lotId", ["unknown"])[0]
                event_payload = _json.dumps({
                    "protocol": "mcp/2024-11-05",
                    "server": self.name,
                    "lotId": lot_id,
                    "jsonrpcEndpoint": "/mcp",
                    "availableTools": list(self.tools.keys()),
                    "availableResources": list(self.resources.keys()),
                    "availablePrompts": list(self.prompts.keys())
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
            elif method == "POST" and path in ("/mcp", "/jsonrpc"):
                auth_header = headers.get("authorization")
                rpc_req = _json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                rpc_resp = await self.handle_jsonrpc_request(rpc_req, auth_header=auth_header)
                _send_json(200, "OK", rpc_resp)
            elif method == "POST" and (path.startswith("/mcp/tools/") or path.startswith("/tools/")):
                tool_name = path.rstrip("/").split("/")[-1]
                if tool_name not in self.tools:
                    _send_json(404, "Not Found", {"error": f"Tool '{tool_name}' not found"})
                else:
                    auth_header = headers.get("authorization")
                    try:
                        # Authorization is unconditional: a missing header resolves to
                        # TIER_1_PUBLIC, it does not skip the check.
                        auth_ctx = authorize_request(auth_header, tool_name)
                        params = _json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                        params, _ = enforce_tier_arguments(auth_ctx.tier, tool_name, params)
                        result = await self.tools[tool_name](**params)
                        _send_json(200, "OK", result)
                    except PermissionError as pe:
                        _send_json(403, "Forbidden", {"error": "FORBIDDEN", "message": str(pe)})
                    except DatabaseUnavailableError as due:
                        _send_json(503, "Service Unavailable", {"error": "DATABASE_UNAVAILABLE", "message": str(due)})
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
            f"with {len(self.tools)} tools, {len(self.resources)} resources, and {len(self.prompts)} prompts."
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


# --- CANONICAL MCP RESOURCES ---

@mcp.resource(
    "inatrace://catalogs/dpa-ecuador",
    name="Catálogo Territorial DPA INEC Ecuador",
    description="Códigos territoriales DPA INEC de 6 dígitos con zero-padding mandatorio para zonas cacaoteras."
)
def resource_dpa_ecuador() -> Dict[str, Any]:
    return {
        "standard": "INEC-DPA-ECUADOR-6D",
        "zero_padding_mandatory": True,
        "parishes": [
            {"dpa_code": "120550", "province": "LOS RIOS", "canton": "VINCES", "parish": "VINCES"},
            {"dpa_code": "120551", "province": "LOS RIOS", "canton": "VINCES", "parish": "ANTONIO SOTOMAYOR"},
            {"dpa_code": "130350", "province": "MANABI", "canton": "CHONE", "parish": "CHONE"},
            {"dpa_code": "130352", "province": "MANABI", "canton": "CHONE", "parish": "CANUTO"},
            {"dpa_code": "130250", "province": "MANABI", "canton": "BOLIVAR", "parish": "CALCETA"},
            {"dpa_code": "091250", "province": "GUAYAS", "canton": "NARANJAL", "parish": "NARANJAL"},
            {"dpa_code": "070150", "province": "EL ORO", "canton": "MACHALA", "parish": "MACHALA"}
        ]
    }


@mcp.resource(
    "inatrace://regulations/eudr-2023-1115",
    name="Reglas de Validación EUDR 2023/1115 y MAG Acuerdo 023",
    description="Umbrales regulatorios oficiales para deforestación cero, geometría WGS84 y conversión física de cacao."
)
def resource_eudr_regulations() -> Dict[str, Any]:
    return {
        "regulation": "EU Regulation 2023/1115 (EUDR)",
        "deforestation_cutoff_date": "2020-12-31",
        "geometry_rules": {
            "smallholder_threshold_ha": 4.0,
            "lte_4ha": "CENTROID_WGS84_MIN_6_DECIMALS",
            "gt_4ha": "CLOSED_POLYGON_RING_NO_SELF_INTERSECTION"
        },
        "mass_balance_rules": {
            "ecuador_regulation": "MAG Acuerdo Ministerial 023",
            "quintal_to_lbs": 100.0,
            "quintal_to_kg": 45.359237,
            "export_bag_standard_kg": 69.0,
            "tolerance_ratio": 1.05
        }
    }


@mcp.resource(
    "inatrace://schemas/sri-modulo11",
    name="Especificación Clave de Acceso SRI y Algoritmo Módulo 11",
    description="Estructura canónica de conciliación tributaria SRI de 49 dígitos y comentario idempotente ERP."
)
def resource_sri_schema() -> Dict[str, Any]:
    return {
        "standard": "SRI-ECUADOR-FICHA-TECNICA-COMPROBANTES-ELECTRONICOS",
        "access_key_length": 49,
        "check_digit_algorithm": "MODULO_11_WEIGHTS_2_TO_7",
        "canonical_comment_template": "LC:{estab}-{pto}-{secuencial}|CA:{clave_acceso_49d}|ERP:{id_interno_erp}"
    }


# --- CANONICAL MCP PROMPTS ---

@mcp.prompt(
    name="auditoria_aduanera_eudr",
    description="Secuencia guiada de inspección aduanera pre-arribo para puertos europeos (Róterdam, Hamburgo, Amberes)."
)
def prompt_auditoria_aduanera_eudr(lot_identifier: str = "784", company_id: int = 1) -> Dict[str, Any]:
    return {
        "description": f"Auditoría aduanera EUDR integral para el lote {lot_identifier} (Organización {company_id})",
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Actúa como Inspector Aduanero de la Unión Europea (DG SANTE / CSW-CERTEX). "
                        f"Ejecuta la secuencia de verificación para el lote '{lot_identifier}' de la empresa {company_id}:\n"
                        f"1. Invoca `audit_lot_traceability(lot_identifier='{lot_identifier}', company_id={company_id}, include_raw_coordinates=False)`.\n"
                        f"2. Verifica `mass_balance.status` (ISO 22005 / MAG 023) y `security_audit.prompt_injection_detected`.\n"
                        f"3. Invoca `verify_eudr_compliance` y `verify_sri_compliance` sobre el `stock_order_id` raíz.\n"
                        f"4. Emite el dictamen final de liberación en puerto (Green Channel vs Hold Inspection)."
                    )
                }
            }
        ]
    }


@mcp.prompt(
    name="diagnostico_forense_soporte_giz",
    description="Protocolo de diagnóstico forense de soporte 60h GIZ (Solo Lectura) para incidentes multi-tenant y QR."
)
def prompt_diagnostico_forense_soporte_giz(company_id: int = 1, qr_url: str = "") -> Dict[str, Any]:
    return {
        "description": f"Diagnóstico forense de soporte técnico GIZ para la organización {company_id}",
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Actúa como Ingeniero Forense de Soporte Nivel 3 (Tier 4 GIZ Read-Only). "
                        f"Analiza la salud operacional de la organización {company_id}:\n"
                        f"1. Ejecuta `diagnose_tenant_health(company_id={company_id})` para auditar los Post-Mortems PM-033, PM-036, PM-037 y PM-040.\n"
                        f"2. Si existe URL QR reportada ('{qr_url}'), ejecuta `diagnose_and_repair_qr(qr_payload='{qr_url}', company_id={company_id})`.\n"
                        f"3. Entrega el reporte de causa raíz y el comando exacto de remediación."
                    )
                }
            }
        ]
    }


# --- CANONICAL TOOLS REGISTRATION ---

# Tool 1: audit_lot_traceability
@mcp.tool()
async def audit_lot_traceability(
    lot_identifier: str,
    company_id: int,
    include_plots: bool = True,
    public_view: bool = False,
    include_raw_coordinates: bool = False,
    require_completed_whisp: bool = False
) -> Dict[str, Any]:
    """
    Reconstruye retrospectivamente el Grafo DAG completo desde el saco de 69kg
    a través de fermentación y secado hasta cada parcela física y productor.
    Incluye validación de balance de masas (ISO 22005), sanitización anti-Prompt Injection
    (OWASP LLM01) y filtro de minimización LOPDP si public_view=True.
    """
    params = AuditLotInput(
        lot_identifier=lot_identifier,
        company_id=company_id,
        include_plots=include_plots,
        public_view=public_view,
        include_raw_coordinates=include_raw_coordinates,
        require_completed_whisp=require_completed_whisp
    )
    return await execute_audit_lot_traceability(params)


# Tool 2: verify_eudr_compliance
@mcp.tool()
async def verify_eudr_compliance(
    stock_order_id: int,
    strict_polygon_closure: bool = True,
    include_raw_coordinates: bool = False,
    require_completed_whisp: bool = False
) -> Dict[str, Any]:
    """
    Audita el cumplimiento del Reglamento de Deforestación de la UE (EUDR 2023/1115):
    - Cierre de anillo topológico WGS84 para predios > 4.0 ha.
    - Precisión de centroide (>= 6 decimales) para predios <= 4.0 ha.
    - Dictamen de corte de no deforestación posterior al 31/12/2020 (tri-state Whisp).
    """
    params = VerifyEudrInput(
        stock_order_id=stock_order_id,
        strict_polygon_closure=strict_polygon_closure,
        include_raw_coordinates=include_raw_coordinates,
        require_completed_whisp=require_completed_whisp
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
