"""
Audit Tools for INATrace MCP Server:
- audit_lot_traceability: Reconstructs the complete backwards DAG tree.
- verify_eudr_compliance: Audits WGS84 closed rings and deforestation cutoff date.
"""
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from src.infrastructure.db_pool import require_db_pool
from src.infrastructure.auth_scope import assert_tenant_access
from src.domain.dag_engine import build_lot_dag
from src.domain.privacy import apply_privacy_gate


class AuditLotInput(BaseModel):
    lot_identifier: str = Field(..., description="UUID v4 del qrCodeTag, código interno o ID numérico del lote")
    company_id: int = Field(..., description="ID de la organización (ej. 1 UNOCACE, 5 Fortaleza del Valle)")
    include_plots: bool = Field(default=True, description="Indica si se deben resolver las parcelas y productores de origen")
    public_view: bool = Field(default=False, description="Aplica el filtro de minimización LOPDP para visualización pública")
    include_raw_coordinates: bool = Field(
        default=False,
        description="Si es False (default para optimizar ventana de contexto LLM), retorna centroide y bounding_box omitiendo el arreglo crudo de vértices"
    )
    require_completed_whisp: bool = Field(
        default=False,
        description="Si es True, exige registro satelital completado en plotdeforestationanalysis (tri-state estricto)"
    )


class VerifyEudrInput(BaseModel):
    stock_order_id: int = Field(..., description="ID numérico de base de datos del StockOrder exportable")
    strict_polygon_closure: bool = Field(default=True, description="Fuerza validación estricta de polígono cerrado para fincas > 4 ha")
    include_raw_coordinates: bool = Field(
        default=False,
        description="Si es False (default), resume geometrías en centroide + bounding_box para ahorrar tokens LLM"
    )
    require_completed_whisp: bool = Field(
        default=False,
        description="Si es True, marca como PENDING_WHISP_ANALYSIS las parcelas sin fila en plotdeforestationanalysis"
    )


async def execute_audit_lot_traceability(params: AuditLotInput) -> Dict[str, Any]:
    """
    Executes lot traceability audit through the retrospective DAG engine.
    """
    pool = require_db_pool("audit_lot_traceability")
    assert_tenant_access(params.company_id, f"organización {params.company_id}")

    async with pool.acquire() as conn:
        result = await build_lot_dag(
            conn,
            params.lot_identifier,
            params.company_id,
            include_raw_coordinates=params.include_raw_coordinates,
            require_completed_whisp=params.require_completed_whisp
        )

    result["data_source"] = "DATABASE"

    if params.public_view:
        return apply_privacy_gate(result, is_public_channel=True)
    return result


async def execute_verify_eudr_compliance(params: VerifyEudrInput) -> Dict[str, Any]:
    """
    Evaluates a stock order's origin plots against EU Regulation 2023/1115 (EUDR).
    """
    pool = require_db_pool("verify_eudr_compliance")

    async with pool.acquire() as conn:
        so_row = await conn.fetchrow(
            "SELECT company_id FROM stockorder WHERE id = $1",
            params.stock_order_id
        )
        if not so_row:
            # An unknown stock order can never be cleared: fail closed, never green-channel.
            return {
                "stock_order_id": params.stock_order_id,
                "data_source": "DATABASE",
                "eudr_status": "STOCK_ORDER_NOT_FOUND",
                "deforestation_cutoff_verified": "2020-12-31",
                "total_plots_evaluated": 0,
                "customs_verdict": "HOLD_CONTAINER_INSPECTION_REQUIRED",
                "message": f"No existe el StockOrder {params.stock_order_id} en la base de datos.",
                "plots_detail": []
            }
        company_id = so_row["company_id"]
        # The owning organization is only known now: a stock_order_id from another
        # tenant must not be auditable by passing its number.
        assert_tenant_access(company_id, f"StockOrder {params.stock_order_id}")

        dag_result = await build_lot_dag(
            conn,
            str(params.stock_order_id),
            company_id,
            include_raw_coordinates=params.include_raw_coordinates,
            require_completed_whisp=params.require_completed_whisp
        )
        participating_plots = dag_result.get("participating_plots", [])

        plot_results = []
        all_compliant = bool(participating_plots)

        for p in participating_plots:
            deforest_info = p.get("deforestation_analysis", {})
            is_valid = deforest_info.get("is_eudr_compliant", False)
            if not is_valid:
                all_compliant = False

            plot_results.append({
                "plot_id": p.get("plot_id"),
                "plot_name": p.get("plot_name"),
                "size_ha": p.get("size_ha"),
                "geometry": p.get("geometry"),
                "whisp_verification_state": deforest_info.get("whisp_verification_state"),
                "deforestation_pass": is_valid,
                "is_compliant": is_valid
            })

        eudr_status = dag_result.get("eudr_summary", {}).get(
            "overall_status",
            "COMPLIANT" if (all_compliant and plot_results) else "NON_COMPLIANT"
        )
        return {
            "stock_order_id": params.stock_order_id,
            "data_source": "DATABASE",
            "eudr_status": eudr_status,
            "strict_whisp_status": dag_result.get("eudr_summary", {}).get("strict_whisp_status"),
            "deforestation_cutoff_verified": "2020-12-31",
            "total_plots_evaluated": len(plot_results),
            "mass_balance": dag_result.get("mass_balance"),
            "customs_verdict": "RELEASE_CONTAINER_GREEN_CHANNEL" if all_compliant else "HOLD_CONTAINER_INSPECTION_REQUIRED",
            "plots_detail": plot_results
        }

