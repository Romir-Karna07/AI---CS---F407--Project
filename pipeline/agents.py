"""
Domain agents. Each takes neutral rows from the readers (any key spelling, any nesting) and builds
rows for its table. What is decided by *fixed code* here: which source key means which field
(pipeline.concepts), unit conversion, district/crop/period normalisation, validation.
What is decided by the *LLM* (graph.py, optional): classifying a group whose headers match no domain,
and mapping headers the concept dictionary does not know.
"""
import json
import re

from .concepts import SOIL_NUTRIENTS, map_columns, norm_key
from .normalize import (CANONICAL_UNIT, MONTHS, STATE_CODE, STATE_NAME, UNSPECIFIED, convert_unit,
                        normalize_crop, normalize_district, parse_period, parse_year_month)
from .readers import YEAR_RE, is_missing, to_number

SOIL_UNITS = {"n": "kg/ha", "p": "kg/ha", "k": "kg/ha", "oc": "%", "ph": "pH units", "ec": "dS/m",
              "s": "ppm", "zn": "ppm", "fe": "ppm", "cu": "ppm", "mn": "ppm", "b": "ppm"}
ZERO_MEANS_MISSING = ("ph",)   # pH can never be 0; the portal uses 0.0 where no value was recorded
_FULL_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}


def month_columns(keys) -> dict:
    """{source_key: month_number} for keys that are month names (Jan, January, JAN ...)."""
    out = {}
    for k in keys:
        n = norm_key(k)
        if n in _FULL_MONTHS:
            out[k] = _FULL_MONTHS[n]
        elif n in MONTHS and len(n) == 3:
            out[k] = MONTHS[n]
    return out


def _mapped(row):
    """-> (canonical field -> value, canonical field -> source key, unmapped {key: value})"""
    data = row["data"]
    mapping, unmapped = map_columns(list(data))
    c = {canon: data[src] for src, canon in mapping.items()}
    src_of = {canon: src for src, canon in mapping.items()}
    return c, src_of, {k: data[k] for k in unmapped}


def _extra(unmapped: dict, c: dict) -> str | None:
    seen = {str(v).strip().lower() for v in c.values() if v is not None}
    out = {}
    for k, v in unmapped.items():
        if is_missing(v):
            continue
        s = str(v)
        if len(s) > 60 or s.strip().lower() in seen:
            continue
        out[str(k)] = v
    return json.dumps(out, ensure_ascii=False) if out else None


def _is_district_label(label) -> bool:
    return normalize_district(label)[2] in ("exact", "alias", "fuzzy")


def _fill_from_labels(c, labels, crop=False):
    """Structural labels (dict keys such as {"Rice": [...]} or {"Ukhrul": {...}}) can carry district / crop."""
    c = dict(c)
    for name, lab in labels:
        if YEAR_RE.search(str(lab)):
            continue
        if is_missing(c.get("district")) and _is_district_label(lab):
            c["district"] = lab
        elif crop and is_missing(c.get("crop")) and not _is_district_label(lab):
            c["crop"] = lab
    return c


def _location(c):
    name, code, how = normalize_district(c.get("district"), c.get("district_code"))
    if code != UNSPECIFIED:
        return name, code, STATE_NAME, STATE_CODE, how
    state = str(c["state"]).strip().upper() if not is_missing(c.get("state")) else UNSPECIFIED
    scode = str(c["state_code"]).strip() if not is_missing(c.get("state_code")) else UNSPECIFIED
    return name, code, state, scode, how


def _text(c, key, default=UNSPECIFIED):
    v = c.get(key)
    return default if is_missing(v) else str(v).strip()


def _period(c, labels):
    label, year = parse_period(c.get("period"))
    if label is None:
        for _, lab in labels:
            if YEAR_RE.search(str(lab)):
                label, year = parse_period(lab)
                break
    return label, year


def _num(c, key):
    return to_number(c.get(key)) if not is_missing(c.get(key)) else None


# ----------------------------------------------------------------------------- soil
def soil_agent(rows):
    out = []
    for row in rows:
        c, src_of, unmapped = _mapped(row)
        c = _fill_from_labels(c, row["labels"])
        name, code, state, scode, how = _location(c)
        period, year = _period(c, row["labels"])
        rec = dict(district=name, district_code=code, state=state, state_code=scode, crop=_text(c, "crop"),
                   unit=";".join(f"{k}:{u}" for k, u in SOIL_UNITS.items()), unit_status="assumed",
                   period=period, year=year, village=_text(c, "village"), tehsil=_text(c, "tehsil"),
                   block=_text(c, "block"), category=_text(c, "category"),
                   record_id=None if is_missing(c.get("record_id")) else str(c["record_id"]),
                   latitude=_num(c, "latitude"), longitude=_num(c, "longitude"),
                   extra=_extra(unmapped, c), locator=row["locator"])
        for n in SOIL_NUTRIENTS:
            rec[n] = _num(c, n)
        for n in ZERO_MEANS_MISSING:  # physically impossible zero = a placeholder for "not measured"
            if rec[n] is not None and rec[n] <= 0:
                extra = json.loads(rec["extra"]) if rec["extra"] else {}
                extra[f"{n}_source_value"] = rec[n]
                rec["extra"], rec[n] = json.dumps(extra), None
        out.append(rec)
    return out


# ----------------------------------------------------------------------------- rainfall
def rainfall_agent(rows):
    out = []
    for row in rows:
        c, src_of, unmapped = _mapped(row)
        c = _fill_from_labels(c, row["labels"])
        name, code, state, scode, how = _location(c)
        unit_text = c.get("unit")
        if is_missing(unit_text):  # unit may be in the header: "Rainfall (inches)"
            m = re.search(r"\(([^)]*)\)", str(src_of.get("rainfall_mm", "")))
            unit_text = m.group(1) if m else None
        base = dict(district=name, district_code=code, state=state, state_code=scode, crop="UNSPECIFIED",
                    locator=row["locator"])
        mcols = month_columns(row["data"])
        if len(mcols) >= 6:  # wide table: one column per month
            year = to_number(c.get("period"))
            if year is None:
                _, y = _period(c, row["labels"])
                year = y
            for src, mo in sorted(mcols.items(), key=lambda kv: kv[1]):
                v = to_number(row["data"][src])
                if v is None or year is None:
                    continue
                val, ustat = convert_unit("rainfall_mm", v, unit_text)
                out.append(dict(base, locator=f"{row['locator']} [{src}]", year_month=f"{int(year):04d}-{mo:02d}",
                                year=int(year), month=mo, rainfall_mm=val, unit="mm", unit_status=ustat,
                                rainy_days_ge_2_5mm=None, days_with_data=None, days_in_month=None,
                                extra=_extra({k: v for k, v in unmapped.items() if k not in mcols}, c)))
            continue
        ym, y, m = parse_year_month(c.get("year_month"), c.get("period"), c.get("year_month"))
        v = _num(c, "rainfall_mm")
        val, ustat = convert_unit("rainfall_mm", v, unit_text)
        rd = _num(c, "rainy_days")
        out.append(dict(base, year_month=ym, year=y, month=m, rainfall_mm=val, unit="mm", unit_status=ustat,
                        rainy_days_ge_2_5mm=None if rd is None else int(rd),
                        days_with_data=None if _num(c, "days_with_data") is None else int(_num(c, "days_with_data")),
                        days_in_month=None if _num(c, "days_in_month") is None else int(_num(c, "days_in_month")),
                        extra=_extra(unmapped, c)))
    return out


# ----------------------------------------------------------------------------- production
def production_agent(rows):
    out = []
    for row in rows:
        c, src_of, unmapped = _mapped(row)
        c = _fill_from_labels(c, row["labels"], crop=True)
        name, code, state, scode, how = _location(c)
        period, year = _period(c, row["labels"])
        crop, crop_raw = normalize_crop(c.get("crop"))
        vals, statuses = {}, []
        for var in ("area", "production", "yield"):
            raw = _num(c, var)
            unit_text = row["units"].get(src_of.get(var, ""), "") or ""
            if not unit_text:  # unit may sit in a column ("Unit") or in the header text "Area (ha)"
                m = re.search(r"\(([^)]*)\)", str(src_of.get(var, "")))
                unit_text = m.group(1) if m else ""
            vals[var], st = convert_unit(var, raw, unit_text)
            statuses.append(st)
        if vals["area"] is None and vals["production"] is None:
            vals["yield"] = None  # a lone yield (often a placeholder 0) is unsupported by area/production
        if all(v is None for v in vals.values()):
            continue
        bad = [s for s in statuses if s.startswith("unknown")]
        ustat = bad[0] if bad else ("declared" if "declared" in statuses else "assumed")
        out.append(dict(district=name, district_code=code, state=state, state_code=scode, crop=crop, crop_raw=crop_raw,
                        season=_text(c, "season"), period=period, year=year, area_ha=vals["area"],
                        production_t=vals["production"], yield_t_ha=vals["yield"],
                        unit=f"area:{CANONICAL_UNIT['area']};production:{CANONICAL_UNIT['production']};yield:{CANONICAL_UNIT['yield']}",
                        unit_status=ustat, extra=_extra(unmapped, c), locator=row["locator"]))
    return out


AGENTS = {"soil": ("soil_agent", soil_agent), "rainfall": ("rainfall_agent", rainfall_agent),
          "production": ("production_agent", production_agent)}
