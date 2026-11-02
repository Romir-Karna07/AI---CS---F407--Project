# Evaluation (Phase 2)

```
python Data/make_partitions.py     # split the corpus by district: dev partitions (ingested) vs held-out (evaluation only)
python eval/build_heldout.py       # builds the OFFICIAL CSV / JSON set: eval/cases + eval/reference (held-out districts only)
python eval/build_pdf_cases.py --set official   # + the 10 official PDF cases (document agent), also from the held-out districts only
python eval/run_eval.py            # runs the pipeline on the official set -> eval/phase2_metrics.csv
python eval/contamination_check.py # overlap between the evaluation set and everything the agents ran on / were tuned on
python eval/build_cases.py         # (dev) rebuilds the development regression set in eval/dev/
python eval/build_pdf_cases.py --set dev        # (dev) + 10 dev PDF cases the PDF reader was developed on
python eval/run_eval.py --set dev  # (dev) runs it -> eval/dev/dev_phase2_metrics.csv
```

## The split (held-out evaluation)
The corpus is split **by district**. Imphal West, Thoubal and Ukhrul (present in all four data files) are **held out**.

| | files | rows in `db/manipur.db` |
|---|---|---|
| **Dev partitions, the 6 files the agents run on** | `Data/partitions/soil_P1.json`, `soil_P2.json` (S24) · `rain_P1_imd.json` (S22) · `rain_P2_nasa.json` (S21) · `prod_P1.csv`, `prod_P2.csv` (S02) | soil 367 · rainfall 3,432 · production 811 |
| **Held-out raw material, 4 files, never ingested** | `eval/heldout_source/soil_P3.json`, `rain_P3_imd.json`, `rain_P3_nasa.json`, `prod_P3.csv` | 0 |

The held-out records are **divided** (no record appears in two files) into **10 evaluation files per agent = 30 files**, each in a
different layout, plus 4 operations cases (blank file, non-data JSON, injected read fault, injected load fault):

| Agent | Held-out records | Files | How they are divided |
|---|---|---|---|
| soil | 70 (71 minus 1 duplicate village) | 10 | random chunks |
| rainfall | 396 IMD (S22) + 396 NASA POWER (S21) | 5 + 5 | whole district-year blocks, one source per file |
| production | 406 district-crop-year rows | 10 | whole (crop, district) pairs; synonym-crop pairs kept together for the synonyms file |

### The 10 PDF files (document agent)
Built by `build_pdf_cases.py` from the same held-out records and rendered as real PDFs with reportlab: ruled grids, tables without ruling lines whose
district / year appear only in the caption, a table continued over pages with a repeated header, a **page physically rotated 90 degrees**, **English +
Hindi (Devanagari) headers**, acres / quintals, and **two scans** (image only, no text layer, light noise + JPEG) that need OCR. 4 production, 3 rainfall,
3 soil files; 302 + 324 + 502 = 1,128 reference fields. They are scored as their own agent (`document_agent`, source group `pdf`) in the macro-average.
The reader was developed on the **dev** twins of these files (`eval/dev/cases/pdf_*.pdf`, built from the dev districts) and on the real PDFs in
`Data/pdf_samples/`; the held-out PDFs were run once, cold. Not covered: Manipuri script (Meitei Mayek / Bengali), handwriting, skewed scans.

Reference fields (CSV / JSON): soil 775 · rainfall 984 · production 1,095. IDs: `soil_h01`..`h10`, `rain_h01`..`h10`, `prod_h01`..`h10`, `ops_01`..`04`.
Layouts: nested JSON, cycle→district lists, keyed-by-district JSON, JSON Lines in `.txt`, tidy CSV, `;` + decimal comma, tab-delimited,
extra / shuffled columns with NA and `-`, wide month columns, Year + Month-name, `Jan-2019`, inches, cm in a Unit column,
district aliases (`Imphal-West`, `thoubal`), Total / Average / Grand Total rows, acres, quintals, kg, blank cells, crop synonyms,
the original 3-row-header APY layout, and French headers (needs the LLM header mapper).

## "Never used to train": what that means and how it is enforced
There is **no model training** in this system: extraction is rule-based and the LLM only maps unknown headers. So "training data" means
**anything the agents were run on or the code was tuned against**. The rule enforced:
* held-out records are **not in the database** and **not in the dev cases** (`eval/dev` is rebuilt without these districts);
* the first official run is frozen in `eval/results/first_run_metrics.csv` / `first_run_per_case.csv` and is never overwritten;
* `eval/contamination_check.py` (run inside `run_eval.py`, row `M18 / overlap_check`) verifies: no evaluation file shares a SHA-256 with a dev
  file or a held-out source file; no DB row or `source_hash` comes from an evaluation file; no DB row is in a held-out district; no
  (agent, district, period, crop, entity) key appears in both the dev and the evaluation references; every frozen hash in
  `eval/heldout/split_manifest.json` still matches. **Result: 0 overlaps.**

**Limits of that claim (read before quoting it):**
1. The pipeline code was written and fixed **before** this split, while looking at the whole corpus and at earlier cases that used these
   districts. The held-out *records* were not used since the split, but the code is not blind to the *data family*; the layouts in the
   held-out files are the same kinds as those in the dev set. Treat the result as "no leakage of records", not as "never saw anything like it".
2. From the split onward, the pipeline code was **not changed** after seeing the official results. Two scorer changes were made after the
   first official run and are disclosed here: (a) `run_eval.py`'s locator tracer learned tab-delimited files (first run M4 0.9945 and
   M10 0.9731, because `soil_h10` is tab-delimited and the tracer only knew `,` and `;`); (b) none to the agents.
   Later work, also disclosed: the **PDF reader** (`pipeline/pdf_reader.py`) and its graph hooks were added after the first CSV / JSON official run. It was developed
   only on the dev PDFs (`eval/dev`) and the real PDFs in `Data/pdf_samples/`; the held-out PDFs were then run once and **no code changed afterwards**. The CSV parser in
   `pipeline/readers.py` was split into `read_csv_file` + `parse_rows` so PDF tables reuse it; the CSV output is identical (27 smoke checks and the dev set pass).
3. The raw full corpus files (`manipur_soil_health.json`, `manipur_rainfall_monthly.json`, `manipur_rainfall_nasa_power.json`,
   `Data/CSV_Agent.csv`) are still in the repo as the record of what was scraped; they contain the held-out districts. `tests/smoke_unseen.py`
   still loads them (into a temporary database) as a regression test.
4. NASA POWER (S21) is a modelled estimate; IMD (S22) is a gridded estimate. The references are the file's own values, so this measures
   extraction, not whether the rainfall is physically right.

## How the references were made (and what "checked" means here)
`build_heldout.py` does **not** import the pipeline. It reads only the held-out files with its own loaders, re-shapes records into the
layouts above, and writes the **original value in canonical units** as the reference. At the end it re-checks that every reference value
(or value / unit factor) literally appears in its case file. This guards against builder bugs; it does **not** replace a human reading
the source. Before the demo, someone should open ~10 % of `eval/reference/*.json` next to `eval/heldout_source/` and tick them off.

## Declared normalisation (PDF: "declare in eval/README.md")
| Field | Rule |
|---|---|
| district | upper-case name from `config/manipur_districts.json`; aliases map to it (`NONEI`->`NONEY`, `Imphal-West`->`IMPHAL WEST`). Codes are the SLUSI portal codes, **not LGD** |
| crop | `config/crop_synonyms.json` (Paddy->Rice, Redish->Radish, Tapioca->Cassava, Bhindi->Okra, Dry chillies->Chilli (dry), ...) |
| period | soil cycle `2017-19`; agricultural year `YYYY-YY` (`1999 - 2000` -> `1999-00`); month `YYYY-MM` |
| units | rainfall mm; area ha; production tonnes; yield t/ha. Soil: N/P/K kg/ha, OC %, EC dS/m, S/Zn/Fe/Cu/Mn/B ppm, pH unitless (standard SHC units, not stated by the source) |
| missing | text -> `UNSPECIFIED`, numbers -> NULL. Soil pH of 0.0 is treated as missing |
| season | part of the production key only when the file has a season column |

## Match rule (PDF)
A field is correct only if district, period, crop (and village for soil / season for production) match after the normalisation above,
the value is exact for integers and within +-1 % for decimals after unit conversion, **and the stored unit equals the expected unit**.
Precision = correct / extracted, recall = correct / reference, F1 = 2PR/(P+R), computed per agent and per source group; the scored
figure is the **macro-average over agents**. Source groups here are file formats (`json`, `csv`); there is **no non-English source
group** beyond one French-header file, and no Manipuri documents.

## Official results (held-out set: 30 CSV / JSON files + 10 PDF files + 4 operations cases; first cold run)
| Metric | Value | Note |
|---|---|---|
| M3 field F1, macro over 4 agents | **0.987** | soil 1.000 (691/691) · rainfall 1.000 (960/960) · production 1.000 (1,095/1,095) · **document_agent 0.947 (1,110/1,216)**; json 714/714, csv 2,032/2,032, **pdf 0.947** |
| M4 / M10 | 1.000 / 1.000 | automated proxies, **page level for PDFs**; for the two scans the page text is itself OCR output, so that check is partly circular |
| M6 / M7 / M8 / M9 | 0.972 / 1.000 / 0.955 / 0.929 | document_agent alone: 0.927 / 1.000 / 0.900 / **0.700** |
| M11 / M12 / M13 | 1.000 / 1.000 / 0.017 | fault-injection row counts retries via `recover` steps only |
| M14-M17 on `db/manipur.db` | 1.0 / 1.0 / 0.0 / 3 of 3 | 4,610 dev rows (no PDF rows: no real PDF table loads into the three tables yet) |
| M18 overlap | 0 | contamination check, 44 evaluation files |
| M1 / M2 | 54/54 = 1.000 / 4/54 = 0.074 | all inventory sources are mapped; only 4 have data loaded, **below the 0.20 floor (0 of 2 points)**. Within the 6 sources in scope now it is 4/6 (row `M2 / in_scope_sources`, information only) |
| M5, M18 adapter | pending | no instructor key; no adapter trained |

**PDF cases, per file (cold run):** 7 of 10 at F1 1.000 (ruled multi-page, unruled with caption context, Hindi headers, wide months with caption, tidy
multi-page, ruled landscape soil, unruled soil with caption). The three that lost points:
* `pdf_04` scanned production, **0.933**: OCR misread a digit (606 instead of 909) and a district ("Thbal" for Thoubal; the fuzzy district match needs 0.85
  similarity and "THBAL" is below it), which also dropped one record.
* `pdf_10` scanned soil, **0.957**: the same "Thbal" misread cost one village (6 fields).
* `pdf_07` rotated unruled rainfall, **0.629**: camelot's whitespace-table detector cut the December column off the rotated page and some district cells came
  back blank, so rows landed under `UNSPECIFIED` and an LLM header-mapping call was made. **Not fixed**, because the file is held out. It passed on the dev twin.

`eval/results/first_run_metrics.csv` is the first official run on the 30 CSV / JSON + 4 operations cases; `first_run_with_pdf_metrics.csv` and
`first_run_with_pdf_per_case.csv` are the first run that includes the PDFs. Neither is ever overwritten. The earlier all-1.0 CSV / JSON result stands as it was:
those 30 files scored F1 = 1.0 on the first try, which says the agents generalise across districts and layouts, not that they would read an arbitrary
document. The PDF files are the first held-out cases that did not.

## Metric status
| Metric | Status |
|---|---|
| M3, M9, M11, M12, M13 | measured by `run_eval.py` on the held-out set |
| M4, M10 | **automated proxies**: the locator in each stored row is opened in the case file and every numeric field must appear there (after unit conversion). No LLM judge is used, so there is no judge to calibrate. The evaluator's live check is the scored one |
| M6 / M7 / M8 | compared with reference trajectories **we wrote from our own design** (strict match, positional); they show the agent behaves as designed, not that the design is optimal |
| M14-M17 | SQL on `db/manipur.db` (dev partitions only; no PDF rows); M17 uses a seeded sample of 3 rows traced automatically |
| M1, M2 | measured from `docs/source_agent_map.csv` (54 rows, one per Phase 1 source, ids checked against `TEAM_28_Sources.xlsx`). M1 = 54/54. **M2 = 4/54 = 0.074**: S24, S22, S02, S21 are `partial`, the other 50 are declared pending (48 of them not started, `phase2_scope = on_hold`). Real PDFs for 7 sources were run through the document agent and are recorded in the map. The 6-source working set (S24, S22, S02, S21 + S01, S03 waiting for a manual download) is reported on its own row |
| M5 | **pending**: needs the instructor's unseen items and answer key |
| M18 | **pending**: no adapter trained. The overlap-by-hash part is reported (0) |

`eval/phase2_metrics.csv` follows the instructor's template (`Eval.xlsx`) exactly: `metric_id, scope, numerator, denominator, value, n_cases, evaluator_or_script, notes`, one row per metric per scope. Floor, target, points and status are in the `notes` column; the full table (metric name, floor, target, points, max points, status) is `eval/results/phase2_metrics_detailed.csv`.

## The development set (`eval/dev/`)
34 cases built by `build_cases.py` from the dev districts only. The pipeline was tuned against an earlier version of these cases; their
first run scored macro F1 0.60 (`eval/dev/results/first_run_metrics.csv`) and the failures drove real fixes in `readers.py` / `concepts.py`.
They are a regression suite, **not** part of the headline numbers.
