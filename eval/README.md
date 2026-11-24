# Evaluation (Phase 2)

```
python Data/make_partitions.py     # split the corpus by district: dev partitions (ingested) vs held-out (evaluation only)
python eval/build_heldout.py       # builds the OFFICIAL CSV / JSON set: eval/cases + eval/reference (held-out districts only)
python eval/build_pdf_cases.py --set official   # + the 10 official PDF cases (document agent), also from the held-out districts only
python eval/run_eval.py            # runs the pipeline on the official set -> eval/phase2_metrics.csv
python eval/contamination_check.py # overlap between the evaluation set and everything the agents ran on / were tuned on
python eval/build_cases.py         # (dev) rebuilds the development regression set in eval/dev/
python eval/build_ai_cases.py --set official   # the 20 AI-capability cases (held-out districts) -> eval/ai/
python eval/build_ai_cases.py --set dev        # their dev twins (dev districts) -> eval/dev/ai/
python eval/run_eval.py --set ai               # AI arm; add --no-ai (no model calls) or --legacy-root <extract of the old commit> for the other arms
python eval/build_pdf_cases.py --set dev        # (dev) + 10 dev PDF cases the PDF reader was developed on
python eval/run_eval.py --set dev  # (dev) runs it -> eval/dev/dev_phase2_metrics.csv
python eval/build_case_index.py    # eval/CASE_INDEX.md: every case file, its agent, format and purpose
python db/sample_queries.py        # 10 sample queries on db/manipur.db with expected results
```

**File names.** Case files are named `<group>_<nn>_<layout / language / failure mode>.<ext>` (e.g. `ai_14_TAMIL.csv`, `ai_17_MUST_FAIL_BINARY_JUNK.csv`, `soil_h09_csv_french_headers_llm.csv`); the reference with the expected output is `reference/<case_id>.json` in the same set. See `eval/CASE_INDEX.md`. Files were renamed on 2026-11-24; contents (and so every frozen SHA-256) are unchanged and the contamination check still passes.

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
| M5, M18 adapter | pending | no instructor key; no adapter trained (`eval/adapter_comparison.csv`) |
| (current numbers, final code, run 2026-11-24, gpt-oss-20b) | **M3 0.9868** (soil / rainfall / production / json / csv 1.000, document_agent 0.9471) · M4 / M10 1.000 · M6 0.883 · M7 1.000 · M8 0.8182 · M9 0.9286 · M11 / M12 1.000 · M13 0.0585 · M14 / M15 1.0 · M16 0.0 · M17 3/3 · M18 overlap 0 (64 files) | the table above is the first cold run; these are the latest run after the AI layer and the later changes below |

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

## AI-capability evaluation (`eval/ai/`, `eval/dev/ai/`)
20 cases per set, built by `build_ai_cases.py` from real records in layouts the rule-based pipeline cannot read: a title / source lines above the header, a unit stated only in the title
("thousand hectares"), a repeated header **and** a note deep inside 150 rows, districts as columns, records across columns, an Excel workbook (cover sheet, title row, merged cells), an HTML page with an
unrelated table, an image of a table, figures in English and in Tamil sentences, Hindi / Bengali-script (native digits) / Tamil tables (headers, district names, month names, unit words), two impossible
values (99,999 and 45,000 mm), a 60-page PDF whose only rainfall table is on page 47, and three files that **must fail cleanly** (random bytes, a price table, a header with no data). 464 reference fields
(official) / 465 (dev). The official set uses the held-out districts only; the dev twin uses dev districts and is what prompts and thresholds were tuned on. `contamination_check.py` covers both
(64 evaluation files, 0 overlaps; the three must-fail files were first byte-identical between the two sets, which the check caught, and were regenerated).

Three arms, same files, same scorer (`ai_metrics*.csv`, `per_case*.csv`):

| Arm | cases fully correct (17 that should succeed) | field F1 (micro) | impossible values reaching the tables | must-fail files failed cleanly |
|---|---|---|---|---|
| **before**: the committed pipeline (extract of commit `3bc5c73`, `--legacy-root`) | 1 | 0.477 | 2 of 2 | 3 of 3 |
| new code, **no model calls** (`--no-ai`; includes the new deterministic Excel / HTML / image readers) | 2 | 0.745 | 2 of 2 | 3 of 3 |
| **AI layer, first cold run** (`first_run_ai_metrics.csv`, model gpt-oss-20b) | **16** | **0.974** | **0 of 2** | 3 of 3 |
| AI layer, second run, same code and files | 15 | 0.961 | 0 of 2 | 3 of 3 |
| AI layer, third run, **after a fix informed by the second run (not cold)** | 16 | 0.974 | 0 of 2 | 3 of 3 |
| dev twin, final code | 17 | 1.000 | 0 of 2 | 3 of 3 |

**Disclosure, in order.** (1) The AI layer was developed on the dev twin; the official AI set was then run once, cold (16 / 17). (2) The original 44-case official set was re-run on the new code; the
French-header case failed because the old header mapper swallowed a model error, so the header mapper and the classifier were moved onto the shared wrapper (budget, rate-limit wait, trace);
the AI set was re-run (15 / 17: the model labelled both the month and the year column `year_month`). (3) A check was added that a column of plain years is the year, whatever the model called it
(unit-tested without the model, `tests/smoke_unseen.py`), and the AI set was run a third time (16 / 17). Runs 2 and 3 therefore followed failures seen on the official set: only run 1 is a cold number.
**Model:** all runs used `gpt-oss-20b` with low reasoning effort, because the 200,000-token daily free-tier quota of `gpt-oss-120b` was exhausted by development; 120b was not re-evaluated.
**The remaining miss** is the image case: one village name read by OCR as "Part Il" instead of "Part II" (one record of 12). **Run-to-run variation is real** (temperature 0 does not make the model deterministic);
there is no answer cache, so every run measures the model afresh.

**Original official set on the final code (earlier run):** field F1 **0.985** (was 0.987): the plausibility check set aside one genuine record in each of `prod_h01` and `prod_h06`; soil, rainfall and json are 1.000, production 0.995,
the PDF cases are unchanged (0.947; scans 0.93 / 0.96, rotated page 0.63). Tool-selection metrics fell slightly (M6 0.888, M8 0.818) because model steps (`llm_plausibility`, `llm_page_select` ...)
are tool calls the reference trajectories do not list; they are reported, not hidden.

### Run of 2026-11-24 (final code, `gpt-oss-20b`) and the changes that preceded it
Results of this run, all on the renamed files:

| Set | Result |
|---|---|
| Official, 44 cases | M3 **0.9868** (document_agent 0.9471; the three PDF cases below 1.000 are the same `pdf_04` 0.933, `pdf_07` 0.629, `pdf_10` 0.957), M9 39 / 42 = 0.9286, 3 of 3 live trace-backs, M16 0 duplicates, contamination check 0 overlaps over 64 files |
| Official AI set, 17 should succeed + 3 must fail | **16 / 17 fully correct, field F1 0.9741 (452 / 464)**, 3 / 3 must-fail failed cleanly, 2 / 2 impossible values set aside and 0 stored, 40 model calls in 20 files (2.0 per file, budget 16). The same result came out of two runs the same day. The one miss is the image case (`ai_08`, OCR: 122 / 134 fields) |
| AI set, no model (`--no-ai`) | 2 / 17, F1 0.7454, 3 / 3 must-fail clean, 2 of 2 impossible values **stored** (nothing to judge them) |
| Dev regression set (44) | M3 0.9968: `pdf_09` dev twin 0.935 and `prod_06` 0.957 (a genuine record set aside by the plausibility check, as in the official set) |
| Dev twin of the AI set | 17 / 17, F1 1.000 |
| `tests/smoke_unseen.py` | 44 / 44 checks pass |
| `gpt-oss-120b` (code default) | **not re-run** on the final code; the last measurement this session was 14 / 17, F1 0.9588 (three misses, among them a districts-as-columns table and the English prose case), see `eval/adapter_comparison.csv` |

**Changes made after the first official runs, and what prompted each (disclosure).** After the AI set was frozen, the AI layer was extended (crop-only production table, state detection from the cover / file name,
cover text and sample rows in the prompts, model column meanings applied per table, one shared answer for tables with identical columns, removal of the answer cache). The official sets were then
re-run, and two regressions seen **on the official cases** were fixed in code:
1. the new header prompt made the model call a year column `year` (not a canonical field, silently dropped) in the Tamil table `ai_14` (that run of the official AI set failed it): fixed by accepting aliases (`FIELD_ALIASES`) and stricter prompt wording;
2. the same prompt made the model call the French `Departement` column of `soil_h09` the `state`, so every row lost its district (F1 0.000): fixed by letting the cells decide (`_place_level`: a `state` column whose values are districts is the district); unit-tested without the model.

Both fixes followed failures seen on the official cases, so the numbers above are **not cold** for those cases (the cold figure for the AI set remains the first run of 2026-11-20, 16 / 17, F1 0.974). A third fix, a context prompt that
now asks for an entry for every requested variable (a missing production unit with the 120b model), was found on the AI set as well.

### Implementation coverage (M2) and the real-PDF loads of 2026-11-24
`docs/source_agent_map.csv` now marks **8 of 54** sources `partial` (M2 = **0.148**, still **below the 0.20 floor**, which needs 11): S24, S22, S21, S02 as before, plus four real
inventory PDFs loaded into `db/manipur.db` by `python db/load_inventory_pdfs.py`:

| Source | File (Data/pdf_samples, git-ignored; URL + SHA-256 in Data/manifest.csv) | Rows stored | Checked |
|---|---|---|---|
| S54 MoFPI state profile | p7, Tables 2, 4, 5, 6 (production 2023-24, '000 MT) | 16 state-level | every value against the page |
| S06 NHB Horticultural Statistics at a Glance 2018 | p187, p192: Manipur row of the state-wise fruit / spice area and production tables, 2013-14..2017-18 | 10 state-level | every value |
| S10 Economic Survey, Manipur 2024-25 (369 pp; only the agriculture chapter examined) | p90, Table 5.12: area under 12 fruits and vegetables, 2022-23..2024-25 | 36 state-level | every value against the page |
| S08 Agriculture Department Manipur | area / production / yield sheets 2018-19 and 2019-20 (by district, A / P / Y coded rows) | 158 (6 districts + state total), 157 valid | spot checks against the sheets (maize, sugarcane, potato rows, district and state columns); one row invalid because the source's rounded figures do not give its own yield |

What was **not** done to raise M2, on purpose: the 54-row inventory was not reduced (the rubric divides by the Phase 1 inventory and the TA checks that the row count equals it), and
evaluation or held-out files were not loaded as "sources" (they come from the four sources already counted and would break the contamination check). Tables were loaded only where the
source states the year and the units consistently: S12 (NABARD, contradictory unit labels), S31 (district plans, no year), S53 (forecasts) and S04 (a header layout the reader does
not take yet) stay `pending`. The held-out districts' rows in the S08 sheets were not stored (`AGRI_EXCLUDE_DISTRICTS`); the contamination check still reports 0 overlaps.

**Pipeline changes this needed (made after the official results above were known; disclosed here):** a whole-state figure is stored under the pseudo-district `MANIPUR (STATE)` instead
of being `invalid`; rows of other states in state-wise tables are not stored; caption units ("'000 MT") and a single year are read by fixed code before the model (this fixed a real bug:
a fourth caption exceeded the model's per-file cap and a value was stored 1,000x too small); `MT` unit spellings; caption legends (`A = Area`) are expanded; crops coded A / P / Y in rows
with districts across are turned into records; a sideways page is turned upright before camelot reads it; a header row holding only the label column's name is part of the header;
a header cell such as "Area (ha)" names a variable, not just a unit. After these changes, on `gpt-oss-120b`: official 44 cases **M3 0.9868** (unchanged), M15 0.9998 (the one S08 row),
M17 3 / 3; AI set 14 / 17, F1 0.957 (the last 120b measurement before them was 14 / 17, 0.959). The `gpt-oss-20b` AI set could not be re-run after these changes: its daily token quota
was used up the same day (that run continued without the model and is not counted). `tests/smoke_unseen.py`: 43 / 44 on 120b; the failing check is the plausibility judge, which in
2 of 3 repeated runs also set aside a real 1,450 mm month next to the impossible 99,999 mm value (the impossible value was set aside every time).

**Which files hold which run:** `eval/phase2_metrics.csv` and `eval/results/` = the official set on the final code with `gpt-oss-120b`; `eval/ai/ai_metrics.csv` and `eval/ai/results/per_case.csv` = the AI set on `gpt-oss-120b` (14 / 17); the `gpt-oss-20b` AI runs (16 / 17, before the reader changes) are kept in `eval/ai/results/history/`. `eval/dev/dev_phase2_metrics.csv` (M3 0.995) comes from a run during which the `gpt-oss-20b` quota ran out, so its model-dependent cases may be understated; re-run it when the quota resets.

## Metric status
| Metric | Status |
|---|---|
| M3, M9, M11, M12, M13 | measured by `run_eval.py` on the held-out set |
| M4, M10 | **automated proxies**: the locator in each stored row is opened in the case file and every numeric field must appear there (after unit conversion). No LLM judge is used, so there is no judge to calibrate. The evaluator's live check is the scored one |
| M6 / M7 / M8 | compared with reference trajectories **we wrote from our own design** (strict match, positional); they show the agent behaves as designed, not that the design is optimal |
| M14-M17 | SQL on `db/manipur.db` (dev partitions only; no PDF rows); M17 uses a seeded sample of 3 rows traced automatically |
| M1, M2 | measured from `docs/source_agent_map.csv` (54 rows, one per Phase 1 source, ids checked against `TEAM_28_Sources.xlsx`). M1 = 54/54. **M2 = 8/54 = 0.148** (2026-11-24; was 4/54 = 0.074): S24, S22, S02, S21, S06, S08, S54, S10 are `partial`, the other 46 are declared pending (48 of them not started, `phase2_scope = on_hold`). Real PDFs for 7 sources were run through the document agent and are recorded in the map. The 6-source working set (S24, S22, S02, S21 + S01, S03 waiting for a manual download) is reported on its own row |
| M5 | **pending**: needs the instructor's unseen items and answer key |
| M18 | **pending**: no adapter trained, so no adapter weights exist and there is no weights link; `eval/adapter_comparison.csv` records the base-model arms and an adapter row marked NOT RUN. The overlap-by-hash part is reported (0) |

`eval/phase2_metrics.csv` follows the instructor's template (`Eval.xlsx`) exactly: `metric_id, scope, numerator, denominator, value, n_cases, evaluator_or_script, notes`, one row per metric per scope. Floor, target, points and status are in the `notes` column; the full table (metric name, floor, target, points, max points, status) is `eval/results/phase2_metrics_detailed.csv`.

## The development set (`eval/dev/`)
34 cases built by `build_cases.py` from the dev districts only. The pipeline was tuned against an earlier version of these cases; their
first run scored macro F1 0.60 (`eval/dev/results/first_run_metrics.csv`) and the failures drove real fixes in `readers.py` / `concepts.py`.
They are a regression suite, **not** part of the headline numbers.

**Later on 2026-11-24 (S10 and the files the team added).** `Data/pdf_samples/` gained `S10_economic_survey_manipur_2024-25.pdf` (renamed from `EcoSurveyManipur.pdf`) and
`S05_desagri_3rd_advance_estimates_2025-26.xlsx` (renamed). The spreadsheet holds all-India estimates only, no Manipur row, so nothing from it is stored and S05 stays `pending`.
From the Economic Survey only chapter 5 (agriculture and allied) was examined; Table 5.12 loads (S10 `partial`, M2 8 / 54 = 0.148). Reader changes this needed, again made after the
official results were known: the "(1) (2) (3)" column-number row and a leading "Sl. No." column are dropped; a table whose column headers are only years takes its measure from the
caption ("Area in hectares"); "Name of the crop" is a crop header; one measure next to a crop column is a production table; a text column whose cells are known crop / livestock names is
the crop column even without the model (the crop vocabulary in `pipeline/crops.py` was extended with common crop names); caption units / years / crop groups are read by fixed code even
when no model is available. With these, all five real files load the same rows **without any model call**. Both models' daily quotas were exhausted by then, so the model-dependent
sets could only be re-run in `--no-ai` mode that day (results below); the with-model numbers above are from before these last changes and must be re-measured when the quota resets.

| `--no-ai` run after these last changes | Result |
|---|---|
| Official 44 cases | M3 0.9803: every case identical to the with-model run except `soil_h09` (French headers, needs the model by design, 0 / 35); stored in `eval/results/no_model_phase2_metrics.csv`, `no_model_per_case.csv` |
| AI set | 2 / 17, F1 0.7454: identical to the earlier no-model arm (`eval/ai/ai_metrics_baseline.csv`) |
| Dev set | M3 0.995 (`eval/dev/dev_phase2_metrics.csv` is now this no-model run) |

`eval/phase2_metrics.csv` stays the with-model run (gpt-oss-120b); only its M1 / M2 rows were recomputed, because they come from `docs/source_agent_map.csv`, not from the model.

### Final state at the end of 2026-11-24 (what the result files hold)
| File | Run | Headline |
|---|---|---|
| `eval/phase2_metrics.csv`, `eval/results/per_case.csv` | official 44 cases, **final code and database**, `gpt-oss-20b`, no quota errors | M3 **0.9868**, M4 / M10 1.0, M1 1.0, **M2 0.1481 (8 / 54)**, M6 0.883, M7 1.0, M8 0.8182, M9 0.9286, M11 / M12 1.0, M13 0.0585, M14 1.0, M15 0.9998, M16 0, M17 3 / 3 |
| `eval/results/no_model_*` | official 44 cases, final code, `--no-ai` | M3 0.9803 (only `soil_h09` differs) |
| `eval/ai/ai_metrics.csv` | AI set, `gpt-oss-120b`, after the state-level / legend / sideways-page changes but **before** the last S10-related reader rules | 14 / 17, F1 0.9565 |
| `eval/ai/ai_metrics_baseline.csv` | AI set, final code, `--no-ai` | 2 / 17, F1 0.7454 |
| `eval/dev/dev_phase2_metrics.csv` | dev set, final code, `--no-ai` | M3 0.995 |

**Not measured on the final code:** the AI set and `tests/smoke_unseen.py` with a model. A run started at 23:07 hit the `gpt-oss-20b` daily token quota within minutes (6 / 17 and 11 smoke failures, every
failure a model call refused); those runs were discarded and are not in the history folders. Re-run `python eval/run_eval.py --set ai` and `python tests/smoke_unseen.py` once the quota resets.
