"""
Builds the PDF evaluation cases for the document agent: 10 PDFs, each a table (or tables) rendered from REAL records.

    python eval/build_pdf_cases.py --set official   # held-out districts (eval/heldout_source)  -> eval/cases/pdf_*.pdf + eval/reference/
    python eval/build_pdf_cases.py --set dev        # dev districts (Data/partitions)           -> eval/dev/cases/pdf_*.pdf + eval/dev/reference/

The dev set is what the PDF reader was developed and tuned on; the official set is built only from the held-out districts and is run cold.
Like the other builders this script does not import the pipeline. References are the ORIGINAL values in canonical units; every reference
value of a text-layer PDF is re-checked against the text layer at the end (scanned PDFs have no text layer, so those are verified by
construction: the value printed on the page is the value written to the reference).

The 10 layouts (agent / what makes it hard):
  01 production  ruled grid, tidy columns, table continues over pages with a repeated header
  02 production  NO ruling lines; district and year appear only in the page caption (one table per page)
  03 production  bilingual English + Hindi (Devanagari) headers; acres + quintals -> expected ha + tonnes
  04 production  SCANNED (image only, OCR needed), ruled grid
  05 rainfall    wide table (Year x Jan..Dec); district only in the caption (one table per page)
  06 rainfall    tidy ruled table, 60+ rows over several pages
  07 rainfall    page physically ROTATED 90 degrees (like Table 2.28 of Agricultural Statistics at a Glance), no ruling lines
  08 soil        ruled landscape table, 12 nutrient columns
  09 soil        no ruling lines; district and SHC cycle only in the caption
  10 soil        SCANNED (image only, OCR needed), ruled
"""
import argparse
import csv
import hashlib
import io
import json
import random
import re
import shutil
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--set", choices=["official", "dev"], required=True)
args = ap.parse_args()
OFFICIAL = args.set == "official"
CASES = ROOT / "eval" / ("cases" if OFFICIAL else "dev/cases")
REFS = ROOT / "eval" / ("reference" if OFFICIAL else "dev/reference")
PREFIX = "cases" if OFFICIAL else "dev/cases"
rng = random.Random(7070 if OFFICIAL else 7071)

from reportlab import rl_config  # noqa: E402
rl_config.invariant = 1          # deterministic PDF bytes (no timestamps / random ids)
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4, landscape  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402

FONT = "Helvetica"
for fp in ("C:/Windows/Fonts/Nirmala.ttf", "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"):
    if Path(fp).exists():
        pdfmetrics.registerFont(TTFont("Deva", fp))
        FONT = "Deva"
        break

CROP_CANON = {"Redish": "Radish", "Tapioca": "Cassava", "Jack Fruit": "Jackfruit", "Bhindi": "Okra", "Dry chillies": "Chilli (dry)",
              "Cashewnut": "Cashew Nut", "Sweet potato": "Sweet Potato"}
SOIL_UNITS = {"n": "kg/ha", "p": "kg/ha", "k": "kg/ha", "oc": "%", "ph": "pH units", "ec": "dS/m", "s": "ppm", "zn": "ppm", "fe": "ppm",
              "cu": "ppm", "mn": "ppm", "b": "ppm"}
SRC_SOIL_KEYS = {"n": ("N",), "p": ("P",), "k": ("K",), "oc": ("OC",), "ph": ("pH",), "ec": ("EC",), "s": ("S",), "zn": ("Zn",),
                 "fe": ("fe", "Fe"), "cu": ("Cu",), "mn": ("Mn",), "b": ("B",)}
NUT = ("n", "p", "k", "ph", "oc", "ec", "s", "zn", "fe", "cu", "mn", "b")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ------------------------------------------------------------------------------------------------ source records
def sources():
    if OFFICIAL:
        base = ROOT / "eval" / "heldout_source"
        return ([base / "soil_P3.json"], base / "rain_P3_imd.json", base / "rain_P3_nasa.json", [base / "prod_P3.csv"])
    base = ROOT / "Data" / "partitions"
    return ([base / "soil_P1.json", base / "soil_P2.json"], base / "rain_P1_imd.json", base / "rain_P2_nasa.json",
            [base / "prod_P1.csv", base / "prod_P2.csv"])


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
    return [{"district": d["district"], **m} for d in json.loads(p.read_text(encoding="utf-8"))["districts"] for m in d["monthly"]
            if m.get("rainfall_mm") is not None]


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
                    a_, b_ = re.findall(r"\d{4}", yr)
                    out.append({"crop": crop, "district": dist, "period": f"{a_}-{b_[-2:]}", "area": a.strip(), "prod": pr.strip(),
                                "yield": y.strip()})
    return out


SOIL_P, RAIN_IMD_P, RAIN_NASA_P, PROD_P = sources()
SOIL, RAIN_IMD, RAIN_NASA, PROD = load_soil(SOIL_P), load_rain(RAIN_IMD_P), load_rain(RAIN_NASA_P), load_prod(PROD_P)


# ------------------------------------------------------------------------------------------------ references
def soil_ref(s):
    f = {k: {"value": float(v), "unit": SOIL_UNITS[k]} for k, v in s["vals"].items() if v is not None}
    return {"key": {"location": s["district"], "period": s["cycle"], "crop": "UNSPECIFIED", "entity": s["village"]}, "fields": f}


CANON = {"NONEI": "NONEY"}     # declared district normalisation (eval/README.md)


def rain_ref(m, days=False):
    f = {"rainfall_mm": {"value": m["rainfall_mm"], "unit": "mm"}}
    if days:
        f["rainy_days_ge_2_5mm"] = {"value": int(m["rainy_days_ge_2.5mm"]), "unit": "days"}
    return {"key": {"location": CANON.get(m["district"], m["district"]), "period": m["month"], "crop": "UNSPECIFIED", "entity": ""}, "fields": f}


def prod_ref(p, yld=True):
    f = {"area_ha": {"value": float(p["area"]), "unit": "ha"}, "production_t": {"value": float(p["prod"]), "unit": "t"}}
    if yld and p["yield"]:
        f["yield_t_ha"] = {"value": float(p["yield"]), "unit": "t/ha"}
    return {"key": {"location": p["district"], "period": p["period"], "crop": CROP_CANON.get(p["crop"], p["crop"]),
                    "entity": "UNSPECIFIED"}, "fields": f}


def fmt(v):
    return "" if v is None else (repr(float(v)) if isinstance(v, float) else str(v))


# ------------------------------------------------------------------------------------------------ PDF rendering
def make_pdf(path, pages, landscape_=False, ruled=True, font_size=7.5, col_widths=None, bilingual=False):
    """pages: list of (caption or None, header row, body rows). A page with more rows than fit continues on the next page with the header repeated."""
    ss = getSampleStyleSheet()
    st = ss["Heading4"].clone("cap")
    st.fontName = FONT if bilingual else "Helvetica-Bold"
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4) if landscape_ else A4, leftMargin=28, rightMargin=28, topMargin=36,
                            bottomMargin=36, title="Agriculture statistics table", author="Team 28 evaluation set")
    story = []
    for i, (cap, header, body) in enumerate(pages):
        if i:
            story.append(PageBreak())
        if cap:
            story += [Paragraph(cap, st), Spacer(1, 6)]
        t = Table([header] + body, colWidths=col_widths, repeatRows=1)
        style = [("FONTNAME", (0, 0), (-1, -1), FONT if bilingual else "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), font_size),
                 ("ALIGN", (1, 1), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if ruled:
            style += [("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8"))]
        t.setStyle(TableStyle(style))
        story.append(t)
    doc.build(story)


def rotate_physically(path):
    """turn the page CONTENT 90 degrees (no /Rotate flag), as in the rotated tables of statistical yearbooks"""
    from pypdf import PdfReader, PdfWriter, Transformation
    r, w = PdfReader(str(path)), PdfWriter()
    for pg in r.pages:
        W, H = float(pg.mediabox.width), float(pg.mediabox.height)
        pg.add_transformation(Transformation().rotate(90).translate(H, 0))
        for box in ("mediabox", "cropbox", "trimbox", "bleedbox", "artbox"):
            getattr(pg, box).lower_left = (0, 0)
            getattr(pg, box).upper_right = (H, W)
        w.add_page(pg)
    with open(path, "wb") as fh:
        w.write(fh)


def scan(path, dpi=150):
    """replace the PDF by an image-only copy (no text layer) with light sensor noise + JPEG compression"""
    import numpy as np
    import pymupdf
    from PIL import Image
    src = pymupdf.open(str(path))
    out = pymupdf.open()
    nrng = np.random.default_rng(3)
    for pg in src:
        pix = pg.get_pixmap(dpi=dpi)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int16)
        arr = np.clip(arr + nrng.normal(0, 5, arr.shape), 0, 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, "JPEG", quality=72)
        np_ = out.new_page(width=pg.rect.width, height=pg.rect.height)
        np_.insert_image(np_.rect, stream=buf.getvalue())
    src.close()
    out.save(str(path), deflate=True)


# ------------------------------------------------------------------------------------------------ choose disjoint records
used = set()


def take(pool, n, key, pred=lambda x: True):
    cand = [x for x in pool if pred(x) and key(x) not in used]
    rng.shuffle(cand)
    got, seen = [], set()
    for x in cand:
        k = key(x)
        if k in seen:
            continue
        seen.add(k)
        got.append(x)
        if len(got) == n:
            break
    assert len(got) == n, (n, len(got))
    used.update(key(x) for x in got)
    return got


pkey = lambda p: ("p", p["crop"], p["district"], p["period"])
cases = []


def add(cid, agent, fname, desc, refs, difficulty="normal", text_layer=True):
    cases.append({"id": cid, "agent": agent, "file": fname, "desc": desc, "records": refs, "difficulty": difficulty, "text_layer": text_layer})


CASES.mkdir(parents=True, exist_ok=True)
REFS.mkdir(parents=True, exist_ok=True)
for f in list(CASES.glob("pdf_*")) + list(REFS.glob("pdf_*")):
    f.unlink()

# ---- production
rs = take(PROD, 36, pkey)
fn = "pdf_01.pdf"
make_pdf(CASES / fn, [(None, ["Crop", "District", "Year", "Area (ha)", "Production (t)", "Yield (t/ha)"],
                      [[p["crop"], p["district"].title(), p["period"], p["area"], p["prod"], p["yield"]] for p in rs])],
         col_widths=[110, 90, 70, 80, 90, 80])
add("pdf_01_production_ruled_multipage", "production", fn, "Ruled grid, tidy columns, table continues on the next page with a repeated header",
    [prod_ref(p) for p in rs])

rs = []
pages = []
import collections
_cnt = collections.Counter((p["district"], p["period"]) for p in PROD if pkey(p) not in used)
for (d, y) in rng.sample(sorted(k for k, v in _cnt.items() if v >= 10), 3):
    sub = take(PROD, 10, pkey, lambda p: p["district"] == d and p["period"] == y)
    rs += sub
    pages.append((f"Table 4.2: Area, production and yield of crops in {d.title()} district, {y}",
                  ["Crop", "Area (Hectare)", "Production (Tonnes)", "Yield (T/Ha)"], [[p["crop"], p["area"], p["prod"], p["yield"]] for p in sub]))
fn = "pdf_02.pdf"
make_pdf(CASES / fn, pages, ruled=False, col_widths=[130, 100, 110, 90])
add("pdf_02_production_unruled_caption_context", "production", fn, "No ruling lines; district and year only in the caption above each table",
    [prod_ref(p) for p in rs], "hard")

rs = take(PROD, 22, pkey)
fn = "pdf_03.pdf"
hdr = ["\u092b\u0938\u0932 Crop", "\u091c\u093f\u0932\u093e District", "\u0935\u0930\u094d\u0937 Year",
       "\u0915\u094d\u0937\u0947\u0924\u094d\u0930\u092b\u0932 Area (acres)", "\u0909\u0924\u094d\u092a\u093e\u0926\u0928 Production (quintals)"]
make_pdf(CASES / fn, [(None, hdr, [[p["crop"], p["district"].title(), p["period"], fmt(round(float(p["area"]) / 0.404686, 4)),
                                    fmt(round(float(p["prod"]) * 10, 3))] for p in rs])], bilingual=True, col_widths=[100, 100, 70, 120, 140])
add("pdf_03_production_bilingual_hindi_headers", "production", fn, "English + Hindi (Devanagari) headers; acres and quintals -> expected ha and tonnes",
    [prod_ref(p, yld=False) for p in rs], "hard")

rs = take(PROD, 20, pkey)
fn = "pdf_04.pdf"
make_pdf(CASES / fn, [(None, ["Crop", "District", "Year", "Area (ha)", "Production (t)", "Yield (t/ha)"],
                      [[p["crop"], p["district"].title(), p["period"], p["area"], p["prod"], p["yield"]] for p in rs])],
         col_widths=[110, 90, 70, 80, 90, 80], font_size=9)
scan(CASES / fn)
add("pdf_04_production_scanned_ocr", "production", fn, "Scanned page (image only, no text layer): OCR needed, ruled grid", [prod_ref(p) for p in rs],
    "hard", text_layer=False)

# ---- rainfall
def blocks(pool):
    b = {}
    for m in pool:
        b.setdefault((m["district"], m["month"][:4]), []).append(m)
    return b


imd_b, nasa_b = blocks(RAIN_IMD), blocks(RAIN_NASA)
rks = lambda m: ("r", id(m))
pages, rs = [], []
for d in sorted({k[0] for k in imd_b}):
    ys = rng.sample(sorted(k[1] for k in imd_b if k[0] == d and k not in used), 3)
    body = []
    for y in sorted(ys):
        used.add((d, y))
        ms = {int(m["month"][5:]): m for m in imd_b[(d, y)]}
        body.append([y] + [fmt(ms[i]["rainfall_mm"]) if i in ms else "" for i in range(1, 13)])
        rs += list(ms.values())
    pages.append((f"Monthly rainfall (mm) for {d.title()} district", ["Year"] + MONTHS, body))
fn = "pdf_05.pdf"
make_pdf(CASES / fn, pages, landscape_=True, col_widths=[40] + [52] * 12)
add("pdf_05_rainfall_wide_months_caption_district", "rainfall", fn, "Year x Jan..Dec table; the district is only in the caption (one table per page)",
    [rain_ref(m) for m in rs], "hard")

nasa_keys = sorted(k for k in nasa_b if ("n",) + k not in used)
chosen = rng.sample(nasa_keys, 5)
rs = [m for k in chosen for m in nasa_b[k]]
used.update(("n",) + k for k in chosen)
rows = sorted(rs, key=lambda m: (m["district"], m["month"]))
fn = "pdf_06.pdf"
make_pdf(CASES / fn, [(None, ["District", "Year-Month", "Rainfall (mm)", "Rainy days (>=2.5 mm)"],
                      [[m["district"].title(), m["month"], fmt(m["rainfall_mm"]), m["rainy_days_ge_2.5mm"]] for m in rows])],
         col_widths=[110, 90, 100, 120])
add("pdf_06_rainfall_tidy_ruled_multipage", "rainfall", fn, "Tidy ruled table, 60 rows over several pages", [rain_ref(m, days=True) for m in rs])

imd_keys = sorted(k for k in imd_b if k not in used)
chosen = rng.sample(imd_keys, 8)
used.update(chosen)
rs = [m for k in chosen for m in imd_b[k]]
body = []
for (d, y) in sorted(chosen):
    ms = {int(m["month"][5:]): m for m in imd_b[(d, y)]}
    body.append([d.title(), y] + [fmt(ms[i]["rainfall_mm"]) if i in ms else "" for i in range(1, 13)])
fn = "pdf_07.pdf"
make_pdf(CASES / fn, [("Table 3.4: Monthly rainfall (mm)", ["District", "Year"] + MONTHS, body)], landscape_=True, ruled=False,
         col_widths=[80, 38] + [46] * 12)
rotate_physically(CASES / fn)
add("pdf_07_rainfall_rotated_page_unruled", "rainfall", fn, "Page content physically rotated 90 degrees, no ruling lines", [rain_ref(m) for m in rs], "hard")

# ---- soil
rs = take(SOIL, 20, lambda s: ("s", s["district"], s["cycle"], s["village"]))
fn = "pdf_08.pdf"
make_pdf(CASES / fn, [(None, ["District", "Village", "Cycle"] + ["N", "P", "K", "pH", "OC", "EC", "S", "Zn", "Fe", "Cu", "Mn", "B"],
                      [[s["district"].title(), s["village"], s["cycle"]] + [fmt(s["vals"][k]) for k in NUT] for s in rs])],
         landscape_=True, font_size=7, col_widths=[70, 110] + [48] + [36] * 12)
add("pdf_08_soil_ruled_landscape", "soil", fn, "Ruled landscape table, 12 nutrient columns", [soil_ref(s) for s in rs])

groups = {}
for s in SOIL:
    groups.setdefault((s["district"], s["cycle"]), []).append(s)
big = sorted((k for k, v in groups.items() if len([x for x in v if ("s", x["district"], x["cycle"], x["village"]) not in used]) >= 6), key=str)
pages, rs = [], []
for (d, c) in rng.sample(big, min(3, len(big))):
    sub = take(SOIL, 6, lambda s: ("s", s["district"], s["cycle"], s["village"]), lambda s, d=d, c=c: s["district"] == d and s["cycle"] == c)
    rs += sub
    pages.append((f"Soil Health Card survey, cycle {c}, {d.title()} district",
                  ["Village", "N (kg/ha)", "P (kg/ha)", "K (kg/ha)", "pH", "OC (%)", "EC (dS/m)", "S (ppm)", "Zn (ppm)", "Fe (ppm)", "Cu (ppm)",
                   "Mn (ppm)", "B (ppm)"], [[s["village"]] + [fmt(s["vals"][k]) for k in NUT] for s in sub]))
fn = "pdf_09.pdf"
make_pdf(CASES / fn, pages, landscape_=True, ruled=False, font_size=7, col_widths=[110] + [52] * 12)
add("pdf_09_soil_unruled_caption_cycle_district", "soil", fn, "No ruling lines; district and SHC cycle only in the caption", [soil_ref(s) for s in rs], "hard")

rs = take(SOIL, 14, lambda s: ("s", s["district"], s["cycle"], s["village"]))
fn = "pdf_10.pdf"
make_pdf(CASES / fn, [(None, ["District", "Village", "Cycle"] + ["N", "P", "K", "pH", "OC", "EC", "S", "Zn", "Fe", "Cu", "Mn", "B"],
                      [[s["district"].title(), s["village"], s["cycle"]] + [fmt(s["vals"][k]) for k in NUT] for s in rs])],
         landscape_=True, font_size=9, col_widths=[80, 120, 55] + [42] * 12)
scan(CASES / fn)
add("pdf_10_soil_scanned_ocr", "soil", fn, "Scanned page (image only): OCR needed, ruled landscape table", [soil_ref(s) for s in rs], "hard", text_layer=False)

# ------------------------------------------------------------------------------------------------ write references + self-check
FACTORS = [1, 0.404686, 0.1, 10.0, 1000, 0.001]
NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def page_numbers(path):
    import pymupdf
    txt = " ".join(p.get_text() for p in pymupdf.open(str(path)))
    return [float(x.replace(",", "")) for x in NUM.findall(txt) if re.search(r"\d", x) and x.replace(",", "").replace("-", "").replace(".", "").isdigit()]


bad = 0
for c in cases:
    ref = {"case_id": c["id"], "agent": c["agent"], "owner_agent": "document_agent", "source_group": "pdf", "file": f"{PREFIX}/{c['file']}",
           "description": c["desc"], "difficulty": c["difficulty"], "needs_llm": False, "inject_fault": None, "expected_status": "complete",
           "expected_trajectory": ["inspect_file", "read_pdf_data", {"soil": "soil_extract", "rainfall": "rainfall_extract",
                                                                    "production": "production_extract"}[c["agent"]], "sqlite_load"],
           "has_text_layer": c["text_layer"], "records": c["records"]}
    if c["text_layer"]:
        nums = page_numbers(CASES / c["file"])
        for rec in c["records"]:
            for f, spec in rec["fields"].items():
                v = spec["value"]
                if not any(abs(n * k - v) <= max(0.011 * abs(v), 0.006) for n in nums for k in FACTORS):
                    bad += 1
                    print("REFERENCE NOT IN PDF TEXT:", c["id"], rec["key"], f, v)
    (REFS / f"{c['id']}.json").write_text(json.dumps(ref, indent=1, ensure_ascii=False), encoding="utf-8")

per = {}
for c in cases:
    per.setdefault(c["agent"], [0, 0])
    per[c["agent"]][0] += 1
    per[c["agent"]][1] += sum(len(r["fields"]) for r in c["records"])
print(f"[{args.set}] PDF cases per agent / reference fields:", per, " total:", len(cases), " reference mismatches:", bad)
assert bad == 0

if OFFICIAL:   # freeze the evaluation files' hashes next to the dev-partition hashes
    MAN = ROOT / "eval" / "heldout" / "split_manifest.json"
    man = json.loads(MAN.read_text(encoding="utf-8"))
    man["eval_cases"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "eval" / "cases").glob("*")) if p.is_file()}
    MAN.write_text(json.dumps(man, indent=1), encoding="utf-8")
    print("froze", len(man["eval_cases"]), "evaluation file hashes")
