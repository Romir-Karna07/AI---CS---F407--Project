"""
Builds docs/source_agent_map.csv: every Phase 1 inventory source (TEAM_28_Sources.xlsx, sheet 'Source Inventory')
-> the agent responsible for it -> status (done / partial / pending) -> evidence or the barrier.

Rules used to group sources into agents (see docs/agents/README.md):
  * structured agents  - the source gives CSV/XLSX/JSON/API tables that the generic reader can unpivot
                         (soil_agent, rainfall_agent, production_agent: implemented)
  * document_agent     - PDF-first sources (text or scanned), often bilingual            (pending)
  * price_agent        - mandi / retail / MSP price feeds (HTML or API)                   (pending)
  * advisory_agent     - free-text advisories, KCC, news; multilingual incl. Manipuri     (pending)
  * web_registry_agent - HTML dashboards and registries (warehouses, schemes, trade)      (pending)
  * reference_geo_agent- LGD / census / boundary / OSM reference data                     (pending)

Status is claimed ONLY where rows from that source are in db/manipur.db today ("done" = the source's Manipur data
for the declared scope is loaded; "partial" = some of it is). Everything else is declared "pending" - pending work only
loses coverage points (Phase2_Eval.pdf), a false "done" would lose demo points too.

Run:  python docs/build_source_agent_map.py
"""
import csv
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "TEAM_28_Sources.xlsx"
OUT = ROOT / "docs" / "source_agent_map.csv"

# id -> (agent, status, evidence_or_barrier)
PENDING_STRUCT = "no sample file collected yet; generic CSV/JSON reader can ingest it once a file is downloaded, but the target table does not exist"
A = {
    # ---- implemented, with data in the database ---------------------------------------------------------
    "S24": ("soil_agent", "partial",
            "367 village-level SHC records (dev partitions Data/partitions/soil_P1+P2.json) loaded; the scraped file has 438 - 70 records of Imphal West, "
            "Thoubal and Ukhrul are held out for evaluation. Not done: the portal's aggregate state/district cards and the other districts' older cycles"),
    "S22": ("rainfall_agent", "partial",
            "monthly district rainfall 2015-2025 for 13 districts (1,716 rows, Data/partitions/rain_P1_imd.json; Imphal West, Thoubal, Ukhrul held out) - derived from IMD Pune's gridded "
            "daily product, not from the district bulletins/PDFs this row names. Not done: bulletin PDFs, Hindi text, forecasts"),
    "S02": ("production_agent", "partial",
            "Data/partitions/prod_P1+P2.csv (29 horticulture crops x 6 of the 9 districts, 811 rows; Imphal West, Thoubal, Ukhrul held out) from Data/CSV_Agent.csv in the APY export layout; "
            "its origin is assumed to be this portal - CONFIRM. Not done: field crops, the 7 post-2016 districts, other seasons"),
    # ---- structured sources, same format family as the implemented agents --------------------------------
    "S01": ("production_agent", "pending", "in scope; waiting for a manual CSV download from data.gov.in (JS site, free API key needed); Manipur coverage is patchy"),
    "S03": ("production_agent", "pending", "in scope; waiting for a manual export from the UPAg state-wise APY dashboard (no scripted download)"),
    "S04": ("production_agent", "pending", "Agricultural Statistics at a Glance: CSV/XLSX tables are readable by the generic reader; PDF part needs document_agent; not collected yet"),
    "S06": ("production_agent", "pending", "NHB/MIDH horticulture statistics: not collected yet (CSV/XLSX part); PDF part needs document_agent"),
    "S07": ("production_agent", "pending", "Agriculture Census holdings tables: CSV/XLSX; needs fact_land table (KB extension); not collected"),
    "S13": ("production_agent", "pending", "Livestock census / BAHS: PDF + CSV; allied production (KB extension table); not collected"),
    "S21": ("rainfall_agent", "partial",
            "monthly district rainfall 2015-2025 for 13 districts (1,716 rows, Data/partitions/rain_P2_nasa.json; 3 districts held out) via "
            "scrapers/scrape_manipur_nasa_power.py (daily API -> monthly sums). Point estimate at each district's bbox centre, "
            "modelled MERRA-2 data, not a district mean or gauge. Not done: other POWER parameters (temperature, humidity ...)"),
    "S23": ("rainfall_agent", "pending", "India-WRIS: server certificate is revoked (checked 2026-10-24), so it was not connected; revisit"),
    # ---- document_agent: PDF-first -----------------------------------------------------------------------
    **{k: ("document_agent", "pending", "PDF source; no PDF/OCR extractor built yet (candidates: Docling, pdfplumber, OCR for scans)")
       for k in ("S05", "S08", "S09", "S10", "S11", "S12", "S14", "S19", "S20", "S25", "S27", "S30", "S31", "S45", "S50", "S53", "S54")},
    # ---- price_agent ---------------------------------------------------------------------------------------
    **{k: ("price_agent", "pending", "price feed (HTML / OGD API); needs fact_price table and a fetcher; not started")
       for k in ("S15", "S16", "S17", "S18")},
    # ---- advisory_agent: free text / multilingual -------------------------------------------------------------
    **{k: ("advisory_agent", "pending", "free-text advisories / queries; needs LLM extraction + Manipuri handling; not started")
       for k in ("S28", "S29", "S32", "S51", "S52")},
    # ---- web_registry_agent: HTML dashboards ---------------------------------------------------------------------
    **{k: ("web_registry_agent", "pending", "HTML dashboard/registry; needs a per-site scraper (Playwright) and fact tables; not started")
       for k in ("S33", "S34", "S35", "S36", "S37", "S38", "S40", "S41", "S42", "S43", "S44", "S46")},
    # ---- reference_geo_agent --------------------------------------------------------------------------------------
    "S47": ("reference_geo_agent", "pending", "LGD directory: needed to replace the SLUSI portal district codes currently in config/manipur_districts.json; download not done"),
    "S48": ("reference_geo_agent", "pending", "Census 2011 PCA CSV/XLSX for dim_district; not collected"),
    "S49": ("reference_geo_agent", "pending", "boundary files (DataMeet): IMD's own Manipur.json district polygons were used for rainfall, but this source was not collected"),
    "S39": ("reference_geo_agent", "pending", "OSM extract (Geofabrik): vector GIS; not collected"),
    "S26": ("reference_geo_agent", "pending", "MARSAC land use: URL unconfirmed (lead only); not collected"),
}

# All 54 inventory sources are mapped (the PDF requires one row per inventory source). `phase2_scope` marks the 6 sources that the three
# implemented table agents can read and that the team is working on now (S24, S22, S02, S21 loaded; S01, S03 waiting for a manual download);
# the rest are "on_hold": mapped to the agent that will own them, declared pending, not started.
IN_SCOPE = ["S24", "S22", "S02", "S21", "S01", "S03"]

# Real PDFs downloaded 2026-11-01 (Data/pdf_samples) and run through read_pdf_data / run.py: what actually happened (docs/challenge.md)
TESTED = {
    "S04": "PDF part tested: Table 2.28 (yield by state, ROTATED page 161) read exactly (40/40 numbers) but it is state-level with no district, so no row is stored",
    "S05": "downloaded desagri time-series final estimates 2024-25 (3 pages): scrambled text layer, all-India foodgrain table without districts; run failed cleanly (no recognised records)",
    "S06": "PDF part tested: Table 3.1 population (p53, no ruling lines) read exactly (20/20) but it is not a soil / rainfall / production table, so nothing is stored",
    "S12": "downloaded State Focus Paper Manipur 2024-25 (152 pp): dense bilingual banking profile p33 read exactly (14/14) but banking data has no target table; nothing stored",
    "S31": "downloaded 3 district plans (Imphal West, Imphal East, Chandel): ruled land-use / irrigation tables read exactly (16/16); the 'normal rainfall' table is seasonal not monthly (0 rows stored, run still says complete); a land-use table was misclassified as production and stored invalid",
    "S53": "downloaded the rice-production journal paper: ARIMA statistics table p10 read exactly (24/24); not a domain table, nothing stored; the data series the paper analyses is not printed as a table",
    "S54": "downloaded the Manipur state profile: p4 overview table has units inside the cell text ('22,327 sq. km'), no numeric table found; failed after the OCR recovery route",
}

# tool that will own each agent type: built = code exists in this repo; planned = named in the Phase 1 inventory, not written
TOOLS = {"soil_agent": ("built", "pipeline/readers.py + agents.py (JSON / CSV)"),
         "rainfall_agent": ("built", "pipeline/readers.py + agents.py (JSON / CSV); NASA POWER via scrapers/scrape_manipur_nasa_power.py"),
         "production_agent": ("built", "pipeline/readers.py + agents.py (CSV / JSON); PDF tables via document_agent"),
         "document_agent": ("built (tables only)", "pipeline/pdf_reader.py: pdfplumber -> camelot stream -> RapidOCR; prose needs an LLM extractor (planned)"),
         "price_agent": ("planned", "OGD API / Playwright fetcher + fact_price table"),
         "advisory_agent": ("planned", "LLM extractor with Manipuri handling (needs the adapter work of M18)"),
         "web_registry_agent": ("planned", "Playwright + BeautifulSoup scraper per site + fact tables"),
         "reference_geo_agent": ("planned", "CSV / GIS loader (LGD, Census, boundaries, OSM) -> dimension tables")}

AGENT_TYPE = {"soil_agent": "structured", "rainfall_agent": "structured", "production_agent": "structured",
              "document_agent": "document (PDF text/scan)", "price_agent": "structured/API (prices)",
              "advisory_agent": "free text (multilingual)", "web_registry_agent": "web/HTML",
              "reference_geo_agent": "reference / GIS"}


def main():
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["Source Inventory"]
    head = [c.value for c in ws[1]]
    ix = {h: i for i, h in enumerate(head) if h}
    inv = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[0]]
    missing = [r[0] for r in inv if r[0] not in A]
    extra = [k for k in A if k not in {r[0] for r in inv}]
    assert not missing and not extra, f"unmapped inventory ids {missing}; mapping ids not in inventory {extra}"

    rows = []
    for r in inv:
        sid = r[0]
        agent, status, ev = A[sid]
        if sid in TESTED:
            ev = ev + " | REAL FILE TESTED: " + TESTED[sid]
        rows.append({
            "source_id": sid, "source_name": r[ix["Source / dataset"]], "category": r[ix["Category"]],
            "modality": r[ix["Modality"]], "language": r[ix["Language"]], "non_english": r[ix["Non-English content?"]],
            "priority": r[ix["Priority"]], "phase1_status": r[ix["Status"]], "agent": agent, "agent_type": AGENT_TYPE[agent],
            "status": status, "tool_status": TOOLS[agent][0], "tool": TOOLS[agent][1],
            "phase2_scope": "in_scope" if sid in IN_SCOPE else "on_hold", "evidence_or_barrier": ev})
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    from collections import Counter
    print(f"{len(rows)} rows (inventory has {len(inv)}); status {dict(Counter(r['status'] for r in rows))}; "
          f"agents {dict(Counter(r['agent'] for r in rows))}")


if __name__ == "__main__":
    main()
