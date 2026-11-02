"""
Builds data/manifest.csv - every corpus file with source, URL, collection date, language, license (Phase2_Eval.pdf, item 11).
URL / language / license come from the Phase 1 inventory (TEAM_28_Sources.xlsx); sizes, hashes and record counts are measured.
Anything we do not actually know is written as 'unknown - ...' rather than guessed.

Run:  python data/build_manifest.py
"""
import csv
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "manifest.csv"

wb = openpyxl.load_workbook(ROOT / "TEAM_28_Sources.xlsx", data_only=True)
ws = wb["Source Inventory"]
head = [c.value for c in ws[1]]
INV = {r[0]: dict(zip(head, r)) for r in ws.iter_rows(min_row=2, values_only=True) if r[0]}


def inv(sid, col):
    return str(INV[sid][col]).replace("\n", " ").strip()


def mtime_date(p):
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


db = ROOT / "db" / "manipur.db"
counts = {}
if db.exists():
    con = sqlite3.connect(db)
    for t in ("soil_health", "rainfall_monthly", "crop_production"):
        for f, n in con.execute(f"SELECT source_file, COUNT(*) FROM {t} GROUP BY source_file"):
            counts[f] = n
    con.close()

CORPUS = [
    ("manipur_soil_health.json", "S24", "https://soilhealth.dac.gov.in/slusi-visualisation/",
     mtime_date(ROOT / "manipur_soil_health.json"),
     "scraped by scrape_manipur_soil.py (portal GraphQL + GeoServer WMS GetFeatureInfo on each SHC point)",
     "village-level SHC points for 13 of 16 districts; Jiribam, Senapati, Tamenglong have none on the portal"),
    ("manipur_rainfall_monthly.json", "S22", "https://imdpune.gov.in/ (gridded daily rainfall); district polygons: "
     "https://mausam.imd.gov.in/imd_latest/contents/district_shapefiles/Manipur.json", mtime_date(ROOT / "manipur_rainfall_monthly.json"),
     "derived by scrape_manipur_rainfall_monthly.py: IMD 0.25-degree grid (imdlib) area-weighted per district, summed per month",
     "estimates from a gridded analysis, not gauge readings; IMD Pune is not itself a row in the inventory (closest: S22) and "
     "its licence is not stated in the inventory - confirm"),
    ("manipur_rainfall_nasa_power.json", "S21", "https://power.larc.nasa.gov/api/temporal/daily/point",
     mtime_date(ROOT / "manipur_rainfall_nasa_power.json"),
     "scraped by scrape_manipur_nasa_power.py: NASA POWER daily PRECTOTCORR at each district's bbox centre, summed per month",
     "modelled MERRA-2 point estimate, not a district mean or gauge reading; licence per inventory (POWER data is openly distributed - confirm terms)"),
    ("Data/CSV_Agent.csv", "S02", None, "unknown - file supplied by the team (added 2026-10-24)",
     "supplied by the team; layout is an APY-style export (State/Crop/District/Season, Area/Production/Yield per year)",
     "origin assumed to be S02 - CONFIRM; 29 horticulture/plantation crops, 9 pre-2016 districts, 1997-2023 with gaps"),
]
rows = []
for path, sid, url, date, method, note in CORPUS:
    p = ROOT / path
    rows.append({"file_path": path, "role": "corpus", "source_id": sid, "source_name": inv(sid, "Source / dataset"),
                 "url": url or inv(sid, "Access URL").split(" ")[0], "collection_date": date,
                 "language": inv(sid, "Language"), "license": inv(sid, "License / terms"),
                 "format": p.suffix.lstrip("."), "bytes": p.stat().st_size, "sha256": sha(p),
                 "n_records_loaded": counts.get(p.name, ""), "collection_method": method, "notes": note})

for pf in sorted((ROOT / "Data" / "partitions").glob("*")) + sorted((ROOT / "eval" / "heldout_source").glob("*")):
    held = "heldout_source" in str(pf)
    sid = {"soil": "S24", "rain_P1": "S22", "rain_P2": "S21", "rain_P3_imd": "S22", "rain_P3_nasa": "S21", "prod": "S02"}
    key = next(v for k, v in sid.items() if pf.name.startswith(k))
    rows.append({"file_path": pf.relative_to(ROOT).as_posix(), "role": "heldout_source (never ingested)" if held else "corpus_partition (ingested)",
                 "source_id": key, "source_name": inv(key, "Source / dataset"), "url": inv(key, "Access URL").split(" ")[0],
                 "collection_date": mtime_date(pf), "language": inv(key, "Language"), "license": inv(key, "License / terms"),
                 "format": pf.suffix.lstrip("."), "bytes": pf.stat().st_size, "sha256": sha(pf),
                 "n_records_loaded": "" if held else counts.get(pf.name, ""),
                 "collection_method": "Data/make_partitions.py: split by district", "notes": "held-out districts: IMPHAL WEST, THOUBAL, UKHRUL" if held else "dev districts only"})

# real PDFs used for the document-agent tool tests (docs/challenge.md); inventory ids come from the file-name prefix
PDF_URL = {"S04": "https://desagri.gov.in/wp-content/uploads/2024/11/", "S05": "https://desagri.gov.in/wp-content/uploads/2026/", "S06": "https://www.nhb.gov.in/statistics/Publication/",
           "S12": "https://databank.nedfi.com/sites/default/files/2025-09/ (NABARD State Focus Paper, hosted by NEDFI)", "S31": "https://icar-crida.res.in/CP-2012/statewiseplans/Manipur/",
           "S53": "https://eudoxuspress.com/index.php/pub/article/download/1883/1209/3532", "S54": "https://www.mofpi.gov.in/sites/default/files/KnowledgeCentre/State%20Profile/Manipur.pdf"}
for pf in sorted((ROOT / "Data" / "pdf_samples").glob("*.pdf")):
    key = pf.name[:3]
    rows.append({"file_path": pf.relative_to(ROOT).as_posix(), "role": "pdf_tool_test_sample (real source, never ingested into the database)", "source_id": key,
                 "source_name": inv(key, "Source / dataset"), "url": PDF_URL.get(key, inv(key, "Access URL").split(" ")[0]), "collection_date": mtime_date(pf),
                 "language": "English + Hindi" if key in ("S04", "S12", "S05") else inv(key, "Language"), "license": inv(key, "License / terms"), "format": "pdf",
                 "bytes": pf.stat().st_size, "sha256": sha(pf), "n_records_loaded": 0, "collection_method": "downloaded 2026-11-01 for docs/pdf_tool_tests",
                 "notes": "Imphal West is a held-out district; this real PDF was only used to compare extraction tools, not to build evaluation cases" if "imphal_west" in pf.name else "tool-test sample"})

# evaluation cases: synthetic re-shapings of the corpus above (not independent data)
AGENT_SRC = {"soil": "S24", "rainfall": "S22", "production": "S02"}
for ref in sorted((ROOT / "eval" / "reference").glob("*.json")) + sorted((ROOT / "eval" / "dev" / "reference").glob("*.json")):
    r = json.loads(ref.read_text(encoding="utf-8"))
    p = ROOT / "eval" / r["file"]
    official = not r["file"].startswith("dev/")
    sid = AGENT_SRC.get(r["agent"], "")
    rows.append({"file_path": f"eval/{r['file']}", "role": "eval_case_heldout" if official else "dev_case", "source_id": sid,
                 "source_name": inv(sid, "Source / dataset") if sid else "synthetic (no source)",
                 "url": "", "collection_date": mtime_date(p), "language": "English (French headers in soil_10)" if "french" in r["case_id"] else "English + Hindi headers" if "hindi" in r["case_id"] else "English",
                 "license": "derived from the corpus file(s) above" if sid else "n/a",
                 "format": p.suffix.lstrip("."), "bytes": p.stat().st_size, "sha256": sha(p), "n_records_loaded": "",
                 "collection_method": ("eval/build_heldout.py re-shapes HELD-OUT records (Imphal West, Thoubal, Ukhrul)" if official else "eval/build_cases.py re-shapes dev-district records") + "; never part of db/manipur.db",
                 "notes": f"{r['difficulty']}: {r['description']}"})

with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
print(f"{len(rows)} files listed ({sum(r['role'] == 'corpus' for r in rows)} corpus, {sum(r['role'] == 'eval_case' for r in rows)} eval cases)")
