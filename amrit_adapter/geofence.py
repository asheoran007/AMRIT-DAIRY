"""COD eligibility by owner-approved geometry only.

Rules (BUSINESS_RULES.yaml / master prompt):
- Zone A Sonipat Extended polygon, Zone B Nindana 10 km radius, Zone C Meham-Bainsi polygon.
- Exact approved GeoJSON / centre coordinates are REQUIRED. Nothing is reconstructed from place names.
- Missing or ambiguous location -> NOT approved (prepaid or manual review). Never auto-approve.
- Same decision object is used by chat and by checkout enforcement.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shapely.geometry import Point, shape

EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class CodDecision:
    decision: str                 # approved_zone_A|approved_zone_B|approved_zone_C|not_approved|manual_review
    reason: str
    policy_version: str
    zone_id: str | None = None
    next_step: str = "prepaid"    # prepaid | manual_review | cod


@dataclass
class ZoneSet:
    policy_version: str
    polygons: dict[str, Any] = field(default_factory=dict)   # zone_id -> shapely geometry
    circles: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # zone_id -> (lat, lon, km)
    loaded: bool = False
    load_error: str | None = None


def load_zones(path: str | Path, policy_version: str) -> ZoneSet:
    zs = ZoneSet(policy_version=policy_version)
    p = Path(path)
    if not p.exists():
        zs.load_error = f"geometry file missing: {p}"
        return zs
    try:
        fc = json.loads(p.read_text(encoding="utf-8"))
        for feat in fc.get("features", []):
            props = feat.get("properties") or {}
            zid = props.get("zone_id")
            if not zid or not props.get("owner_approved") is True:
                continue  # unapproved geometry is ignored on purpose
            geom = feat.get("geometry") or {}
            if geom.get("type") == "Point" and props.get("radius_km"):
                lon, lat = geom["coordinates"][:2]
                zs.circles[zid] = (float(lat), float(lon), float(props["radius_km"]))
            elif geom.get("type") in ("Polygon", "MultiPolygon"):
                zs.polygons[zid] = shape(geom)
        zs.loaded = bool(zs.polygons or zs.circles)
        if not zs.loaded:
            zs.load_error = "no owner_approved features in geometry file"
    except Exception as exc:  # noqa: BLE001
        zs.load_error = f"geometry parse error: {exc}"
    return zs


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def decide_cod(zones: ZoneSet, lat: float | None, lon: float | None, location_confidence: str = "unknown") -> CodDecision:
    """location_confidence: 'customer_confirmed' | 'geocoded' | 'unknown'.
    Only customer-confirmed or precise coordinates are accepted; a geocoder guess goes to manual review."""
    pv = zones.policy_version
    if not zones.loaded:
        return CodDecision("not_approved", zones.load_error or "approved geometry unavailable", pv, next_step="prepaid")
    if lat is None or lon is None:
        return CodDecision("not_approved", "location missing", pv, next_step="prepaid")
    if location_confidence not in ("customer_confirmed", "precise"):
        return CodDecision("manual_review", f"location confidence '{location_confidence}' insufficient", pv, next_step="manual_review")
    pt = Point(lon, lat)
    for zid in sorted(zones.polygons):
        if zones.polygons[zid].covers(pt):   # boundary counts as inside, documented
            return CodDecision(f"approved_zone_{zid}", "inside approved polygon (boundary inclusive)", pv, zid, "cod")
    for zid in sorted(zones.circles):
        clat, clon, rkm = zones.circles[zid]
        if haversine_km(lat, lon, clat, clon) <= rkm:
            return CodDecision(f"approved_zone_{zid}", "inside approved radius (boundary inclusive)", pv, zid, "cod")
    return CodDecision("not_approved", "outside all approved zones", pv, next_step="prepaid")
