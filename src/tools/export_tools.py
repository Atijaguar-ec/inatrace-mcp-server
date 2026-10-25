"""
Regulatory Export Tools for INATrace MCP Server:
- generate_traces_jsonld: Exports W3C JSON-LD Due Diligence Statement for DG SANTE / TRACES-NT.
- export_agrocalidad_guia: Exports CSV delimited by ';' with UTF-8 BOM for Agrocalidad GUIA system.
"""
import io
import csv
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from src.infrastructure.db_pool import require_db_pool
from src.infrastructure.auth_scope import assert_tenant_access
from src.domain.traces_ld import build_traces_jsonld
from src.domain.dag_engine import build_lot_dag



class StockOrderNotFoundError(LookupError):
    """Raised when a regulatory export is requested for a StockOrder that does not exist."""

    def __init__(self, stock_order_id: int):
        super().__init__(
            f"No existe el StockOrder {stock_order_id}: no se emite documentación "
            f"regulatoria sobre un lote inexistente."
        )
        self.stock_order_id = stock_order_id


class GenerateTracesJsonLdInput(BaseModel):
    stock_order_id: int = Field(..., description="ID numérico de base de datos del StockOrder exportable")
    operator_ruc: str = Field(..., pattern=r"^\d{13}$", description="RUC de 13 dígitos del exportador u operador en Ecuador")
    issuing_authority: str = Field(default="AGROCALIDAD-EC", description="Autoridad competente emisora en Ecuador")
    operator_name: str = Field(default="UNIÓN DE ORGANIZACIONES CAFETALERAS Y CACAOTERAS DEL ECUADOR - UNOCACE", description="Razón social del operador")


class ExportAgrocalidadInput(BaseModel):
    company_id: int = Field(..., description="ID de la organización (ej. 1 UNOCACE, 5 Fortaleza del Valle)")
    date_from: str = Field(..., description="Fecha inicial en formato YYYY-MM-DD")
    date_to: str = Field(..., description="Fecha final en formato YYYY-MM-DD")


async def execute_generate_traces_jsonld(params: GenerateTracesJsonLdInput) -> Dict[str, Any]:
    """
    Builds the formal JSON-LD DDS document for submission to the European Commission TRACES-NT API.
    """
    pool = require_db_pool("generate_traces_jsonld")

    production_places = []
    async with pool.acquire() as conn:
        so_row = await conn.fetchrow("""
            SELECT qrcodetag, totalquantity, identifier, company_id
            FROM stockorder
            WHERE id = $1
        """, params.stock_order_id)
        if not so_row:
            # A DDS must never be minted for a stock order that does not exist.
            raise StockOrderNotFoundError(params.stock_order_id)

        stock_uuid = so_row["qrcodetag"] or so_row["identifier"] or f"ec00-{params.stock_order_id:06d}-2026-eudr"
        net_weight_kg = float(so_row["totalquantity"] or 0.0)
        company_id = so_row["company_id"]
        # A DDS for another tenant's lot must not be mintable by passing its id.
        assert_tenant_access(company_id, f"StockOrder {params.stock_order_id}")

        dag_result = await build_lot_dag(conn, str(params.stock_order_id), company_id)
        for p in dag_result.get("participating_plots", []):
            geom_eval = p.get("geometry", {})
            coords = geom_eval.get("coordinates", [])
            geom_type = geom_eval.get("type", "Point")

            if geom_type == "CENTROID" or (p.get("size_ha", 0) <= 4.0 and len(coords) == 1):
                geom = {"type": "Point", "coordinates": list(coords[0]) if coords else [-79.7541, -1.5421]}
            else:
                geom = {"type": "Polygon", "coordinates": [coords]}

            production_places.append({
                "plot_id": p.get("plot_id"),
                "geo_id": p.get("geo_id"),
                "dpa_code": p.get("dpa_code", "120550"),
                "plot_size_ha": p.get("size_ha", 0.0),
                "deforestation_risk": p.get("deforestation_analysis", {}).get("risk_pcrop", "NONE"),
                "geometry": geom
            })

    return build_traces_jsonld(
        stock_order_uuid=stock_uuid,
        operator_ruc=params.operator_ruc,
        operator_name=params.operator_name,
        net_weight_kg=net_weight_kg,
        issuing_authority=params.issuing_authority,
        production_places=production_places
    )


async def execute_export_agrocalidad_guia(params: ExportAgrocalidadInput) -> Dict[str, Any]:
    """
    Exports official CSV delimited by ';' with UTF-8 BOM encoding for Agrocalidad GUIA system.
    """
    headers = [
        "RUC_OPERADOR", "CODIGO_SITIO_GUIA", "CODIGO_AREA_GUIA", "FECHA_RECEPCION",
        "CODIGO_DPA_INEC", "PROVINCIA", "CANTON", "PARROQUIA", "NOMBRE_PRODUCTOR",
        "TIPO_PRODUCTO", "PESO_BRUTO_KG", "TARA_KG", "PESO_NETO_KG", "NUMERO_SACOS",
        "DESTINO_MOVILIZACION", "ESTADO_FITOSANITARIO"
    ]

    rows_data = []
    pool = require_db_pool("export_agrocalidad_guia")
    assert_tenant_access(params.company_id, f"organización {params.company_id}")

    from datetime import date as dt_date
    try:
        d_from = dt_date.fromisoformat(str(params.date_from).strip())
        d_to = dt_date.fromisoformat(str(params.date_to).strip())
    except (ValueError, TypeError):
        d_from = params.date_from
        d_to = params.date_to

    # Any failure of the BI view propagates: an empty or partial regulatory CSV
    # must never be passed off as a complete one.
    async with pool.acquire() as conn:
        db_rows = await conn.fetch("""
            SELECT
                COALESCE(numero_ruc_operador, '') AS ruc_operador,
                COALESCE(codigo_sitio_origen_guia, '') AS codigo_sitio_guia,
                COALESCE(codigo_area_origen_guia, '') AS codigo_area_guia,
                fecha_acopio_entrega AS fecha_recepcion,
                COALESCE(codigo_dpa_inec, '000000') AS codigo_dpa_inec,
                COALESCE(provincia_predio, '') AS provincia,
                COALESCE(canton_predio, '') AS canton,
                COALESCE(parroquia_predio, '') AS parroquia,
                COALESCE(nombre_productor, '') AS nombre_productor,
                COALESCE(descripcion_producto, 'Cacao') AS tipo_producto,
                COALESCE(peso_neto_oficial_kg, 0.0) AS peso_bruto_kg,
                0.0 AS tara_kg,
                COALESCE(peso_neto_oficial_kg, 0.0) AS peso_neto_kg,
                1 AS numero_sacos,
                COALESCE(centro_acopio_destino, '') AS destino_movilizacion,
                COALESCE(estado_calidad_regulatoria, 'OK') AS estado_fitosanitario
            FROM bi.rpt_cacao_agrocalidad_guia
            WHERE organization_id = $1 AND fecha_acopio_entrega BETWEEN $2 AND $3
            ORDER BY fecha_acopio_entrega ASC
        """, params.company_id, d_from, d_to)
        for r in db_rows:
            rows_data.append([str(r[h.lower()] or "") for h in headers])

    # Write CSV with semicolon separator and UTF-8 BOM (\uFEFF)
    output = io.StringIO()
    output.write("\uFEFF")  # UTF-8 BOM required by Agrocalidad ADR-BI-001
    writer = csv.writer(output, delimiter=";")
    writer.writerow(headers)
    for row in rows_data:
        writer.writerow(row)

    csv_content = output.getvalue()

    return {
        "status": "GENERATED",
        "data_source": "DATABASE",
        "format": "CSV_SEMICOLON_UTF8_BOM",
        "encoding": "UTF-8-SIG",
        "delimiter": ";",
        "total_records": len(rows_data),
        "company_id": params.company_id,
        "date_range": f"{params.date_from} to {params.date_to}",
        "csv_content": csv_content
    }
