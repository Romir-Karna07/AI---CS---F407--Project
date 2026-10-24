"""
Builds the Phase 2 evaluation set: eval/cases/* (input files) + eval/reference/*.json (expected output).

How the references are made (independent of the pipeline - this script does not import it):
  * Values come straight from the real source files (manipur_soil_health.json, manipur_rainfall_monthly.json,
    Data/CSV_Agent.csv), read here with their own small, hard-coded loaders.
  * Each case re-shapes a handful of those records into a different file layout (other nesting, other header
    names, delimiters, units, decimal commas, ...). The reference keeps the ORIGINAL value in canonical units,
    so a pipeline that mis-reads layout, header meaning or units gets the field wrong.
  * Unit conversions below use their own constants. Expected district / crop / period names are the
    normalisations declared in eval/README.md.
  * Every reference value is re-checked against the case file text at the end of this script
    (value, or value / unit-factor, must literally appear), so a reference cannot silently drift from its case.

Deterministic (seeded). Re-run:  python eval/build_cases.py
"""
import csv
import io
import json
import random
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "cases"
REFS = ROOT / "eval" / "reference"
rng = random.Random(42)

# ------------------------------------------------------------------ expected normalisation (declared in eval/README.md)
DISTRICT_CANON = {"NONEI": "NONEY"}
CROP_CANON = {"Redish": "Radish", "Tapioca": "Cassava", "Jack Fruit": "Jackfruit", "Bhindi": "Okra",
              "Dry chillies": "Chilli (dry)", "Cashewnut": "Cashew Nut", "Sweet potato": "Sweet Potato"}
SOIL_UNITS = {"n": "kg/ha", "p": "kg/ha", "k": "kg/ha", "oc": "%", "ph": "pH units", "ec": "dS/m", "s": "ppm",
              "zn": "ppm", "fe": "ppm", "cu": "ppm", "mn": "ppm", "b": "ppm"}
SRC_SOIL_KEYS = {"n": ("N",), "p": ("P",), "k": ("K",), "oc": ("OC",), "ph": ("pH",), "ec": ("EC",), "s": ("S",),
                 "zn": ("Zn",), "fe": ("fe", "Fe"), "cu": ("Cu",), "mn": ("Mn",), "b": ("B",)}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def period_label(raw: str) -> str:          # '1999 - 2000' -> '1999-00'
    a, b = re.findall(r"\d{4}", raw)
    return f"{a}-{b[-2:]}"


# ------------------------------------------------------------------ independent source loaders
def load_soil():
    doc = json.loads((ROOT / "manipur_soil_health.json").read_text(encoding="utf-8"))
    out = []
    for d in doc["districts"]:
        for cyc, rows in d["shc_cycles"].items():
            for r in rows:
                vals = {}
                for k, srcs in SRC_SOIL_KEYS.items():
                    v = next((r[s] for s in srcs if s in r), None)
                    vals[k] = v
                out.append({"district": d["district"], "code": d["district_code"], "cycle": cyc, "raw": r,
                            "village": r.get("VILLAGE") or r.get("village"), "tehsil": r.get("TEHSIL"),
                            "category": r.get("Category"), "vals": vals})
    return out


def load_rain():
    doc = json.loads((ROOT / "manipur_rainfall_monthly.json").read_text(encoding="utf-8"))
    return [{"district": d["district"], **m} for d in doc["districts"] for m in d["monthly"]]


def load_prod():
    rows = list(csv.reader((ROOT / "Data" / "CSV_Agent.csv").open(encoding="utf-8-sig")))
    years = [(rows[0][j], j) for j in range(2, len(rows[0]), 3) if rows[0][j]]
    out, crop = [], None
    for r in rows[3:]:
        if not r[1]:
            if re.match(r"^\d+\.", r[0]):
                crop = re.sub(r"^\d+\.\s*", "", r[0])
            continue
        dist = re.sub(r"^\d+\.\s*", "", r[0])
        for yr, j in years:
            a, p, y = r[j:j + 3]
            if a.strip() or p.strip():
                out.append({"crop": crop, "district": dist.upper(), "season": r[1], "year_raw": yr,
                            "period": period_label(yr), "area": a.strip(), "prod": p.strip(), "yield": y.strip()})
    return out


SOIL, RAIN, PROD = load_soil(), load_rain(), load_prod()


# ------------------------------------------------------------------ helpers
def expected_trajectory(src_type, agent, llm=False):
    t = ["inspect_file", "read_json_data" if src_type == "json" else "read_csv_data"]
    if llm:
        t.append("llm_map_headers")
    return t + [{"soil": "soil_extract", "rainfall": "rainfall_extract", "production": "production_extract"}.get(agent, "none"),
                "sqlite_load"]


def canon_district(name):
    n = str(name).upper()
    return DISTRICT_CANON.get(n, n)


def fnum(v):
    return None if v in (None, "", "NA") else float(v)


def soil_record(s, text=("village", "tehsil", "category"), skip=()):
    fields = {}
    for k, v in s["vals"].items():
        if v is not None and k not in skip:
            fields[k] = {"value": float(v), "unit": SOIL_UNITS[k]}
    if "tehsil" in text and s["tehsil"]:
        fields["tehsil"] = {"value": s["tehsil"], "unit": "text"}
    if "category" in text and s["category"]:
        fields["category"] = {"value": s["category"], "unit": "text"}
    return {"key": {"location": s["district"], "period": s["cycle"], "crop": "UNSPECIFIED", "entity": s["village"]},
            "fields": fields}


def rain_record(m, ym=None, mm=None, days=True):
    f = {"rainfall_mm": {"value": mm if mm is not None else m["rainfall_mm"], "unit": "mm"}}
    if days and "rainy_days_ge_2.5mm" in m:
        f["rainy_days_ge_2_5mm"] = {"value": int(m["rainy_days_ge_2.5mm"]), "unit": "days"}
    return {"key": {"location": canon_district(m["district"]), "period": ym or m["month"], "crop": "UNSPECIFIED",
                    "entity": ""}, "fields": f}


def prod_record(p, area=True, prod=True, yld=True, with_season=False):
    f = {}
    if area and p["area"]:
        f["area_ha"] = {"value": float(p["area"]), "unit": "ha"}
    if prod and p["prod"]:
        f["production_t"] = {"value": float(p["prod"]), "unit": "t"}
    if yld and p["yield"] and (p["area"] or p["prod"]):
        f["yield_t_ha"] = {"value": float(p["yield"]), "unit": "t/ha"}
    return {"key": {"location": p["district"], "period": p["period"], "crop": CROP_CANON.get(p["crop"], p["crop"]),
                    "entity": p["season"] if with_season else "UNSPECIFIED"}, "fields": f}   # season only if the file has one


def pick(pool, n, pred=lambda x: True, uniq=lambda x: id(x)):
    cand = [x for x in pool if pred(x)]
    rng.shuffle(cand)
    seen, out = set(), []
    for x in cand:
        k = uniq(x)
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
        if len(out) == n:
            break
    assert len(out) == n, (n, len(out))
    return out


def csv_text(rows, delim=","):
    buf = io.StringIO()
    csv.writer(buf, delimiter=delim, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def fmt(v, comma=False):
    s = "" if v is None else (repr(float(v)) if isinstance(v, float) else str(v))
    return s.replace(".", ",") if comma else s


cases = []   # (id, agent, filename, content, description, difficulty, records, extra)


def add(cid, agent, fname, content, desc, records, difficulty="normal", llm=False, fault=None, src_type=None,
        expected_status="complete"):
    ext = fname.rsplit(".", 1)[-1]
    st = src_type or ("json" if (ext == "json" or (ext == "txt" and content.lstrip().startswith(("{", "[")))) else "csv")
    cases.append(dict(id=cid, agent=agent, file=fname, content=content, desc=desc, records=records,
                      difficulty=difficulty, needs_llm=llm, fault=fault, source_group=st,
                      expected_status=expected_status, trajectory=expected_trajectory(st, agent, llm)))


# ================================================================== SOIL (10)
def uniq_village(s):
    return (s["district"], s["cycle"], s["village"])


# s01 original JSON shape, 2017-19 slice
recs = pick(SOIL, 6, lambda s: s["cycle"] == "2017-19" and s["district"] in ("BISHNUPUR", "THOUBAL", "IMPHAL EAST"),
            uniq_village)
by = {}
for s in recs:
    by.setdefault((s["district"], s["code"]), []).append(s["raw"])
doc = {"source": "slice", "state": "MANIPUR", "state_code": "14", "districts": [
    {"district": d, "district_code": c, "shc_cycles": {"2017-19": rows}} for (d, c), rows in by.items()]}
add("soil_01_json_original_shape", "soil", "soil_01.json", json.dumps(doc, indent=1),
    "Same nesting as the scraped SHC file, 6 records from 3 districts", [soil_record(s) for s in recs])

# s02 2024-25 slice (no tehsil/category, lowercase 'village')
recs = pick(SOIL, 8, lambda s: s["cycle"] == "2024-25" and s["vals"]["ph"], uniq_village)
doc = {"districts": [{"district": "CHURACHANDPUR", "district_code": "254",
                      "shc_cycles": {"2024-25": [s["raw"] for s in recs]}}]}
add("soil_02_json_2024_25", "soil", "soil_02.json", json.dumps(doc), "2024-25 cycle layout (lowercase village, no tehsil)",
    [soil_record(s, text=()) for s in recs])

# s03 tidy CSV, long header names with units in brackets
recs = pick(SOIL, 8, lambda s: s["cycle"] == "2017-19" and all(v is not None for v in s["vals"].values()), uniq_village)
hdr = ["District", "Tehsil", "Village", "Category", "Cycle", "Available Nitrogen (kg/ha)", "Available Phosphorus (kg/ha)",
       "Available Potassium (kg/ha)", "pH", "Organic Carbon (%)", "EC (dS/m)", "Sulphur (ppm)", "Zinc (ppm)",
       "Iron (ppm)", "Copper (ppm)", "Manganese (ppm)", "Boron (ppm)"]
rows = [hdr] + [[s["district"].title(), s["tehsil"], s["village"], s["category"], s["cycle"]] +
                [fmt(s["vals"][k]) for k in ("n", "p", "k", "ph", "oc", "ec", "s", "zn", "fe", "cu", "mn", "b")] for s in recs]
add("soil_03_csv_long_headers", "soil", "soil_03.csv", csv_text(rows), "Tidy CSV, descriptive headers with units",
    [soil_record(s) for s in recs])

# s04 semicolon + decimal comma, short headers
recs = pick(SOIL, 8, lambda s: s["cycle"] == "2017-19" and all(v is not None for v in s["vals"].values()), uniq_village)
hdr = ["Dist", "Taluk", "Village", "Cycle", "N", "P", "K", "pH", "OC", "EC", "S", "Zn", "Fe", "Cu", "Mn", "B"]
rows = [hdr] + [[s["district"], s["tehsil"], s["village"], s["cycle"]] +
                [fmt(s["vals"][k], comma=True) for k in ("n", "p", "k", "ph", "oc", "ec", "s", "zn", "fe", "cu", "mn", "b")]
                for s in recs]
add("soil_04_csv_semicolon_decimal_comma", "soil", "soil_04.csv", csv_text(rows, ";"),
    "';' delimiter with decimal commas", [soil_record(s, text=("tehsil",)) for s in recs], difficulty="hard")

# s05 JSON keyed by district name
recs = pick(SOIL, 6, lambda s: s["cycle"] == "2017-19" and s["vals"]["n"] is not None,
            lambda s: s["district"])
doc = {s["district"]: {"cycle": s["cycle"], "village": s["village"], "nitrogen": s["vals"]["n"],
                       "phosphorus": s["vals"]["p"], "potassium": s["vals"]["k"], "ph": s["vals"]["ph"],
                       "organic_carbon": s["vals"]["oc"], "ec": s["vals"]["ec"], "sulphur": s["vals"]["s"],
                       "zinc": s["vals"]["zn"], "iron": s["vals"]["fe"], "copper": s["vals"]["cu"],
                       "manganese": s["vals"]["mn"], "boron": s["vals"]["b"]} for s in recs}
add("soil_05_json_keyed_by_district", "soil", "soil_05.json", json.dumps(doc, indent=1),
    "Object keyed by district name, spelled-out nutrient names", [soil_record(s, text=()) for s in recs])

# s06 cycle -> district -> list
recs = pick(SOIL, 8, lambda s: s["cycle"] == "2017-19" and s["district"] in ("CHANDEL", "UKHRUL", "KAKCHING"), uniq_village)
doc = {"by_cycle": {"2017-19": {}}}
for s in recs:
    doc["by_cycle"]["2017-19"].setdefault(s["district"], []).append(
        {"Village": s["village"], **{k.upper() if k != "ph" else "pH": v for k, v in s["vals"].items() if v is not None}})
add("soil_06_json_cycle_district_lists", "soil", "soil_06.json", json.dumps(doc), "cycle -> district -> records",
    [soil_record(s, text=()) for s in recs])

# s07 JSON Lines saved as .txt
recs = pick(SOIL, 8, lambda s: s["cycle"] == "2017-19", uniq_village)
lines = [json.dumps({"district": s["district"], "cycle": s["cycle"], "village": s["village"],
                     **{k.upper() if k != "ph" else "pH": v for k, v in s["vals"].items() if v is not None}}) for s in recs]
add("soil_07_jsonl_as_txt", "soil", "soil_07.txt", "\n".join(lines) + "\n", "JSON Lines in a .txt file",
    [soil_record(s, text=()) for s in recs], difficulty="hard")

# s08 CSV: extra columns, shuffled order, NA / - blanks
recs = pick(SOIL, 8, lambda s: s["cycle"] == "2017-19", uniq_village)
hdr = ["Sample ID", "Cycle", "Lab", "District", "Village", "B", "Zn", "N", "P", "K", "pH", "OC", "EC", "S", "Fe", "Cu", "Mn", "Remarks"]
rows = [hdr]
for i, s in enumerate(recs):
    v = s["vals"]
    rows.append([f"X{i:03d}", s["cycle"], "Imphal Lab", s["district"], s["village"]] +
                [("NA" if v[k] is None else fmt(v[k])) if i % 2 else ("-" if v[k] is None else fmt(v[k]))
                 for k in ("b", "zn", "n", "p", "k", "ph", "oc", "ec", "s", "fe", "cu", "mn")] + ["ok"])
add("soil_08_csv_extra_cols_blanks", "soil", "soil_08.csv", csv_text(rows),
    "Shuffled columns, irrelevant extra columns, NA / '-' for missing", [soil_record(s, text=()) for s in recs],
    difficulty="hard")

# s09 pH recorded as 0.0 -> must be treated as missing
recs = pick(SOIL, 6, lambda s: s["cycle"] == "2024-25" and s["vals"]["ph"] == 0, uniq_village)
doc = {"districts": [{"district": "CHURACHANDPUR", "district_code": "254",
                      "shc_cycles": {"2024-25": [s["raw"] for s in recs]}}]}
add("soil_09_ph_zero_is_missing", "soil", "soil_09.json", json.dumps(doc),
    "pH = 0.0 in the source means 'not measured': expected ph absent", [soil_record(s, text=(), skip=("ph",)) for s in recs],
    difficulty="hard")

# s10 French headers - needs the LLM header mapper
recs = pick(SOIL, 6, lambda s: s["cycle"] == "2017-19" and all(s["vals"][k] is not None for k in ("n", "p", "k", "ph", "oc")),
            uniq_village)
hdr = ["Departement", "Commune", "Azote", "Phosphore", "Potasse", "Acidite_pH", "Carbone_org", "Cycle"]
rows = [hdr] + [[s["district"], s["village"], fmt(s["vals"]["n"]), fmt(s["vals"]["p"]), fmt(s["vals"]["k"]),
                 fmt(s["vals"]["ph"]), fmt(s["vals"]["oc"]), s["cycle"]] for s in recs]
r10 = [soil_record(s, text=()) for s in recs]
for r in r10:
    r["fields"] = {k: v for k, v in r["fields"].items() if k in ("n", "p", "k", "ph", "oc")}
add("soil_10_csv_french_headers_llm", "soil", "soil_10.csv", csv_text(rows),
    "Non-English headers; rules cannot map them, the Groq header mapper must", r10, difficulty="hard", llm=True)

# ================================================================== RAINFALL (10)
def rain_pick(n, **kw):
    return pick(RAIN, n, uniq=lambda m: (m["district"], m["month"]), **kw)


# r01 original shape
recs = rain_pick(12, pred=lambda m: m["district"] in ("CHANDEL", "THOUBAL", "UKHRUL") and m["month"].startswith("2019"))
by = {}
for m in recs:
    by.setdefault(m["district"], []).append({k: v for k, v in m.items() if k != "district"})
doc = {"source": "slice", "method": "slice", "unit": "mm", "districts": [{"district": d, "monthly": ms} for d, ms in by.items()]}
add("rain_01_json_original_shape", "rainfall", "rain_01.json", json.dumps(doc, indent=1), "Same nesting as the monthly file",
    [rain_record(m) for m in recs])

# r02 tidy CSV YYYY-MM
recs = rain_pick(12, pred=lambda m: m["district"] in ("BISHNUPUR", "SENAPATI", "KAMJONG"))
rows = [["District", "YearMonth", "Rainfall (mm)", "Rainy Days (>=2.5 mm)"]] + \
       [[m["district"].title(), m["month"], fmt(m["rainfall_mm"]), m["rainy_days_ge_2.5mm"]] for m in recs]
add("rain_02_csv_tidy", "rainfall", "rain_02.csv", csv_text(rows), "Tidy CSV with YYYY-MM", [rain_record(m) for m in recs])

# r03 wide months
pairs = [("UKHRUL", "2018"), ("TAMENGLONG", "2021"), ("PHERZAWL", "2016")]
rows = [["District", "Year"] + [m[:3] for m in MONTHS]]
recs = []
for d, y in pairs:
    ms = sorted((m for m in RAIN if m["district"] == d and m["month"].startswith(y)), key=lambda m: m["month"])
    assert len(ms) == 12
    rows.append([d.title(), y] + [fmt(m["rainfall_mm"]) for m in ms])
    recs += [rain_record(m, days=False) for m in ms]
add("rain_03_csv_wide_months", "rainfall", "rain_03.csv", csv_text(rows), "One column per month (Jan..Dec)", recs,
    difficulty="hard")

# r04 separate Year / Month-name columns
recs = rain_pick(12, pred=lambda m: m["district"] in ("CHURACHANDPUR", "JIRIBAM"))
rows = [["District Name", "Year", "Month", "Rainfall"]] + \
       [[m["district"].title(), m["month"][:4], MONTHS[int(m["month"][5:]) - 1], fmt(m["rainfall_mm"])] for m in recs]
add("rain_04_csv_year_month_columns", "rainfall", "rain_04.csv", csv_text(rows), "Separate Year and full month name columns",
    [rain_record(m, days=False) for m in recs])

# r05 'Jan-2019'
recs = rain_pick(12, pred=lambda m: m["district"] in ("IMPHAL WEST", "KANGPOKPI"))
rows = [["District", "Month", "Rain (mm)"]] + \
       [[m["district"], f"{MONTHS[int(m['month'][5:]) - 1][:3]}-{m['month'][:4]}", fmt(m["rainfall_mm"])] for m in recs]
add("rain_05_csv_mon_dash_year", "rainfall", "rain_05.csv", csv_text(rows), "Month as 'Jan-2019'",
    [rain_record(m, days=False) for m in recs])

# r06 inches (expected mm)
recs = rain_pick(10, pred=lambda m: m["district"] in ("CHANDEL", "THOUBAL") and m["rainfall_mm"] > 0)
rows = [["District", "YearMonth", "Rainfall (inches)"]] + \
       [[m["district"], m["month"], fmt(round(m["rainfall_mm"] / 25.4, 4))] for m in recs]
add("rain_06_csv_inches", "rainfall", "rain_06.csv", csv_text(rows), "Rainfall in inches (unit in header) -> expected mm",
    [rain_record(m, days=False) for m in recs], difficulty="hard")

# r07 nested json: district -> list of records
recs = rain_pick(12, pred=lambda m: m["district"] in ("KAKCHING", "SENAPATI", "NONEI"))
doc = {}
for m in recs:
    doc.setdefault(m["district"], []).append({"month": m["month"], "rain_mm": m["rainfall_mm"]})
add("rain_07_json_district_keyed", "rainfall", "rain_07.json", json.dumps(doc), "Object keyed by district, list of {month, rain_mm}",
    [rain_record(m, days=False) for m in recs])

# r08 JSONL with district spelling variants
alias = {"IMPHAL EAST": "Imphal-East", "NONEI": "Noney", "JIRIBAM": "Jiribam", "IMPHAL WEST": "imphal west",
         "TENGNOUPAL": "TENGNOUPAL"}
recs = rain_pick(10, pred=lambda m: m["district"] in alias)
lines = [json.dumps({"dist": alias[m["district"]], "ym": m["month"], "rainfall_mm": m["rainfall_mm"]}) for m in recs]
add("rain_08_jsonl_district_aliases", "rainfall", "rain_08.txt", "\n".join(lines) + "\n",
    "JSON Lines, district spellings (Imphal-East, Noney, imphal west)", [rain_record(m, days=False) for m in recs], difficulty="hard")

# r09 centimetres via a Unit column
recs = rain_pick(10, pred=lambda m: m["district"] in ("UKHRUL", "BISHNUPUR") and m["rainfall_mm"] > 0)
rows = [["District", "Month", "Rainfall", "Unit"]] + [[m["district"], m["month"], fmt(round(m["rainfall_mm"] / 10, 3)), "cm"] for m in recs]
add("rain_09_csv_cm_unit_column", "rainfall", "rain_09.csv", csv_text(rows), "Unit column says cm -> expected mm",
    [rain_record(m, days=False) for m in recs], difficulty="hard")

# r10 footer rows that are not data
recs = rain_pick(8, pred=lambda m: m["district"] in ("THOUBAL", "KAMJONG") and m["month"].startswith("2020"))
rows = [["District", "YearMonth", "Rainfall (mm)"]] + [[m["district"], m["month"], fmt(m["rainfall_mm"])] for m in recs]
rows += [["Total", "", fmt(round(sum(m["rainfall_mm"] for m in recs), 1))], ["Average", "", fmt(round(sum(m["rainfall_mm"] for m in recs) / 8, 1))]]
add("rain_10_csv_summary_footer", "rainfall", "rain_10.csv", csv_text(rows), "Trailing Total / Average rows must not become data",
    [rain_record(m, days=False) for m in recs], difficulty="hard")

# ================================================================== PRODUCTION (10)
def prod_pick(n, **kw):
    return pick(PROD, n, uniq=lambda p: (p["crop"], p["district"], p["period"]), **kw)


def orig_layout(selected, years, with_totals=True, grand=False):
    """rows in the original 3-row-header layout for the given crops/districts/years."""
    rows = [["State/Crop/District", "Season"] + [c for y in years for c in (y, "", "")],
            ["", ""] + ["Area", "Production", "Yield"] * len(years),
            ["", ""] + ["(Hectare)", "(Tonnes)", "(Ton./Ha.)"] * len(years), ["Manipur"] + [""] * (1 + 3 * len(years))]
    src = list(csv.reader((ROOT / "Data" / "CSV_Agent.csv").open(encoding="utf-8-sig")))
    yidx = {y: j for y, j in ((src[0][j], j) for j in range(2, len(src[0]), 3) if src[0][j])}
    cur, n = None, 0
    for r in src[3:]:
        if not r[1] and re.match(r"^\d+\.", r[0]):
            cur = re.sub(r"^\d+\.\s*", "", r[0])
            if cur in selected:
                n += 1
                rows.append([f"{n}. {cur}", ""] + [""] * 3 * len(years))
            continue
        if cur not in selected:
            continue
        if r[0].startswith("Total"):
            if with_totals:
                rows.append(r[:2] + [c for y in years for c in r[yidx[y]:yidx[y] + 3]])
            continue
        d = re.sub(r"^\d+\.\s*", "", r[0])
        if d in selected[cur]:
            rows.append(r[:2] + [c for y in years for c in r[yidx[y]:yidx[y] + 3]])
    if grand:
        rows.append(["Grand Total", ""] + ["99999", "99999", "1"] * len(years))
    return rows


def refs_from_orig(rows, years):
    out, crop = [], None
    for r in rows[4:]:
        if not r[1] and re.match(r"^\d+\.", r[0]):
            crop = re.sub(r"^\d+\.\s*", "", r[0])
            continue
        if r[0].startswith(("Total", "Grand")):
            continue
        for i, y in enumerate(years):
            a, p, yl = r[2 + 3 * i:5 + 3 * i]
            if a.strip() or p.strip():
                out.append(prod_record({"crop": crop, "district": re.sub(r"^\d+\.\s*", "", r[0]).upper(), "season": r[1],
                                        "period": period_label(y), "area": a.strip(), "prod": p.strip(), "yield": yl.strip()},
                                       with_season=True))
    return out


YEARS4 = ["2001 - 2002", "2005 - 2006", "2012 - 2013", "2022 - 2023"]
sel = {"Banana": {"Bishnupur", "Chandel", "Thoubal"}, "Potato": {"Imphal east", "Ukhrul", "Senapati"}}

# p01 original multi-row-header layout
rows = orig_layout(sel, YEARS4)
add("prod_01_csv_original_layout", "production", "prod_01.csv", csv_text(rows),
    "Original 3-row header, hierarchy rows, year blocks, Total rows (skipped)", refs_from_orig(rows, YEARS4))

# p02 tidy
recs = prod_pick(12, pred=lambda p: p["area"] and p["prod"] and p["yield"])
rows = [["Crop", "District", "Year", "Area (ha)", "Production (t)", "Yield (t/ha)"]] + \
       [[p["crop"], p["district"].title(), p["period"], p["area"], p["prod"], p["yield"]] for p in recs]
add("prod_02_csv_tidy", "production", "prod_02.csv", csv_text(rows), "Tidy CSV, units in headers", [prod_record(p) for p in recs])

# p03 acres / quintals
recs = prod_pick(10, pred=lambda p: p["area"] and p["prod"] and float(p["area"]) >= 20)
rows = [["Crop", "District", "Period", "Area (acres)", "Production (quintals)"]] + \
       [[p["crop"], p["district"], p["period"], fmt(round(float(p["area"]) / 0.404686, 2)), fmt(float(p["prod"]) * 10)] for p in recs]
add("prod_03_csv_acres_quintals", "production", "prod_03.csv", csv_text(rows), "Acres + quintals -> expected ha + tonnes",
    [prod_record(p, yld=False) for p in recs], difficulty="hard")

# p04 nested JSON crop -> year -> list
recs = prod_pick(10, pred=lambda p: p["area"] and p["prod"] and p["crop"] in ("Banana", "Wheat", "Tomato"))
doc = {"crops": {}}
for p in recs:
    doc["crops"].setdefault(p["crop"], {}).setdefault(p["period"], []).append(
        {"district": p["district"].title(), "area_ha": float(p["area"]), "production_t": float(p["prod"])})
add("prod_04_json_crop_year_nested", "production", "prod_04.json", json.dumps(doc, indent=1), "crop -> year -> district records",
    [prod_record(p, yld=False) for p in recs])

# p05 two-row header: years row + variable row, crop and district as label columns
recs = prod_pick(10, pred=lambda p: p["area"] and p["prod"] and p["yield"] and p["crop"] in ("Cabbage", "Onion"))
byk = {}
for p in recs:
    byk.setdefault((p["crop"], p["district"]), {})[p["period"]] = p
periods = sorted({p["period"] for p in recs})
rows = [["Crop", "District"] + [c for y in periods for c in (y, "", "")], ["", ""] + ["Area", "Production", "Yield"] * len(periods)]
for (c, d), ps in byk.items():
    rows.append([c, d.title()] + [x for y in periods for x in ((ps[y]["area"], ps[y]["prod"], ps[y]["yield"]) if y in ps else ("", "", ""))])
add("prod_05_csv_two_row_header_wide", "production", "prod_05.csv", csv_text(rows),
    "Years row + Area/Production/Yield row, crop and district in label columns", [prod_record(p) for p in recs], difficulty="hard")

# p06 crop synonyms
want = ["Redish", "Tapioca", "Jack Fruit", "Bhindi", "Dry chillies", "Cashewnut"]
recs = []
for c in want:
    recs += prod_pick(2, pred=lambda p, c=c: p["crop"] == c and p["area"] and p["prod"] and p["yield"])
rows = [["Crop", "District", "Year", "Area (Hectare)", "Production (Tonnes)", "Yield (t/ha)"]] + \
       [[p["crop"], p["district"], p["period"], p["area"], p["prod"], p["yield"]] for p in recs]
add("prod_06_csv_crop_synonyms", "production", "prod_06.csv", csv_text(rows),
    "Crop names that need standardising (Redish->Radish, Tapioca->Cassava, ...)", [prod_record(p) for p in recs], difficulty="hard")

# p07 original layout with Total rows and a Grand Total footer
sel7 = {"Wheat": {"Imphal east", "Thoubal"}, "Tomato": {"Bishnupur", "Imphal west"}}
y7 = ["2019 - 2020", "2021 - 2022", "2022 - 2023"]
rows = orig_layout(sel7, y7, grand=True)
add("prod_07_csv_totals_and_grand_total", "production", "prod_07.csv", csv_text(rows),
    "Original layout + Total rows + a Grand Total row that must all be skipped", refs_from_orig(rows, y7), difficulty="hard")

# p08 kg + hectares
recs = prod_pick(10, pred=lambda p: p["area"] and p["prod"])
rows = [["Crop", "District", "Year", "Area (Hectare)", "Production (Kg)"]] + \
       [[p["crop"], p["district"], p["period"], p["area"], fmt(float(p["prod"]) * 1000)] for p in recs]
add("prod_08_csv_kg", "production", "prod_08.csv", csv_text(rows), "Production in kg -> expected tonnes",
    [prod_record(p, yld=False) for p in recs], difficulty="hard")

# p09 semicolon + decimal comma
recs = prod_pick(10, pred=lambda p: p["area"] and p["prod"] and p["yield"] and "." in p["yield"])
rows = [["Crop", "Distr.", "Agri Year", "Area (ha)", "Production (t)", "Yield (t/ha)"]] + \
       [[p["crop"], p["district"], p["period"], p["area"], p["prod"], p["yield"].replace(".", ",")] for p in recs]
add("prod_09_csv_semicolon_decimal_comma", "production", "prod_09.csv", csv_text(rows, ";"), "';' delimiter, decimal commas in yield",
    [prod_record(p) for p in recs], difficulty="hard")

# p10 blank area cells (only production + yield known)
recs = prod_pick(10, pred=lambda p: p["area"] and p["prod"] and p["yield"])
rows = [["Crop", "District", "Year", "Area (ha)", "Production (t)", "Yield (t/ha)"]] + \
       [[p["crop"], p["district"], p["period"], "" if i % 2 else p["area"], p["prod"], p["yield"]] for i, p in enumerate(recs)]
r10 = [prod_record(p) if i % 2 == 0 else prod_record(p, area=False) for i, p in enumerate(recs)]
add("prod_10_csv_blank_cells", "production", "prod_10.csv", csv_text(rows), "Blank area cells: those fields must be absent, not 0",
    r10, difficulty="hard")

# ================================================================== OPERATIONS CASES (for M11 / M12; no field reference)
add("ops_01_empty_file", "none", "ops_01.csv", "", "Empty file must fail cleanly", [], difficulty="error", expected_status="failed")
cases[-1]["trajectory"] = ["inspect_file", "read_csv_data", "read_csv_data", "read_json_data"]  # retry, then other parser
add("ops_02_not_agri_json", "none", "ops_02.json", json.dumps({"title": "meeting notes", "attendees": ["a", "b"]}),
    "Valid JSON with no agricultural data must fail cleanly", [], difficulty="error", expected_status="failed")
cases[-1]["trajectory"] = ["inspect_file", "read_json_data"]
src = next(c for c in cases if c["id"] == "rain_02_csv_tidy")
add("ops_03_fault_read", "rainfall", "ops_03.csv", src["content"], "Injected read fault -> retry -> complete",
    src["records"], difficulty="fault", fault="read")
cases[-1]["trajectory"] = ["inspect_file", "read_csv_data", "read_csv_data", "rainfall_extract", "sqlite_load"]
src = next(c for c in cases if c["id"] == "soil_03_csv_long_headers")
add("ops_04_fault_load", "soil", "ops_04.csv", src["content"], "Injected load fault -> retry -> complete", src["records"],
    difficulty="fault", fault="load")
cases[-1]["trajectory"] = ["inspect_file", "read_csv_data", "soil_extract", "sqlite_load", "sqlite_load"]

# ================================================================== write + self-check
for d in (CASES, REFS):
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

FACTORS = [1, 0.404686, 0.1, 0.001, 25.4, 10, 1000, 100, 1 / 0.404686, 10.0]


def numbers_in(text):
    """every number that literally occurs in the file: (a) ',' as a delimiter, '.' as decimal (CSV / JSON);
    (b) ';' as delimiter, ',' as decimal (European CSV)."""
    a = re.findall(r"\d+(?:\.\d+)?", text.replace(",", " "))
    b = [c.strip().replace(",", ".") for c in re.split(r"[;\n]", text)]
    b = [float(x) for x in b if re.fullmatch(r"\d+(?:\.\d+)?", x)]
    return [float(x) for x in a] + b


bad = 0
for c in cases:
    (CASES / c["file"]).write_text(c["content"], encoding="utf-8", newline="\n")
    nums = numbers_in(c["content"])
    for rec in c["records"]:
        for f, spec in rec["fields"].items():
            v = spec["value"]
            if isinstance(v, str):
                ok = v.lower() in c["content"].lower()
            else:
                ok = any(abs(n * k - v) <= max(0.011 * abs(v), 0.006) for n in nums for k in FACTORS)
            if not ok:
                bad += 1
                print("REFERENCE NOT FOUND IN CASE FILE:", c["id"], rec["key"], f, spec)
    ref = {"case_id": c["id"], "agent": c["agent"], "source_group": c["source_group"], "file": f"cases/{c['file']}",
           "description": c["desc"], "difficulty": c["difficulty"], "needs_llm": c["needs_llm"],
           "inject_fault": c["fault"], "expected_status": c["expected_status"],
           "expected_trajectory": c["trajectory"], "records": c["records"]}
    (REFS / f"{c['id']}.json").write_text(json.dumps(ref, indent=1, ensure_ascii=False), encoding="utf-8")

per = {}
for c in cases:
    per.setdefault(c["agent"], [0, 0])
    per[c["agent"]][0] += 1
    per[c["agent"]][1] += sum(len(r["fields"]) for r in c["records"])
print("cases per agent / reference fields:", per, " total cases:", len(cases), " reference mismatches:", bad)
assert bad == 0
