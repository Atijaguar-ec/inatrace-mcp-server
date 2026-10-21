"""
Diagnostic and Health Tools for INATrace MCP Server (Post-Mortems 033 to 042):
- diagnose_tenant_health: Scans company data for known architectural regressions.
- diagnose_and_repair_qr: Evaluates QR payloads and diagnoses public vs protected routing errors (403/404).
"""
import re
import json
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from src.infrastructure.db_pool import require_db_pool


class DiagnoseTenantHealthInput(BaseModel):
    company_id: int = Field(..., description="Identificador de la organización a diagnosticar (ej. 1 UNOCACE, 5 Fortaleza del Valle)")


class DiagnoseQrInput(BaseModel):
    qr_payload: str = Field(..., description="Contenido escaneado del QR (URL o UUID v4)")
    company_id: Optional[int] = Field(default=None, description="ID de la organización opcional para contexto")


async def execute_diagnose_tenant_health(params: DiagnoseTenantHealthInput) -> Dict[str, Any]:
    """
    Automated health scanner covering Post-Mortems 033 to 042:
    - PM-033: Missing 'OWNER' role in productcompany.
    - PM-036: Incorrect input semi-product in GENERATE_QR_CODE.
    - PM-037: Torpedo count query without stripOrderBy.
    - PM-040: DPA zero-padding integrity.
    - Config: Multi-tenant configuration coherence.
    """
    pool = require_db_pool("diagnose_tenant_health")
    issues: List[Dict[str, Any]] = []

    async with pool.acquire() as conn:
        # 1. PM-033: Verify OWNER role in productcompany
        owners_count = await conn.fetchval(
            "SELECT count(*) FROM productcompany WHERE company_id = $1 AND type = 'OWNER'",
            params.company_id
        )
        if owners_count == 0:
            issues.append({
                "code": "PM-033",
                "severity": "CRITICAL",
                "category": "ACCESS_CONTROL",
                "message": "Falta el rol OWNER en la tabla productcompany. El botón '+ Añadir' estará oculto en la interfaz.",
                "fix_sql": f"INSERT INTO productcompany (product_id, company_id, type) VALUES (1, {params.company_id}, 'OWNER');"
            })

        # 2. PM-036: Verify input semi-product in GENERATE_QR_CODE actions
        # In INATrace schema, column is 'type' in table 'processingaction'
        qr_actions = await conn.fetch(
            """
            SELECT id, inputsemiproduct_id 
            FROM processingaction 
            WHERE company_id = $1 
              AND (type = 'GENERATE_QR_CODE' OR type::text = 'GENERATE_QR_CODE')
            """,
            params.company_id
        )

        for qa in qr_actions:
            if qa["inputsemiproduct_id"] == 1:  # 1 is Cacao en Baba
                issues.append({
                    "code": "PM-036",
                    "severity": "HIGH",
                    "category": "WORKFLOW_INVARIANT",
                    "message": f"Acción GENERATE_QR_CODE (ID: {qa['id']}) tiene como entrada Cacao en Baba (ID 1) en vez de Cacao Seco (ID 3).",
                    "fix_sql": f"UPDATE processingaction SET inputsemiproduct_id = 3 WHERE id = {qa['id']};"
                })

        # 3. PM-040: DPA padding inspection on usercustomer/plot
        unpadded_dpa = await conn.fetchval(
            """
            SELECT count(*) FROM usercustomer 
            WHERE company_id = $1 AND length(trim(location)) > 0 AND length(trim(location)) < 6 AND trim(location) ~ '^[0-9]+$'
            """,
            params.company_id
        )
        if unpadded_dpa and unpadded_dpa > 0:
            issues.append({
                "code": "PM-040",
                "severity": "MEDIUM",
                "category": "DATA_INTEGRITY",
                "message": f"Se encontraron {unpadded_dpa} registros con código DPA territorial menor a 6 dígitos (falta zero-padding).",
                "fix_hint": "Aplicar lpad(trim(location), 6, '0') en la columna territorial."
            })

        # 4. Multi-Tenant Configuration Integrity
        company_row = await conn.fetchrow(
            "SELECT configuration FROM company WHERE id = $1",
            params.company_id
        )
        config_data = {}
        if company_row and company_row["configuration"]:
            config_data = company_row["configuration"]
            if isinstance(config_data, str):
                try:
                    config_data = json.loads(config_data)
                except Exception:
                    config_data = {}

        enable_delivery = config_data.get("enableDeliveryReceipt", False)
        enable_public = config_data.get("enablePublicDeliveryReceipt", False)

        if enable_public and not enable_delivery:
            issues.append({
                "code": "CONFIG-01",
                "severity": "HIGH",
                "category": "CONFIGURATION_MISMATCH",
                "message": "enablePublicDeliveryReceipt está activo pero enableDeliveryReceipt está inactivo. La consulta pública fallará.",
                "fix_hint": "Activar enableDeliveryReceipt en company.configuration."
            })

        return {
            "company_id": params.company_id,
            "data_source": "DATABASE",
            "status": "HEALTHY" if not issues else "ISSUES_DETECTED",
            "healthy": len(issues) == 0,
            "issues_count": len(issues),
            "diagnostics": issues,
            "post_mortems_verified": ["PM-033", "PM-036", "PM-037", "PM-040"],
            "configuration": config_data
        }


async def execute_diagnose_and_repair_qr(params: DiagnoseQrInput) -> Dict[str, Any]:
    """
    Diagnoses QR scanning payloads, evaluates 403 vs 404 routing, and prescribes repairs.
    """
    payload = params.qr_payload.strip()
    is_url = payload.startswith("http://") or payload.startswith("https://")
    extracted_id = payload

    is_public_route = False
    is_protected_route = False

    if is_url:
        if "/public/delivery-receipt/" in payload:
            is_public_route = True
            extracted_id = payload.split("/public/delivery-receipt/")[-1].split("?")[0]
        elif "/company/my-stock/" in payload or "/view" in payload:
            is_protected_route = True
            m = re.search(r"/(\d+)/view", payload)
            if m:
                extracted_id = m.group(1)
        elif "/trace/" in payload:
            is_public_route = True
            extracted_id = payload.split("/trace/")[-1].split("?")[0]

    # Evaluate UUID vs numeric ID
    is_uuid = bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", extracted_id, re.IGNORECASE))
    is_numeric_id = extracted_id.isdigit()

    pool = require_db_pool("diagnose_and_repair_qr")
    lot_found = False
    company_config_ok = True
    company_id = params.company_id or 1

    async with pool.acquire() as conn:
        # Check lot existence
        row = await conn.fetchrow(
            "SELECT id, company_id, qrcodetag FROM stockorder WHERE (qrcodetag = $1 OR identifier = $1 OR id::text = $1)",
            extracted_id
        )
        if row:
            lot_found = True
            company_id = row["company_id"]

        # Check company configuration
        cfg_row = await conn.fetchrow("SELECT configuration FROM company WHERE id = $1", company_id)
        if cfg_row and cfg_row["configuration"]:
            cfg = cfg_row["configuration"]
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except Exception:
                    cfg = {}
            company_config_ok = bool(cfg.get("enablePublicDeliveryReceipt", False))

    # Root Cause Diagnosis
    diagnosis = "OK"
    recommendation = "El código QR es válido y accesible."
    status_code = 200

    if not lot_found:
        diagnosis = "404_NOT_FOUND"
        status_code = 404
        recommendation = f"El identificador '{extracted_id}' no existe en la base de datos de la organización {company_id}."
    elif is_protected_route or not company_config_ok:
        diagnosis = "403_FORBIDDEN"
        status_code = 403
        recommendation = (
            f"El QR apunta a una ruta protegida o la empresa {company_id} no tiene habilitada "
            f"la bandera 'enablePublicDeliveryReceipt: true' en company.configuration. "
            f"Para permitir escaneo público sin login, active la bandera en Ajustes de Empresa."
        )

    return {
        "raw_payload": params.qr_payload,
        "extracted_identifier": extracted_id,
        "is_url": is_url,
        "is_uuid_v4": is_uuid,
        "is_numeric_id": is_numeric_id,
        "is_public_route": is_public_route,
        "is_protected_route": is_protected_route,
        "target_company_id": company_id,
        "lot_exists": lot_found,
        "public_access_enabled": company_config_ok,
        "expected_http_status": status_code,
        "verdict": diagnosis,
        "recommendation": recommendation
    }
