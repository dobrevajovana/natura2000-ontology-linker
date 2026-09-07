"""Offline gazetteer: country/sea centroids and a modest locality table.

No network calls. All data is embedded so the app works air-gapped once
samples are fetched. Names are matched case-insensitively, punctuation- and
whitespace-insensitively, via an alias index built once at import time.
"""

import logging
import re

logger = logging.getLogger(__name__)


def _normalise(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = name.strip().lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# --- Countries: canonical name -> (lat, lon, ISO 3166-1 alpha-2) -----------

_COUNTRIES: dict[str, tuple[float, float, str]] = {
    "Afghanistan": (33.94, 67.71, "AF"),
    "Albania": (41.15, 20.17, "AL"),
    "Algeria": (28.03, 1.66, "DZ"),
    "Andorra": (42.55, 1.60, "AD"),
    "Angola": (-11.20, 17.87, "AO"),
    "Antigua and Barbuda": (17.06, -61.80, "AG"),
    "Argentina": (-38.42, -63.62, "AR"),
    "Armenia": (40.07, 45.04, "AM"),
    "Australia": (-25.27, 133.78, "AU"),
    "Austria": (47.52, 14.55, "AT"),
    "Azerbaijan": (40.14, 47.58, "AZ"),
    "Bahamas": (25.03, -77.40, "BS"),
    "Bahrain": (26.07, 50.56, "BH"),
    "Bangladesh": (23.68, 90.36, "BD"),
    "Barbados": (13.19, -59.54, "BB"),
    "Belarus": (53.71, 27.95, "BY"),
    "Belgium": (50.50, 4.47, "BE"),
    "Belize": (17.19, -88.50, "BZ"),
    "Benin": (9.31, 2.32, "BJ"),
    "Bhutan": (27.51, 90.43, "BT"),
    "Bolivia": (-16.29, -63.59, "BO"),
    "Bosnia and Herzegovina": (43.92, 17.68, "BA"),
    "Botswana": (-22.33, 24.68, "BW"),
    "Brazil": (-14.24, -51.93, "BR"),
    "Brunei": (4.54, 114.73, "BN"),
    "Bulgaria": (42.73, 25.49, "BG"),
    "Burkina Faso": (12.24, -1.56, "BF"),
    "Burundi": (-3.37, 29.92, "BI"),
    "Cabo Verde": (16.54, -24.01, "CV"),
    "Cambodia": (12.57, 104.99, "KH"),
    "Cameroon": (7.37, 12.35, "CM"),
    "Canada": (56.13, -106.35, "CA"),
    "Central African Republic": (6.61, 20.94, "CF"),
    "Chad": (15.45, 18.73, "TD"),
    "Chile": (-35.68, -71.54, "CL"),
    "China": (35.86, 104.20, "CN"),
    "Colombia": (4.57, -74.30, "CO"),
    "Comoros": (-11.88, 43.87, "KM"),
    "Congo": (-0.23, 15.83, "CG"),
    "Democratic Republic of the Congo": (-4.04, 21.76, "CD"),
    "Costa Rica": (9.75, -83.75, "CR"),
    "Croatia": (45.10, 15.20, "HR"),
    "Cuba": (21.52, -77.78, "CU"),
    "Cyprus": (35.13, 33.43, "CY"),
    "Czechia": (49.82, 15.47, "CZ"),
    "Denmark": (56.26, 9.50, "DK"),
    "Djibouti": (11.83, 42.59, "DJ"),
    "Dominica": (15.41, -61.37, "DM"),
    "Dominican Republic": (18.74, -70.16, "DO"),
    "Ecuador": (-1.83, -78.18, "EC"),
    "Egypt": (26.82, 30.80, "EG"),
    "El Salvador": (13.79, -88.90, "SV"),
    "Equatorial Guinea": (1.65, 10.27, "GQ"),
    "Eritrea": (15.18, 39.78, "ER"),
    "Estonia": (58.60, 25.01, "EE"),
    "Eswatini": (-26.52, 31.47, "SZ"),
    "Ethiopia": (9.15, 40.49, "ET"),
    "Fiji": (-17.71, 178.07, "FJ"),
    "Finland": (61.92, 25.75, "FI"),
    "France": (46.23, 2.21, "FR"),
    "Gabon": (-0.80, 11.61, "GA"),
    "Gambia": (13.44, -15.31, "GM"),
    "Georgia": (42.32, 43.36, "GE"),
    "Germany": (51.17, 10.45, "DE"),
    "Ghana": (7.95, -1.02, "GH"),
    "Greece": (39.07, 21.82, "GR"),
    "Greenland": (71.71, -42.60, "GL"),
    "Grenada": (12.11, -61.68, "GD"),
    "Guatemala": (15.78, -90.23, "GT"),
    "Guinea": (9.95, -9.70, "GN"),
    "Guinea-Bissau": (11.80, -15.18, "GW"),
    "Guyana": (4.86, -58.93, "GY"),
    "Haiti": (18.97, -72.29, "HT"),
    "Honduras": (15.20, -86.24, "HN"),
    "Hong Kong": (22.32, 114.17, "HK"),
    "Hungary": (47.16, 19.50, "HU"),
    "Iceland": (64.96, -19.02, "IS"),
    "India": (20.59, 78.96, "IN"),
    "Indonesia": (-0.79, 113.92, "ID"),
    "Iran": (32.43, 53.69, "IR"),
    "Iraq": (33.22, 43.68, "IQ"),
    "Ireland": (53.14, -7.69, "IE"),
    "Israel": (31.05, 34.85, "IL"),
    "Italy": (41.87, 12.57, "IT"),
    "Ivory Coast": (7.54, -5.55, "CI"),
    "Jamaica": (18.11, -77.30, "JM"),
    "Japan": (36.20, 138.25, "JP"),
    "Jordan": (30.59, 36.24, "JO"),
    "Kazakhstan": (48.02, 66.92, "KZ"),
    "Kenya": (-0.02, 37.91, "KE"),
    "Kiribati": (-3.37, -168.73, "KI"),
    "Kosovo": (42.60, 20.90, "XK"),
    "Kuwait": (29.31, 47.48, "KW"),
    "Kyrgyzstan": (41.20, 74.77, "KG"),
    "Laos": (19.86, 102.50, "LA"),
    "Latvia": (56.88, 24.60, "LV"),
    "Lebanon": (33.85, 35.86, "LB"),
    "Lesotho": (-29.61, 28.23, "LS"),
    "Liberia": (6.43, -9.43, "LR"),
    "Libya": (26.34, 17.23, "LY"),
    "Liechtenstein": (47.17, 9.56, "LI"),
    "Lithuania": (55.17, 23.88, "LT"),
    "Luxembourg": (49.82, 6.13, "LU"),
    "Macau": (22.20, 113.55, "MO"),
    "North Macedonia": (41.61, 21.75, "MK"),
    "Madagascar": (-18.77, 46.87, "MG"),
    "Malawi": (-13.25, 34.30, "MW"),
    "Malaysia": (4.21, 101.98, "MY"),
    "Maldives": (3.20, 73.22, "MV"),
    "Mali": (17.57, -4.00, "ML"),
    "Malta": (35.94, 14.38, "MT"),
    "Marshall Islands": (7.13, 171.18, "MH"),
    "Mauritania": (21.01, -10.94, "MR"),
    "Mauritius": (-20.35, 57.55, "MU"),
    "Mexico": (23.63, -102.55, "MX"),
    "Micronesia": (7.43, 150.55, "FM"),
    "Moldova": (47.41, 28.37, "MD"),
    "Monaco": (43.74, 7.42, "MC"),
    "Mongolia": (46.86, 103.85, "MN"),
    "Montenegro": (42.71, 19.37, "ME"),
    "Morocco": (31.79, -7.09, "MA"),
    "Mozambique": (-18.67, 35.53, "MZ"),
    "Myanmar": (21.91, 95.96, "MM"),
    "Namibia": (-22.96, 18.49, "NA"),
    "Nauru": (-0.52, 166.93, "NR"),
    "Nepal": (28.39, 84.12, "NP"),
    "Netherlands": (52.13, 5.29, "NL"),
    "New Zealand": (-40.90, 174.89, "NZ"),
    "Nicaragua": (12.87, -85.21, "NI"),
    "Niger": (17.61, 8.08, "NE"),
    "Nigeria": (9.08, 8.68, "NG"),
    "North Korea": (40.34, 127.51, "KP"),
    "Norway": (60.47, 8.47, "NO"),
    "Oman": (21.51, 55.92, "OM"),
    "Pakistan": (30.38, 69.35, "PK"),
    "Palau": (7.51, 134.58, "PW"),
    "Palestine": (31.95, 35.23, "PS"),
    "Panama": (8.54, -80.78, "PA"),
    "Papua New Guinea": (-6.31, 143.96, "PG"),
    "Paraguay": (-23.44, -58.44, "PY"),
    "Peru": (-9.19, -75.02, "PE"),
    "Philippines": (12.88, 121.77, "PH"),
    "Poland": (51.92, 19.15, "PL"),
    "Portugal": (39.40, -8.22, "PT"),
    "Puerto Rico": (18.22, -66.59, "PR"),
    "Qatar": (25.35, 51.18, "QA"),
    "Romania": (45.94, 24.97, "RO"),
    "Russia": (61.52, 105.32, "RU"),
    "Rwanda": (-1.94, 29.87, "RW"),
    "Saint Kitts and Nevis": (17.36, -62.78, "KN"),
    "Saint Lucia": (13.91, -60.98, "LC"),
    "Saint Vincent and the Grenadines": (12.98, -61.29, "VC"),
    "Samoa": (-13.76, -172.10, "WS"),
    "San Marino": (43.94, 12.46, "SM"),
    "Sao Tome and Principe": (0.19, 6.61, "ST"),
    "Saudi Arabia": (23.89, 45.08, "SA"),
    "Senegal": (14.50, -14.45, "SN"),
    "Serbia": (44.02, 21.01, "RS"),
    "Seychelles": (-4.68, 55.49, "SC"),
    "Sierra Leone": (8.46, -11.78, "SL"),
    "Singapore": (1.35, 103.82, "SG"),
    "Slovakia": (48.67, 19.70, "SK"),
    "Slovenia": (46.15, 14.99, "SI"),
    "Solomon Islands": (-9.65, 160.16, "SB"),
    "Somalia": (5.15, 46.20, "SO"),
    "South Africa": (-30.56, 22.94, "ZA"),
    "South Korea": (35.91, 127.77, "KR"),
    "South Sudan": (6.88, 31.31, "SS"),
    "Spain": (40.46, -3.75, "ES"),
    "Sri Lanka": (7.87, 80.77, "LK"),
    "Sudan": (12.86, 30.22, "SD"),
    "Suriname": (3.92, -56.03, "SR"),
    "Sweden": (60.13, 18.64, "SE"),
    "Switzerland": (46.82, 8.23, "CH"),
    "Syria": (34.80, 39.00, "SY"),
    "Taiwan": (23.70, 120.96, "TW"),
    "Tajikistan": (38.86, 71.28, "TJ"),
    "Tanzania": (-6.37, 34.89, "TZ"),
    "Thailand": (15.87, 100.99, "TH"),
    "Timor-Leste": (-8.87, 125.73, "TL"),
    "Togo": (8.62, 0.82, "TG"),
    "Tonga": (-21.18, -175.20, "TO"),
    "Trinidad and Tobago": (10.69, -61.22, "TT"),
    "Tunisia": (33.89, 9.54, "TN"),
    "Turkey": (38.96, 35.24, "TR"),
    "Turkmenistan": (38.97, 59.56, "TM"),
    "Tuvalu": (-7.11, 177.65, "TV"),
    "Uganda": (1.37, 32.29, "UG"),
    "Ukraine": (48.38, 31.17, "UA"),
    "United Arab Emirates": (23.42, 53.85, "AE"),
    "United Kingdom": (55.38, -3.44, "GB"),
    "United States of America": (37.09, -95.71, "US"),
    "Uruguay": (-32.52, -55.77, "UY"),
    "Uzbekistan": (41.38, 64.59, "UZ"),
    "Vanuatu": (-15.38, 166.96, "VU"),
    "Vatican City": (41.90, 12.45, "VA"),
    "Venezuela": (6.42, -66.59, "VE"),
    "Vietnam": (14.06, 108.28, "VN"),
    "Yemen": (15.55, 48.52, "YE"),
    "Zambia": (-13.13, 27.85, "ZM"),
    "Zimbabwe": (-19.02, 29.15, "ZW"),
    "Antarctica": (-82.86, 135.0, "AQ"),
}

# Alias (as archives spell it) -> canonical name in _COUNTRIES.
_COUNTRY_ALIASES: dict[str, str] = {
    "USA": "United States of America",
    "United States": "United States of America",
    "US": "United States of America",
    "America": "United States of America",
    "UK": "United Kingdom",
    "Great Britain": "United Kingdom",
    "England": "United Kingdom",
    "Scotland": "United Kingdom",
    "Wales": "United Kingdom",
    "Northern Ireland": "United Kingdom",
    "Republic of Korea": "South Korea",
    "Korea, Republic of": "South Korea",
    "Democratic People's Republic of Korea": "North Korea",
    "Korea, Democratic People's Republic of": "North Korea",
    "Russian Federation": "Russia",
    "Czech Republic": "Czechia",
    "Viet Nam": "Vietnam",
    "Islamic Republic of Iran": "Iran",
    "United Republic of Tanzania": "Tanzania",
    "Plurinational State of Bolivia": "Bolivia",
    "Bolivarian Republic of Venezuela": "Venezuela",
    "Syrian Arab Republic": "Syria",
    "Lao People's Democratic Republic": "Laos",
    "Republic of Moldova": "Moldova",
    "Macedonia": "North Macedonia",
    "FYROM": "North Macedonia",
    "Former Yugoslav Republic of Macedonia": "North Macedonia",
    "Cote d'Ivoire": "Ivory Coast",
    "Côte d'Ivoire": "Ivory Coast",
    "Cape Verde": "Cabo Verde",
    "Swaziland": "Eswatini",
    "Burma": "Myanmar",
    "DRC": "Democratic Republic of the Congo",
    "DR Congo": "Democratic Republic of the Congo",
    "Congo-Kinshasa": "Democratic Republic of the Congo",
    "Zaire": "Democratic Republic of the Congo",
    "Congo-Brazzaville": "Congo",
    "Republic of the Congo": "Congo",
    "Holland": "Netherlands",
    "UAE": "United Arab Emirates",
    "Ivory Coast (Cote d'Ivoire)": "Ivory Coast",
    "Bosnia": "Bosnia and Herzegovina",
    "Brunei Darussalam": "Brunei",
    "Cabo Verde (Cape Verde)": "Cabo Verde",
    "Republic of Ireland": "Ireland",
    "Vatican": "Vatican City",
    "Holy See": "Vatican City",
    "Trinidad": "Trinidad and Tobago",
    "St Kitts and Nevis": "Saint Kitts and Nevis",
    "St Lucia": "Saint Lucia",
    "St Vincent and the Grenadines": "Saint Vincent and the Grenadines",
    "PRC": "China",
    "People's Republic of China": "China",
    "Republic of China": "Taiwan",
}

# --- Seas & oceans: name -> (lat, lon); ISO code is always empty. ----------

_SEAS: dict[str, tuple[float, float]] = {
    "Atlantic Ocean": (0.0, -25.0),
    "Pacific Ocean": (0.0, -160.0),
    "Indian Ocean": (-20.0, 80.0),
    "Arctic Ocean": (85.0, 0.0),
    "Southern Ocean": (-65.0, 0.0),
    "Mediterranean Sea": (35.0, 18.0),
    "North Sea": (56.0, 3.5),
    "Baltic Sea": (58.0, 20.0),
    "Kattegat": (57.0, 11.3),
    "Skagerrak": (58.0, 9.0),
    "Black Sea": (43.4, 34.0),
    "Red Sea": (20.0, 38.0),
    "Caribbean Sea": (15.0, -73.0),
    "Caspian Sea": (41.5, 50.7),
    "Coral Sea": (-18.0, 152.0),
    "Tasman Sea": (-40.0, 163.0),
    "Bering Sea": (58.0, -178.0),
    "Barents Sea": (74.0, 36.0),
    "Norwegian Sea": (68.0, 2.0),
    "South China Sea": (12.0, 113.0),
    "Sea of Japan": (40.0, 135.0),
    "Gulf of Mexico": (25.0, -90.0),
    "Persian Gulf": (26.5, 52.0),
    "Adriatic Sea": (43.0, 15.0),
    "Aegean Sea": (38.9, 25.3),
    "Irish Sea": (53.5, -5.0),
    "Celtic Sea": (50.0, -7.0),
    "Weddell Sea": (-72.0, -45.0),
    "Ross Sea": (-75.0, -175.0),
    "Labrador Sea": (58.0, -53.0),
    "Greenland Sea": (77.0, -1.0),
    "Beaufort Sea": (73.0, -140.0),
    "Chukchi Sea": (69.0, -171.0),
    "Laptev Sea": (76.0, 125.0),
    "Kara Sea": (76.0, 80.0),
    "White Sea": (65.9, 37.0),
    "Bay of Bengal": (15.0, 88.0),
    "Arabian Sea": (15.0, 65.0),
    "Gulf of Aden": (12.5, 48.0),
    "Gulf of Guinea": (1.0, 3.0),
    "Bay of Biscay": (45.0, -4.0),
    "English Channel": (50.0, -1.0),
}

# --- Regions / localities: name -> (lat, lon). Natura2000-focused. --------

_REGIONS: dict[str, tuple[float, float]] = {
    "Kattegat": (57.0, 11.3),
    "Skagerrak": (58.0, 9.0),
    "Wadden Sea": (53.5, 6.0),
    "Limfjord": (56.85, 9.2),
    "Oresund": (55.85, 12.7),
    "Bothnian Bay": (65.0, 22.0),
    "Gulf of Finland": (59.8, 26.0),
    "Gulf of Riga": (57.6, 23.5),
    "Doggerbank": (54.75, 2.85),
    "Dogger Bank": (54.75, 2.85),
    "Jutland": (56.3, 9.5),
    "Zealand": (55.4, 11.8),
    "Scania": (55.9, 13.6),
    "Skane": (55.9, 13.6),
    "Bavaria": (48.95, 11.4),
    "Provence": (43.9, 5.7),
    "Tuscany": (43.4, 11.1),
    "Andalusia": (37.5, -4.7),
    "Catalonia": (41.8, 1.9),
    "Normandy": (49.2, 0.0),
    "Brittany": (48.2, -3.0),
    "Lombardy": (45.5, 9.5),
    "Rhineland": (50.7, 7.0),
    "London": (51.5074, -0.1278),
    "Paris": (48.8566, 2.3522),
    "Berlin": (52.52, 13.405),
    "Copenhagen": (55.6761, 12.5683),
    "Stockholm": (59.3293, 18.0686),
    "Oslo": (59.9139, 10.7522),
    "Amsterdam": (52.3676, 4.9041),
    "Madrid": (40.4168, -3.7038),
    "Rome": (41.9028, 12.4964),
    "Vienna": (48.2082, 16.3738),
    "Helsinki": (60.1699, 24.9384),
    "Reykjavik": (64.1466, -21.9426),
    "New York": (40.7128, -74.0060),
    "Los Angeles": (34.0522, -118.2437),
    "Beijing": (39.9042, 116.4074),
    "Tokyo": (35.6762, 139.6503),
    "Sydney": (-33.8688, 151.2093),
    "Cape Town": (-33.9249, 18.4241),
    "Nairobi": (-1.2921, 36.8219),
    "Rio de Janeiro": (-22.9068, -43.1729),
    "Buenos Aires": (-34.6037, -58.3816),
    "Moscow": (55.7558, 37.6173),
    "Delhi": (28.7041, 77.1025),
    "Mumbai": (19.0760, 72.8777),
}


def _build_index() -> dict[str, tuple[str, float, float, str]]:
    """normalised alias -> (canonical name, lat, lon, iso)."""
    idx: dict[str, tuple[str, float, float, str]] = {}
    for name, (lat, lon, iso) in _COUNTRIES.items():
        idx[_normalise(name)] = (name, lat, lon, iso)
    for alias, canonical in _COUNTRY_ALIASES.items():
        lat, lon, iso = _COUNTRIES[canonical]
        idx[_normalise(alias)] = (canonical, lat, lon, iso)
    for name, (lat, lon) in _SEAS.items():
        idx[_normalise(name)] = (name, lat, lon, "")
    return idx


def _build_region_index() -> dict[str, tuple[float, float]]:
    return {_normalise(name): coords for name, coords in _REGIONS.items()}


_INDEX = _build_index()
_REGION_INDEX = _build_region_index()

logger.debug(
    "gazetteer loaded: %d countries, %d seas, %d regions",
    len(_COUNTRIES), len(_SEAS), len(_REGIONS),
)


def lookup_country(name: str) -> tuple[float, float, str] | None:
    """Resolve a country or sea/ocean name to (lat, lon, iso_alpha2).

    Seas carry an empty ISO code. Returns None when unknown.
    """
    if not name:
        return None
    hit = _INDEX.get(_normalise(name))
    if hit is None:
        return None
    _, lat, lon, iso = hit
    return lat, lon, iso


def lookup_region(name: str, country_hint: str = "") -> tuple[float, float] | None:
    """Resolve a named locality/region/marine sub-basin to (lat, lon).

    ``country_hint`` is accepted for future disambiguation but a bare
    name-match is used since the embedded table has no colliding names.
    Falls back to the country/sea table so a locality value that is
    actually a sea name (e.g. "Kattegat") still resolves.
    """
    if not name:
        return None
    key = _normalise(name)
    if key in _REGION_INDEX:
        return _REGION_INDEX[key]
    hit = _INDEX.get(key)
    if hit is not None:
        return hit[1], hit[2]
    return None


def normalise_country(name: str) -> str:
    """Canonical country/sea name for ``name``, or "" if unknown."""
    if not name:
        return ""
    hit = _INDEX.get(_normalise(name))
    return hit[0] if hit is not None else ""
