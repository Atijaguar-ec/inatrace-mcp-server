"""
ERP and Weighing Scale Interoperability Tools (CQRS Architecture):
- ingest_erp_purchase_order: Ingests weighing transactions converted to pounds delegating to Spring Boot.
- sync_farmer_profile: Synchronizes farmer master data delegating to Spring Boot.
- verify_sri_compliance: Audits SRI Modulo 11 access keys and canonical comment reconciliation.
"""
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from src.infrastructure.spring_client import spring_client
from src.infrastructure.db_pool import require_db_pool
from src.domain.sri_validator import (
    validate_sri_access_key,
    parse_canonical_sri_comment,
    extract_liquidation_code_from_sri_key
)



class IngestErpPurchaseInput(BaseModel):
    company_id: int = Field(..., description="ID de la empresa (ej. 5 Fortaleza del Valle, 1 UNOCACE)")
    farmer_id: int = Field(..., description="ID del socio UserCustomer")
    parcel_name: str = Field(..., min_length=1, description="Nombre exacto de la parcela en INATrace (ej. 'Finca San Antonio')")
    gross_weight_kg: float = Field(..., gt=0, description="Peso bruto registrado en báscula en kilogramos")
    tare_kg: float = Field(default=0.0, ge=0, description="Tara en kilogramos")
    sri_access_key: str = Field(..., min_length=49, max_length=49, description="Clave de acceso de 49 dígitos del SRI")
    erp_internal_id: str = Field(..., description="ID interno del pesaje en el ERP contable")


class SyncFarmerProfileInput(BaseModel):
    company_id: int = Field(..., description="ID de la empresa (ej. 5)")
    farmer_cedula: str = Field(..., min_length=10, max_length=13, description="Cédula de 10 dígitos o RUC de 13 dígitos")
    first_name: str = Field(..., min_length=1, description="Nombres del socio agricultor")
    last_name: str = Field(..., min_length=1, description="Apellidos del socio agricultor")
    phone: str = Field(default="", description="Número de teléfono móvil de contacto")
    canton_dpa_code: str = Field(default="120550", description="Código DPA INEC de 6 dígitos del cantón")
    has_smartphone: bool = Field(default=False, description="Dispone de teléfono inteligente")


class VerifySriInput(BaseModel):
    stock_order_id: int = Field(..., description="ID de base de datos del StockOrder a auditar")


async def execute_ingest_erp_purchase_order(params: IngestErpPurchaseInput) -> Dict[str, Any]:
    """
    Ingests a weigh-in purchase order:
    1. Validates the 49-digit SRI access key Modulo 11 check digit.
    2. Converts weight from kg to canonical pounds (PESOLIBRA = round(kg * 2.20462, 2)).
    3. Builds the canonical reconciliation comment.
    4. Delegates write to Spring Boot Core API (PUT /api/chain/stock-order).
    """
    # Step 1: Validate SRI key with Modulo 11
    if not validate_sri_access_key(params.sri_access_key):
        return {
            "status": "VALIDATION_ERROR",
            "error_code": "SRI_MODULO_11_INVALID",
            "message": f"La clave de acceso del SRI '{params.sri_access_key}' no cumple el algoritmo de Módulo 11.",
            "is_valid": False
        }

    # Step 2: Weight conversion to pounds (canonic unit for Cacao en Baba)
    net_weight_kg = params.gross_weight_kg - params.tare_kg
    if net_weight_kg <= 0:
        return {
            "status": "VALIDATION_ERROR",
            "error_code": "INVALID_NET_WEIGHT",
            "message": "El peso neto debe ser mayor a cero."
        }

    net_weight_lbs = round(net_weight_kg * 2.20462, 2)
    tare_lbs = round(params.tare_kg * 2.20462, 2)
    gross_weight_lbs = round(params.gross_weight_kg * 2.20462, 2)

    # Step 3: Canonical reconciliation comment
    lc = extract_liquidation_code_from_sri_key(params.sri_access_key) or "001-001-000000001"
    sri_comment = f"LC:{lc}|CA:{params.sri_access_key}|ERP:{params.erp_internal_id}"

    # Step 4: Spring Boot payload
    payload = {
        "companyId": params.company_id,
        "producerUserCustomerId": params.farmer_id,
        "parcelLot": params.parcel_name,
        "totalGrossQuantity": gross_weight_lbs,
        "tare": tare_lbs,
        "totalQuantity": net_weight_lbs,
        "fulfilledQuantity": net_weight_lbs,
        "comments": sri_comment,
        "measurementUnitType": "PESOLIBRA"
    }

    resp = await spring_client.put_stock_order(payload)

    if resp.get("status") != "SUCCESS":
        return {
            "status": "REJECTED_BY_CORE",
            "action": "DELEGATION_FAILED",
            "farmer_id": params.farmer_id,
            "parcel_lot": params.parcel_name,
            "net_weight_kg": round(net_weight_kg, 2),
            "net_weight_lbs": net_weight_lbs,
            "sri_access_key": params.sri_access_key,
            "sri_comment": sri_comment,
            "sri_modulo11_valid": True,
            "core_response": resp
        }

    return {
        "status": "ACCEPTED",
        "action": "DELEGATED_TO_CORE_API",
        "farmer_id": params.farmer_id,
        "parcel_lot": params.parcel_name,
        "net_weight_kg": round(net_weight_kg, 2),
        "net_weight_lbs": net_weight_lbs,
        "sri_access_key": params.sri_access_key,
        "sri_comment": sri_comment,
        "sri_modulo11_valid": True,
        "core_response": resp
    }


async def execute_sync_farmer_profile(params: SyncFarmerProfileInput) -> Dict[str, Any]:
    """
    Synchronizes farmer catalog from ERP to INATrace delegating to Spring Boot.
    """
    payload = {
        "companyId": params.company_id,
        "farmerCompanyInternalId": params.farmer_cedula,
        "name": params.first_name,
        "surname": params.last_name,
        "phone": params.phone,
        "hasSmartphone": params.has_smartphone,
        "type": "FARMER",
        "status": "ACTIVE",
        "dpaCode": str(params.canton_dpa_code).zfill(6)
    }

    resp = await spring_client.post_user_customer(payload)

    if resp.get("status") != "SUCCESS":
        return {
            "status": "REJECTED_BY_CORE",
            "action": "DELEGATION_FAILED",
            "company_id": params.company_id,
            "farmer_name": f"{params.first_name} {params.last_name}",
            "cedula": params.farmer_cedula,
            "core_response": resp
        }

    return {
        "status": "ACCEPTED",
        "action": "DELEGATED_TO_CORE_API",
        "company_id": params.company_id,
        "farmer_name": f"{params.first_name} {params.last_name}",
        "cedula": params.farmer_cedula,
        "core_response": resp
    }


async def execute_verify_sri_compliance(params: VerifySriInput) -> Dict[str, Any]:
    """
    Audits an existing StockOrder comments field for SRI compliance and idempotency.
    """
    pool = require_db_pool("verify_sri_compliance")

    async with pool.acquire() as conn:
        comments = await conn.fetchval(
            "SELECT comments FROM stockorder WHERE id = $1",
            params.stock_order_id
        )

    if not comments:
        return {
            "stock_order_id": params.stock_order_id,
            "sri_status": "NON_COMPLIANT",
            "message": "La compra no registra el campo comments con la clave del SRI",
            "is_valid": False
        }

    parsed = parse_canonical_sri_comment(comments)
    if not parsed:
        return {
            "stock_order_id": params.stock_order_id,
            "sri_status": "INVALID_FORMAT",
            "raw_comments": comments,
            "expected_pattern": "LC:{estab}-{pto}-{secuencial}|CA:{clave_acceso_49d}|ERP:{id_interno_erp}",
            "is_valid": False
        }

    return {
        "stock_order_id": params.stock_order_id,
        "sri_status": "COMPLIANT" if parsed.is_valid_modulo11 else "CHECKSUM_FAILED",
        "liquidation_code": parsed.liquidation_code,
        "access_key": parsed.access_key,
        "erp_internal_id": parsed.erp_internal_id,
        "is_valid_modulo11": parsed.is_valid_modulo11,
        "fiscal_deduction_eligible": parsed.is_valid_modulo11
    }
