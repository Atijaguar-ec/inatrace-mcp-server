"""
Retrospective Traceability DAG Engine (Directed Acyclic Graph).
Recursively navigates backwards from commercial output lots (StockOrder) through
post-harvest transformation orders (ProcessingOrder, Transaction) down to
farmer deliveries, plots, and WGS84 coordinates.
"""
from typing import Dict, Any, List, Optional, Set
from src.domain.sri_validator import parse_canonical_sri_comment
from src.domain.eudr_geometry import evaluate_plot_geometry
from src.domain.prompt_guard import sanitize_untrusted_text
from src.domain.mass_balance import evaluate_lot_mass_balance
import re as _re
import unicodedata as _unicodedata


def _normalize_parcel_name(val: Optional[str]) -> str:
    if not val:
        return ""
    norm = _unicodedata.normalize("NFKD", str(val).strip().lower())
    norm = "".join(ch for ch in norm if not _unicodedata.combining(ch))
    return _re.sub(r"\s+", " ", norm)


DAG_RECURSIVE_QUERY = """
WITH RECURSIVE lot_lineage AS (
    -- Anchor: queried root StockOrder
    SELECT 
        s.id AS stock_order_id,
        s.identifier AS lot_code,
        s.totalquantity AS amount,
        s.qrcodetag,
        s.status,
        s.facility_id,
        s.company_id,
        s.producerusercustomer_id,
        s.parcellot,
        s.comments,
        s.processingorder_id,
        1 AS level
    FROM stockorder s
    WHERE (s.qrcodetag = $1 OR s.identifier = $1 OR s.id::text = $1)
      AND s.company_id = $2

    UNION ALL

    -- Recursive step: traverse input transactions of processing orders
    SELECT 
        prev_s.id AS stock_order_id,
        prev_s.identifier AS lot_code,
        prev_s.totalquantity AS amount,
        prev_s.qrcodetag,
        prev_s.status,
        prev_s.facility_id,
        prev_s.company_id,
        prev_s.producerusercustomer_id,
        prev_s.parcellot,
        prev_s.comments,
        prev_s.processingorder_id,
        ll.level + 1
    FROM lot_lineage ll
    JOIN transaction t ON (
        t.targetprocessingorder_id = ll.processingorder_id
        OR t.targetprocessingorder_id IS NOT NULL AND ll.processingorder_id IS NOT NULL AND t.targetprocessingorder_id = ll.processingorder_id
    )
    JOIN stockorder prev_s ON prev_s.id = t.sourcestockorder_id
    WHERE ll.level < 10 AND prev_s.id <> ll.stock_order_id
)
SELECT DISTINCT ll.*
FROM lot_lineage ll
ORDER BY ll.level ASC;
"""


def process_dag_results(
    lot_identifier: str,
    company_id: int,
    lineage_rows: List[Dict[str, Any]],
    plots_data: List[Dict[str, Any]],
    coordinates_data: List[Dict[str, Any]],
    deforestation_data: List[Dict[str, Any]],
    include_raw_coordinates: bool = True,
    require_completed_whisp: bool = False
) -> Dict[str, Any]:
    """
    Pure transformation function to assemble the complete DAG dossier.
    Decoupled from asyncpg to allow deterministic unit testing with mock datasets.
    Includes:
    - Exact parcelLot filtering per farmer (with fallback if no plot name matches)
    - OWASP LLM01 Indirect Prompt Injection sanitization (prompt_guard)
    - ISO 22005 / MAG Acuerdo 023 biological mass balance verification
    - Whisp Tri-State verification (VERIFIED vs PENDING_WHISP_ANALYSIS vs NON_COMPLIANT)
    - Tokenomics context compression (include_raw_coordinates)
    """
    if not lineage_rows:
        return {
            "found": False,
            "lot_identifier": lot_identifier,
            "company_id": company_id,
            "message": f"No se encontró el lote '{lot_identifier}' para la empresa {company_id}"
        }

    root_row = lineage_rows[0]
    security_threats: List[str] = []

    # Map declared parcelLot names per farmer from lineage rows
    farmer_declared_parcels: Dict[int, Set[str]] = {}
    for row in lineage_rows:
        fid = row.get("producerusercustomer_id")
        raw_parcel = row.get("parcellot")
        if fid and raw_parcel:
            _, p_threats = sanitize_untrusted_text(str(raw_parcel))
            security_threats.extend(p_threats)
            norm_p = _normalize_parcel_name(str(raw_parcel))
            if norm_p:
                if fid not in farmer_declared_parcels:
                    farmer_declared_parcels[fid] = set()
                farmer_declared_parcels[fid].add(norm_p)

    # Filter plots_data by exact parcelLot match per farmer when a match exists
    filtered_plots_data: List[Dict[str, Any]] = []
    plots_by_farmer: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for p in plots_data:
        fid = p.get("farmer_id") or p.get("usercustomer_id")
        plots_by_farmer.setdefault(fid, []).append(p)

    for fid, f_plots in plots_by_farmer.items():
        declared_set = farmer_declared_parcels.get(fid) if fid is not None else None
        if declared_set:
            matched = [
                p for p in f_plots
                if _normalize_parcel_name(p.get("plotname") or p.get("name")) in declared_set
            ]
            # Use exact matches if found; fallback to farmer's plots if legacy free-text did not match
            filtered_plots_data.extend(matched if matched else f_plots)
        else:
            filtered_plots_data.extend(f_plots)

    # Map coordinates by plot_id
    coords_by_plot: Dict[int, List[tuple]] = {}
    for c in coordinates_data:
        pid = c.get("plot_id")
        if pid not in coords_by_plot:
            coords_by_plot[pid] = []
        coords_by_plot[pid].append((float(c["longitude"]), float(c["latitude"])))

    # Map deforestation by plot_id
    deforest_by_plot: Dict[int, Dict[str, Any]] = {}
    for d in deforestation_data:
        pid = d.get("plot_id")
        if pid and pid not in deforest_by_plot:
            deforest_by_plot[pid] = d

    # Resolve participating plots
    participating_plots = []
    all_whisp_present = bool(filtered_plots_data)

    for p in filtered_plots_data:
        pid = p["id"]
        coords = coords_by_plot.get(pid, [])
        plot_size = float(p.get("size") or 0.0)
        geom_eval = evaluate_plot_geometry(
            coords,
            plot_size,
            include_raw_coordinates=include_raw_coordinates
        )
        has_whisp_record = pid in deforest_by_plot
        if not has_whisp_record:
            all_whisp_present = False
        deforest = deforest_by_plot.get(pid, {})

        # Sanitize untrusted free-text fields against OWASP LLM01 Indirect Prompt Injection
        raw_plot_name = p.get("plotname") or p.get("name") or ""
        clean_plot_name, t1 = sanitize_untrusted_text(str(raw_plot_name))
        raw_farmer_name = f"{p.get('name', '')} {p.get('surname', '')}".strip() or p.get("farmer_name", "Productor")
        clean_farmer_name, t2 = sanitize_untrusted_text(str(raw_farmer_name))
        raw_canton = p.get("canton", "Vinces")
        clean_canton, t3 = sanitize_untrusted_text(str(raw_canton))
        security_threats.extend(t1 + t2 + t3)

        # Risk & Tri-State assessment
        post_2020_disturbance = deforest.get("indicatordisturbanceafter2020")
        is_compliant_cutoff = (
            post_2020_disturbance is None
            or post_2020_disturbance == "0"
            or post_2020_disturbance == "none"
        )
        whisp_status = deforest.get("status", "VERIFIED") if has_whisp_record else "PENDING_WHISP_ANALYSIS"
        whisp_gate_ok = has_whisp_record if require_completed_whisp else True
        is_plot_compliant = is_compliant_cutoff and geom_eval.get("is_valid", False) and whisp_gate_ok

        participating_plots.append({
            "plot_id": pid,
            "plot_name": clean_plot_name,
            "size_ha": plot_size,
            "geo_id": p.get("geoid"),
            "cocoa_variety": p.get("cocoavariety", "Nacional"),
            "production_estimate_qq": float(p.get("productionestimate") or 0.0),
            "farmer_id": p.get("farmer_id") or p.get("usercustomer_id"),
            "farmer_name": clean_farmer_name,
            "canton": clean_canton,
            "dpa_code": p.get("dpa_code", "120550"),
            "geometry": geom_eval,
            "deforestation_analysis": {
                "source_table": "plotdeforestationanalysis",
                "status": whisp_status,
                "whisp_verification_state": "VERIFIED_BY_WHISP" if has_whisp_record else "PENDING_WHISP_ANALYSIS",
                "whisp_token": deforest.get("whisptoken"),
                "submission_mode": deforest.get("submissionmode"),
                "risk_pcrop": deforest.get("riskpcrop", "low" if has_whisp_record else "unknown"),
                "indicator_tree_cover": deforest.get("indicatortreecover"),
                "indicator_commodities": deforest.get("indicatorcommodities"),
                "indicator_before_2020": deforest.get("indicatordisturbancebefore2020"),
                "indicator_after_2020": post_2020_disturbance,
                "whisp_area_ha": deforest.get("area"),
                "completed_at": str(deforest["completedat"]) if deforest.get("completedat") else None,
                "is_eudr_compliant": is_plot_compliant
            }
        })

    # SRI settlement parsing from comments (with prompt injection inspection)
    sri_settlements = []
    for row in lineage_rows:
        comment = row.get("comments")
        if comment:
            _, c_threats = sanitize_untrusted_text(str(comment))
            security_threats.extend(c_threats)
            parsed = parse_canonical_sri_comment(comment)
            if parsed:
                sri_settlements.append(parsed.model_dump())

    # Evaluate Biological Mass Balance & Anti-Laundering (ISO 22005 / MAG 023)
    mass_balance = evaluate_lot_mass_balance(lineage_rows, participating_plots)

    # Assess overall EUDR status (EUDR Art. 9 requires at least one geolocated plot)
    all_plots_compliant = (
        bool(participating_plots) and all(
            p["deforestation_analysis"]["is_eudr_compliant"] for p in participating_plots
        )
    )
    if all_plots_compliant:
        overall_status = "COMPLIANT"
    elif require_completed_whisp and participating_plots and not all_whisp_present and all(
        p["geometry"].get("is_valid", False) and (
            p["deforestation_analysis"]["indicator_after_2020"] in (None, "0", "none")
        )
        for p in participating_plots
    ):
        overall_status = "PENDING_WHISP_ANALYSIS"
    else:
        overall_status = "NON_COMPLIANT"

    return {
        "found": True,
        "lot_identifier": lot_identifier,
        "company_id": company_id,
        "root_lot": {
            "stock_order_id": root_row.get("stock_order_id"),
            "lot_code": root_row.get("lot_code"),
            "qr_code_tag": root_row.get("qrcodetag"),
            "amount_kg": float(root_row.get("amount") or 0.0),
            "status": root_row.get("status")
        },
        "depth_levels": max(r.get("level", 1) for r in lineage_rows),
        "total_nodes": len(lineage_rows),
        "participating_plots_count": len(participating_plots),
        "participating_plots": participating_plots,
        "sri_settlements": sri_settlements,
        "mass_balance": mass_balance,
        "security_audit": {
            "owasp_llm01_checked": True,
            "prompt_injection_detected": len(security_threats) > 0,
            "threats_neutralized": security_threats
        },
        "eudr_summary": {
            "overall_status": overall_status,
            "strict_whisp_status": (
                "COMPLIANT" if (all_plots_compliant and all_whisp_present)
                else ("PENDING_WHISP_ANALYSIS" if (participating_plots and not all_whisp_present) else "NON_COMPLIANT")
            ),
            "whisp_coverage": "COMPLETE" if all_whisp_present else "PARTIAL_OR_PENDING",
            "cutoff_date": "2020-12-31",
            "whisp_source": "LOCAL_DB_PLOTDEFORESTATIONANALYSIS",
            "total_plots_audited": len(participating_plots)
        }
    }


async def build_lot_dag(
    conn: Any,
    lot_identifier: str,
    company_id: int,
    include_raw_coordinates: bool = True,
    require_completed_whisp: bool = False
) -> Dict[str, Any]:
    """
    Executes the recursive DAG lineage query on PostgreSQL and resolves
    associated plots, coordinates, and deforestation analyses from the local
    plotdeforestationanalysis table.
    """
    rows = await conn.fetch(DAG_RECURSIVE_QUERY, lot_identifier, company_id)
    if not rows:
        return {
            "found": False,
            "lot_identifier": lot_identifier,
            "company_id": company_id,
            "message": f"Lote no encontrado: {lot_identifier}"
        }

    lineage_rows = [dict(r) for r in rows]
    farmer_ids = [r["producerusercustomer_id"] for r in lineage_rows if r.get("producerusercustomer_id")]

    # Fetch plots linked via farmer with tenant isolation on usercustomer.company_id
    plots_query = """
        SELECT DISTINCT 
            pl.id, 
            pl.plotname, 
            pl.size, 
            pl.geoid, 
            pl.cocoavariety,
            pl.productionestimate,
            uc.id AS farmer_id,
            uc.name,
            uc.surname
        FROM plot pl
        JOIN usercustomer uc ON (pl.farmer_id = uc.id)
        WHERE uc.id = ANY($1::bigint[])
          AND uc.company_id = $2
    """
    plots_rows = await conn.fetch(plots_query, farmer_ids, company_id) if farmer_ids else []
    plots_data = [dict(p) for p in plots_rows]
    plot_ids = [p["id"] for p in plots_data]

    # Fetch coordinates
    coords_rows = []
    if plot_ids:
        coords_rows = await conn.fetch("""
            SELECT plot_id, latitude, longitude 
            FROM plotcoordinate 
            WHERE plot_id = ANY($1::bigint[])
            ORDER BY plot_id, id ASC
        """, plot_ids)
    coords_data = [dict(c) for c in coords_rows]

    # Fetch latest Whisp deforestation analysis from local plotdeforestationanalysis table
    deforest_rows = []
    if plot_ids:
        deforest_rows = await conn.fetch("""
            SELECT DISTINCT ON (plot_id)
                plot_id, status, whisptoken, submissionmode, geoid,
                requestedat, completedat, riskpcrop,
                indicatortreecover, indicatorcommodities,
                indicatordisturbancebefore2020, indicatordisturbanceafter2020,
                area, areaunit, country, errormessage
            FROM plotdeforestationanalysis
            WHERE plot_id = ANY($1::bigint[])
            ORDER BY plot_id, id DESC
        """, plot_ids)
    deforest_data = [dict(d) for d in deforest_rows]

    return process_dag_results(
        lot_identifier=lot_identifier,
        company_id=company_id,
        lineage_rows=lineage_rows,
        plots_data=plots_data,
        coordinates_data=coords_data,
        deforestation_data=deforest_data,
        include_raw_coordinates=include_raw_coordinates,
        require_completed_whisp=require_completed_whisp
    )
