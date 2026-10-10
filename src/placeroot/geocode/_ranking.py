"""Name normalisation, match tiers and result ranking for geocode()."""

import math
import re
import sys as _sys
import unicodedata

from placeroot import home_region

_pkg = _sys.modules["placeroot.geocode"]


def _kick_autowarm(hit: dict | None) -> None:
    """Background metro warm on a city-scale hit. Never raises, never waits."""
    try:
        from placeroot import autowarm

        autowarm.maybe_autowarm_hit(hit)
    except Exception:  # noqa: BLE001 - resolve must not fail because warm failed
        _pkg.logger.warning("autowarm kick failed", exc_info=True)


DEFAULT_LIMIT = 5

MAX_LIMIT = 25

DIVISION_OVERFETCH = 50  # rows pulled per theme before Python-side ranking trims to `limit`


# #83: bbox radius for the places-theme fallback once an anchor point (a
# division match already in hand, or one derived from a trailing word in the
# query — see _fallback_anchor) is available. Same "same metro area" idea as
# resolve_place's own _RESOLVE_PLACE_RADIUS_M, just defined here too since
# geocode() needs it before that constant's #22 section further down.
_PLACES_FALLBACK_RADIUS_M = 30_000


# Longest query (in words) whose every leading word _fallback_anchor will try
# as an anchor of its own. Each try is one local-table lookup, and a query
# long enough to exceed this is prose rather than a "place name + city"
# phrase, where the trailing-suffix reading is the only one worth paying for.
_MAX_ANCHOR_TOKENS = 6


# Words that *begin* place names and never locate one by themselves. "san
# jose airport" split on the leading "san", which exact-matched a division in
# Henan and anchored a South Bay query at 36.2N 115.7E — the residual "jose
# airport" then found nothing, 7.7s later. Distinct from
# _GENERIC_PLACE_WORDS: those say what a place is, these are the first half
# of its name, and both are useless as an anchor on their own.
_NAME_PREFIX_WORDS = frozenset(
    """
    big cape east el fort grand la las little los lower monte mount new north
    old port saint san santa santo sao são sierra south st ste upper villa west
""".split()
)


# How much population a specific division (a city) must carry, relative to the
# broad one (its state/country) sharing the name, before it is preferred as a
# search anchor. New York City is 43% of New York State and is what "New York"
# means in "Times Square New York"; a hamlet named Tokyo is 0.04% of 東京都 and
# must never displace it. See _pick_anchor_row.
_ANCHOR_SPECIFIC_SHARE = 0.1


# #463: the same share, applied to the *answer* ranking rather than the
# anchor pick. A locality carrying at least this fraction of its own region's
# (or country's) population, under the same name, is what the name means as
# an answer too — São Paulo city is 25% of São Paulo state, and Nominatim,
# Photon and Pelias all return the city first. Kept as its own name so the
# two uses can diverge later without one silently moving the other; see
# _flag_namesake_localities.
_NAMESAKE_LOCALITY_SHARE = _ANCHOR_SPECIFIC_SHARE


# Words that say what a place *is*, never where it is. A one-word anchor
# candidate drawn from this set is refused outright.
#
# #268: _query_divisions matches substrings, so every one of these finds a
# division somewhere — "Center" found Center, Pennsylvania and sent a Palo
# Alto query to Pittsburgh; "Tower" found Tower Grove in St. Louis and spent
# 33s scanning Missouri for the Eiffel Tower. The result is not a weak
# anchor, it is a confidently wrong one, and it is worse than no anchor:
# without one the caller returns in under a second and tells the user to name
# a city, which is an answer they can act on.
#
# Only single-word candidates are checked. A multi-word candidate carries its
# own qualifier ("Park Ridge", "Union Square") and is a real name again, and
# any of these words *is* allowed to anchor when the user supplies nothing
# else to go on — the rule is about not preferring a feature noun over a
# genuine place name, not about banning the string.
#
# #469: the same set is what _place_match_label refuses to accept as the
# *only* word a place candidate shares with the query. The two uses are one
# judgment: a word that says what a place is cannot say which place it is —
# not as an anchor, and not as evidence that "Snow Peak Land Station" has
# anything to do with "Shibuya Station". "hall" and "shrine" were added for
# that use and are refused as anchors under the same reasoning.
_GENERIC_PLACE_WORDS = frozenset(
    """
    academy airport aquarium arena avenue basilica bay beach boulevard bridge dam falls
    building campus castle cathedral centre center chapel church cinema clinic
    club college crossing dock field fountain garden gardens gate gym hall harbor
    harbour hospital hotel institute island junction library mall market
    memorial monument mosque museum observatory palace park pharmacy pier
    plaza port preschool quay resort restaurant road school shrine square stadium
    station store street studio synagogue temple terminal theater theatre
    tower university wharf zoo
""".split()
)


# Subdirectory (under cache.cache_dir()/<release>/) for the #43 materialized
# divisions name table — kept distinct from cache.py's own places/<theme>
# tile layout so the two never collide on a filename, even though they
# share the same cache dir and eviction pool.
_DIVISIONS_TABLE_SUBDIR = "geocode-divisions"

_DIVISIONS_TABLE_FILENAME = "table.parquet"

# #214: the alternate-name table, written alongside the primary one.
_ALT_NAMES_TABLE_FILENAME = "alt_names.parquet"

# #410: the language-tagged name table, written alongside the primary one.
# Distinct from the alt-name table above: that one folds/dedupes
# names.common down to spellings for *searching* and discards which
# language each came from; this one keeps (id, lang) intact for *serving*
# the caller's requested language back — the two answer different
# questions off the same source column, so keeping them separate tables
# means neither has to carry columns the other's query pattern doesn't use.
_LANG_NAMES_TABLE_FILENAME = "lang_names.parquet"


# #224: the bbox columns carried by the materialized divisions table. Named
# with a bbox_ prefix rather than reusing the struct so the stale-cache check
# is a plain column-name test (see _divisions_table_has_bbox), and so `lat`/
# `lon` -- which are bbox.ymin/bbox.xmin, unchanged since #43 -- keep meaning
# exactly what they meant before.
_DIVISIONS_BBOX_COLUMNS = ("bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax")


# Below this span (degrees, in either axis) a bbox is a point, not an extent.
# Overture's division rows store a point's float32 rounding envelope: measured
# live at 7.6e-6 to 1.5e-5 degrees wide, so this floor sits ~6x above the
# widest observed noise and far below any real division polygon -- even a
# single city block spans more than 1e-4 degrees (~11m). See the module
# docstring's #224 section.
_DEGENERATE_BBOX_SPAN_DEG = 1e-4


# Bigger number = ranked higher among same name-match tier. Chosen so a
# free-text query like "Springfield" surfaces the city before a same-named
# neighborhood or the containing state, which is the common case for an
# agent asking "where is X". Used both as a direct tiebreak and (#47) as
# part of the no-population proxy chain.
_SUBTYPE_WEIGHT = {
    "locality": 4,
    "localadmin": 3,
    "neighborhood": 2,
    "region": 1,
    "county": 1,
    "country": 0,
    "dependency": 0,
}


# Embedded 50-state map (#46): abbreviation -> full name. Covers the common
# "City, ST" case without any extra query. Region suffixes this doesn't
# recognize (non-US regions, spelled-out names not listed here) fall back
# to _resolve_region_from_table.
US_STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}

_US_STATES_BY_NAME = {name.lower(): abbr for abbr, name in US_STATES.items()}


# Embedded ISO 3166-1 table (#457): alpha-2 -> (name, alpha-3). All 249
# currently-assigned codes, generic lookup rather than a handful of
# hardcoded countries -- any 2-letter uppercase suffix that is a key here is
# a country-suffix hit, and the paired alpha-3 is recognized too
# (COUNTRIES_BY_ALPHA3 below is derived from this, not maintained by hand).
COUNTRIES = {
    "AF": ("Afghanistan", "AFG"),
    "AX": ("Aland Islands", "ALA"),
    "AL": ("Albania", "ALB"),
    "DZ": ("Algeria", "DZA"),
    "AS": ("American Samoa", "ASM"),
    "AD": ("Andorra", "AND"),
    "AO": ("Angola", "AGO"),
    "AI": ("Anguilla", "AIA"),
    "AQ": ("Antarctica", "ATA"),
    "AG": ("Antigua and Barbuda", "ATG"),
    "AR": ("Argentina", "ARG"),
    "AM": ("Armenia", "ARM"),
    "AW": ("Aruba", "ABW"),
    "AU": ("Australia", "AUS"),
    "AT": ("Austria", "AUT"),
    "AZ": ("Azerbaijan", "AZE"),
    "BS": ("Bahamas", "BHS"),
    "BH": ("Bahrain", "BHR"),
    "BD": ("Bangladesh", "BGD"),
    "BB": ("Barbados", "BRB"),
    "BY": ("Belarus", "BLR"),
    "BE": ("Belgium", "BEL"),
    "BZ": ("Belize", "BLZ"),
    "BJ": ("Benin", "BEN"),
    "BM": ("Bermuda", "BMU"),
    "BT": ("Bhutan", "BTN"),
    "BO": ("Bolivia", "BOL"),
    "BQ": ("Bonaire, Sint Eustatius and Saba", "BES"),
    "BA": ("Bosnia and Herzegovina", "BIH"),
    "BW": ("Botswana", "BWA"),
    "BV": ("Bouvet Island", "BVT"),
    "BR": ("Brazil", "BRA"),
    "IO": ("British Indian Ocean Territory", "IOT"),
    "BN": ("Brunei Darussalam", "BRN"),
    "BG": ("Bulgaria", "BGR"),
    "BF": ("Burkina Faso", "BFA"),
    "BI": ("Burundi", "BDI"),
    "CV": ("Cabo Verde", "CPV"),
    "KH": ("Cambodia", "KHM"),
    "CM": ("Cameroon", "CMR"),
    "CA": ("Canada", "CAN"),
    "KY": ("Cayman Islands", "CYM"),
    "CF": ("Central African Republic", "CAF"),
    "TD": ("Chad", "TCD"),
    "CL": ("Chile", "CHL"),
    "CN": ("China", "CHN"),
    "CX": ("Christmas Island", "CXR"),
    "CC": ("Cocos (Keeling) Islands", "CCK"),
    "CO": ("Colombia", "COL"),
    "KM": ("Comoros", "COM"),
    "CG": ("Congo", "COG"),
    "CD": ("Congo, Democratic Republic of the", "COD"),
    "CK": ("Cook Islands", "COK"),
    "CR": ("Costa Rica", "CRI"),
    "CI": ("Cote d'Ivoire", "CIV"),
    "HR": ("Croatia", "HRV"),
    "CU": ("Cuba", "CUB"),
    "CW": ("Curacao", "CUW"),
    "CY": ("Cyprus", "CYP"),
    "CZ": ("Czechia", "CZE"),
    "DK": ("Denmark", "DNK"),
    "DJ": ("Djibouti", "DJI"),
    "DM": ("Dominica", "DMA"),
    "DO": ("Dominican Republic", "DOM"),
    "EC": ("Ecuador", "ECU"),
    "EG": ("Egypt", "EGY"),
    "SV": ("El Salvador", "SLV"),
    "GQ": ("Equatorial Guinea", "GNQ"),
    "ER": ("Eritrea", "ERI"),
    "EE": ("Estonia", "EST"),
    "SZ": ("Eswatini", "SWZ"),
    "ET": ("Ethiopia", "ETH"),
    "FK": ("Falkland Islands (Malvinas)", "FLK"),
    "FO": ("Faroe Islands", "FRO"),
    "FJ": ("Fiji", "FJI"),
    "FI": ("Finland", "FIN"),
    "FR": ("France", "FRA"),
    "GF": ("French Guiana", "GUF"),
    "PF": ("French Polynesia", "PYF"),
    "TF": ("French Southern Territories", "ATF"),
    "GA": ("Gabon", "GAB"),
    "GM": ("Gambia", "GMB"),
    "GE": ("Georgia", "GEO"),
    "DE": ("Germany", "DEU"),
    "GH": ("Ghana", "GHA"),
    "GI": ("Gibraltar", "GIB"),
    "GR": ("Greece", "GRC"),
    "GL": ("Greenland", "GRL"),
    "GD": ("Grenada", "GRD"),
    "GP": ("Guadeloupe", "GLP"),
    "GU": ("Guam", "GUM"),
    "GT": ("Guatemala", "GTM"),
    "GG": ("Guernsey", "GGY"),
    "GN": ("Guinea", "GIN"),
    "GW": ("Guinea-Bissau", "GNB"),
    "GY": ("Guyana", "GUY"),
    "HT": ("Haiti", "HTI"),
    "HM": ("Heard Island and McDonald Islands", "HMD"),
    "VA": ("Holy See", "VAT"),
    "HN": ("Honduras", "HND"),
    "HK": ("Hong Kong", "HKG"),
    "HU": ("Hungary", "HUN"),
    "IS": ("Iceland", "ISL"),
    "IN": ("India", "IND"),
    "ID": ("Indonesia", "IDN"),
    "IR": ("Iran", "IRN"),
    "IQ": ("Iraq", "IRQ"),
    "IE": ("Ireland", "IRL"),
    "IM": ("Isle of Man", "IMN"),
    "IL": ("Israel", "ISR"),
    "IT": ("Italy", "ITA"),
    "JM": ("Jamaica", "JAM"),
    "JP": ("Japan", "JPN"),
    "JE": ("Jersey", "JEY"),
    "JO": ("Jordan", "JOR"),
    "KZ": ("Kazakhstan", "KAZ"),
    "KE": ("Kenya", "KEN"),
    "KI": ("Kiribati", "KIR"),
    "KP": ("Korea, Democratic People's Republic of", "PRK"),
    "KR": ("Korea, Republic of", "KOR"),
    "KW": ("Kuwait", "KWT"),
    "KG": ("Kyrgyzstan", "KGZ"),
    "LA": ("Lao People's Democratic Republic", "LAO"),
    "LV": ("Latvia", "LVA"),
    "LB": ("Lebanon", "LBN"),
    "LS": ("Lesotho", "LSO"),
    "LR": ("Liberia", "LBR"),
    "LY": ("Libya", "LBY"),
    "LI": ("Liechtenstein", "LIE"),
    "LT": ("Lithuania", "LTU"),
    "LU": ("Luxembourg", "LUX"),
    "MO": ("Macao", "MAC"),
    "MG": ("Madagascar", "MDG"),
    "MW": ("Malawi", "MWI"),
    "MY": ("Malaysia", "MYS"),
    "MV": ("Maldives", "MDV"),
    "ML": ("Mali", "MLI"),
    "MT": ("Malta", "MLT"),
    "MH": ("Marshall Islands", "MHL"),
    "MQ": ("Martinique", "MTQ"),
    "MR": ("Mauritania", "MRT"),
    "MU": ("Mauritius", "MUS"),
    "YT": ("Mayotte", "MYT"),
    "MX": ("Mexico", "MEX"),
    "FM": ("Micronesia", "FSM"),
    "MD": ("Moldova", "MDA"),
    "MC": ("Monaco", "MCO"),
    "MN": ("Mongolia", "MNG"),
    "ME": ("Montenegro", "MNE"),
    "MS": ("Montserrat", "MSR"),
    "MA": ("Morocco", "MAR"),
    "MZ": ("Mozambique", "MOZ"),
    "MM": ("Myanmar", "MMR"),
    "NA": ("Namibia", "NAM"),
    "NR": ("Nauru", "NRU"),
    "NP": ("Nepal", "NPL"),
    "NL": ("Netherlands", "NLD"),
    "NC": ("New Caledonia", "NCL"),
    "NZ": ("New Zealand", "NZL"),
    "NI": ("Nicaragua", "NIC"),
    "NE": ("Niger", "NER"),
    "NG": ("Nigeria", "NGA"),
    "NU": ("Niue", "NIU"),
    "NF": ("Norfolk Island", "NFK"),
    "MK": ("North Macedonia", "MKD"),
    "MP": ("Northern Mariana Islands", "MNP"),
    "NO": ("Norway", "NOR"),
    "OM": ("Oman", "OMN"),
    "PK": ("Pakistan", "PAK"),
    "PW": ("Palau", "PLW"),
    "PS": ("Palestine, State of", "PSE"),
    "PA": ("Panama", "PAN"),
    "PG": ("Papua New Guinea", "PNG"),
    "PY": ("Paraguay", "PRY"),
    "PE": ("Peru", "PER"),
    "PH": ("Philippines", "PHL"),
    "PN": ("Pitcairn", "PCN"),
    "PL": ("Poland", "POL"),
    "PT": ("Portugal", "PRT"),
    "PR": ("Puerto Rico", "PRI"),
    "QA": ("Qatar", "QAT"),
    "RE": ("Reunion", "REU"),
    "RO": ("Romania", "ROU"),
    "RU": ("Russian Federation", "RUS"),
    "RW": ("Rwanda", "RWA"),
    "BL": ("Saint Barthelemy", "BLM"),
    "SH": ("Saint Helena, Ascension and Tristan da Cunha", "SHN"),
    "KN": ("Saint Kitts and Nevis", "KNA"),
    "LC": ("Saint Lucia", "LCA"),
    "MF": ("Saint Martin (French part)", "MAF"),
    "PM": ("Saint Pierre and Miquelon", "SPM"),
    "VC": ("Saint Vincent and the Grenadines", "VCT"),
    "WS": ("Samoa", "WSM"),
    "SM": ("San Marino", "SMR"),
    "ST": ("Sao Tome and Principe", "STP"),
    "SA": ("Saudi Arabia", "SAU"),
    "SN": ("Senegal", "SEN"),
    "RS": ("Serbia", "SRB"),
    "SC": ("Seychelles", "SYC"),
    "SL": ("Sierra Leone", "SLE"),
    "SG": ("Singapore", "SGP"),
    "SX": ("Sint Maarten (Dutch part)", "SXM"),
    "SK": ("Slovakia", "SVK"),
    "SI": ("Slovenia", "SVN"),
    "SB": ("Solomon Islands", "SLB"),
    "SO": ("Somalia", "SOM"),
    "ZA": ("South Africa", "ZAF"),
    "GS": ("South Georgia and the South Sandwich Islands", "SGS"),
    "SS": ("South Sudan", "SSD"),
    "ES": ("Spain", "ESP"),
    "LK": ("Sri Lanka", "LKA"),
    "SD": ("Sudan", "SDN"),
    "SR": ("Suriname", "SUR"),
    "SJ": ("Svalbard and Jan Mayen", "SJM"),
    "SE": ("Sweden", "SWE"),
    "CH": ("Switzerland", "CHE"),
    "SY": ("Syrian Arab Republic", "SYR"),
    "TW": ("Taiwan", "TWN"),
    "TJ": ("Tajikistan", "TJK"),
    "TZ": ("Tanzania, United Republic of", "TZA"),
    "TH": ("Thailand", "THA"),
    "TL": ("Timor-Leste", "TLS"),
    "TG": ("Togo", "TGO"),
    "TK": ("Tokelau", "TKL"),
    "TO": ("Tonga", "TON"),
    "TT": ("Trinidad and Tobago", "TTO"),
    "TN": ("Tunisia", "TUN"),
    "TR": ("Turkiye", "TUR"),
    "TM": ("Turkmenistan", "TKM"),
    "TC": ("Turks and Caicos Islands", "TCA"),
    "TV": ("Tuvalu", "TUV"),
    "UG": ("Uganda", "UGA"),
    "UA": ("Ukraine", "UKR"),
    "AE": ("United Arab Emirates", "ARE"),
    "GB": ("United Kingdom", "GBR"),
    "US": ("United States", "USA"),
    "UM": ("United States Minor Outlying Islands", "UMI"),
    "UY": ("Uruguay", "URY"),
    "UZ": ("Uzbekistan", "UZB"),
    "VU": ("Vanuatu", "VUT"),
    "VE": ("Venezuela", "VEN"),
    "VN": ("Viet Nam", "VNM"),
    "VG": ("Virgin Islands (British)", "VGB"),
    "VI": ("Virgin Islands (U.S.)", "VIR"),
    "WF": ("Wallis and Futuna", "WLF"),
    "EH": ("Western Sahara", "ESH"),
    "YE": ("Yemen", "YEM"),
    "ZM": ("Zambia", "ZMB"),
    "ZW": ("Zimbabwe", "ZWE"),
}

# alpha-3 -> alpha-2, derived rather than hand-maintained so the two tables
# can't drift apart.
_COUNTRIES_BY_ALPHA3 = {a3: a2 for a2, (_name, a3) in COUNTRIES.items()}

_COUNTRIES_BY_NAME = {name.lower(): a2 for a2, (name, _a3) in COUNTRIES.items()}

# Common aliases that aren't the ISO short name itself, or aren't a plain
# alpha-2/alpha-3 code: "UK" is everyday English for GB (Overture's own
# `country` column uses GB, never UK), "USA"/"U.S."/"U.S.A." for US. Deliberately
# small and unambiguous — a name like "Georgia" that is also a US state stays
# out of this table and off the alias path entirely; it is resolved (if at
# all) through COUNTRIES/_COUNTRIES_BY_NAME or not resolved as a country.
_COUNTRY_ALIASES = {
    "UK": "GB",
    "U.K.": "GB",
    "U.S.": "US",
    "U.S.A.": "US",
}


def _strip_diacritics(s: str) -> str:
    """NFD-normalize and drop combining marks (#53) — "São Paulo" -> "Sao Paulo".

    Only used for matching; canonical names returned to callers are never
    passed through this.
    """
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def _normalize_for_match(s: str) -> str:
    return _strip_diacritics(s).lower()


# #214: Latin letters that carry no combining mark of their own, so neither
# Python's NFD pass (_strip_diacritics) nor DuckDB's strip_accents() touches
# them (duckdb#15706): "München" folds to "munchen", but "Preßburg" stays
# "preßburg" and "Łódź" only loses the ź. Overture's names.common is full of
# them — the German, Nordic, Polish and Icelandic exonyms are exactly the
# alternates an English-speaking caller reaches for — and a plain-ASCII query
# ("Pressburg", "Lodz", "Malmo") never reaches the row without this.
#
# Kept to letters with an unambiguous ASCII expansion, and applied *after*
# lower(), so only the lowercase forms need listing. Deliberately scoped to
# the #214 alternate-name fold rather than retrofitted onto
# _normalize_for_match: the primary-name tiers (#53) and the #215 fuzzy
# threshold were both measured against strip_accents alone, and widening
# their folding is a separate change with its own regressions to justify.
_UNFOLDED_LETTERS = {
    "ß": "ss",
    "ø": "o",
    "ł": "l",
    "đ": "d",
    "ð": "d",
    "þ": "th",
    "æ": "ae",
    "œ": "oe",
    "ħ": "h",
    "ı": "i",
}


def _fold_alt_name(s: str) -> str:
    """Python side of the #214 alternate-name fold: lowercase, accents
    stripped, then _UNFOLDED_LETTERS applied. Must stay byte-identical to
    _fold_alt_name_sql, which folds the stored column at materialization
    time — the two only ever meet as an equality/ILIKE comparison.
    """
    folded = _pkg._normalize_for_match(s)
    for src, dst in _UNFOLDED_LETTERS.items():
        folded = folded.replace(src, dst)
    return folded


def _fold_alt_name_sql(expr: str) -> str:
    """SQL twin of _fold_alt_name over `expr`. Used once per alternate at
    materialization time so the query-time comparison is a plain ILIKE on a
    stored column rather than a per-row function chain."""
    sql = f"lower(strip_accents({expr}))"
    for src, dst in _UNFOLDED_LETTERS.items():
        sql = f"replace({sql}, '{src}', '{dst}')"
    return sql


_TIER_PUNCT_RE = re.compile(r"[^\w\s]+")


def _fold_for_tier(s: str) -> str:
    """Comparison form for _match_tier: NFKD, combining marks dropped,
    casefolded (so "Straße" and "STRASSE" agree, which lower() alone does
    not), punctuation collapsed to spaces and whitespace squeezed (so
    "Notre-Dame" and "notre dame" agree).

    Distinct from _normalize_for_match on purpose: that fold is shared
    with the SQL side (_fold_alt_name_sql must stay byte-identical to it)
    and the #215 fuzzy threshold was calibrated against it; this one is
    only ever compared Python-to-Python, so it can fold harder.
    """
    stripped = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(_TIER_PUNCT_RE.sub(" ", stripped.casefold()).split())


def _match_tier(name: str, query: str) -> int:
    """3 = exact, 2 = prefix, 1 = substring.

    1 is the floor, not "0 = no match": every row a caller hands this was
    already found by a substring search (or an alternate-name / variant
    search, see _effective_tier), so a name that matches nothing at all
    never reaches here, and the weakest tier is simply "related somehow".

    Case-, diacritic- and punctuation-insensitive (#53): "Sao Paulo" and
    "São Paulo" compare equal, as do "Notre-Dame"/"notre dame" and
    "Straße"/"STRASSE" — see _fold_for_tier.
    """
    n, q = _fold_for_tier(name), _fold_for_tier(query)
    if not q:
        return 1
    if n == q:
        return 3
    if n.startswith(q):
        return 2
    return 1


def _effective_tier(row: dict, query: str) -> int:
    """Match tier for `row`, using its stored `_tier` (#53) when present.

    A variant-sourced row was found via a *different* literal string than
    `query` (e.g. "Saint Louis" matched against the "St. Louis" the caller
    actually typed) — recomputing _match_tier(row["name"], query) at rank
    time would grade it against text it was never matched on, and (since
    "Saint Louis" isn't a prefix/substring of "St. Louis") wrongly demote a
    genuine exact match down to the weak fallback tier. `_tier`, set when
    the row was fetched, is the tier it actually achieved.
    """
    stored = row.get("_tier")
    return stored if stored is not None else _pkg._match_tier(row["name"], query)


# #221: match tiers split into two groups for ranking. Exact (3) and prefix
# (2) are both "the caller's string names this place"; substring (1) is only
# "the caller's string occurs somewhere in this name", which is a far weaker
# claim — "York" is a substring of "New York" and of eleven other things.
# _rank_key orders by this group *before* prominence, and by the tier itself
# only after; see its docstring.
_STRONG_TIER = 2


# perf: the literal tier at which a division answer found inside a caller's
# near box (#476) is confident enough to stand geocode()'s later local
# passes down — the same exact-or-prefix line _STRONG_TIER draws for the
# abbreviation retries, applied to the diacritic-folded pass as well.
_CONFIDENT_TIER = _STRONG_TIER


def _home_bias_flag(row: dict, *, active: bool = True) -> int:
    """0 when `row` sits inside the configured home region (#406), else 1.

    Same shape as `_well_known_city_near` just above it in the tuple: a
    bounded, scope-limited nudge inserted into `_rank_key`'s tie-break chain
    *after* the tier-group term, so it can never let a weak/substring match
    beat a strong one, and *before* every population-based term, so it can
    settle same-tier-group ties the way a canonical well-known-city pin
    already does. When no home is configured (or `active=False`, used to
    compute the unbiased ordering for the #406 disclosure check) this
    returns 1 uniformly for every row — a constant term is a no-op for
    relative sort order, which is what makes "no home configured -> byte-
    identical behavior" true by construction rather than by a feature flag.
    """
    if not active:
        return 1
    return 0 if home_region.in_home_region(row.get("lat"), row.get("lon")) else 1


# #463: private row tag set by _flag_namesake_localities, read by _rank_key:
# the population of the region/country the tagged locality is the namesake
# of, which the locality ranks *as if* it carried. Rides along on the row
# dict like `_variant`/`_fuzzy`; never serialized — geocode() builds each
# returned entry field by field.
_NAMESAKE_LOCALITY_KEY = "_namesake_locality"


def _flag_namesake_localities(rows: list[dict], query: str) -> None:
    """#463 pre-pass over one candidate list: tag each locality that is the
    namesake city of a region/country also present in `rows`.

    A locality qualifies when some region or country row in `rows` (a) has
    the same folded name, (b) matched `query` at the same strong (exact or
    prefix) tier — neither side fuzzy — (c) contains the locality (a region
    row's `region` is its own ISO code, which the locality's `region` must
    equal; for a country row the `country` codes must match), (d) has a
    nonzero population, and (e) that population is at most
    1/_NAMESAKE_LOCALITY_SHARE times the locality's. The share guard is the
    same one _pick_anchor_row uses: a 0.04% hamlet named after its region
    is a coincidence, a 25-43% city is the place the name means.

    The tag's value is the containing row's population (the largest, if the
    locality is the namesake of both its region and its country); _rank_key
    ranks the locality as if that were its own, and the #47 subtype term
    then orders it immediately ahead of the region — and nowhere else.

    Idempotent — clears any earlier tag first — and a no-op on a list with no
    such pair, so every ranking without a namesake pair is byte-identical to
    before #463. Mutates `rows` in place; the tag is read by _rank_key.
    """
    for row in rows:
        row.pop(_NAMESAKE_LOCALITY_KEY, None)
    broads = []
    for row in rows:
        if row.get("subtype") not in ("region", "country") or row.get("_fuzzy"):
            continue
        if (row.get("population") or 0) <= 0:
            continue
        tier = _effective_tier(row, query)
        if tier < _STRONG_TIER:
            continue
        broads.append((row, tier, _pkg._normalize_for_match(row["name"])))
    if not broads:
        return
    for row in rows:
        if row.get("subtype") != "locality" or row.get("_fuzzy"):
            continue
        population = row.get("population") or 0
        if population <= 0:
            continue
        tier = _effective_tier(row, query)
        if tier < _STRONG_TIER:
            continue
        name = _pkg._normalize_for_match(row["name"])
        for broad, broad_tier, broad_name in broads:
            if broad_tier != tier or broad_name != name:
                continue
            if broad["subtype"] == "region":
                inside = broad.get("region") is not None and row.get("region") == broad["region"]
            else:
                inside = broad.get("country") is not None and row.get("country") == broad["country"]
            if inside and population >= _NAMESAKE_LOCALITY_SHARE * broad["population"]:
                row[_NAMESAKE_LOCALITY_KEY] = max(
                    row.get(_NAMESAKE_LOCALITY_KEY) or 0, broad["population"]
                )


def _rank_key(row: dict, query: str, region_population: dict[str, int], *, home_bias: bool = True):
    """Sort key: (#215) literal-over-fuzzy, then (#221) strong-vs-substring
    tier group, then whether a *nonzero* population is known, then the match
    tier, then (#47) whether a population is known at all and its value,
    else a documented proxy chain of subtype rank / hierarchy depth / the
    row's own region's population, then (#53) literal-over-variant, then id
    for full determinism. All ascending (smaller sorts first).

    `home_bias` (#406) inserts one more term, right after the well-known-city
    pin and ahead of every population term — see `_home_bias_flag`. Pass
    `home_bias=False` to recompute the *unconfigured* ordering (used only to
    detect whether the bias changed the winner, for the disclosure note).

    Tier vs prominence (#221). Tier used to dominate outright, so any
    exact-tier row beat every prefix-tier row no matter what stood behind
    them: live, "東京" put a population-less Nagano neighborhood above 東京都
    and its 13.9M people, because 東京 is exactly the neighborhood's name and
    only a prefix of the prefecture's. The rule now is that *real
    prominence* outranks the tier, but only within the strong (exact/prefix)
    group and only against a row with no prominence at all — the exact-tier
    namesakes this rescues past are Overture rows with population NULL, and
    that emptiness is itself the signal (#47) that the match is a spelling
    coincidence rather than the place anyone means.

    "Real prominence" is a population greater than zero, not merely a
    non-NULL one. Overture ships plenty of divisions carrying an explicit
    population of 0 (abandoned and unincorporated places), and a bare
    null-check would let one of those rescue a prefix match past an exact
    one — reading a filled-in column as prominence when the value says the
    opposite. A 0 therefore ranks with the NULLs *here*; it keeps its #47
    meaning below the tier term, where "we know it is 0" still orders ahead
    of "we do not know", which is the ordering #47 established and #221 does
    not touch.

    Everything else is unchanged and deliberately so: two populated rows
    still order by tier first, so an exact match with 10k people still beats
    a prefix match with 10M ("Portland" is not a worse answer than "Portland
    Heights" because the latter is bigger); two population-less rows still
    order by tier; and a substring match still cannot leapfrog either,
    however populous, because the group term sits ahead of the population
    one. This is the same judgement #214 already made one level down — an
    alternate-name hit (`_variant`) wins on its own prominence against a
    population-less literal namesake, which is why "Munich" resolves to
    München — applied to the tier ladder instead of the literal/variant one.

    rank_score deliberately does *not* follow this (see _rank_score): it
    answers "how well does this name match what you typed", where an exact
    match really is a better match, so the top-ranked result can carry a
    lower rank_score than the one below it. #53 already produced that shape
    for variant hits; #221 only widens it.

    The #215 fuzzy term leads, ahead of even the tier: a fuzzy row matched
    a *different* string than the caller typed, so it belongs below every
    row that matched the typed string somehow — including a bare substring
    match. Among themselves fuzzy rows order by similarity first (the SQL
    already picked them by it); for every literal row both terms are
    constant, so this prefix is a no-op on the pre-#215 ordering.

    The literal-over-variant tiebreak sits *after* the #47 chain, not
    before it: real data has plenty of tiny, unrelated places sharing a
    literal-exact name with a query ("St. Louis" also matches a handful of
    small towns/villages, worldwide, literally spelled that way) — a
    variant match's own population/prominence has to be allowed to win over
    those the same way it would against any other literal match. Literal
    only wins when every other signal is tied, which is the case #53 is
    actually documented to care about (two otherwise-identical candidates,
    one found straight, one found through a spelling variant).

    Namesake localities (#463). Two exact-tier, populated rows still order
    by raw population, and that is wrong for exactly one shape: a city and
    the region it sits in, sharing a name. "São Paulo" returned the state
    (45.5M, centroid 259 km from the city) above the city (11.5M) for three
    weekly corpus runs, where every other geocoder returns the city, because
    a region's population always includes its namesake city's and so always
    exceeds it. `_flag_namesake_localities` tags such a locality before the
    sort — same-name, same strong tier, inside that region, and carrying at
    least _NAMESAKE_LOCALITY_SHARE of its population, the guard
    _pick_anchor_row already uses so a namesake hamlet can never displace a
    genuine region — with the region's population, and the tagged locality
    is then ranked *as if it carried that population*. Every term above the
    population one is untouched, so the two rows tie all the way down to
    the #47 subtype term, where locality (4) orders ahead of region (1): the
    city lands immediately ahead of its region and nowhere else. That is
    deliberately not a flag term of its own higher in the tuple: demoting
    the region there sinks it below every same-name hamlet (a 3,688-person
    "São Paulo" macrohood would become result #2), and promoting the city
    there lifts it over everything, including a same-name country ("Mexico"
    would return Ciudad de México over México, because Overture carries the
    city both as a locality and as the region MX-CMX). With no tagged row the
    ordering is byte-identical: "Kansas" still returns the state, "東京"
    still returns 東京都. rank_score, as above, does not follow it.
    """
    tier = _effective_tier(row, query)
    population = row.get("population")
    namesake_of = row.get(_NAMESAKE_LOCALITY_KEY)
    if namesake_of:
        # #463: rank as the region this locality is the namesake of.
        population = max(population or 0, namesake_of)
    weight = _SUBTYPE_WEIGHT.get(row.get("subtype"), 0)
    depth = len(row.get("admin_context") or [])
    region_pop = region_population.get(row.get("region")) or 0
    return (
        1 if row.get("_fuzzy") else 0,
        -(row.get("_similarity") or 0.0),
        0 if tier >= _STRONG_TIER else 1,
        _pkg._well_known_city_near(row, query),
        _home_bias_flag(row, active=home_bias),
        0 if (population or 0) > 0 else 1,
        -tier,
        0 if population is not None else 1,
        -(population or 0),
        -weight,
        depth,
        -region_pop,
        1 if row.get("_variant") else 0,
        row["id"],
    )


# #406: bounded home-region bonus on rank_score's ~0-1 scale. Sized between
# the two existing bonuses on this scale: below population_bonus's 0.05
# ceiling (a home candidate's displayed score still reads as less decisive
# than genuine population-driven prominence), above the #53 variant penalty
# of 0.01 (big enough to be a real, visible tiebreak rather than rounding
# noise). Mirrors, on the display scale, the same tuple position
# _home_bias_flag occupies in _rank_key: after tier/well-known-city, ahead
# of population.
_HOME_BIAS_SCORE_BONUS = 0.03


def _rank_score(row: dict, query: str) -> float:
    tier = _effective_tier(row, query)
    # #215: a fuzzy row didn't match the typed string at any tier, so it
    # scores below the weakest literal one (substring, 0.4) — the bounded
    # subtype/population bonuses below can add at most 0.09, keeping every
    # fuzzy score under 0.4 no matter how prominent the corrected place is.
    tier_score = 0.3 if row.get("_fuzzy") else {3: 1.0, 2: 0.7, 1: 0.4}[tier]
    weight = _SUBTYPE_WEIGHT.get(row.get("subtype"), 0)
    population = row.get("population")
    # A small, bounded bonus so rank_score stays roughly consistent with
    # the tiebreak order above without population dominating the score's
    # scale — a locality of 10M people isn't "10x more correct" than one
    # of 10k, it's a tiebreak, not a confidence signal.
    population_bonus = min(0.05, math.log10(population + 1) / 140) if population else 0.0
    score = tier_score + weight * 0.01 + population_bonus
    if row.get("_variant"):
        # Small, fixed penalty (#53) so a variant-sourced row's rank_score
        # never ties a same-tier literal match's — consistent with the
        # ordering _rank_key already enforces.
        score -= 0.01
    if _home_bias_flag(row) == 0:
        score += _HOME_BIAS_SCORE_BONUS
    return round(score, 3)


def _admin_context(hierarchies, self_name: str | None = None) -> list[str]:
    """Containing-chain names from the first hierarchy path, self excluded.

    hierarchies comes back from DuckDB as plain Python lists/dicts:
    list-of-paths, each path a list of {division_id, name, subtype} dicts
    ordered top-level ancestor first, the division itself last (verified
    against live Overture divisions data). self_name strips that trailing
    self-entry so admin_context is only what *contains* the result, not the
    result itself. Any structural surprise (schema drift across releases)
    degrades to an empty chain rather than raising, matching overture.py's
    degrade-don't-crash approach.
    """
    try:
        if not hierarchies:
            return []
        path = hierarchies[0]
        names = [entry["name"] for entry in path if entry and entry.get("name")]
        if self_name and names and names[-1] == self_name:
            names = names[:-1]
        return names
    except (TypeError, KeyError, AttributeError):
        return []


def _admin_chain_context(chain: list[str] | None, self_name: str | None = None) -> list[str]:
    """Same as _admin_context, but for the #43 local table's pre-flattened
    admin_chain column (a plain list of names, no per-entry struct)."""
    if not chain:
        return []
    names = [n for n in chain if n]
    if self_name and names and names[-1] == self_name:
        names = names[:-1]
    return names
