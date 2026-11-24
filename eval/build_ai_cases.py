"""
Builds the evaluation set for the AI-assisted capabilities: files the rule-based pipeline cannot (or can barely) read.

    python eval/build_ai_cases.py --set dev        # dev districts (Data/partitions)         -> eval/dev/ai/cases + reference  (prompts / thresholds are tuned on these)
    python eval/build_ai_cases.py --set official   # held-out districts (eval/heldout_source) -> eval/ai/cases + reference      (run once, cold)

Every case is real records re-shaped into a layout that breaks the rule-based reader. The reference holds the ORIGINAL values in canonical
units, so a reader that mis-reads the layout, a unit, a language or an impossible value is scored wrong. 20 cases:
  structure   preamble lines above the header / unit only in the caption / repeated header + footnote deep inside 150 rows /
              districts as columns (melt) / records across columns (transposed)
  formats     Excel workbook with a cover sheet, title row and merged cells / HTML page with an unrelated table / image of a table
  prose       figures inside sentences (English), in Tamil
  languages   Hindi, Bengali-script (native digits) and Tamil CSV: headers, district names, month names and unit words
  pdf         60-page PDF whose only rainfall table is on page 47 (beyond the old 40-page limit)
  plausibility impossible values (99,999 mm ...) that must be set aside for review, never stored
  must fail   random binary named .csv; a price table (no agent for it) - they must fail cleanly within the retry budget and store nothing
No language, layout or value here is known to the pipeline code. The builder does not import the pipeline.
"""
import argparse
import csv
import hashlib
import io
import json
import random
import re
import shutil
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--set", choices=["official", "dev"], required=True)
args = ap.parse_args()
OFFICIAL = args.set == "official"
BASE = ROOT / "eval" / ("ai" if OFFICIAL else "dev/ai")
CASES, REFS = BASE / "cases", BASE / "reference"
PREFIX = "ai/cases" if OFFICIAL else "dev/ai/cases"
rng = random.Random(9090 if OFFICIAL else 9091)

CANON = {"NONEI": "NONEY"}
CROP_CANON = {"Redish": "Radish", "Tapioca": "Cassava", "Jack Fruit": "Jackfruit", "Bhindi": "Okra", "Dry chillies": "Chilli (dry)",
              "Cashewnut": "Cashew Nut", "Sweet potato": "Sweet Potato"}
SOIL_UNITS = {"n": "kg/ha", "p": "kg/ha", "k": "kg/ha", "oc": "%", "ph": "pH units", "ec": "dS/m", "s": "ppm", "zn": "ppm", "fe": "ppm",
              "cu": "ppm", "mn": "ppm", "b": "ppm"}
SRC_SOIL_KEYS = {"n": ("N",), "p": ("P",), "k": ("K",), "oc": ("OC",), "ph": ("pH",), "ec": ("EC",), "s": ("S",), "zn": ("Zn",), "fe": ("fe", "Fe"),
                 "cu": ("Cu",), "mn": ("Mn",), "b": ("B",)}
NUT = ("n", "p", "k", "ph", "oc", "ec", "s", "zn", "fe", "cu", "mn", "b")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
# language material (the pipeline knows none of it)
HI = {"IMPHAL WEST": "इम्फाल पश्चिम", "THOUBAL": "थौबल", "UKHRUL": "उखरुल", "BISHNUPUR": "बिष्णुपुर", "CHANDEL": "चंदेल", "SENAPATI": "सेनापति",
      "TAMENGLONG": "तमेंगलोंग", "IMPHAL EAST": "इम्फाल पूर्व", "CHURACHANDPUR": "चुराचांदपुर", "KAKCHING": "काकचिंग", "KANGPOKPI": "कांगपोकपी",
      "JIRIBAM": "जिरीबाम", "KAMJONG": "कामजोंग", "NONEY": "नोनी", "PHERZAWL": "फेरज़ावल", "TENGNOUPAL": "तेंगनौपाल"}
BN = {"IMPHAL WEST": "ইম্ফাল পশ্চিম", "THOUBAL": "থৌবাল", "UKHRUL": "উখরুল", "BISHNUPUR": "বিষ্ণুপুর", "CHANDEL": "চান্দেল", "SENAPATI": "সেনাপতি",
      "TAMENGLONG": "তামেংলং", "IMPHAL EAST": "ইম্ফাল পূর্ব", "CHURACHANDPUR": "চূড়াচাঁদপুর", "KAKCHING": "কাকচিং", "KANGPOKPI": "কাংপোকপি",
      "JIRIBAM": "জিরিবাম", "KAMJONG": "কামজং", "NONEY": "নোনি", "PHERZAWL": "ফেরজাউল", "TENGNOUPAL": "তেংনৌপাল"}
TA = {"IMPHAL WEST": "இம்பால் மேற்கு", "THOUBAL": "தௌபால்", "UKHRUL": "உக்ருல்", "BISHNUPUR": "விஷ்ணுபூர்", "CHANDEL": "சாந்தேல்", "SENAPATI": "சேனாபதி",
      "TAMENGLONG": "தமெங்லாங்", "IMPHAL EAST": "இம்பால் கிழக்கு", "CHURACHANDPUR": "சூரசந்த்பூர்", "KAKCHING": "காக்சிங்", "KANGPOKPI": "காங்போக்பி",
      "JIRIBAM": "ஜிரிபாம்", "KAMJONG": "காம்ஜோங்", "NONEY": "நோனி", "PHERZAWL": "பெர்சாவ்ல்", "TENGNOUPAL": "தெங்னோபால்"}
M_HI = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
M_BN = ["জানুয়ারি", "ফেব্রুয়ারি", "মার্চ", "এপ্রিল", "মে", "জুন", "জুলাই", "আগস্ট", "সেপ্টেম্বর", "অক্টোবর", "নভেম্বর", "ডিসেম্বর"]
M_TA = ["ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்", "ஜூலை", "ஆகஸ்ட்", "செப்டம்பர்", "அக்டோபர்", "நவம்பர்", "டிசம்பர்"]
BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


# ------------------------------------------------------------------------------------------------ source records
def load_soil(paths):
    out, seen = [], set()
    for p in paths:
        for d in json.loads(p.read_text(encoding="utf-8"))["districts"]:
            for cyc, rows in d["shc_cycles"].items():
                for r in rows:
                    v = r.get("VILLAGE") or r.get("village")
                    if not v or (d["district"], cyc, v) in seen:
                        continue
                    seen.add((d["district"], cyc, v))
                    vals = {k: next((r[s] for s in ss if s in r), None) for k, ss in SRC_SOIL_KEYS.items()}
                    if vals["ph"] is not None and vals["ph"] <= 0:
                        vals["ph"] = None
                    out.append({"district": d["district"], "cycle": cyc, "village": v, "vals": vals})
    return out


def load_rain(p):
    return [{"district": CANON.get(d["district"], d["district"]), **m} for d in json.loads(p.read_text(encoding="utf-8"))["districts"]
            for m in d["monthly"] if m.get("rainfall_mm") is not None]


def load_prod(paths):
    out = []
    for p in paths:
        rows = list(csv.reader(p.open(encoding="utf-8-sig")))
        years = [(rows[0][j], j) for j in range(2, len(rows[0]), 3) if rows[0][j]]
        crop = None
        for r in rows[3:]:
            if not r[1]:
                if re.match(r"^\d+\.", r[0]):
                    crop = re.sub(r"^\d+\.\s*", "", r[0])
                continue
            dist = re.sub(r"^\d+\.\s*", "", r[0]).upper()
            for yr, j in years:
                a, pr, y = r[j:j + 3]
                if a.strip() and pr.strip():
                    x, z = re.findall(r"\d{4}", yr)
                    out.append({"crop": crop, "district": dist, "period": f"{x}-{z[-2:]}", "area": a.strip(), "prod": pr.strip(), "yield": y.strip()})
    return out


if OFFICIAL:
    h = ROOT / "eval" / "heldout_source"
    SOIL, RAIN, PROD = load_soil([h / "soil_P3.json"]), load_rain(h / "rain_P3_imd.json"), load_prod([h / "prod_P3.csv"])
    LANG_D = ["IMPHAL WEST", "THOUBAL", "UKHRUL"]
else:
    pdir = ROOT / "Data" / "partitions"
    SOIL, RAIN, PROD = load_soil([pdir / "soil_P1.json", pdir / "soil_P2.json"]), load_rain(pdir / "rain_P1_imd.json"), load_prod([pdir / "prod_P1.csv", pdir / "prod_P2.csv"])
    LANG_D = ["BISHNUPUR", "CHANDEL", "SENAPATI", "TAMENGLONG"]
RAIN_BY = {}
for m in RAIN:
    RAIN_BY.setdefault((m["district"], m["month"][:4]), {})[int(m["month"][5:])] = m


# ------------------------------------------------------------------------------------------------ references
def rain_ref(m, days=False):
    f = {"rainfall_mm": {"value": m["rainfall_mm"], "unit": "mm"}}
    if days:
        f["rainy_days_ge_2_5mm"] = {"value": int(m["rainy_days_ge_2.5mm"]), "unit": "days"}
    return {"key": {"location": m["district"], "period": m["month"], "crop": "UNSPECIFIED", "entity": ""}, "fields": f}


def prod_ref(p, area=True, prod=True, yld=False):
    f = {}
    if area:
        f["area_ha"] = {"value": float(p["area"]), "unit": "ha"}
    if prod:
        f["production_t"] = {"value": float(p["prod"]), "unit": "t"}
    if yld and p["yield"]:
        f["yield_t_ha"] = {"value": float(p["yield"]), "unit": "t/ha"}
    return {"key": {"location": p["district"], "period": p["period"], "crop": CROP_CANON.get(p["crop"], p["crop"]), "entity": "UNSPECIFIED"}, "fields": f}


def soil_ref(s):
    f = {k: {"value": float(v), "unit": SOIL_UNITS[k]} for k, v in s["vals"].items() if v is not None}
    return {"key": {"location": s["district"], "period": s["cycle"], "crop": "UNSPECIFIED", "entity": s["village"]}, "fields": f}


def fmt(v):
    return "" if v is None else (repr(float(v)) if isinstance(v, float) else str(v))


def csv_text(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def pick(pool, n, key, pred=lambda x: True):
    cand = [x for x in pool if pred(x)]
    rng.shuffle(cand)
    out, seen = [], set()
    for x in cand:
        if key(x) in seen:
            continue
        seen.add(key(x))
        out.append(x)
        if len(out) == n:
            break
    assert len(out) == n, (n, len(out))
    return out


cases = []


def add(cid, agent, fname, desc, refs, group, expected_status="complete", difficulty="hard", expected_quarantine=None, note=""):
    cases.append({"id": cid, "agent": agent, "file": fname, "desc": desc, "records": refs, "source_group": group, "expected_status": expected_status,
                  "difficulty": difficulty, "expected_quarantine": expected_quarantine or [], "note": note})


if BASE.exists():
    shutil.rmtree(BASE)
CASES.mkdir(parents=True)
REFS.mkdir(parents=True)


def write(name, text):
    (CASES / name).write_text(text, encoding="utf-8", newline="\n")


# ================================================================== STRUCTURE
rs = pick(RAIN, 10, lambda m: (m["district"], m["month"]))
write("ai_01_PREAMBLE_LINES.csv", "Table 4: Monthly rainfall of Manipur\nSource: India Meteorological Department, gridded data\n\nDistrict,Month,Rainfall (mm)\n" +
      "\n".join(f"{m['district'].title()},{m['month']},{fmt(m['rainfall_mm'])}" for m in rs) + "\n")
add("ai_01_preamble_lines_csv", "rainfall", "ai_01_PREAMBLE_LINES.csv", "Title and source lines above the header, blank line, then the table", [rain_ref(m) for m in rs], "csv")

rs = pick(PROD, 8, lambda p: (p["district"], p["crop"], p["period"]), lambda p: float(p["area"]) >= 5 and float(p["prod"]) >= 5)
write("ai_02_UNITS_IN_CAPTION.csv", "Area in thousand hectares; production in thousand tonnes\nCrop,District,Year,Area,Production\n" +
      "\n".join(f"{p['crop']},{p['district'].title()},{p['period']},{fmt(float(p['area']) / 1000)},{fmt(float(p['prod']) / 1000)}" for p in rs) + "\n")
add("ai_02_units_only_in_caption_csv", "production", "ai_02_UNITS_IN_CAPTION.csv", "Columns carry no unit: 'thousand hectares / tonnes' is stated only in the first line",
    [prod_ref(p) for p in rs], "csv")

d = LANG_D[0]
years = sorted({y for (dd, y) in RAIN_BY if dd == d})
ds = [m for m in RAIN if m["district"] == d]
rs = ds[:150]
lines = ["District,Month,Rainfall (mm)"] + [f"{m['district'].title()},{m['month']},{fmt(m['rainfall_mm'])}" for m in rs]
lines.insert(81, "District,Month,Rainfall (mm)")           # a repeated header deep inside the data
lines.insert(122, "Revised figures; see note 3")             # a note deep inside the data
write("ai_03_ANOMALY_DEEP_IN_TABLE.csv", "\n".join(lines) + "\n")
add("ai_03_anomaly_deep_in_table_csv", "rainfall", "ai_03_ANOMALY_DEEP_IN_TABLE.csv", "150 data rows; a repeated header at line 82 and a note at line 123", [rain_ref(m) for m in rs], "csv")

ys = [y for (dd, y), mm in RAIN_BY.items() if dd == LANG_D[0] and len(mm) == 12]
dists = LANG_D[:3]
y = next(y for y in sorted({y for (dd, y), mm in RAIN_BY.items() if len(mm) == 12}) if all((dd, y) in RAIN_BY and len(RAIN_BY[(dd, y)]) == 12 for dd in dists))
rows = [[f"Monthly rainfall (mm), Manipur {y}"], ["Month"] + [dd.title() for dd in dists]] + \
       [[f"{y}-{mo:02d}"] + [fmt(RAIN_BY[(dd, y)][mo]["rainfall_mm"]) for dd in dists] for mo in range(1, 13)]
write("ai_04_DISTRICTS_AS_COLUMNS.csv", "\n".join(",".join(r) for r in rows) + "\n")
add("ai_04_districts_as_columns_csv", "rainfall", "ai_04_DISTRICTS_AS_COLUMNS.csv", "Months in rows, one column per district (entities as columns)",
    [rain_ref(RAIN_BY[(dd, y)][mo]) for dd in dists for mo in range(1, 13)], "csv")

mo = 7
y = next(y for y in sorted({y for (dd, y), mm in RAIN_BY.items() if mo in mm}) if all((dd, y) in RAIN_BY and mo in RAIN_BY[(dd, y)] for dd in dists))
write("ai_05_TRANSPOSED.csv", f"Rainfall statistics for {MONTHS[mo - 1]} {y}\nParameter," + ",".join(dd.title() for dd in dists) + "\nRainfall (mm)," +
      ",".join(fmt(RAIN_BY[(dd, y)][mo]["rainfall_mm"]) for dd in dists) + "\nRain days," + ",".join(str(RAIN_BY[(dd, y)][mo]["rainy_days_ge_2.5mm"]) for dd in dists) + "\n")
add("ai_05_records_across_columns_csv", "rainfall", "ai_05_TRANSPOSED.csv", "Transposed: variables in rows, districts across; the month is only in the title",
    [rain_ref(RAIN_BY[(dd, y)][mo], days=True) for dd in dists], "csv")

# ================================================================== FORMATS
import openpyxl

dd3 = LANG_D[:3]
y = next(y for y in sorted({y for (dd, y), mm in RAIN_BY.items() if len(mm) == 12}) if all((dd, y) in RAIN_BY for dd in dd3))
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Cover"
ws.append(["Agricultural statistics of Manipur"])
ws.append(["Compiled by the Directorate. Sheets: Rainfall, Notes"])
w2 = wb.create_sheet("Rainfall")
w2.append([f"Monthly rainfall (mm), {y}"])
w2.append([])
w2.append(["District", "Month", "Rainfall (mm)"])
r0, refs = 4, []
for dd in dd3:
    start = w2.max_row + 1
    for mo in range(1, 5):
        w2.append([dd.title(), f"{y}-{mo:02d}", RAIN_BY[(dd, y)][mo]["rainfall_mm"]])
        refs.append(rain_ref(RAIN_BY[(dd, y)][mo]))
    w2.merge_cells(start_row=start, end_row=w2.max_row, start_column=1, end_column=1)
    w2.cell(start, 1).value = dd.title()
w3 = wb.create_sheet("Notes")
w3.append(["Figures are provisional."])
wb.save(CASES / "ai_06_EXCEL_COVER_SHEET.xlsx")
add("ai_06_workbook_cover_title_merged_xlsx", "rainfall", "ai_06_EXCEL_COVER_SHEET.xlsx", "Excel workbook: cover sheet, notes sheet, title row, district cells merged vertically", refs, "xlsx")

rs = pick(PROD, 8, lambda p: (p["district"], p["crop"], p["period"]))
rowsx = "".join(f"<tr><td>{p['crop']}</td><td>{p['district'].title()}</td><td>{p['period']}</td><td>{p['area']}</td><td>{p['prod']}</td></tr>" for p in rs)
write("ai_07_HTML_WITH_OTHER_TABLE.html", "<html><head><title>Directorate of Agriculture</title></head><body><h1>Directorate of Agriculture, Manipur</h1><ul><li>Home</li><li>Schemes</li></ul>"
      "<table><tr><td>Officer</td><td>Phone</td></tr><tr><td>A. Singh</td><td>0385 2450123</td></tr></table>"
      f"<h2>Area and production of crops</h2><table><caption>Table 2</caption><tr><th>Crop</th><th>District</th><th>Year</th><th>Area (ha)</th><th>Production (t)</th></tr>{rowsx}</table>"
      "<p>Source: Directorate.</p></body></html>")
add("ai_07_html_page_with_other_table_html", "production", "ai_07_HTML_WITH_OTHER_TABLE.html", "Web page: navigation, an unrelated contact table, then the data table", [prod_ref(p) for p in rs], "html")

ss = pick(SOIL, 12, lambda s: (s["district"], s["cycle"], s["village"]), lambda s: sum(v is not None for v in s["vals"].values()) >= 9)
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Table, TableStyle

tmp = CASES / "_tmp.pdf"
data = [["District", "Village", "Cycle"] + ["N", "P", "K", "pH", "OC", "EC", "S", "Zn", "Fe", "Cu", "Mn", "B"]] + \
       [[s["district"].title(), s["village"], s["cycle"]] + [fmt(s["vals"][k]) for k in NUT] for s in ss]
t = Table(data, colWidths=[70, 100, 50] + [38] * 12)
t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, "black"), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
SimpleDocTemplate(str(tmp), pagesize=landscape(A4)).build([t])
import numpy as np
import pymupdf
from PIL import Image

pix = pymupdf.open(str(tmp))[0].get_pixmap(dpi=170)
arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int16)
arr = np.clip(arr + np.random.default_rng(3).normal(0, 4, arr.shape), 0, 255).astype(np.uint8)
Image.fromarray(arr).save(CASES / "ai_08_IMAGE_OCR.png")
tmp.unlink()
add("ai_08_image_of_a_table_png", "soil", "ai_08_IMAGE_OCR.png", "A photographed / scanned table saved as a PNG (no PDF, no text layer)", [soil_ref(s) for s in ss], "image")

# ================================================================== PROSE
rs = pick(PROD, 6, lambda p: (p["district"], p["crop"], p["period"]), lambda p: float(p["area"]).is_integer() and float(p["prod"]).is_integer() and float(p["area"]) >= 10)
sent = [f"In {p['period']}, {p['district'].title()} district produced {int(float(p['prod'])):,} tonnes of {p['crop'].lower()} from {int(float(p['area'])):,} hectares." for p in rs]
sent.insert(2, "The Chief Minister inaugurated a new seed centre and praised the farmers of the state.")
sent.insert(5, "Manipur has 16 districts and a population of about 2.9 million.")
write("ai_09_ENGLISH_PROSE_PRODUCTION.txt", " ".join(sent[:4]) + "\n\n" + " ".join(sent[4:]) + "\n")
add("ai_09_figures_in_sentences_txt", "production", "ai_09_ENGLISH_PROSE_PRODUCTION.txt", "Production figures inside English sentences, with two distractor sentences", [prod_ref(p) for p in rs], "text")

mm = pick(RAIN, 6, lambda m: (m["district"], m["month"]), lambda m: m["rainfall_mm"] > 0)
sent = [f"In {MONTHS[int(m['month'][5:]) - 1]} {m['month'][:4]}, {m['district'].title()} recorded {fmt(m['rainfall_mm'])} mm of rainfall." for m in mm]
write("ai_10_ENGLISH_PROSE_RAINFALL.txt", " ".join(sent[:3]) + "\n\nThe monsoon was reported to be normal this year.\n\n" + " ".join(sent[3:]) + "\n")
add("ai_10_rainfall_in_sentences_txt", "rainfall", "ai_10_ENGLISH_PROSE_RAINFALL.txt", "Monthly rainfall stated in English sentences", [rain_ref(m) for m in mm], "text")

rs = pick(PROD, 4, lambda p: (p["district"], p["crop"], p["period"]), lambda p: float(p["area"]).is_integer() and float(p["prod"]).is_integer() and float(p["area"]) >= 10
          and p["district"] in TA and p["crop"] in ("Banana", "Potato", "Wheat", "Onion", "Tomato", "Cabbage", "Pineapple", "Turmeric", "Ginger", "Mango", "Papaya", "Orange"))
CROP_TA = {"Banana": "வாழை", "Potato": "உருளைக்கிழங்கு", "Wheat": "கோதுமை", "Onion": "வெங்காயம்", "Tomato": "தக்காளி", "Cabbage": "முட்டைக்கோஸ்",
           "Pineapple": "அன்னாசி", "Turmeric": "மஞ்சள்", "Ginger": "இஞ்சி", "Mango": "மாம்பழம்", "Papaya": "பப்பாளி", "Orange": "ஆரஞ்சு"}
sent = [f"{p['period']} ஆம் ஆண்டில் {TA[p['district']]} மாவட்டத்தில் {int(float(p['area'])):,} ஹெக்டேரில் {int(float(p['prod'])):,} டன் {CROP_TA[p['crop']]} உற்பத்தி செய்யப்பட்டது." for p in rs]
write("ai_11_TAMIL_PROSE.txt", "\n".join(sent) + "\n")
add("ai_11_tamil_prose_txt", "production", "ai_11_TAMIL_PROSE.txt", "Production figures inside Tamil sentences (a language no code or prompt mentions)", [prod_ref(p) for p in rs], "text")

# ================================================================== LANGUAGES (headers, district names, month names, unit words)
def lang_csv(fname, cid, desc, names, months, header, unit_note, digits=None):
    mset = [m for m in RAIN if m["district"] in LANG_D[:3]]
    rs = pick(mset, 12, lambda m: (m["district"], m["month"]))
    def dg(s):
        return s.translate(digits) if digits else s
    rows = [header] + [[names[m["district"]], months[int(m["month"][5:]) - 1], dg(m["month"][:4]), dg(fmt(m["rainfall_mm"]))] for m in rs]
    write(fname, csv_text(rows))
    add(cid, "rainfall", fname, desc, [rain_ref(m) for m in rs], "csv")


lang_csv("ai_12_HINDI.csv", "ai_12_hindi_headers_values_units_csv", "Hindi: headers, district names, month names and the unit word (मिमी)", HI, M_HI, ["जिला", "माह", "वर्ष", "वर्षा (मिमी)"], "")
lang_csv("ai_13_BENGALI_SCRIPT.csv", "ai_13_bengali_script_native_digits_csv", "Bengali script (used for Manipuri): headers, names, months, unit word and native digits", BN, M_BN,
         ["জেলা", "মাস", "বছর", "বৃষ্টিপাত (মিমি)"], "", BN_DIGITS)
lang_csv("ai_14_TAMIL.csv", "ai_14_tamil_unseen_language_csv", "Tamil: a language unrelated to Manipur and unknown to the pipeline", TA, M_TA, ["மாவட்டம்", "மாதம்", "ஆண்டு", "மழை (மிமீ)"], "")

# ================================================================== PLAUSIBILITY
rs = pick(RAIN, 16, lambda m: (m["district"], m["month"]), lambda m: m["rainfall_mm"] > 0)
bad = {3: 99999.0, 11: 45000.0}
lines = ["District,Month,Rainfall (mm)"]
for i, m in enumerate(rs):
    lines.append(f"{m['district'].title()},{m['month']},{fmt(bad.get(i, m['rainfall_mm']))}")
write("ai_15_IMPOSSIBLE_VALUES.csv", "\n".join(lines) + "\n")
add("ai_15_impossible_values_csv", "rainfall", "ai_15_IMPOSSIBLE_VALUES.csv", "Two impossible rainfall values (99,999 and 45,000 mm in a month): must be set aside for review, not stored",
    [rain_ref(m) for i, m in enumerate(rs) if i not in bad], "csv",
    expected_quarantine=[{"location": rs[i]["district"], "period": rs[i]["month"], "field": "rainfall_mm", "value": bad[i]} for i in bad])

# ================================================================== LONG PDF (page selection)
target = 47
ym = next((dd, y) for (dd, y), mm in sorted(RAIN_BY.items()) if len(mm) >= 10)
tab = [RAIN_BY[ym][mo] for mo in range(1, 11)]
styles = getSampleStyleSheet()
story = []
for pg in range(1, 61):
    if pg == target:
        story += [Paragraph(f"Table 9.3: Monthly rainfall (mm) in {ym[0].title()} district, {ym[1]}", styles["Heading3"]),
                  Table([["Month", "Rainfall (mm)"]] + [[MONTHS[int(m['month'][5:]) - 1], fmt(m["rainfall_mm"])] for m in tab], style=[("GRID", (0, 0), (-1, -1), 0.5, "black")])]
    elif pg % 9 == 0:
        story += [Paragraph("Table: Population of states (Census 2011)", styles["Heading3"]),
                  Table([["State", "Male", "Female"]] + [[f"State {i}", str(1000000 + i * 13 + pg), str(900000 + i * 17 + pg)] for i in range(12)], style=[("GRID", (0, 0), (-1, -1), 0.5, "black")])]
    else:
        story += [Paragraph(f"Chapter section {pg}. " + "The department continued its programmes for farmers across the state and held review meetings. " * 12, styles["BodyText"])]
    story.append(PageBreak())
SimpleDocTemplate(str(CASES / "ai_16_PDF_60PAGES_TABLE_P47.pdf"), pagesize=A4, title="Annual report", author="Team 28 evaluation set").build(story)
add("ai_16_table_on_page_47_of_60_pdf", "rainfall", "ai_16_PDF_60PAGES_TABLE_P47.pdf", "60-page report; the only rainfall table (district and year only in its caption) is on page 47, beyond the old 40-page limit",
    [rain_ref(m) for m in tab], "pdf")

# ================================================================== MUST FAIL CLEANLY
(CASES / "ai_17_MUST_FAIL_BINARY_JUNK.csv").write_bytes(bytes(random.Random(5 if OFFICIAL else 6).randrange(256) for _ in range(3000)))
add("ai_17_binary_junk_named_csv", "none", "ai_17_MUST_FAIL_BINARY_JUNK.csv", "Random bytes named .csv: must fail cleanly, within the retry budget, storing nothing", [], "binary", expected_status="failed")
_mk, _cm = (("Moreh", ["Banana", "Ginger", "Garlic", "Pea"]) if OFFICIAL else ("Imphal", ["Tomato", "Potato", "Onion", "Cabbage"]))
write("ai_18_MUST_FAIL_PRICE_TABLE.csv", "Market,Commodity,Date,Min Price,Max Price,Modal Price\n" + "\n".join(f"{_mk},{c},2021-03-{9 + i:02d},{22 + 3 * i},{41 + i},{30 + 2 * i}" for i, c in enumerate(_cm)) + "\n")
add("ai_18_price_table_no_agent_csv", "none", "ai_18_MUST_FAIL_PRICE_TABLE.csv", "A mandi price table (no agent exists for it): must fail cleanly and store NOTHING (no invented rows)", [], "csv", expected_status="failed")
write("ai_19_MUST_FAIL_HEADERS_ONLY.csv", ("Table 7. Farm holdings of the state\nSource: Census\n\nBlock,Holding size\n" if OFFICIAL else "Table 1. Agricultural statistics of the state\nSource: Directorate\n\nDistrict,Year\n"))
add("ai_19_headers_only_csv", "none", "ai_19_MUST_FAIL_HEADERS_ONLY.csv", "Title lines and a header but no data rows: must fail cleanly", [], "csv", expected_status="failed")
rs = pick(PROD, 8, lambda p: (p["district"], p["crop"], p["period"]))
sheets = openpyxl.Workbook()
ws = sheets.active
ws.title = "Production"
ws.append(["Crop production of Manipur"])
ws.append(["Crop", "District", "Year", "Area (ha)", "Production (t)"])
for p in rs:
    ws.append([p["crop"], p["district"].title(), p["period"], float(p["area"]), float(p["prod"])])
sheets.save(CASES / "ai_20_EXCEL_ONE_SHEET.xlsx")
add("ai_20_workbook_one_sheet_title_xlsx", "production", "ai_20_EXCEL_ONE_SHEET.xlsx", "Excel workbook, one sheet, a title row above the header", [prod_ref(p) for p in rs], "xlsx", difficulty="normal")

# ================================================================== write references + checks
bad = 0
for c in cases:
    ref = {"case_id": c["id"], "agent": c["agent"], "source_group": c["source_group"], "file": f"{PREFIX}/{c['file']}", "description": c["desc"],
           "difficulty": c["difficulty"], "needs_llm": True, "inject_fault": None, "expected_status": c["expected_status"], "expected_trajectory": None,
           "expected_quarantine": c["expected_quarantine"], "records": c["records"], "owner_agent": "ai_layer"}
    (REFS / f"{c['id']}.json").write_text(json.dumps(ref, indent=1, ensure_ascii=False), encoding="utf-8")
    n = sum(len(r["fields"]) for r in c["records"])
    print(f"  {c['id']:44} {c['agent']:10} fields={n:4} {c['source_group']}")
print(f"[{args.set}] {len(cases)} AI cases; reference fields:", sum(sum(len(r['fields']) for r in c['records']) for c in cases))
if OFFICIAL:
    MAN = ROOT / "eval" / "heldout" / "split_manifest.json"
    man = json.loads(MAN.read_text(encoding="utf-8"))
    man["eval_cases_ai"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(CASES.glob("*")) if p.is_file()}
    MAN.write_text(json.dumps(man, indent=1), encoding="utf-8")
    print("froze", len(man["eval_cases_ai"]), "AI evaluation file hashes")
