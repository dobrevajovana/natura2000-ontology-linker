"""Geolocation extraction and geometry helpers.

Turns the flattened BioSample ``attributes`` dict into a ``Location`` by
trying, in order: explicit coordinates, a named locality, a country/sea
centroid. All lookups are offline (see ``geoapp.gazetteer``).
"""

import logging
import math
import re
from typing import Optional

from geoapp import gazetteer
from geoapp.schema import Location, Sample

logger = logging.getLogger(__name__)

# INSDC/ENA/NCBI "no value" placeholders, lowercased. Used for coordinate
# strings as well as free-text country/region strings.
NO_VALUE_PLACEHOLDERS: set[str] = {
    "not collected",
    "not applicable",
    "not provided",
    "missing",
    "unknown",
    "n/a",
    "na",
    "none",
    "-",
    "restricted access",
}

_LAT_LON_COMBINED_KEY = "lat lon"
_LAT_KEYS = (
    "geographic location (latitude)",
    "latitude",
    "lat",
    "decimallatitude",
)
_LON_KEYS = (
    "geographic location (longitude)",
    "longitude",
    "lon",
    "decimallongitude",
)
_REGION_LOCALITY_KEY = "geographic location (region and locality)"
_REGION_KEY = "geographic location (region)"
_GEO_LOC_NAME_KEY = "geo loc name"
_COUNTRY_SEA_KEY = "geographic location (country and/or sea)"
_COUNTRY_KEY = "country"

_HEMISPHERE_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*°?\s*([NSEWnsew])")
_SINGLE_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?)\s*°?\s*(?:DD)?\s*([NSEWnsew])?$", re.IGNORECASE
)


def _norm_key(key: str) -> str:
    return re.sub(r"[_\s]+", " ", key.strip().lower())


def _is_placeholder(text: str) -> bool:
    return text.strip().lower().strip(".") in NO_VALUE_PLACEHOLDERS


def _validate_latlon(lat: float, lon: float) -> Optional[tuple[float, float]]:
    if abs(lat) > 90 and abs(lon) <= 90:
        lat, lon = lon, lat
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return lat, lon


def parse_lat_lon(text: str) -> Optional[tuple[float, float]]:
    """Parse a combined ``lat_lon``/``lat lon`` string into (lat, lon).

    Handles ENA decimal pairs ("57.25689, 11.14193") and NCBI hemisphere
    pairs ("57.25 N 11.14 E", "36.9 S 174.7 W"). Returns None for
    placeholders, malformed input, or out-of-range coordinates.
    """
    if not text:
        return None
    s = " ".join(text.strip().split())
    if not s or _is_placeholder(s):
        return None

    hem_matches = _HEMISPHERE_RE.findall(s)
    if len(hem_matches) == 2:
        lat: Optional[float] = None
        lon: Optional[float] = None
        for val_s, hem in hem_matches:
            val = float(val_s)
            hem = hem.upper()
            if hem in ("N", "S"):
                lat = val if hem == "N" else -val
            else:
                lon = val if hem == "E" else -val
        if lat is None or lon is None:
            return None
        return _validate_latlon(lat, lon)

    parts = [p for p in re.split(r"[,\s]+", s.strip()) if p]
    if len(parts) != 2:
        return None
    try:
        a, b = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return _validate_latlon(a, b)


def _parse_single(text: str) -> Optional[float]:
    """Parse one coordinate value, tolerating a unit suffix or hemisphere letter."""
    if not text:
        return None
    s = " ".join(text.strip().split())
    if not s or _is_placeholder(s):
        return None
    m = _SINGLE_RE.match(s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None
    val = float(m.group(1))
    hem = m.group(2)
    if hem and hem.upper() in ("S", "W"):
        val = -val
    return val


def _first_kv(attrs: dict[str, str], keys: tuple[str, ...]) -> tuple[Optional[str], Optional[str]]:
    for key in keys:
        val = attrs.get(key)
        if val:
            return key, val
    return None, None


def locate(attributes: dict[str, str]) -> Location:
    """Derive a Location from a sample's flattened characteristics dict.

    Tries coordinates, then locality, then country, in that order, and
    always fills country/region metadata from whichever text is available.
    """
    attrs = {_norm_key(k): v.strip() for k, v in attributes.items() if isinstance(v, str) and v.strip()}
    loc = Location()

    combined = attrs.get(_LAT_LON_COMBINED_KEY)
    if combined and not _is_placeholder(combined):
        coords = parse_lat_lon(combined)
        if coords:
            loc.lat, loc.lon = coords
            loc.precision = "coordinates"
            loc.source_field = "lat_lon"
            loc.raw_latlon_text = combined

    if loc.lat is None:
        lat_key, lat_raw = _first_kv(attrs, _LAT_KEYS)
        lon_key, lon_raw = _first_kv(attrs, _LON_KEYS)
        if lat_raw and lon_raw and not _is_placeholder(lat_raw) and not _is_placeholder(lon_raw):
            lat_val = _parse_single(lat_raw)
            lon_val = _parse_single(lon_raw)
            if lat_val is not None and lon_val is not None:
                validated = _validate_latlon(lat_val, lon_val)
                if validated:
                    loc.lat, loc.lon = validated
                    loc.precision = "coordinates"
                    loc.source_field = f"{lat_key}/{lon_key}"
                    loc.raw_latlon_text = f"{lat_raw} / {lon_raw}"

    geo_loc_name = attrs.get(_GEO_LOC_NAME_KEY, "")
    gln_country, gln_region = "", ""
    if geo_loc_name and not _is_placeholder(geo_loc_name):
        if ":" in geo_loc_name:
            before, after = geo_loc_name.split(":", 1)
            gln_country, gln_region = before.strip(), after.strip()
        else:
            gln_country = geo_loc_name.strip()

    region_val, region_field = "", ""
    if attrs.get(_REGION_LOCALITY_KEY) and not _is_placeholder(attrs[_REGION_LOCALITY_KEY]):
        region_val, region_field = attrs[_REGION_LOCALITY_KEY], _REGION_LOCALITY_KEY
    elif gln_region:
        region_val, region_field = gln_region, "geo_loc_name"
    elif attrs.get(_REGION_KEY) and not _is_placeholder(attrs[_REGION_KEY]):
        region_val, region_field = attrs[_REGION_KEY], _REGION_KEY

    country_val, country_field = "", ""
    if attrs.get(_COUNTRY_SEA_KEY) and not _is_placeholder(attrs[_COUNTRY_SEA_KEY]):
        country_val, country_field = attrs[_COUNTRY_SEA_KEY], _COUNTRY_SEA_KEY
    elif gln_country:
        country_val, country_field = gln_country, "geo_loc_name"
    elif attrs.get(_COUNTRY_KEY) and not _is_placeholder(attrs[_COUNTRY_KEY]):
        country_val, country_field = attrs[_COUNTRY_KEY], _COUNTRY_KEY

    if loc.lat is None and region_val:
        hit = gazetteer.lookup_region(region_val, country_hint=country_val)
        if hit:
            loc.lat, loc.lon = hit
            loc.precision = "locality"
            loc.source_field = region_field

    if loc.lat is None and country_val:
        hit = gazetteer.lookup_country(country_val)
        if hit:
            loc.lat, loc.lon, loc.country_code = hit
            loc.precision = "country"
            loc.source_field = country_field

    if country_val:
        canon = gazetteer.normalise_country(country_val)
        loc.country = canon or country_val
        if not loc.country_code:
            hit = gazetteer.lookup_country(country_val)
            if hit:
                loc.country_code = hit[2]
    if region_val:
        loc.region = region_val

    if geo_loc_name:
        loc.raw_geo_text = geo_loc_name
    else:
        bits = [v for v in (country_val, region_val) if v]
        if bits:
            loc.raw_geo_text = " / ".join(bits)

    return loc


def apply_locations(samples: list[Sample]) -> None:
    """Populate ``sample.location`` in place for every sample."""
    for sample in samples:
        try:
            sample.location = locate(sample.attributes)
        except Exception:
            logger.exception("locate() failed for sample %s", sample.accession)


def bounding_box(samples: list[Sample]) -> Optional[tuple[float, float, float, float]]:
    """(min_lat, min_lon, max_lat, max_lon) over all located samples, or None."""
    lats = []
    lons = []
    for s in samples:
        if s.location.has_coords:
            lats.append(s.location.lat)
            lons.append(s.location.lon)
    if not lats:
        return None
    return min(lats), min(lons), max(lats), max(lons)


_EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def in_bbox(lat: Optional[float], lon: Optional[float],
            min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> bool:
    """True if the point is inside the box; an unlocated point is never inside.

    Takes bare coordinates rather than a :class:`Location` so the web layer,
    which works with plain dicts loaded from JSON, can call it directly.
    """
    if lat is None or lon is None:
        return False
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def within_radius(lat: Optional[float], lon: Optional[float],
                  center_lat: float, center_lon: float, radius_km: float) -> bool:
    """True if the point lies within ``radius_km`` of the centre."""
    if lat is None or lon is None:
        return False
    return haversine_km(lat, lon, center_lat, center_lon) <= radius_km


if __name__ == "__main__":
    def _check(label: str, cond: bool) -> None:
        status = "OK" if cond else "FAIL"
        print(f"[{status}] {label}")
        assert cond, label

    def _approx(a: Optional[float], b: float, tol: float = 1e-3) -> bool:
        return a is not None and abs(a - b) < tol

    # --- parse_lat_lon: real-world formats ---------------------------------
    _check("ENA decimal comma", parse_lat_lon("57.25689, 11.14193") == (57.25689, 11.14193))
    _check("ENA decimal no space", parse_lat_lon("57.25689,11.14193") == (57.25689, 11.14193))
    _check("ENA decimal whitespace", parse_lat_lon("-33.9 18.4") == (-33.9, 18.4))
    r = parse_lat_lon("57.25 N 11.14 E")
    _check("NCBI hemisphere spaced", r is not None and _approx(r[0], 57.25) and _approx(r[1], 11.14))
    r = parse_lat_lon("36.9 S 174.7 W")
    _check("NCBI hemisphere S/W", r is not None and _approx(r[0], -36.9) and _approx(r[1], -174.7))
    r = parse_lat_lon("57.25N 11.14E")
    _check("NCBI hemisphere no space", r is not None and _approx(r[0], 57.25) and _approx(r[1], 11.14))

    for placeholder in ("not collected", "not applicable", "missing", "NA", "", "n/a", "unknown", "-", "restricted access"):
        _check(f"placeholder {placeholder!r} -> None", parse_lat_lon(placeholder) is None)

    _check("out of range lat -> None", parse_lat_lon("120.0, 200.0") is None)
    _check("out of range lon -> None", parse_lat_lon("57.0, 200.0") is None)
    r = parse_lat_lon("100.0, 45.0")
    _check("unambiguous swap corrected", r is not None and _approx(r[0], 45.0) and _approx(r[1], 100.0))

    # --- separate-field parsing ---------------------------------------------
    _check("_parse_single unit suffix", _approx(_parse_single("57.25 DD"), 57.25))
    _check("_parse_single hemisphere S", _approx(_parse_single("36.9 S"), -36.9))
    _check("_parse_single plain", _approx(_parse_single("11.14"), 11.14))
    _check("_parse_single placeholder", _parse_single("not provided") is None)

    # --- locate(): realistic attribute dicts --------------------------------
    loc1 = locate({"lat lon": "57.25689, 11.14193", "geographic location (country and/or sea)": "Sweden"})
    _check("locate: lat lon -> coordinates", loc1.precision == "coordinates" and _approx(loc1.lat, 57.25689))
    _check("locate: country still filled", loc1.country == "Sweden")

    loc2 = locate({"geographic location (country and/or sea)": "Denmark"})
    _check("locate: country only -> country precision", loc2.precision == "country")
    _check("locate: Denmark centroid", _approx(loc2.lat, 56.26, 1.0) and _approx(loc2.lon, 9.50, 1.0))
    _check("locate: country_code DK", loc2.country_code == "DK")

    loc3 = locate({"geo_loc_name": "Denmark: Kattegat"})
    _check("locate: geo_loc_name locality -> locality precision", loc3.precision == "locality")
    _check("locate: geo_loc_name country filled", loc3.country == "Denmark")
    _check("locate: geo_loc_name region filled", loc3.region == "Kattegat")

    loc4 = locate({"organism": "Gadus morhua"})
    _check("locate: nothing -> none precision", loc4.precision == "none" and not loc4.has_coords)

    loc5 = locate({
        "geographic location (latitude)": "57.25 N",
        "geographic location (longitude)": "11.14 E",
    })
    _check("locate: separate lat/lon fields", loc5.precision == "coordinates" and _approx(loc5.lat, 57.25) and _approx(loc5.lon, 11.14))

    # --- gazetteer sanity checks on known centroids -------------------------
    dk = gazetteer.lookup_country("Denmark")
    jp = gazetteer.lookup_country("Japan")
    br = gazetteer.lookup_country("Brazil")
    _check("Denmark centroid ~56.0/10.0", dk is not None and _approx(dk[0], 56.0, 1.0) and _approx(dk[1], 10.0, 1.0))
    _check("Japan centroid ~36.2/138.3", jp is not None and _approx(jp[0], 36.2, 1.0) and _approx(jp[1], 138.3, 1.0))
    _check("Brazil centroid ~-14.2/-51.9", br is not None and _approx(br[0], -14.2, 1.0) and _approx(br[1], -51.9, 1.0))
    _check("USA alias resolves", gazetteer.lookup_country("USA") is not None)
    _check("Czech Republic alias resolves to Czechia", gazetteer.normalise_country("Czech Republic") == "Czechia")
    _check("Kattegat resolves as sea", gazetteer.lookup_country("Kattegat") is not None)

    # --- geometry helpers ----------------------------------------------------
    d = haversine_km(55.6761, 12.5683, 59.3293, 18.0686)  # Copenhagen -> Stockholm
    _check("haversine Copenhagen-Stockholm ~522km", 480 < d < 560)
    _check("in_bbox true", in_bbox(loc2.lat, loc2.lon, 50.0, 5.0, 60.0, 15.0))
    _check("in_bbox false", not in_bbox(loc2.lat, loc2.lon, 0.0, 0.0, 10.0, 10.0))
    _check("in_bbox unlocated", not in_bbox(None, None, 0.0, 0.0, 90.0, 90.0))
    _check("within_radius true", within_radius(loc2.lat, loc2.lon, 56.0, 10.0, 100.0))
    _check("within_radius false", not within_radius(loc2.lat, loc2.lon, 0.0, 0.0, 10.0))
    _check("within_radius unlocated", not within_radius(None, None, 0.0, 0.0, 1e6))

    fake_samples = [
        Sample(accession="A1", location=Location(lat=56.0, lon=10.0)),
        Sample(accession="A2", location=Location(lat=57.0, lon=11.0)),
        Sample(accession="A3", location=Location()),
    ]
    bbox = bounding_box(fake_samples)
    _check("bounding_box excludes unlocated", bbox == (56.0, 10.0, 57.0, 11.0))

    apply_locations(fake_samples)
    _check("apply_locations mutates in place", fake_samples[2].location.precision == "none")

    print("\nAll assertions passed.")
