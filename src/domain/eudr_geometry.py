"""
Geodesic Topology and Validation Engine for EUDR Compliance (EU Regulation 2023/1115).
Ensures WGS84 (EPSG:4326) closed rings for plots > 4.0 ha, centroid precision (>= 6 decimals)
for plots <= 4.0 ha, and auto-repair of self-intersecting polygons (Post-Mortem 039).
"""
import math
from typing import List, Tuple, Dict, Any, Optional

try:
    from shapely.geometry import Polygon as ShapelyPolygon, Point as ShapelyPoint
    SHAPELY_AVAILABLE = True
except ImportError:
    ShapelyPolygon = None
    ShapelyPoint = None
    SHAPELY_AVAILABLE = False


def _count_decimal_places(val: Any) -> int:
    """Returns number of decimal digits in a float or string representation."""
    if isinstance(val, str):
        s = val.strip()
        if '.' in s:
            return len(s.split('.')[1])
        return 0
    try:
        f = float(val)
        s = f"{f:.10f}".rstrip('0')
        if '.' in s:
            return len(s.split('.')[1])
    except (ValueError, TypeError):
        pass
    return 0


def normalize_wgs84_coords(coords: List[Any]) -> List[Tuple[float, float]]:
    """
    Normalizes coordinates to (longitude, latitude) tuples.
    Detects and corrects if coordinates were passed as (latitude, longitude) based on Ecuadorian bounds:
    - Latitude roughly -5.0 to +2.0
    - Longitude roughly -82.0 to -75.0
    """
    normalized = []
    for pt in coords:
        lon_val = float(pt[0])
        lat_val = float(pt[1])
        # If first value is in Ecuadorian latitude range and second in longitude range, swap
        if abs(lon_val) <= 6.0 and -85.0 <= lat_val <= -70.0:
            normalized.append((lat_val, lon_val))
        else:
            normalized.append((lon_val, lat_val))
    return normalized


def _shoelace_area_wgs84_ha(coords: List[Tuple[float, float]]) -> float:
    """
    Computes approximate surface area in hectares using planar projection approximation
    at Ecuadorian equatorial latitudes (1 deg lat ~ 111 km, 1 deg lon ~ 111.32 km).
    Used as pure-Python fallback when Shapely is not loaded.
    """
    if len(coords) < 3:
        return 0.0

    # Ensure ring is closed
    pts = list(coords)
    if pts[0] != pts[-1]:
        pts.append(pts[0])

    n = len(pts)
    # Average latitude for lon scaling
    avg_lat = sum(p[1] for p in pts) / n
    lat_m_per_deg = 110574.0
    lon_m_per_deg = 111320.0 * math.cos(math.radians(avg_lat))

    # Convert degrees to meters
    area_m2 = 0.0
    for i in range(n - 1):
        x1 = pts[i][0] * lon_m_per_deg
        y1 = pts[i][1] * lat_m_per_deg
        x2 = pts[i + 1][0] * lon_m_per_deg
        y2 = pts[i + 1][1] * lat_m_per_deg
        area_m2 += (x1 * y2) - (x2 * y1)

    area_m2 = abs(area_m2) / 2.0
    return round(area_m2 / 10000.0, 4)  # 1 ha = 10,000 m2


def validate_and_close_polygon(coordinates: List[Tuple[float, float]]) -> Tuple[List[Tuple[float, float]], bool]:
    """
    Validates and enforces topologic ring closure on a polygon.
    Returns: (closed_coordinates, was_closure_enforced)
    """
    if len(coordinates) < 3:
        raise ValueError("Un polígono requiere un mínimo de 3 vértices distintos.")

    norm_coords = normalize_wgs84_coords(coordinates)
    first = norm_coords[0]
    last = norm_coords[-1]

    # Geodesic tolerance ~ 1cm in WGS84
    is_closed = abs(first[0] - last[0]) < 1e-7 and abs(first[1] - last[1]) < 1e-7

    if is_closed:
        closed_coords = list(norm_coords)
        enforced = False
    else:
        closed_coords = list(norm_coords) + [first]
        enforced = True

    if SHAPELY_AVAILABLE:
        poly = ShapelyPolygon(closed_coords)
        if not poly.is_valid:
            poly = poly.buffer(0)  # Self-intersection auto-repair (Post-Mortem 039)
        return list(poly.exterior.coords), enforced
    else:
        return closed_coords, enforced


def _compute_bbox_and_centroid(coords: List[Tuple[float, float]]) -> Tuple[List[float], List[float]]:
    """Computes deterministic WGS84 [min_lon, min_lat, max_lon, max_lat] and [lon, lat] centroid."""
    if not coords:
        return [], []
    lons = [float(p[0]) for p in coords]
    lats = [float(p[1]) for p in coords]
    bbox = [round(min(lons), 6), round(min(lats), 6), round(max(lons), 6), round(max(lats), 6)]
    centroid = [round(sum(lons) / len(lons), 6), round(sum(lats) / len(lats), 6)]
    return bbox, centroid


def evaluate_plot_geometry(
    coordinates: List[Tuple[float, float]],
    plot_size_ha: float,
    strict_polygon_closure: bool = True,
    include_raw_coordinates: bool = True
) -> Dict[str, Any]:
    """
    Evaluates plot coordinates against EUDR Article 9 rules:
    - Plots <= 4.0 ha: Single coordinate (centroid) admitted if precision >= 6 decimals.
    - Plots > 4.0 ha: Enforces closed ring polygon (closedRing).
    - When include_raw_coordinates=False, omits bulky vertex arrays to protect LLM context budget.
    """
    if not coordinates:
        return {
            "type": "NONE",
            "is_valid": False,
            "error": "No se encontraron coordenadas para la parcela",
            "coordinates": [],
            "centroid": [],
            "bounding_box": [],
            "calculated_area_ha": 0.0
        }

    norm_coords = normalize_wgs84_coords(coordinates)

    # Case 1: Predio > 4.0 ha excede umbral de centroide y exige polígono cerrado (EUDR Art. 9)
    if plot_size_ha > 4.0 and len(norm_coords) < 3:
        bbox, centroid = _compute_bbox_and_centroid(norm_coords)
        return {
            "type": "POLYGON",
            "is_valid": False,
            "error": f"Predio de {plot_size_ha} ha excede las 4.0 ha y requiere polígono cerrado obligatorio con mínimo 3 vértices (recibidos: {len(norm_coords)}) (EUDR Art. 9)",
            "coordinates": norm_coords if include_raw_coordinates else [],
            "centroid": centroid,
            "bounding_box": bbox,
            "calculated_area_ha": 0.0
        }

    # Case 2: Centroid for small plots (<= 4.0 ha)
    if plot_size_ha <= 4.0 and len(norm_coords) == 1:
        lon, lat = norm_coords[0]
        raw_lon = coordinates[0][0]
        raw_lat = coordinates[0][1]
        dec_lon = _count_decimal_places(raw_lon)
        dec_lat = _count_decimal_places(raw_lat)
        precision_valid = (dec_lon >= 6 and dec_lat >= 6)
        pt = (round(lon, 7), round(lat, 7))

        res = {
            "type": "CENTROID",
            "is_valid": precision_valid,
            "centroid": [round(lon, 6), round(lat, 6)],
            "bounding_box": [round(lon, 6), round(lat, 6), round(lon, 6), round(lat, 6)],
            "decimal_precision": {"lon": dec_lon, "lat": dec_lat},
            "plot_size_ha": plot_size_ha,
            "calculated_area_ha": plot_size_ha,
            "closed_ring_enforced": False,
            "error": None if precision_valid else "El centroide requiere mínimo 6 decimales de precisión para cumplir EUDR"
        }
        if include_raw_coordinates:
            res["coordinates"] = [pt]
        else:
            res["coordinates_omitted_for_context_budget"] = True
        return res

    # Case 3: Polygon with insufficient vertices (< 3)
    if len(norm_coords) < 3:
        bbox, centroid = _compute_bbox_and_centroid(norm_coords)
        return {
            "type": "POLYGON",
            "is_valid": False,
            "error": f"Predio de {plot_size_ha} ha requiere polígono cerrado con mínimo 3 vértices (recibidos: {len(norm_coords)})",
            "coordinates": norm_coords if include_raw_coordinates else [],
            "centroid": centroid,
            "bounding_box": bbox,
            "calculated_area_ha": 0.0
        }

    try:
        closed_coords, enforced = validate_and_close_polygon(norm_coords)
    except Exception as exc:
        bbox, centroid = _compute_bbox_and_centroid(norm_coords)
        return {
            "type": "POLYGON",
            "is_valid": False,
            "error": f"Error topológico en polígono: {str(exc)}",
            "coordinates": norm_coords if include_raw_coordinates else [],
            "centroid": centroid,
            "bounding_box": bbox,
            "calculated_area_ha": 0.0
        }

    # Compute area, centroid and bounding box
    if SHAPELY_AVAILABLE:
        poly = ShapelyPolygon(closed_coords)
        calc_area = _shoelace_area_wgs84_ha(closed_coords)
        minx, miny, maxx, maxy = poly.bounds
        bbox = [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)]
        centroid = [round(poly.centroid.x, 6), round(poly.centroid.y, 6)]
    else:
        calc_area = _shoelace_area_wgs84_ha(closed_coords)
        bbox, centroid = _compute_bbox_and_centroid(closed_coords)

    res = {
        "type": "POLYGON",
        "is_valid": True,
        "closed_ring_enforced": enforced,
        "vertices_count": len(closed_coords),
        "centroid": centroid,
        "bounding_box": bbox,
        "plot_size_ha": plot_size_ha,
        "calculated_area_ha": calc_area,
        "error": None
    }
    if include_raw_coordinates:
        res["coordinates"] = closed_coords
    else:
        res["coordinates_omitted_for_context_budget"] = True
    return res


