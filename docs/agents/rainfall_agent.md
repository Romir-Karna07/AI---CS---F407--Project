# rainfall_agent

| | |
|---|---|
| **Table** | `rainfall_monthly` |
| **Sources** | S22 IMD rainfall (**partial**, IMD grid); S21 NASA POWER (**partial**, `manipur_rainfall_nasa_power.json`, 2,112 rows); S23 India-WRIS **pending** (server certificate revoked; the site did not respond on 2026-11-01) - see `docs/source_agent_map.csv` |
| **Source group** | structured files (JSON / CSV with a month and a rainfall column, or one column per month) |
| **Code** | `pipeline/agents.py::rainfall_agent` |

## Responsibility
Produce one row per district per month: `year_month`, `year`, `month`, `rainfall_mm`, rainy days (>= 2.5 mm), days with data,
days in month, with provenance. Rainfall is stored in mm whatever the source unit (mm, cm, inches).

## Inputs -> outputs
* In: tidy table (`District, Month, Rainfall`), nested JSON, JSON Lines, or a wide table with Jan..Dec columns.
* Month formats understood: `2015-01`, `Jan-2015`, `January 2015`, `2015/1`, separate Year + Month columns.
* Out: rows in `rainfall_monthly`; summary / "Total" rows are skipped; rows with an unparsable month are rejected and reported.

## Decisions: fixed code vs model
Same split as `soil_agent`: structure, column meaning, unit conversion (`mm`, `cm`, `inches` read from a Unit column or the
header), district aliases (NONEI -> NONEY) and validation are **fixed code**. The LLM only maps unknown headers, gated as
described in `soil_agent.md`.

## Tools
`inspect_file` -> `read_json_data` / `read_csv_data` -> (`llm_map_headers`) -> `rainfall_extract` -> `sqlite_load`.

## Evaluation (held-out districts; see `eval/README.md` for the split and its limits)
* **Official:** 10 held-out files `rain_h01`..`h10` in 10 layouts (wide months, inches, cm in a Unit column, `Jan-2019`, aliases, summary footer, JSON Lines ...), five built from the
  IMD grid file (S22) and five from NASA POWER (S21): **F1 1.000** on the first cold run (960/960 fields; 396 + 396 held-out records divided by whole district-year blocks).
* The PDF cases for rainfall (`pdf_05`, `pdf_06`, `pdf_07`) are in `document_agent.md`: 1.000, 1.000 and **0.629** (rotated page).
* **Dev set:** 11 older cases in `eval/dev/`; first run macro F1 0.60 for the whole set (see `soil_agent.md`).

## Limitations
* **What is loaded is not what S22 names.** `manipur_rainfall_monthly.json` is an area-weighted estimate from IMD Pune's
  0.25-degree gridded daily rainfall (cells ~25 km wide), 2015-2025, all 16 districts (13 districts, 1,716 rows, in the database; 3 held out). It is not the district bulletin /
  rain-gauge series and differed by about +18% from the IMD figure we could find for Manipur 2022 (state level).
* Small districts (Imphal East 3 cells, Thoubal / Bishnupur / Kakching 4-6) are smooth estimates.
* The IMD page itself only serves the current day / week / month; the DRMS map has 7 days of station data (8 Manipur
  stations). Neither is in the database.
* NASA POWER (S21) is a modelled point estimate at each district's bbox centre (≈ +10-15 % vs the IMD-grid file in the two districts checked for 2022), not a district mean. The daily -> monthly step lives in `scrapers/scrape_manipur_nasa_power.py`, not in the pipeline.
* No PDF bulletins, no Hindi text, no forecasts.
