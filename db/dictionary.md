# Data dictionary - `db/manipur.db`

Companion to `db/schema.sql` (the DDL; created by `pipeline/db.py`, safe to re-run). One SQLite file, six tables.
Conventions: missing **text** is the literal `UNSPECIFIED`; missing **numbers** are `NULL`; no source field is dropped (what matches no column goes to `extra`).
Sample queries with expected results: `db/sample_queries.py` -> `db/sample_queries_results.md`. Plain-text dump: `db/manipur_dump.sql`.

Contents of the delivered database: the six dev partitions in `Data/partitions/` plus five real inventory PDFs loaded by `python db/load_inventory_pdfs.py` (S54, S06, S10, S08 x 2). The held-out districts Imphal West, Thoubal and Ukhrul are never loaded (`AGRI_EXCLUDE_DISTRICTS`; checked by `eval/contamination_check.py`):

| table | rows | what |
|---|---|---|
| `districts` | 17 | dimension: the 16 districts of Manipur + the pseudo-district `MANIPUR (STATE)` for whole-state figures |
| `runs` | 15 | one row per ingestion run (the provenance anchor; a re-run of an already loaded file is recorded too and inserts nothing) |
| `soil_health` | 367 | Soil Health Card points, 10 districts, cycles 2015-17 / 2017-19 / 2024-25 |
| `rainfall_monthly` | 3,432 | 13 districts x 132 months (2015-01 .. 2025-12) x 2 sources (IMD grid, NASA POWER) |
| `crop_production` | 1,031 | 811 APY rows (S02: 6 districts, 29 horticulture crops, 1997-98 .. 2022-23) + 158 Agriculture Department rows (S08: 6 districts + state total, field crops, 2018-19 and 2019-20) + 62 state-level rows (S54 2023-24, S06 2013-14 .. 2017-18, S10 2022-23 .. 2024-25); 1,030 valid |
| `review_queue` | 0 | values the plausibility check set aside for a person (table is created on first use; this database predates the check) |

## Columns shared by the three fact tables (provenance and validation)

| column | type | meaning |
|---|---|---|
| `id` | INTEGER PK | surrogate key |
| `source_file` | TEXT | file the row came from (name only) |
| `source_hash` | TEXT | SHA-256 of that file's bytes |
| `source_type` | TEXT | `csv`, `json`, `xlsx`, `html`, `pdf`, `image`, `text` |
| `agent` | TEXT | `soil_agent`, `rainfall_agent` or `production_agent` |
| `run_id` | TEXT FK -> `runs` | the ingestion run |
| `locator` | TEXT | where in the file: `line 6, cols F-H, period 1999 - 2000`, `p1 t1 line 3` (PDF), character offsets (prose) ... |
| `district`, `district_code` | TEXT | canonical upper-case name and SLUSI/SHC portal code (`UNSPECIFIED` if the name is not in the Manipur reference list). A figure for the whole state (a state profile, the Manipur row of a state-wise national table, a 'Total State' column) is stored as `MANIPUR (STATE)` / `14-STATE`; rows about other states or All India are never stored |
| `state`, `state_code` | TEXT | `MANIPUR`, `14` |
| `unit` | TEXT | units of the measures, e.g. `area:ha;production:t;yield:t/ha` (values are stored already converted to these) |
| `unit_status` | TEXT | `declared` (the source states the unit), `assumed` (standard unit for the table), `unknown:<text>` (a unit that could not be converted - row is `invalid`) |
| `validation_status` | TEXT | `valid` or `invalid` |
| `validation_notes` | TEXT | why a row is `invalid`, e.g. `ph_out_of_range:14.2`, `yield_inconsistent:...`, `district_not_in_manipur_reference:X` |
| `extra` | TEXT (JSON) | source fields that matched no column (e.g. `source`, `partition`, `aspirational`), `ph_source_value` when a pH of 0 was replaced by NULL, and `context_facts` (unit / district / period / state taken from a title, each with the quoted words that justify it) |

Uniqueness: `UNIQUE (source_hash, locator)` - re-ingesting the same file inserts nothing (idempotent). Semantic duplicates (same place / time / crop in two sources) are counted by the indexes `ix_*_nat`.

## `districts`
| column | type | meaning |
|---|---|---|
| `district_code` | TEXT PK | SLUSI/SHC portal code (252-260, 711-717); **not** verified against LGD. `14-STATE` = the whole state |
| `district_name` | TEXT UNIQUE | canonical upper-case name (`NONEY`; the rainfall files spell it `NONEI`, handled by alias) |
| `state`, `state_code` | TEXT | `MANIPUR`, `14` |

## `runs`
| column | meaning |
|---|---|
| `run_id` | `run_<timestamp>_<hash6>` |
| `input_file`, `source_hash` | the file ingested and its SHA-256 |
| `started_at`, `finished_at` | ISO timestamps |
| `status` | `complete` (rows stored), `partial` (one agent failed, others loaded), `failed` (nothing stored) |
| `rows_extracted`, `rows_inserted`, `rows_duplicate`, `rows_invalid` | counts for the run |
| `retries` | recoveries used (maximum 2) |
| `notes` | free text |

## `soil_health` (source S24, Soil Health Card portal)
One row per SHC point. Units are the standard SHC units; the portal does not print them, hence `unit_status = assumed`.

| column | type | unit / values |
|---|---|---|
| `crop` | TEXT | `UNSPECIFIED` unless the source names one |
| `period` | TEXT | SHC cycle `2015-17`, `2017-19`, `2024-25` |
| `year` | INTEGER | first year of the cycle |
| `village`, `tehsil`, `block` | TEXT | as in the source (`tehsil` is not published for 2024-25) |
| `category` | TEXT | fertility / salinity class from the source (`Sufficient`, `Deficient`, `Non saline` ...) |
| `record_id` | TEXT | portal feature id, e.g. `14_254_shc_2017-19.3` |
| `latitude`, `longitude` | REAL | degrees, **approximate** (map-pixel position, not surveyed) |
| `n`, `p`, `k` | REAL | available nitrogen / phosphorus / potassium, kg/ha (soil test, never a fertiliser quantity) |
| `oc` | REAL | organic carbon, % |
| `ph` | REAL | pH units; a source value of exactly 0 is impossible and is stored as NULL (original in `extra.ph_source_value`) |
| `ec` | REAL | electrical conductivity, dS/m |
| `s`, `zn`, `fe`, `cu`, `mn`, `b` | REAL | sulphur, zinc, iron, copper, manganese, boron, ppm (nullable) |

Validation: pH 2-11, OC 0-15, EC 0-25, N/P 0-3000, K and micronutrients 0-5000, latitude 23.5-25.8, longitude 92.9-94.9; at least 3 nutrient values; period present. Known real source anomalies are kept and flagged, not corrected (Zn 222 and 157 ppm).

## `rainfall_monthly` (sources S22 IMD gridded, S21 NASA POWER)
One row per district, month and source. **Always filter on `source_file`** (or compare both on purpose): the two sources cover the same 13 districts and months.

| column | type | meaning |
|---|---|---|
| `crop` | TEXT | always `UNSPECIFIED` (column kept so the three fact tables share a shape) |
| `year_month` | TEXT | `YYYY-MM` |
| `year`, `month` | INTEGER | derived from `year_month` |
| `rainfall_mm` | REAL | monthly total, mm (inches / cm in a source are converted) |
| `rainy_days_ge_2_5mm` | INTEGER | days with >= 2.5 mm |
| `days_with_data`, `days_in_month` | INTEGER | coverage of the month |

Caveat: IMD values are area-weighted estimates from a 0.25 degree gridded analysis, NASA POWER a modelled point estimate at the district's bounding-box centre; neither is a rain-gauge reading.
Validation: 0-3000 mm, month 1-12, rainy days <= days in month. A value that is physically impossible (e.g. 99,999 mm) is **not stored**: it goes to `review_queue`.

## `crop_production` (source S02, Area-Production-Yield reports)
Crops only. Livestock, dairy, fishery and forestry rows are not stored (`pipeline/crops.py`).

| column | type | meaning |
|---|---|---|
| `crop` | TEXT | canonical name from `config/crop_synonyms.json` (`Paddy` -> `Rice`, `Dry chillies` -> `Chilli (dry)`) |
| `crop_raw` | TEXT | the name exactly as the source wrote it |
| `season` | TEXT | `Whole Year` in the delivered data (`Kharif`, `Rabi` ... if a source gives them), else `UNSPECIFIED` |
| `period` | TEXT | agricultural year `1999-00` or calendar year |
| `year` | INTEGER | first year of the period |
| `area_ha` | REAL | hectares (acres and thousand-hectares converted) |
| `production_t` | REAL | tonnes (kg, quintals, lakh tonnes converted) |
| `yield_t_ha` | REAL | tonnes per hectare |

Validation: area <= 1e7 ha, production <= 1e8 t, yield <= 500 t/ha, yield within 5 % (or 0.01) of production / area, period present, at least one measure. `Total (...)` rows are skipped as derived aggregates.

## `review_queue`
Rows a person must look at; they are **not** in the data tables.

| column | meaning |
|---|---|
| `run_id`, `agent`, `table_name` | where the value would have gone |
| `source_file`, `source_hash`, `locator`, `district` | provenance |
| `field`, `value` | the impossible field and its value |
| `reason`, `flagged_by` | why, and which check / model flagged it (the model never edits a value; it only sets rows aside) |
| `row_json` | the complete candidate row |
| `review_status` | `pending_review` (default), `accepted`, `corrected`, `rejected` |
| `created_at` | timestamp |

## State-level rows
`district = 'MANIPUR (STATE)'` marks a figure for the whole state. Do not add it to the district rows of the same crop and year (it would count the state twice);
query it on its own, or exclude it with `district_code <> '14-STATE'`. Its source quote (state name, caption units, crop group) is in `extra.context_facts`.

## Typical joins
* Soil with production by district: `soil_health.district_code = crop_production.district_code`.
* Rainfall with soil: align the SHC cycle with calendar years; the grains differ (village point vs district month).
* Provenance: `JOIN runs USING (run_id)`, or `source_hash` to find the exact file bytes.
