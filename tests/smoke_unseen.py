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

# --- crops only, and the state from the document (no model needed) -----------------------------
from pipeline.concepts import concept_for  # noqa: E402
from pipeline.llm import parse_headers, verify_header_proposal  # noqa: E402
check("header 'Milk production' / 'Fish (lakh MT)' are not crop production",
      concept_for("Milk production (thousand MT)") is None and concept_for("Fish production") is None and concept_for("Rice production") == "production")
check("model proposals are checked against the data (urea is not soil nitrogen; a crop column holds no numbers; 'year' means period)",
      verify_header_proposal({"Urea": "n"}, [{"data": {"Urea": "2860.1"}}]) == {}
      and verify_header_proposal({"Item": "crop"}, [{"data": {"Item": "123"}}]) == {}
      and parse_headers({"ஆண்டு": "year"}, ["ஆண்டு"]) == {"ஆண்டு": "period"})
r = run(U / "manipur_state_profile.csv")
check("livestock rows (milk, fish, egg) never enter crop production; crops do",
      sorted(x[0] for x in q("select crop from crop_production where source_file='manipur_state_profile.csv'")) == ["Banana", "Potato", "Rice"])
check("  state MANIPUR is taken from the file name when the table has no district / state column",
      q("select distinct state,state_code from crop_production where source_file='manipur_state_profile.csv'") == [("MANIPUR", "14")])

r = run(U / "rain_lines.txt")
check("JSON Lines saved as .txt", r["status"] == "complete" and r["result"]["totals"]["extracted"] == 2)

r = run(U / "soil_dictkeyed.json")
check("json keyed by district name", q("select district,n from soil_health where district='UKHRUL'") == [("UKHRUL", 280.0)])

# --- the model proposes, the data decides (deterministic check of a header proposal; no model call)
from pipeline.llm import verify_header_proposal  # noqa: E402
_rows = [{"data": {"MONTH": m, "YEAR": y}} for m, y in (("x", "2019"), ("y", "\u09e8\u09e6\u09e7\u09ef"), ("z", "2020"))]
_v = verify_header_proposal({"MONTH": "year_month", "YEAR": "year_month"}, _rows)
check("header proposal: a column of plain years (any digit script) is the year, not the month", _v == {"MONTH": "year_month", "YEAR": "period"}, str(_v))

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
check("re-ingesting a file adds 0 rows (1,217 rows = stored + set aside for review)",
      r1["result"]["totals"]["inserted"] + r1["result"]["totals"].get("quarantined", 0) == 1217 and r2["result"]["totals"]["inserted"] == 0)
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
    check("LLM cannot invent a domain for metadata-only records (438 data rows = stored + set aside for review)",
          q("select count(*) from soil_health where source_file='manipur_soil_health.json'")[0][0]
          + q("select count(*) from review_queue where source_file='manipur_soil_health.json'")[0][0] == 438)
    # --- AI layer: files the rule-based reader cannot take (needs the model;)
    U2 = ROOT / "tests" / "unseen_ai"

    def stored(table, f):
        return q(f"select count(*) from {table} where source_file=?", f)[0][0]

    r = run(U2 / "preamble.csv")
    check("AI: title + source lines above the header are repaired (3 rows)", r["status"] == "complete" and stored("rainfall_monthly", "preamble.csv") == 3)
    r = run(U2 / "units_caption.csv")
    check("AI: unit stated only in the title ('thousand hectares') -> 3500 ha",
          q("select area_ha from crop_production where source_file='units_caption.csv' and district='THOUBAL'") == [(3500.0,)])
    r = run(U2 / "late_anomaly.csv")
    check("AI: repeated header (line 302) and note (line 453) inside 600 rows are dropped, all 600 rows kept",
          r["status"] == "complete" and stored("rainfall_monthly", "late_anomaly.csv") == 600 and r["result"]["totals"]["rejected"] == 0)
    r = run(U2 / "melt.csv")
    check("AI: districts as columns -> 9 rows, provenance names the cell", stored("rainfall_monthly", "melt.csv") == 9
          and q("select locator from rainfall_monthly where source_file='melt.csv' limit 1")[0][0].startswith("line 3 col"))
    r = run(U2 / "transposed.csv")
    check("AI: records across columns; the month comes from the title (2019-01)",
          q("select year_month from rainfall_monthly where source_file='transposed.csv' and district='UKHRUL'") == [("2019-01",)])
    r = run(U2 / "multi.xlsx")
    check("AI: Excel workbook with cover / notes sheets and a title row -> 8 rows", stored("rainfall_monthly", "multi.xlsx") == 8)
    r = run(U2 / "page.html")
    check("AI: HTML page with an unrelated table -> 2 production rows", stored("crop_production", "page.html") == 2)
    r = run(U2 / "prose.txt")
    check("AI: figures inside sentences -> 3 verified facts, locator = character offsets",
          stored("crop_production", "prose.txt") + stored("rainfall_monthly", "prose.txt") == 3
          and q("select locator from crop_production where source_file='prose.txt' limit 1")[0][0].startswith("chars "))
    r = run(U2 / "hindi.csv")
    check("AI: Hindi headers, district names, month names and unit word -> valid rows",
          q("select district, year_month, validation_status from rainfall_monthly where source_file='hindi.csv' and district='UKHRUL' order by year_month")
          == [("UKHRUL", "2020-01", "valid"), ("UKHRUL", "2020-02", "valid")])
    r = run(U2 / "implausible.csv")
    check("AI: an impossible value (99,999 mm) is set aside for review, not stored",
          q("select count(*) from review_queue where source_file='implausible.csv'")[0][0] == 1 and
          q("select count(*) from rainfall_monthly where source_file='implausible.csv' and rainfall_mm>5000")[0][0] == 0)
    junk = tmp / "junk.csv"
    junk.write_bytes(bytes(range(256)) * 8)
    r = run(junk)
    check("AI: unreadable bytes fail cleanly within the retry budget (<= 2 recoveries)", r["status"] == "failed" and r["retries"] <= 2)
else:
    print("SKIP LLM fallback tests (no GROQ_API_KEY)")

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
