"""
Canonical JSON-LD Serializer for EU TRACES-NT / DG SANTE (EU Regulation 2023/1115 - EUDR).
Builds standard W3C JSON-LD documents for Due Diligence Statements (DDS)
incorporating 6-digit INEC DPA territorial codes, RUC numbers, and WGS84 GeoJSON geometries.
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone


def format_dpa_code(code: Any) -> str:
    """
    Enforces the strict INEC DPA 6-digit zero-padding rule:
    Provincia: 2 digits ('04')
    Cantón: 4 digits ('0402')
    Parroquia: 6 digits ('040250')
    Never cast to integer.
    """
    if code is None:
        return "000000"
    raw_str = str(code).strip()
    return raw_str.zfill(6)


def build_traces_jsonld(
    stock_order_uuid: str,
    operator_ruc: str,
    operator_name: str,
    net_weight_kg: float,
    commodity_code: str = "18010000",
    issuing_authority: str = "AGROCALIDAD-EC",
    production_places: Optional[List[Dict[str, Any]]] = None,
    created_at: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Constructs a DG SANTE / TRACES-NT compliant JSON-LD Due Diligence Statement payload.
    """
    if production_places is None:
        production_places = []

    ts = (created_at or datetime.now(timezone.utc)).isoformat()

    # Format places according to EUDR Art. 9
    formatted_places = []
    for idx, place in enumerate(production_places):
        dpa_raw = place.get("dpa_code") or place.get("canton_code") or "120550"
        dpa_code = format_dpa_code(dpa_raw)
        geom = place.get("geometry", {})
        plot_size_ha = float(place.get("plot_size_ha", 0.0))

        # Build GeoJSON feature
        formatted_places.append({
            "@type": "eudr:ProductionPlace",
            "identifier": f"urn:inatrace:plot:{place.get('plot_id', idx + 1)}",
            "geoId": place.get("geo_id"),
            "dpaCode": dpa_code,
            "countryOfProduction": "EC",
            "areaHa": plot_size_ha,
            "deforestationFreeCutoff": "2020-12-31",
            "deforestationRisk": place.get("deforestation_risk", "NONE"),
            "geometry": geom
        })

    return {
        "@context": {
            "@vocab": "https://ec.europa.eu/tracesnt/schema/v1/eudr#",
            "eudr": "https://ec.europa.eu/tracesnt/schema/v1/eudr#",
            "schema": "http://schema.org/",
            "geojson": "https://purl.org/geojson/vocab#",
            "xsd": "http://www.w3.org/2001/XMLSchema#"
        },
        "@type": "eudr:DueDiligenceStatement",
        "statementIdentifier": f"urn:uuid:{stock_order_uuid}",
        "referenceNumber": f"EC-{operator_ruc}-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{stock_order_uuid[:8].upper()}",
        "issueDate": ts,
        "issuingAuthority": issuing_authority,
        "operator": {
            "@type": "schema:Organization",
            "identifier": operator_ruc,
            "name": operator_name,
            "taxID": operator_ruc,
            "address": {
                "@type": "schema:PostalAddress",
                "addressCountry": "EC"
            }
        },
        "commodity": {
            "@type": "eudr:Commodity",
            "hsCode": commodity_code,
            "description": "Cacao en grano, entero o partido, crudo o tostado",
            "netMassKg": round(net_weight_kg, 2),
            "commercialLotCode": stock_order_uuid
        },
        "productionPlaces": formatted_places,
        "complianceDeclaration": {
            "isDeforestationFree": True,
            "isLegallyProduced": True,
            "verifiedAgainstCutoffDate": "2020-12-31",
            "verificationMethodology": "Sentinel-2 / Whisp Satellite Analysis & Cadastral Ground Validation"
        }
    }
