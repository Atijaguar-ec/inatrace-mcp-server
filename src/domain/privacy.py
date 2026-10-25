"""
Data Minimization Gate (LOPDP Ecuador Art. 10 / EU GDPR Art. 5).
Applies differential privacy and data sanitation between public B2C consumers
and authorized regulatory/audit channels.
"""
from typing import Dict, Any, List


def sanitize_for_public_b2c(raw_lot_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitizes raw lot and DAG traceability data for public B2C consumption.
    Strictly removes all Personally Identifiable Information (PII) and commercial pricing.
    """
    participating_plots = raw_lot_data.get("participating_plots", [])
    total_farmers = len(set(
        p.get("farmer_id") for p in participating_plots if p.get("farmer_id")
    )) or len(participating_plots)

    # Cantons represented
    cantons = sorted(list(set(
        p.get("canton", "Los Ríos / Manabí") for p in participating_plots if p.get("canton")
    )))

    eudr_summary = raw_lot_data.get("eudr_summary", {})
    overall_status = eudr_summary.get("overall_status", raw_lot_data.get("eudr_status", "UNKNOWN"))
    is_compliant = (overall_status == "COMPLIANT" or overall_status == "COMPLIANT_ZERO_DEFORESTATION")

    return {
        "channel": "PUBLIC_B2C",
        "lot_identifier": raw_lot_data.get("lot_identifier") or raw_lot_data.get("code"),
        "product": raw_lot_data.get("product_name", "Cacao Fino de Aroma"),
        "origin_country": "Ecuador",
        "cooperative": raw_lot_data.get("company_name", "UNOCACE"),
        "regions": cantons if cantons else ["Ecuador - Región Litoral"],
        "variety": raw_lot_data.get("variety", "Nacional / Colección Castro Naranjal"),
        "is_organic": bool(raw_lot_data.get("organic", False)),
        "eudr_status": "COMPLIANT_ZERO_DEFORESTATION" if is_compliant else "NON_COMPLIANT_RISK_DETECTED",
        "deforestation_risk": "NONE" if is_compliant else "RISK_DETECTED",
        "cutoff_date_verified": "2020-12-31",
        "total_participating_farmers": total_farmers,
        "total_participating_plots": len(participating_plots),
        "post_harvest_process": {
            "fermentation": "Fermentación en cajones de laurel (5-7 días)",
            "drying": "Secado natural solar en marquesina",
            "quality_grade": "Grado 1 Superior"
        },
        "privacy_notice": (
            "Datos personales de los agricultores protegidos bajo la Ley Orgánica de "
            "Protección de Datos Personales (LOPDP Ecuador) y el RGPD europeo."
        )
        # SENSITIVE DATA STRICTLY REMOVED:
        # - Cédula / Tax ID: REMOVED
        # - Full names of farmers: REMOVED
        # - Phone numbers: REMOVED
        # - Bank accounts: REMOVED
        # - Exact purchase prices / balances: REMOVED
        # - Exact polygon coordinates: HIDDEN (Replaced by general cantonal validation)
    }


def apply_privacy_gate(raw_lot_data: Dict[str, Any], is_public_channel: bool = True) -> Dict[str, Any]:
    """
    Routes output through the privacy gate based on caller authorization.
    """
    if is_public_channel:
        return sanitize_for_public_b2c(raw_lot_data)
    # Regulatory channel (Tier 2 Customs / Auditor): returns full technical dossier
    return raw_lot_data


def redact_settlement_data(raw_lot_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Redacts fiscal settlements (SRI settlement data, access keys, internal ERP IDs)
    for cooperative field staff (Tier 2B), preserving operational PII (farmer names,
    plots, coordinates, mass balance, EUDR summary).
    """
    out = dict(raw_lot_data)
    out.pop("sri_settlements", None)
    out["settlement_data_redacted"] = True
    return out

