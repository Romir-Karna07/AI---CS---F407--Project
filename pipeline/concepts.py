"""
Concept dictionary: maps whatever a source file calls a column/key onto the canonical fields
of the three database tables. Matching is on a normalised key (lower-case, alphanumerics only),
so "Organic Carbon", "organic_carbon" and "OC" all resolve to `oc`. Unknown keys are not
dropped - they are kept in each row's `extra` JSON and can be mapped by the LLM fallback.
"""
import re

SOIL_NUTRIENTS = ["n", "p", "k", "oc", "ph", "ec", "s", "zn", "fe", "cu", "mn", "b"]

# canonical field -> normalised synonyms
SYNONYMS = {
    # ---- soil ---------------------------------------------------------------
    "n": ["n", "nitrogen", "availablenitrogen", "avn", "nlevel", "nkgha"],
    "p": ["p", "phosphorus", "phosphorous", "availablephosphorus", "plevel", "pkgha", "p2o5"],
    "k": ["k", "potassium", "availablepotassium", "klevel", "kkgha", "k2o"],
    "oc": ["oc", "organiccarbon", "organiccarbonpct", "orgcarbon", "organiccarbonpercent"],
    "ph": ["ph", "phlevel", "soilph", "phvalue"],
    "ec": ["ec", "electricalconductivity", "eclevel", "ecdsm"],
    "s": ["s", "sulphur", "sulfur", "sulphurppm", "slevel"],
    "zn": ["zn", "zinc", "znlevel", "znppm"],
    "fe": ["fe", "iron", "felevel", "feppm"],
    "cu": ["cu", "copper", "culevel", "cuppm"],
    "mn": ["mn", "manganese", "mnlevel", "mnppm"],
    "b": ["b", "boron", "blevel", "bppm"],
    "village": ["village", "villagename", "vill"],
    "tehsil": ["tehsil", "tahsil", "taluk", "taluka", "subdistrict"],
    "block": ["block", "blockname"],
    "category": ["category", "fertilitycategory", "fertilityclass", "rating", "soilcategory"],
    "record_id": ["featureid", "recordid", "sampleid", "samplenumber", "cardnumber", "shcid", "id"],
    # ---- shared -------------------------------------------------------------
    "district": ["district", "districtname", "dist", "distname"],
    "district_code": ["districtcode", "districtid", "districtlgd", "districtl", "lgdcode", "distcode", "districtno"],
    "state": ["state", "statename"],
    "state_code": ["statecode", "statelgd", "stateid"],
    "latitude": ["latitude", "lat", "approxlatitude"],
    "longitude": ["longitude", "lon", "long", "lng", "approxlongitude"],
    "crop": ["crop", "cropname", "commodity"],
    "season": ["season", "cropseason"],
    "period": ["period", "cycle", "shccycles", "shccycle", "year", "agriculturalyear", "financialyear", "fy", "cropyear"],
    "unit": ["unit", "units", "uom"],
    # ---- rainfall -----------------------------------------------------------
    "year_month": ["yearmonth", "month", "monthyear", "ym", "date"],
    "rainfall_mm": ["rainfallmm", "rainfall", "rain", "rainmm", "precipitation", "precipitationmm", "monthlyrainfall",
                    "actualrainfall", "actual"],
    "rainy_days": ["rainydaysge25mm", "rainydays", "rainydaysover25mm", "rainydaysgt25mm", "numberofrainydays", "raindays"],
    "days_with_data": ["dayswithdata", "validdays"],
    "days_in_month": ["daysinmonth"],
    # ---- production ---------------------------------------------------------
    "area": ["area", "areaha", "areahectare", "cultivatedarea", "areaofcultivation", "sownarea"],
    "production": ["production", "productiontonnes", "productiont", "output", "totalproduction"],
    "yield": ["yield", "yieldtha", "yieldtonha", "productivity", "yieldvalue"],
}

_LOOKUP = {}
for canon, syns in SYNONYMS.items():
    for s in syns:
        _LOOKUP.setdefault(s, canon)

DOMAIN_FIELDS = {
    "soil": set(SOIL_NUTRIENTS) | {"village", "tehsil", "category"},
    "rainfall": {"rainfall_mm", "rainy_days", "year_month", "days_with_data", "days_in_month"},
    "production": {"area", "production", "yield", "crop"},
}
# fields that identify a domain strongly (weights used by the classifier)
DOMAIN_CORE = {
    "soil": {"n", "p", "k", "oc", "ph", "ec", "zn", "cu", "mn", "b", "fe", "s"},
    "rainfall": {"rainfall_mm", "rainy_days"},
    "production": {"area", "production", "yield"},
}


def norm_key(k) -> str:
    """'Organic Carbon (%)' -> 'organiccarbon'  ;  'rainy_days_ge_2.5mm' -> 'rainydaysge25mm'."""
    s = str(k).lower()
    s = re.sub(r"\([^)]*\)", "", s) if re.sub(r"\([^)]*\)", "", s).strip() else s  # drop "(Hectare)" style suffixes
    return re.sub(r"[^a-z0-9]", "", s)


# Fallback when a header is not an exact synonym: substring rules, in priority order.
# Headers describing a derived quantity (normal / departure / percent ...) are never auto-mapped by these.
_CONTAINS = [("rainydays", "rainy_days"), ("raindays", "rainy_days"), ("rainfall", "rainfall_mm"),
             ("nitrogen", "n"), ("phosph", "p"), ("potass", "k"), ("organiccarbon", "oc"),
             ("electricalcond", "ec"), ("sulph", "s"), ("sulf", "s"), ("zinc", "zn"), ("boron", "b"),
             ("copper", "cu"), ("manganese", "mn"), ("iron", "fe"), ("village", "village"),
             ("district", "district"), ("distr", "district"), ("production", "production"), ("yield", "yield"), ("latitude", "latitude"),
             ("longitude", "longitude"), ("year", "period")]
_DERIVED = ("normal", "departure", "deviation", "percent", "pct", "cumulative", "storage", "code", "distribution", "yearmonth")


def concept_for(key) -> str | None:
    k = norm_key(key)
    if k in _LOOKUP:
        return _LOOKUP[k]
    if any(t in k for t in _DERIVED):
        return None
    for frag, canon in _CONTAINS:
        if frag in k:
            return canon
    return None


def map_columns(keys) -> tuple[dict, list]:
    """{source_key: canonical} for recognised keys, plus the list of unrecognised keys.
    If two source keys claim the same canonical field, the first wins and the second is unmapped."""
    mapping, unmapped, taken = {}, [], set()
    for k in keys:
        c = concept_for(k)
        if c and c not in taken:
            mapping[k] = c
            taken.add(c)
        else:
            unmapped.append(k)
    return mapping, unmapped


def score_domains(canon_fields) -> dict:
    fields = set(canon_fields)
    scores = {d: len(fields & core) for d, core in DOMAIN_CORE.items()}
    # soil needs several nutrients, not one stray 'b' / 'k' column
    if scores["soil"] < 3:
        scores["soil"] = 0
    if scores["production"] < 2:
        scores["production"] = 0
    return scores


def register_synonym(key, canonical: str) -> None:
    """Teach the mapper a header it did not know (used with LLM-proposed mappings, per run)."""
    if canonical in SYNONYMS:
        _LOOKUP[norm_key(key)] = canonical
