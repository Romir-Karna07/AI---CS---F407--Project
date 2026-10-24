# rainfall_agent

| | |
|---|---|
| **Table** | `rainfall_monthly` |
| **Sources** | S22 IMD rainfall bulletins (**partial**); S21 NASA POWER and S23 India-WRIS declared **pending** - see `docs/source_agent_map.csv` |
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

## Evaluation (dev set)
11 cases / 158 reference fields, including wide months, inches, centimetres, aliases and summary rows; F1 1.00 on our own
cases (same caveat as `soil_agent.md`).

## Limitations
* **What is loaded is not what S22 names.** `manipur_rainfall_monthly.json` is an area-weighted estimate from IMD Pune's
  0.25-degree gridded daily rainfall (cells ~25 km wide), 2015-2025, all 16 districts. It is not the district bulletin /
  rain-gauge series and differed by about +18% from the IMD figure we could find for Manipur 2022 (state level).
* Small districts (Imphal East 3 cells, Thoubal / Bishnupur / Kakching 4-6) are smooth estimates.
* The IMD page itself only serves the current day / week / month; the DRMS map has 7 days of station data (8 Manipur
  stations). Neither is in the database.
* Daily data (NASA POWER) is not supported yet: a daily -> monthly step is missing.
* No PDF bulletins, no Hindi text, no forecasts.
