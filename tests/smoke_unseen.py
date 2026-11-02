"""
Dev smoke test: files the pipeline was NOT written around (tests/unseen/*) must load with the right values.
These are developer fixtures, NOT the Phase 2 evaluation set (that lives in eval/ and must not overlap training data).
Run:  python tests/smoke_unseen.py
Uses a temporary DB and log dir, so it never touches db/manipur.db or logs/.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp())
os.environ["AGRI_LOG_DIR"] = str(tmp / "logs")
sys.path.insert(0, str(ROOT))
from pipeline.graph import run_file  # noqa: E402

DB = tmp / "t.db"
U = ROOT / "tests" / "unseen"
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def q(sql, *a):
    con = sqlite3.connect(DB)
    try:
        return con.execute(sql, a).fetchall()
    finally:
        con.close()


def run(f, **kw):
    return run_file(f, db_path=DB, **kw)


# --- unseen shapes ----------------------------------------------------------------------------
r = run(U / "rain_tidy.csv")
check("tidy rainfall csv: 5 rows, complete", r["status"] == "complete" and r["result"]["totals"]["extracted"] == 5)
check("  district alias NONEI -> NONEY/714", q("select district,district_code from rainfall_monthly where year_month='2020-03'") == [("NONEY", "714")])
check("  'Jan-2020' parsed", q("select rainfall_mm,rainy_days_ge_2_5mm from rainfall_monthly where year_month='2020-01'") == [(12.5, 2)])
check("  unknown district flagged invalid", q("select validation_status from rainfall_monthly where district='ATLANTIS'") == [("invalid",)])

r = run(U / "rain_wide.csv")
check("wide rainfall csv (month columns) -> 24 rows", r["result"]["totals"]["extracted"] == 24)
check("  Ukhrul 2021-07 = 350", q("select rainfall_mm from rainfall_monthly where district='UKHRUL' and year_month='2021-07'") == [(350.0,)])

r = run(U / "soil_tidy.csv")
check("semicolon csv with decimal comma", q("select n,ph,s from soil_health where village='Ningthoukhong'") == [(280.5, 5.8, 12.5)])

r = run(U / "prod_nested.json")
check("nested production json: Paddy->Rice, acres->ha, quintal->t",
      q("select crop,round(area_ha,3),production_t from crop_production where district='THOUBAL'") == [("Rice", 404.686, 2500.0)])

r = run(U / "rain_lines.txt")
check("JSON Lines saved as .txt", r["status"] == "complete" and r["result"]["totals"]["extracted"] == 2)

r = run(U / "soil_dictkeyed.json")
check("json keyed by district name", q("select district,n from soil_health where district='UKHRUL'") == [("UKHRUL", 280.0)])

# --- things that must fail cleanly -------------------------------------------------------------------
check("empty file fails (no crash)", run(U / "empty.csv")["status"] == "failed")
check("non-data json fails (no crash)", run(U / "notes.json")["status"] == "failed")
check("missing file fails (no crash)", run(U / "nope.csv")["status"] == "failed")

# --- fault injection ---------------------------------------------------------------------------------
r = run(U / "rain_wide.csv", inject_fault="read")
check("injected read fault recovered (retries=1)", r["status"] == "complete" and r["retries"] == 1)
r = run(U / "soil_tidy.csv", inject_fault="load")
check("injected load fault recovered", r["status"] == "complete" and r["retries"] == 1)
r = run(ROOT / "manipur_soil_health.json", inject_fault="soil_agent")
check("injected agent fault is isolated + reported", r["status"] == "failed" and "soil_agent" in str(r["result"]["notes"]["agent_errors"]))

# --- idempotence + real files (regression) ----------------------------------------------------------
r1 = run(ROOT / "Data" / "CSV_Agent.csv"); r2 = run(ROOT / "Data" / "CSV_Agent.csv")
check("re-ingesting a file adds 0 rows", r1["result"]["totals"]["inserted"] == 1217 and r2["result"]["totals"]["inserted"] == 0)
r = run(ROOT / "manipur_soil_health.json")
check("real soil json: 438 rows", r["result"]["totals"]["extracted"] == 438)
check("  pH 0.0 treated as missing (44 NULL, none invalid, source value kept in extra)",
      q("select count(*) from soil_health where ph is null")[0][0] == 44
      and q("select count(*) from soil_health where validation_status='invalid'")[0][0] == 0
      and q("select count(*) from soil_health where extra like '%ph_source_value%'")[0][0] == 44)
check("real rainfall json: 2112 rows", run(ROOT / "manipur_rainfall_monthly.json")["result"]["totals"]["extracted"] == 2112)

# --- PDF input (document agent): dev PDFs, never the held-out evaluation PDFs -------------------------------
PDFDIR = ROOT / "eval" / "dev" / "cases"
r = run(PDFDIR / "pdf_08.pdf")
check("PDF (ruled soil table): complete, 20 rows via read_pdf_data", r["status"] == "complete" and r["result"]["totals"]["extracted"] == 20
      and "read_pdf_data" in [t["tool"] for t in r["trajectory"]], str(r["result"].get("totals")))
check("  provenance locator names page, table and line", q("select locator from soil_health where source_file='pdf_08.pdf' limit 1")[0][0].startswith("p1 t1 line"))
r = run(PDFDIR / "pdf_02.pdf")
check("PDF without ruling lines: district + year taken from the caption", r["status"] == "complete" and
      q("select count(*) from crop_production where source_file='pdf_02.pdf' and district<>'UNSPECIFIED' and period is not null")[0][0] == 30)
r = run(PDFDIR / "pdf_08.pdf", inject_fault="read")
check("PDF injected read fault recovered (retry, retries=1)", r["status"] == "complete" and r["retries"] == 1)
bad = tmp / "broken.pdf"
bad.write_bytes(b"%PDF-1.4 this is not a real pdf")
r = run(bad)
check("corrupt PDF fails cleanly after trying the OCR route (no crash)", r["status"] == "failed" and r["retries"] == 2)

# --- live LLM fallback (only if a Groq key is configured) ------------------------------------------
from pipeline.llm import get_llm  # noqa: E402
if get_llm() is not None:
    run(U / "soil_french.csv")
    check("LLM fallback maps unknown headers (Azote->n, Acidite_pH->ph, Departement->district)",
          q("select district,n,ph from soil_health where village='Litan'") == [("UKHRUL", 290.0, 5.9)])
    # LLM must NOT turn a metadata-only table into data rows (regression: bbox-only district records)
    check("LLM cannot invent a domain for metadata-only records",
          q("select count(*) from soil_health where source_file='manipur_soil_health.json'")[0][0] == 438)
else:
    print("SKIP LLM fallback tests (no GROQ_API_KEY)")

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
