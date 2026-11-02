"""
Builds the OFFICIAL Phase 2 evaluation set: eval/cases/* (inputs) + eval/reference/*.json (expected outputs).

Material: ONLY the held-out partition (districts IMPHAL WEST, THOUBAL, UKHRUL) in eval/heldout_source/, produced by
Data/make_partitions.py. Those records are not in db/manipur.db, not in the development cases (eval/dev) and must never be
used to tune the pipeline. This script does not import the pipeline.

Each agent's held-out records are DIVIDED (no record appears in two files) into 10 files, each written in a different layout:
    soil       71 records            -> 10 files   (JSON nestings, CSV/TSV, ';' + decimal comma, JSON Lines, extra columns, French headers)
    rainfall   396 IMD + 396 NASA    -> 5 + 5 files (whole district-year blocks; wide months, inches, cm, 'Jan-2019', aliases, footer rows)
    production 406 rows              -> 10 files   (original APY layout +/- totals, tidy, acres/quintals, kg, nested JSON, synonyms ...)
plus 4 operations cases (empty file, non-data JSON, injected read fault, injected load fault) built from slices of those files.

Reference values are the ORIGINAL source values in canonical units; at the end every reference value is re-checked to appear
(directly or via a unit factor) in its case file. Deterministic (seed 2026).   Run:  python eval/build_heldout.py
"""
import csv
import io
import json
import random
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "eval" / "heldout_source"
CASES = ROOT / "eval" / "cases"
REFS = ROOT / "eval" / "reference"
rng = random.Random(2026)

CROP_CANON = {"Redish": "Radish", "Tapioca": "Cassava", "Jack Fruit": "Jackfruit", "Bhindi": "Okra",
              "Dry chillies": "Chilli (dry)", "Cashewnut": "Cashew Nut", "Sweet potato": "Sweet Potato"}
SOIL_UNITS = {"n": "kg/ha", "p": "kg/ha", "k": "kg/ha", "oc": "%", "ph": "pH units", "ec": "dS/m", "s": "ppm",
              "zn": "ppm", "fe": "ppm", "cu": "ppm", "mn": "ppm", "b": "ppm"}
SRC_SOIL_KEYS = {"n": ("N",), "p": ("P",), "k": ("K",), "oc": ("OC",), "ph": ("pH",), "ec": ("EC",), "s": ("S",),
                 "zn": ("Zn",), "fe": ("fe", "Fe"), "cu": ("Cu",), "mn": ("Mn",), "b": ("B",)}
NUT = ("n", "p", "k", "ph", "oc", "ec", "s", "zn", "fe", "cu", "mn", "b")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December"]


# ------------------------------------------------------------------ small helpers
def fmt(v, comma=False):
    s = "" if v is None else (repr(float(v)) if isinstance(v, float) else str(v))
    return s.replace(".", ",") if comma else s


def csv_text(rows, delim=","):
    buf = io.StringIO()
    csv.writer(buf, delimiter=delim, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def period_label(raw):
    a, b = re.findall(r"\d{4}", raw)
    return f"{a}-{b[-2:]}"


def divide(items, n):
    """shuffle and cut into n near-equal, non-overlapping chunks"""
    items = list(items)
    rng.shuffle(items)
    k, r = divmod(len(items), n)
    out, i = [], 0
    for c in range(n):
        j = i + k + (1 if c < r else 0)
        out.append(items[i:j])
        i = j
    return out


# ------------------------------------------------------------------ held-out loaders
def load_soil():
    doc = json.loads((SRC / "soil_P3.json").read_text(encoding="utf-8"))
    out, seen = [], set()
    for d in doc["districts"]:
        for cyc, rows in d["shc_cycles"].items():
            for r in rows:
                village = r.get("VILLAGE") or r.get("village")
                key = (d["district"], cyc, village)
                if not village or key in seen:      # one record per (district, cycle, village): evaluation keys must be unique
                    continue
                seen.add(key)
                vals = {k: next((r[s] for s in srcs if s in r), None) for k, srcs in SRC_SOIL_KEYS.items()}
                if vals["ph"] is not None and vals["ph"] <= 0:
                    vals["ph"] = None               # declared rule: pH 0.0 = not measured
                out.append({"district": d["district"], "code": d["district_code"], "cycle": cyc, "raw": r, "village": village,
                            "tehsil": r.get("TEHSIL"), "category": r.get("Category"), "vals": vals})
    return out


def load_rain(name):
    doc = json.loads((SRC / name).read_text(encoding="utf-8"))
    return [{"district": d["district"], **m} for d in doc["districts"] for m in d["monthly"] if m.get("rainfall_mm") is not None]


def load_prod():
    rows = list(csv.reader((SRC / "prod_P3.csv").open(encoding="utf-8-sig")))
    years = [(rows[0][j], j) for j in range(2, len(rows[0]), 3) if rows[0][j]]
    out, crop = [], None
    for r in rows[3:]:
        if not r[1]:
            if re.match(r"^\d+\.", r[0]):
                crop = re.sub(r"^\d+\.\s*", "", r[0])
            continue
        dist = re.sub(r"^\d+\.\s*", "", r[0]).upper()
        for yr, j in years:
            a, p, y = r[j:j + 3]
            if a.strip() or p.strip():
                out.append({"crop": crop, "district": dist, "season": r[1], "year_raw": yr, "period": period_label(yr),
                            "area": a.strip(), "prod": p.strip(), "yield": y.strip()})
    return out


SOIL = load_soil()
RAIN_IMD, RAIN_NASA = load_rain("rain_P3_imd.json"), load_rain("rain_P3_nasa.json")
PROD = load_prod()


# ------------------------------------------------------------------ reference records
def soil_record(s, text=("village", "tehsil", "category"), only=None):
    fields = {k: {"value": float(v), "unit": SOIL_UNITS[k]} for k, v in s["vals"].items()
              if v is not None and (only is None or k in only)}
    if "tehsil" in text and s["tehsil"]:
        fields["tehsil"] = {"value": s["tehsil"], "unit": "text"}
    if "category" in text and s["category"]:
        fields["category"] = {"value": s["category"], "unit": "text"}
    return {"key": {"location": s["district"], "period": s["cycle"], "crop": "UNSPECIFIED", "entity": s["village"]}, "fields": fields}


def rain_record(m, days=True):
    f = {"rainfall_mm": {"value": m["rainfall_mm"], "unit": "mm"}}
    if days and "rainy_days_ge_2.5mm" in m:
        f["rainy_days_ge_2_5mm"] = {"value": int(m["rainy_days_ge_2.5mm"]), "unit": "days"}
    return {"key": {"location": m["district"], "period": m["month"], "crop": "UNSPECIFIED", "entity": ""}, "fields": f}


def prod_record(p, area=True, prod=True, yld=True, with_season=False):
    f = {}
    if area and p["area"]:
        f["area_ha"] = {"value": float(p["area"]), "unit": "ha"}
    if prod and p["prod"]:
        f["production_t"] = {"value": float(p["prod"]), "unit": "t"}
    if yld and p["yield"] and (p["area"] or p["prod"]):
        f["yield_t_ha"] = {"value": float(p["yield"]), "unit": "t/ha"}
    return {"key": {"location": p["district"], "period": p["period"], "crop": CROP_CANON.get(p["crop"], p["crop"]),
                    "entity": p["season"] if with_season else "UNSPECIFIED"}, "fields": f}


def lower_keys(s):
    return {k.upper() if k != "ph" else "pH": v for k, v in s["vals"].items() if v is not None}


# ================================================================== SOIL layouts: each takes records -> (ext, content, refs, desc, difficulty, llm)
def soil_l0(rs):
    by = {}
    for s in rs:
        by.setdefault((s["district"], s["code"]), {}).setdefault(s["cycle"], []).append(s["raw"])
    doc = {"source": "slice", "state": "MANIPUR", "state_code": "14",
           "districts": [{"district": d, "district_code": c, "shc_cycles": cy} for (d, c), cy in by.items()]}
    return "json", json.dumps(doc, indent=1), [soil_record(s) for s in rs], "Same nesting as the scraped SHC file", "normal", False


def soil_l1(rs):
    doc = {"by_cycle": {}}
    for s in rs:
        doc["by_cycle"].setdefault(s["cycle"], {}).setdefault(s["district"], []).append({"Village": s["village"], **lower_keys(s)})
    return "json", json.dumps(doc), [soil_record(s, text=()) for s in rs], "cycle -> district -> records", "normal", False


def soil_l2(rs):
    hdr = ["District", "Tehsil", "Village", "Category", "Cycle", "Available Nitrogen (kg/ha)", "Available Phosphorus (kg/ha)",
           "Available Potassium (kg/ha)", "pH", "Organic Carbon (%)", "EC (dS/m)", "Sulphur (ppm)", "Zinc (ppm)", "Iron (ppm)",
           "Copper (ppm)", "Manganese (ppm)", "Boron (ppm)"]
    rows = [hdr] + [[s["district"].title(), s["tehsil"] or "", s["village"], s["category"] or "", s["cycle"]] +
                    [fmt(s["vals"][k]) for k in NUT] for s in rs]
    return "csv", csv_text(rows), [soil_record(s) for s in rs], "Tidy CSV, descriptive headers with units", "normal", False


def soil_l3(rs):
    hdr = ["Dist", "Taluk", "Village", "Cycle"] + ["N", "P", "K", "pH", "OC", "EC", "S", "Zn", "Fe", "Cu", "Mn", "B"]
    rows = [hdr] + [[s["district"], s["tehsil"] or "", s["village"], s["cycle"]] + [fmt(s["vals"][k], True) for k in NUT] for s in rs]
    return "csv", csv_text(rows, ";"), [soil_record(s, text=("tehsil",)) for s in rs], "';' delimiter with decimal commas", "hard", False


def soil_l4(rs):
    doc = {}
    for s in rs:
        doc.setdefault(s["district"], []).append({"cycle": s["cycle"], "village": s["village"],
            **{n: s["vals"][k] for n, k in (("nitrogen", "n"), ("phosphorus", "p"), ("potassium", "k"), ("ph", "ph"),
               ("organic_carbon", "oc"), ("ec", "ec"), ("sulphur", "s"), ("zinc", "zn"), ("iron", "fe"), ("copper", "cu"),
               ("manganese", "mn"), ("boron", "b")) if s["vals"][k] is not None}})
    return "json", json.dumps(doc, indent=1), [soil_record(s, text=()) for s in rs], "Object keyed by district, spelled-out nutrient names", "normal", False


def soil_l5(rs):
    lines = [json.dumps({"district": s["district"], "cycle": s["cycle"], "village": s["village"], **lower_keys(s)}) for s in rs]
    return "txt", "\n".join(lines) + "\n", [soil_record(s, text=()) for s in rs], "JSON Lines in a .txt file", "hard", False


def soil_l6(rs):
    hdr = ["Sample ID", "Cycle", "Lab", "District", "Village", "B", "Zn", "N", "P", "K", "pH", "OC", "EC", "S", "Fe", "Cu", "Mn", "Remarks"]
    rows = [hdr]
    for i, s in enumerate(rs):
        v = s["vals"]
        rows.append([f"X{i:03d}", s["cycle"], "Imphal Lab", s["district"], s["village"]] +
                    [("NA" if v[k] is None else fmt(v[k])) if i % 2 else ("-" if v[k] is None else fmt(v[k]))
                     for k in ("b", "zn", "n", "p", "k", "ph", "oc", "ec", "s", "fe", "cu", "mn")] + ["ok"])
    return "csv", csv_text(rows), [soil_record(s, text=()) for s in rs], "Shuffled columns, irrelevant extra columns, NA / '-' for missing", "hard", False


def soil_l7(rs):
    names = {"n": "Available Nitrogen (kg/ha)", "p": "Available Phosphorus (kg/ha)", "k": "Available Potassium (kg/ha)", "ph": "pH",
             "oc": "Organic Carbon (%)", "ec": "EC (dS/m)", "s": "Sulphur (ppm)", "zn": "Zinc (ppm)", "fe": "Iron (ppm)",
             "cu": "Copper (ppm)", "mn": "Manganese (ppm)", "b": "Boron (ppm)"}
    doc = {"report": {"data": [{"District": s["district"], "Cycle": s["cycle"], "Village": s["village"],
                                **{names[k]: v for k, v in s["vals"].items() if v is not None}} for s in rs]}}
    return "json", json.dumps(doc, indent=1), [soil_record(s, text=()) for s in rs], "Array of records with long unit-bearing keys under wrapper keys", "normal", False


def soil_l8(rs):
    hdr = ["Departement", "Commune", "Azote", "Phosphore", "Potasse", "Acidite_pH", "Carbone_org", "Cycle"]
    rows = [hdr] + [[s["district"], s["village"], fmt(s["vals"]["n"]), fmt(s["vals"]["p"]), fmt(s["vals"]["k"]), fmt(s["vals"]["ph"]),
                     fmt(s["vals"]["oc"]), s["cycle"]] for s in rs]
    return "csv", csv_text(rows), [soil_record(s, text=(), only=("n", "p", "k", "ph", "oc")) for s in rs], \
        "Non-English headers; rules cannot map them, the Groq header mapper must", "hard", True


def soil_l9(rs):
    hdr = ["District Name", "Village Name", "SHC Cycle", "Nitrogen", "Phosphorus", "Potassium", "pH", "Organic Carbon", "EC",
           "Sulphur", "Zinc", "Iron", "Copper", "Manganese", "Boron"]
    rows = [hdr] + [[s["district"], s["village"], s["cycle"]] + [fmt(s["vals"][k]) for k in NUT] for s in rs]
    return "txt", csv_text(rows, "\t"), [soil_record(s, text=()) for s in rs], "Tab-delimited text file, full nutrient names", "normal", False


SOIL_LAYOUTS = [("json_original_shape", soil_l0), ("json_cycle_district_lists", soil_l1), ("csv_long_headers", soil_l2),
                ("csv_semicolon_decimal_comma", soil_l3), ("json_keyed_by_district", soil_l4), ("jsonl_as_txt", soil_l5),
                ("csv_extra_cols_blanks", soil_l6), ("json_wrapped_long_keys", soil_l7), ("csv_french_headers_llm", soil_l8),
                ("tsv_full_names", soil_l9)]


# ================================================================== RAINFALL layouts
def rain_l0(rs):
    by = {}
    for m in rs:
        by.setdefault(m["district"], []).append({k: v for k, v in m.items() if k != "district"})
    doc = {"source": "slice", "unit": "mm", "districts": [{"district": d, "monthly": ms} for d, ms in by.items()]}
    return "json", json.dumps(doc, indent=1), [rain_record(m) for m in rs], "Same nesting as the monthly file", "normal"


def rain_l1(rs):
    rows = [["District", "YearMonth", "Rainfall (mm)", "Rainy Days (>=2.5 mm)"]] + \
           [[m["district"].title(), m["month"], fmt(m["rainfall_mm"]), m["rainy_days_ge_2.5mm"]] for m in rs]
    return "csv", csv_text(rows), [rain_record(m) for m in rs], "Tidy CSV with YYYY-MM", "normal"


def rain_l2(rs):
    blocks = {}
    for m in rs:
        blocks.setdefault((m["district"], m["month"][:4]), {})[int(m["month"][5:])] = m
    rows = [["District", "Year"] + [x[:3] for x in MONTHS]]
    for (d, y), mm in sorted(blocks.items()):
        rows.append([d.title(), y] + [fmt(mm[i]["rainfall_mm"]) if i in mm else "" for i in range(1, 13)])
    return "csv", csv_text(rows), [rain_record(m, days=False) for m in rs], "One column per month (Jan..Dec)", "hard"


def rain_l3(rs):
    rows = [["District Name", "Year", "Month", "Rainfall"]] + \
           [[m["district"].title(), m["month"][:4], MONTHS[int(m["month"][5:]) - 1], fmt(m["rainfall_mm"])] for m in rs]
    return "csv", csv_text(rows), [rain_record(m, days=False) for m in rs], "Separate Year and full month name columns", "normal"


def rain_l4(rs):
    rows = [["District", "Month", "Rain (mm)"]] + \
           [[m["district"], f"{MONTHS[int(m['month'][5:]) - 1][:3]}-{m['month'][:4]}", fmt(m["rainfall_mm"])] for m in rs]
    return "csv", csv_text(rows), [rain_record(m, days=False) for m in rs], "Month as 'Jan-2019'", "normal"


def rain_l5(rs):
    rows = [["District", "YearMonth", "Rainfall (inches)"]] + [[m["district"], m["month"], fmt(round(m["rainfall_mm"] / 25.4, 6))] for m in rs]
    return "csv", csv_text(rows), [rain_record(m, days=False) for m in rs], "Rainfall in inches (unit in header) -> expected mm", "hard"


def rain_l6(rs):
    doc = {}
    for m in rs:
        doc.setdefault(m["district"], []).append({"month": m["month"], "rain_mm": m["rainfall_mm"]})
    return "json", json.dumps(doc), [rain_record(m, days=False) for m in rs], "Object keyed by district, list of {month, rain_mm}", "normal"


ALIAS = {"IMPHAL WEST": "Imphal-West", "THOUBAL": "thoubal", "UKHRUL": "UKHRUL"}


def rain_l7(rs):
    lines = [json.dumps({"dist": ALIAS[m["district"]], "ym": m["month"], "rainfall_mm": m["rainfall_mm"]}) for m in rs]
    return "txt", "\n".join(lines) + "\n", [rain_record(m, days=False) for m in rs], "JSON Lines, district spellings (Imphal-West, thoubal)", "hard"


def rain_l8(rs):
    rows = [["District", "Month", "Rainfall", "Unit"]] + [[m["district"], m["month"], fmt(round(m["rainfall_mm"] / 10, 5)), "cm"] for m in rs]
    return "csv", csv_text(rows), [rain_record(m, days=False) for m in rs], "Unit column says cm -> expected mm", "hard"


def rain_l9(rs):
    rows = [["District", "YearMonth", "Rainfall (mm)"]] + [[m["district"], m["month"], fmt(m["rainfall_mm"])] for m in rs]
    tot = round(sum(m["rainfall_mm"] for m in rs), 1)
    rows += [["Total", "", fmt(tot)], ["Average", "", fmt(round(tot / len(rs), 1))]]
    return "csv", csv_text(rows), [rain_record(m, days=False) for m in rs], "Trailing Total / Average rows must not become data", "hard"


RAIN_LAYOUTS = [("json_original_shape", rain_l0), ("csv_tidy", rain_l1), ("csv_wide_months", rain_l2),
                ("csv_year_month_columns", rain_l3), ("csv_mon_dash_year", rain_l4), ("csv_inches", rain_l5),
                ("json_district_keyed", rain_l6), ("jsonl_district_aliases", rain_l7), ("csv_cm_unit_column", rain_l8),
                ("csv_summary_footer", rain_l9)]


# ================================================================== PRODUCTION layouts
SRC_ROWS = list(csv.reader((SRC / "prod_P3.csv").open(encoding="utf-8-sig")))
YIDX = {y: j for y, j in ((SRC_ROWS[0][j], j) for j in range(2, len(SRC_ROWS[0]), 3) if SRC_ROWS[0][j])}


def orig_layout(pairs, with_totals=True, grand=False):
    """original APY layout (3-row header, hierarchy rows, year blocks) restricted to the (crop, DISTRICT) pairs."""
    yrs = sorted({p["year_raw"] for p in PROD if (p["crop"], p["district"]) in pairs}, key=lambda y: int(y[:4]))
    rows = [["State/Crop/District", "Season"] + [c for y in yrs for c in (y, "", "")],
            ["", ""] + ["Area", "Production", "Yield"] * len(yrs),
            ["", ""] + ["(Hectare)", "(Tonnes)", "(Ton./Ha.)"] * len(yrs), ["Manipur"] + [""] * (1 + 3 * len(yrs))]
    cur, n, grp = None, 0, []
    groups = []
    for r in SRC_ROWS[4:]:
        if not r[1] and re.match(r"^\d+\.", r[0]):
            cur = re.sub(r"^\d+\.\s*", "", r[0])
            continue
        if r[1] and (cur, re.sub(r"^\d+\.\s*", "", r[0]).upper()) in pairs:
            groups.append((cur, r))
    for crop in dict.fromkeys(c for c, _ in groups):
        n += 1
        rows.append([f"{n}. {crop}", ""] + [""] * 3 * len(yrs))
        sums = [0.0, 0.0] * len(yrs)
        for c, r in groups:
            if c != crop:
                continue
            cells = [x for y in yrs for x in r[YIDX[y]:YIDX[y] + 3]]
            rows.append(r[:2] + cells)
            for i in range(len(yrs)):
                for k in (0, 1):
                    try:
                        sums[2 * i + k] += float(cells[3 * i + k])
                    except ValueError:
                        pass
        if with_totals:
            rows.append([f"Total ({crop})", ""] + [x for i in range(len(yrs)) for x in (fmt(round(sums[2 * i], 2)), fmt(round(sums[2 * i + 1], 2)), "")])
    if grand:
        rows.append(["Grand Total", ""] + ["99999", "99999", "1"] * len(yrs))
    return rows, yrs


def refs_from_orig(rows, yrs):
    out, crop = [], None
    for r in rows[4:]:
        if not r[1] and re.match(r"^\d+\.", r[0]):
            crop = re.sub(r"^\d+\.\s*", "", r[0])
            continue
        if r[0].startswith(("Total", "Grand")):
            continue
        for i, y in enumerate(yrs):
            a, p, yl = r[2 + 3 * i:5 + 3 * i]
            if a.strip() or p.strip():
                out.append(prod_record({"crop": crop, "district": re.sub(r"^\d+\.\s*", "", r[0]).upper(), "season": r[1],
                                        "period": period_label(y), "area": a.strip(), "prod": p.strip(), "yield": yl.strip()},
                                       with_season=True))
    return out


def pairs_of(rs):
    return {(p["crop"], p["district"]) for p in rs}


def prod_l0(rs):
    rows, yrs = orig_layout(pairs_of(rs))
    return "csv", csv_text(rows), refs_from_orig(rows, yrs), "Original 3-row header, hierarchy rows, year blocks, Total rows (skipped)", "normal"


def prod_l1(rs):
    rows = [["Crop", "District", "Year", "Area (ha)", "Production (t)", "Yield (t/ha)"]] + \
           [[p["crop"], p["district"].title(), p["period"], p["area"], p["prod"], p["yield"]] for p in rs]
    return "csv", csv_text(rows), [prod_record(p) for p in rs], "Tidy CSV, units in headers", "normal"


def prod_l2(rs):
    rows = [["Crop", "District", "Period", "Area (acres)", "Production (quintals)"]] + \
           [[p["crop"], p["district"], p["period"], fmt(round(float(p["area"]) / 0.404686, 4)) if p["area"] else "",
             fmt(round(float(p["prod"]) * 10, 3)) if p["prod"] else ""] for p in rs]
    return "csv", csv_text(rows), [prod_record(p, yld=False) for p in rs], "Acres + quintals -> expected ha + tonnes", "hard"


def prod_l3(rs):
    doc = {"crops": {}}
    for p in rs:
        d = {"district": p["district"].title()}
        if p["area"]:
            d["area_ha"] = float(p["area"])
        if p["prod"]:
            d["production_t"] = float(p["prod"])
        doc["crops"].setdefault(p["crop"], {}).setdefault(p["period"], []).append(d)
    return "json", json.dumps(doc, indent=1), [prod_record(p, yld=False) for p in rs], "crop -> year -> district records", "normal"


def prod_l4(rs):
    byk = {}
    for p in rs:
        byk.setdefault((p["crop"], p["district"]), {})[p["period"]] = p
    periods = sorted({p["period"] for p in rs})
    rows = [["Crop", "District"] + [c for y in periods for c in (y, "", "")], ["", ""] + ["Area", "Production", "Yield"] * len(periods)]
    for (c, d), ps in byk.items():
        rows.append([c, d.title()] + [x for y in periods for x in ((ps[y]["area"], ps[y]["prod"], ps[y]["yield"]) if y in ps else ("", "", ""))])
    return "csv", csv_text(rows), [prod_record(p) for p in rs], "Years row + Area/Production/Yield row, crop and district in label columns", "hard"


def prod_l5(rs):
    rows = [["Crop", "District", "Year", "Area (Hectare)", "Production (Tonnes)", "Yield (t/ha)"]] + \
           [[p["crop"], p["district"], p["period"], p["area"], p["prod"], p["yield"]] for p in rs]
    return "csv", csv_text(rows), [prod_record(p) for p in rs], "Crop names that need standardising (Redish->Radish, Tapioca->Cassava, ...)", "hard"


def prod_l6(rs):
    rows, yrs = orig_layout(pairs_of(rs), grand=True)
    return "csv", csv_text(rows), refs_from_orig(rows, yrs), "Original layout + Total rows + a Grand Total row that must all be skipped", "hard"


def prod_l7(rs):
    rows = [["Crop", "District", "Year", "Area (Hectare)", "Production (Kg)"]] + \
           [[p["crop"], p["district"], p["period"], p["area"], fmt(round(float(p["prod"]) * 1000, 3)) if p["prod"] else ""] for p in rs]
    return "csv", csv_text(rows), [prod_record(p, yld=False) for p in rs], "Production in kg -> expected tonnes", "hard"


def prod_l8(rs):
    rows = [["Crop", "Distr.", "Agri Year", "Area (ha)", "Production (t)", "Yield (t/ha)"]] + \
           [[p["crop"], p["district"], p["period"], p["area"].replace(".", ","), p["prod"].replace(".", ","),
             p["yield"].replace(".", ",")] for p in rs]
    return "csv", csv_text(rows, ";"), [prod_record(p) for p in rs], "';' delimiter, decimal commas in every number column", "hard"


def prod_l9(rs):
    rows = [["Crop", "District", "Year", "Area (ha)", "Production (t)", "Yield (t/ha)"]] + \
           [[p["crop"], p["district"], p["period"], "" if i % 2 else p["area"], p["prod"], p["yield"]] for i, p in enumerate(rs)]
    refs = [prod_record(p) if i % 2 == 0 else prod_record(p, area=False) for i, p in enumerate(rs)]
    return "csv", csv_text(rows), refs, "Blank area cells: those fields must be absent, not 0", "hard"


PROD_LAYOUTS = [("csv_original_layout", prod_l0), ("csv_tidy", prod_l1), ("csv_acres_quintals", prod_l2),
                ("json_crop_year_nested", prod_l3), ("csv_two_row_header_wide", prod_l4), ("csv_crop_synonyms", prod_l5),
                ("csv_totals_and_grand_total", prod_l6), ("csv_kg", prod_l7), ("csv_semicolon_decimal_comma", prod_l8),
                ("csv_blank_cells", prod_l9)]


# ================================================================== assemble: divide the held-out records, one chunk per layout
cases = []


def expected_trajectory(st, agent, llm=False):
    t = ["inspect_file", "read_json_data" if st == "json" else "read_csv_data"]
    if llm:
        t.append("llm_map_headers")
    return t + [{"soil": "soil_extract", "rainfall": "rainfall_extract", "production": "production_extract"}.get(agent, "none"), "sqlite_load"]


def add(cid, agent, fname, content, desc, records, difficulty="normal", llm=False, fault=None, expected_status="complete", traj=None):
    ext = fname.rsplit(".", 1)[-1]
    st = "json" if (ext == "json" or (ext == "txt" and content.lstrip().startswith(("{", "[")))) else "csv"
    cases.append(dict(id=cid, agent=agent, file=fname, content=content, desc=desc, records=records, difficulty=difficulty,
                      needs_llm=llm, fault=fault, source_group=st, expected_status=expected_status,
                      trajectory=traj or expected_trajectory(st, agent, llm), n_source_records=0))


chunks = {}
# soil: 71 records -> 10 files
chunks["soil"] = divide(SOIL, 10)
for i, ((name, fn), rs) in enumerate(zip(SOIL_LAYOUTS, chunks["soil"]), 1):
    ext, content, refs, desc, diff, llm = fn(rs)
    add(f"soil_h{i:02d}_{name}", "soil", f"soil_h{i:02d}.{ext}", content, desc, refs, diff, llm)
    cases[-1]["n_source_records"] = len(rs)

# rainfall: whole (district, year) blocks; layouts alternate IMD (S22) / NASA POWER (S21) so neither source is a single case type
def rain_chunks(pool):
    blocks = {}
    for m in pool:
        blocks.setdefault((m["district"], m["month"][:4]), []).append(m)
    return [[m for b in c for m in b] for c in divide(list(blocks.values()), 5)]


imd_c, nasa_c = rain_chunks(RAIN_IMD), rain_chunks(RAIN_NASA)
chunks["rainfall"] = []
for i, (name, fn) in enumerate(RAIN_LAYOUTS, 1):
    rs = (imd_c if i % 2 else nasa_c)[(i - 1) // 2]
    src = "imd" if i % 2 else "nasa"
    chunks["rainfall"].append(rs)
    ext, content, refs, desc, diff = fn(rs)
    add(f"rain_h{i:02d}_{name}_{src}", "rainfall", f"rain_h{i:02d}.{ext}", content, f"{desc} [{'S22 IMD grid' if src == 'imd' else 'S21 NASA POWER'}]", refs, diff)
    cases[-1]["n_source_records"] = len(rs)

# production: divide whole (crop, district) pairs; the synonym-crop pairs are kept together for the synonyms layout
pairs = sorted(pairs_of(PROD))
syn = [pr for pr in pairs if pr[0] in CROP_CANON]
rest = [pr for pr in pairs if pr[0] not in CROP_CANON]
pair_chunks = divide(rest, 9)
pair_chunks.insert(5, syn)
chunks["production"] = []
for i, ((name, fn), prs) in enumerate(zip(PROD_LAYOUTS, pair_chunks), 1):
    prs = set(prs)
    rs = [p for p in PROD if (p["crop"], p["district"]) in prs]
    chunks["production"].append(rs)
    ext, content, refs, desc, diff = fn(rs)
    add(f"prod_h{i:02d}_{name}", "production", f"prod_h{i:02d}.{ext}", content, desc, refs, diff)
    cases[-1]["n_source_records"] = len(rs)

# operations cases (robustness / fault injection; slices of held-out chunks)
add("ops_01_empty_file", "none", "ops_01.csv", "\n\n", "Blank file (only newlines) must fail cleanly", [], "error", expected_status="failed",
    traj=["inspect_file", "read_csv_data", "read_csv_data", "read_json_data"])
add("ops_02_not_agri_json", "none", "ops_02.json", json.dumps({"title": "committee agenda", "items": ["budget", "travel"], "room": 4}),
    "Valid JSON with no agricultural data must fail cleanly", [], "error", expected_status="failed", traj=["inspect_file", "read_json_data"])
ext, content, refs, desc, diff = rain_l1(chunks["rainfall"][1][:12])
add("ops_03_fault_read", "rainfall", "ops_03.csv", content, "Injected read fault -> retry -> complete", refs, "fault", fault="read",
    traj=["inspect_file", "read_csv_data", "read_csv_data", "rainfall_extract", "sqlite_load"])
ext, content, refs, desc, diff, _ = soil_l2(chunks["soil"][2][:8])
add("ops_04_fault_load", "soil", "ops_04.csv", content, "Injected load fault -> retry -> complete", refs, "fault", fault="load",
    traj=["inspect_file", "read_csv_data", "soil_extract", "sqlite_load", "sqlite_load"])

# ================================================================== write + self-check
for d in (CASES, REFS):                 # rebuild everything except the PDF cases (eval/build_pdf_cases.py owns pdf_*)
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*"):
        if old.is_file() and not old.name.startswith("pdf_"):
            old.unlink()

FACTORS = [1, 0.404686, 0.1, 0.001, 25.4, 10, 1000, 100, 1 / 0.404686, 10.0]


def numbers_in(text):
    a = re.findall(r"\d+(?:\.\d+)?", text.replace(",", " "))
    b = [c.strip().replace(",", ".") for c in re.split(r"[;\t\n]", text)]
    b = [float(x) for x in b if re.fullmatch(r"\d+(?:\.\d+)?", x)]
    return [float(x) for x in a] + b


bad = 0
for c in cases:
    (CASES / c["file"]).write_text(c["content"], encoding="utf-8", newline="\n")
    nums = numbers_in(c["content"])
    for rec in c["records"]:
        for f, spec in rec["fields"].items():
            v = spec["value"]
            ok = v.lower() in c["content"].lower() if isinstance(v, str) else \
                any(abs(n * k - v) <= max(0.011 * abs(v), 0.006) for n in nums for k in FACTORS)
            if not ok:
                bad += 1
                print("REFERENCE NOT FOUND IN CASE FILE:", c["id"], rec["key"], f, spec)
    ref = {"case_id": c["id"], "agent": c["agent"], "source_group": c["source_group"], "file": f"cases/{c['file']}",
           "description": c["desc"], "difficulty": c["difficulty"], "needs_llm": c["needs_llm"], "inject_fault": c["fault"],
           "expected_status": c["expected_status"], "expected_trajectory": c["trajectory"],
           "n_source_records": c["n_source_records"], "records": c["records"]}
    (REFS / f"{c['id']}.json").write_text(json.dumps(ref, indent=1, ensure_ascii=False), encoding="utf-8")

per = {}
for c in cases:
    per.setdefault(c["agent"], [0, 0, 0])
    per[c["agent"]][0] += 1
    per[c["agent"]][1] += sum(len(r["fields"]) for r in c["records"])
    per[c["agent"]][2] += c["n_source_records"]
print("held-out records per agent:", {"soil": len(SOIL), "rainfall": len(RAIN_IMD) + len(RAIN_NASA), "production": len(PROD)})
print("cases / reference fields / source records:", per, " total:", len(cases), " mismatches:", bad)
assert bad == 0

# freeze the evaluation files' hashes next to the dev-partition hashes (used by eval/contamination_check.py)
import hashlib
MAN = ROOT / "eval" / "heldout" / "split_manifest.json"
man = json.loads(MAN.read_text(encoding="utf-8"))
man["eval_cases"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(CASES.glob("*"))}
MAN.write_text(json.dumps(man, indent=1), encoding="utf-8")
print("froze", len(man["eval_cases"]), "evaluation file hashes in", MAN.relative_to(ROOT).as_posix())
