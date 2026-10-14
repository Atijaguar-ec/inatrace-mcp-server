"""
Biological Mass Balance & Anti-Laundering Engine (ISO 22005 / MAG Acuerdo 023).
Verifies that the commercial lot weight is biologically compatible with the
authorized annual quota (productionEstimate in Quintales qq) of participating plots
and that post-harvest transformation stages do not inflate mass.
"""
from typing import List, Dict, Any, Union

LBS_PER_QUINTAL = 100.0
KG_PER_QUINTAL = 45.359237
BABA_TO_DRY_YIELD_FACTOR = 0.40  # Standard 40% yield from wet cacao (baba) to dry cacao
DEFAULT_TOLERANCE_RATIO = 1.05   # 5% tolerance margin


def evaluate_lot_mass_balance(
    lot_or_lineage: Union[float, int, List[Dict[str, Any]]],
    participating_plots: List[Dict[str, Any]],
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO
) -> Dict[str, Any]:
    """
    Computes total authorized capacity across participating plots and verifies
    whether the lot weight and transformation lineage are within biological bounds.
    """
    lineage_rows: List[Dict[str, Any]] = []
    if isinstance(lot_or_lineage, list):
        lineage_rows = lot_or_lineage
        lot_amount_kg = float(lineage_rows[0].get("amount") or 0.0) if lineage_rows else 0.0
    else:
        lot_amount_kg = float(lot_or_lineage or 0.0)

    total_estimate_qq = sum(float(p.get("production_estimate_qq") or 0.0) for p in participating_plots)
    total_area_ha = sum(float(p.get("size_ha") or 0.0) for p in participating_plots)

    # If plots lack explicit production_estimate_qq, estimate conservative 15 qq/ha
    effective_qq = total_estimate_qq if total_estimate_qq > 0 else round(total_area_ha * 15.0, 2)
    authorized_max_lbs = round(effective_qq * LBS_PER_QUINTAL, 2)
    authorized_max_kg = round(effective_qq * KG_PER_QUINTAL, 2)
    lot_amount_lbs = round(lot_amount_kg * 2.20462, 2)

    anomalies: List[Dict[str, Any]] = []

    # Check 1: Plot Annual Quota Capacity (ISO 22005 anti-laundering check)
    if participating_plots and authorized_max_kg > 0:
        if lot_amount_kg > (authorized_max_kg * tolerance_ratio):
            anomalies.append({
                "code": "ISO22005_QUOTA_CAPACITY_EXCEEDED",
                "severity": "HIGH",
                "description": (
                    f"El peso del lote ({lot_amount_kg:.2f} kg) excede la capacidad máxima "
                    f"autorizada de las parcelas ({authorized_max_kg:.2f} kg / {effective_qq:.2f} qq)."
                )
            })

    # Check 2: Post-Harvest Transformation Conservation of Mass (MAG Acuerdo 023)
    if len(lineage_rows) > 1:
        deeper_rows = [r for r in lineage_rows[1:] if float(r.get("amount") or 0.0) > 0]
        if deeper_rows:
            input_sum_kg = sum(float(r.get("amount") or 0.0) for r in deeper_rows)
            if input_sum_kg > 0 and lot_amount_kg > (input_sum_kg * tolerance_ratio):
                anomalies.append({
                    "code": "MAG023_TRANSFORMATION_INFLATION",
                    "severity": "CRITICAL",
                    "description": (
                        f"El lote de salida ({lot_amount_kg:.2f} kg) supera la suma de lotes "
                        f"de entrada ({input_sum_kg:.2f} kg), violando la conservación de masa."
                    )
                })

    is_compliant = len(anomalies) == 0 and bool(participating_plots)
    utilization_pct = round((lot_amount_kg / authorized_max_kg) * 100.0, 2) if authorized_max_kg > 0 else 0.0

    return {
        "status": "MASS_BALANCE_OK" if is_compliant else "MASS_BALANCE_EXCEEDED",
        "is_compliant": is_compliant,
        "is_compatible": is_compliant,
        "lot_weight_kg": round(lot_amount_kg, 2),
        "lot_weight_lbs": lot_amount_lbs,
        "total_plots_area_ha": round(total_area_ha, 4),
        "total_authorized_qq": round(effective_qq, 2),
        "authorized_max_kg": authorized_max_kg,
        "authorized_max_lbs": authorized_max_lbs,
        "baba_to_dry_conversion_factor": BABA_TO_DRY_YIELD_FACTOR,
        "tolerance_ratio": tolerance_ratio,
        "quota_utilization_percent": utilization_pct,
        "anomalies_count": len(anomalies),
        "anomalies": anomalies,
        "anti_laundering_verdict": "CLEAN_ORIGIN_VOLUME" if is_compliant else "ALERT_OVER_DECLARATION_RISK"
    }
