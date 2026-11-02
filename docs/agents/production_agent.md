# production_agent

| | |
|---|---|
| **Table** | `crop_production` |
| **Sources** | S02 APY district/season-wise (**partial**); S01, S03, S04, S06, S07, S13 declared **pending** - see `docs/source_agent_map.csv` |
| **Source group** | structured files (CSV/XLSX-style tables, nested JSON) |
| **Code** | `pipeline/agents.py::production_agent`, CSV layout detection in `pipeline/readers.py` |

## Responsibility
Produce one row per district x crop x season x period with `area_ha`, `production_t`, `yield_t_ha` in canonical units
(hectare, tonne, tonne/hectare), crop names standardised (`config/crop_synonyms.json`: Paddy -> Rice, Redish -> Radish,
Tapioca -> Cassava, ...).

## Inputs -> outputs
* In: tidy CSV, nested JSON (crop -> year -> districts), or the APY export layout: a 3-row header (years / Area-Production-
  Yield / units), a `State/Crop/District` first column with hierarchy rows, one Area-Production-Yield block per year.
* Unit words read from headers or a unit column: hectare / acre / sq km; tonne / kg / quintal; Ton./Ha., kg/ha, q/ha.
* Out: rows in `crop_production`. `Total (...)` / `Grand Total` rows are skipped as derived aggregates (they would double-count).
  A lone yield with neither area nor production (often a placeholder 0) is dropped.

## Decisions: fixed code vs model
Layout detection, hierarchy, unit conversion, crop synonyms, district normalisation and validation (yield ~ production / area
within 5% or the source's rounding) are **fixed code**. The LLM only maps unknown headers (gated; see `soil_agent.md`).

## Tools
`inspect_file` -> `read_csv_data` / `read_json_data` -> (`llm_map_headers`) -> `production_extract` -> `sqlite_load`.

## Evaluation (held-out districts; see `eval/README.md` for the split and its limits)
* **Official:** 10 held-out files `prod_h01`..`h10` (original APY layout +/- totals, tidy, acres + quintals, kg, nested JSON, two-row header, crop synonyms, `;` with decimal commas,
  blank cells): **F1 1.000** on the first cold run (1,095/1,095 fields; 406 held-out district-crop-year rows divided by whole (crop, district) pairs).
* The PDF cases for production (`pdf_01`..`pdf_04`) are in `document_agent.md`: 1.000, 1.000, 1.000 and 0.933 (scan).
* **Dev set:** 10 older cases in `eval/dev/`; first run F1 was 0.18 on production before the layout fixes (`eval/dev/results/first_run_metrics.csv`).

## Limitations
* The corpus file `Data/CSV_Agent.csv` is team-supplied (6 of its 9 districts, 811 rows, are in the database as `Data/partitions/prod_P1+P2.csv`; Imphal West, Thoubal and Ukhrul are held out): 29 horticulture / plantation crops, 9 pre-2016 districts,
  1997-2023 with missing years (no 1998-99, 2020-21). Its origin is **assumed** to be S02 and must be confirmed.
* The 7 districts created in 2016 are absent from that file; no field crops (rice, maize ...) either.
* `season` is "Whole Year" in the corpus; Kharif / Rabi series are supported by the code but untested on real files.
* PDF / scanned crop reports (S04, S05, S10, S12 ...) are not readable yet - that is `document_agent` (pending).
