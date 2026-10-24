"""Value normalisation: districts, crops, periods, months, units. Reference data lives in config/*.json."""
import difflib
import json
import re
from pathlib import Path

from .readers import YEAR_RE, to_number

ROOT = Path(__file__).resolve().parent.parent
UNSPECIFIED = "UNSPECIFIED"

_districts = json.loads((ROOT / "config" / "manipur_districts.json").read_text(encoding="utf-8"))
_crop_syn = json.loads((ROOT / "config" / "crop_synonyms.json").read_text(encoding="utf-8"))["synonyms"]


def _key(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


_BY_NAME = {}
_BY_CODE = {}
for d in _districts["districts"]:
    _BY_CODE[d["code"]] = d["name"]
    for n in [d["name"], *d["aliases"]]:
        _BY_NAME[_key(n)] = d["name"]
_NAME_TO_CODE = {d["name"]: d["code"] for d in _districts["districts"]}
STATE_NAME = _districts["state"]
STATE_CODE = _districts["state_code"]
VALID_DISTRICT_CODES = set(_BY_CODE)


def normalize_district(name=None, code=None) -> tuple[str, str, str]:
    """-> (canonical_name, code, how). `how` is exact | alias | fuzzy | code | unmatched | missing."""
    if code is not None and str(code).strip():
        c = str(code).strip().split(".")[0]
        if c in _BY_CODE:
            if name:
                by_name = _BY_NAME.get(_key(name))
                if by_name and by_name != _BY_CODE[c]:
                    # name and code disagree: trust the name (codes differ between systems)
                    return by_name, _NAME_TO_CODE[by_name], "name_over_code"
            return _BY_CODE[c], c, "code"
    if name is None or not str(name).strip():
        return UNSPECIFIED, UNSPECIFIED, "missing"
    k = _key(re.sub(r"^\s*\d+\s*[\.\)]\s*", "", str(name)))
    if k in _BY_NAME:
        canon = _BY_NAME[k]
        how = "exact" if k == _key(canon) else "alias"
        return canon, _NAME_TO_CODE[canon], how
    close = difflib.get_close_matches(k, list(_BY_NAME), n=1, cutoff=0.85)
    if close:
        canon = _BY_NAME[close[0]]
        return canon, _NAME_TO_CODE[canon], "fuzzy"
    return str(name).strip().upper(), UNSPECIFIED, "unmatched"


def normalize_crop(raw) -> tuple[str, str]:
    """-> (canonical crop, raw text). Missing crop -> 'UNSPECIFIED'."""
    if raw is None or not str(raw).strip():
        return UNSPECIFIED, ""
    s = re.sub(r"^\s*\d+\s*[\.\)]\s*", "", str(raw)).strip()
    k = re.sub(r"\s+", " ", s.lower())
    return _crop_syn.get(k, s.title()), str(raw).strip()


# ----------------------------------------------------------------------------- time
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_period(text) -> tuple[str | None, int | None]:
    """'2017-19' -> ('2017-19', 2017) ; '1997 - 1998' -> ('1997-98', 1997) ; '2021' -> ('2021', 2021)."""
    if text is None:
        return None, None
    s = str(text).strip()
    m = re.search(r"(1[89]\d{2}|20\d{2})\s*[-–/to]*\s*(\d{4}|\d{2})?\b", s)
    if not m:
        return None, None
    y1 = int(m.group(1))
    end = m.group(2)
    if end:
        label = f"{y1}-{end[-2:]}"
    else:
        label = str(y1)
    return label, y1


def parse_year_month(value, year=None, month=None) -> tuple[str | None, int | None, int | None]:
    """Accepts '2015-01', '2015/1', 'Jan-2015', 'January 2015', '2015-01-31', or separate year+month. -> (YYYY-MM, y, m)"""
    def fmt(y, m):
        return (f"{y:04d}-{m:02d}", y, m) if y and 1 <= m <= 12 else (None, None, None)

    if value is not None and str(value).strip():
        s = str(value).strip()
        m = re.match(r"^(\d{4})[-/.](\d{1,2})(?:[-/.]\d{1,2})?$", s)
        if m:
            return fmt(int(m.group(1)), int(m.group(2)))
        m = re.match(r"^(\d{1,2})[-/.](\d{4})$", s)
        if m:
            return fmt(int(m.group(2)), int(m.group(1)))
        m = re.match(r"^([A-Za-z]{3,9})[\s\-_/,.]*(\d{4})$", s) or None
        if m and m.group(1)[:3].lower() in MONTHS:
            return fmt(int(m.group(2)), MONTHS[m.group(1)[:3].lower()])
        m = re.match(r"^(\d{4})[\s\-_/,.]*([A-Za-z]{3,9})$", s)
        if m and m.group(2)[:3].lower() in MONTHS:
            return fmt(int(m.group(1)), MONTHS[m.group(2)[:3].lower()])
    # separate columns
    y = int(to_number(year)) if to_number(year) is not None else None
    mo = None
    if month is not None and str(month).strip():
        n = to_number(month)
        if n is not None:
            mo = int(n)
        elif str(month).strip()[:3].lower() in MONTHS:
            mo = MONTHS[str(month).strip()[:3].lower()]
    if y and mo:
        return fmt(y, mo)
    return None, None, None


# ----------------------------------------------------------------------------- units
# canonical units: area ha, production t, yield t/ha, rainfall mm
_AREA = {"ha": 1.0, "hectare": 1.0, "hectares": 1.0, "acre": 0.404686, "acres": 0.404686, "sqkm": 100.0,
         "000ha": 1000.0, "lakhha": 100000.0, "lakhhectare": 100000.0, "000hectare": 1000.0}
_PROD = {"t": 1.0, "tonne": 1.0, "tonnes": 1.0, "ton": 1.0, "tons": 1.0, "mt": 1.0, "kg": 0.001, "quintal": 0.1,
         "quintals": 0.1, "q": 0.1, "000t": 1000.0, "000tonnes": 1000.0, "lakht": 100000.0, "lakhtonnes": 100000.0}
_YIELD = {"tha": 1.0, "tonha": 1.0, "tonsha": 1.0, "tonnesha": 1.0, "tonneperha": 1.0, "kgha": 0.001, "qha": 0.1,
          "quintalha": 0.1}
_RAIN = {"mm": 1.0, "millimetre": 1.0, "millimeter": 1.0, "millimetres": 1.0, "millimeters": 1.0, "cm": 10.0,
         "centimetre": 10.0, "centimeter": 10.0, "centimetres": 10.0, "centimeters": 10.0,
         "inch": 25.4, "inches": 25.4, "in": 25.4}
_TABLES = {"area": _AREA, "production": _PROD, "yield": _YIELD, "rainfall_mm": _RAIN}
CANONICAL_UNIT = {"area": "ha", "production": "t", "yield": "t/ha", "rainfall_mm": "mm"}


def convert_unit(variable: str, value, unit_text):
    """-> (value_in_canonical_unit, unit_status). unit_status: declared | assumed | unknown:<text>."""
    if value is None:
        return None, "assumed"
    if not unit_text or not str(unit_text).strip():
        return value, "assumed"
    k = re.sub(r"[^a-z0-9]", "", re.sub(r"[./]", "", str(unit_text).lower().replace("per", "")).replace("(", "").replace(")", ""))
    table = _TABLES.get(variable, {})
    if k in table:
        return value * table[k], "declared"
    return value, f"unknown:{str(unit_text).strip()}"
