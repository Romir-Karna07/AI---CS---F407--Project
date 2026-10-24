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
            "438 village-level SHC records for Manipur (2015-17, 2017-19, 2024-25) scraped from the SLUSI visualisation layer and loaded "
            "(manipur_soil_health.json). Not done: the portal's aggregate state/district cards and the other districts' older cycles"),
    "S22": ("rainfall_agent", "partial",
            "monthly district rainfall 2015-2025 for all 16 districts loaded (manipur_rainfall_monthly.json) - derived from IMD Pune's gridded "
            "daily product, not from the district bulletins/PDFs this row names. Not done: bulletin PDFs, Hindi text, forecasts"),
    "S02": ("production_agent", "partial",
            "Data/CSV_Agent.csv (29 horticulture crops x 9 districts x 1997-2023, 1,217 area/production/yield rows) is in the APY export layout; "
            "its origin is assumed to be this portal - CONFIRM. Not done: field crops, the 7 post-2016 districts, other seasons"),
    # ---- structured sources, same format family as the implemented agents --------------------------------
    "S01": ("production_agent", "pending", "OGD CSV/API datasets: " + PENDING_STRUCT.replace("the target table does not exist", "needs dataset selection")),
    "S03": ("production_agent", "pending", "UPAg state-wise APY: HTML/CSV export; not collected yet"),
    "S04": ("production_agent", "pending", "Agricultural Statistics at a Glance: CSV/XLSX tables are readable by the generic reader; PDF part needs document_agent; not collected yet"),
    "S06": ("production_agent", "pending", "NHB/MIDH horticulture statistics: not collected yet (CSV/XLSX part); PDF part needs document_agent"),
    "S07": ("production_agent", "pending", "Agriculture Census holdings tables: CSV/XLSX; needs fact_land table (KB extension); not collected"),
    "S13": ("production_agent", "pending", "Livestock census / BAHS: PDF + CSV; allied production (KB extension table); not collected"),
    "S21": ("rainfall_agent", "pending", "NASA POWER daily API: pipeline handles monthly rows only; needs a daily->monthly step and an API fetcher; not collected"),
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
        rows.append({
            "source_id": sid, "source_name": r[ix["Source / dataset"]], "category": r[ix["Category"]],
            "modality": r[ix["Modality"]], "language": r[ix["Language"]], "non_english": r[ix["Non-English content?"]],
            "priority": r[ix["Priority"]], "phase1_status": r[ix["Status"]], "agent": agent, "agent_type": AGENT_TYPE[agent],
            "status": status, "evidence_or_barrier": ev})
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    from collections import Counter
    print(f"{len(rows)} rows (inventory has {len(inv)}); status {dict(Counter(r['status'] for r in rows))}; "
          f"agents {dict(Counter(r['agent'] for r in rows))}")


if __name__ == "__main__":
    main()
