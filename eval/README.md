# Evaluation (Phase 2)

```
python eval/build_cases.py     # rebuilds eval/cases + eval/reference from the real source files (deterministic)
python eval/run_eval.py        # runs the real pipeline on every case, writes eval/phase2_metrics.csv
python eval/run_eval.py --skip-llm-cases   # same, without the one case that needs the Groq header mapper
```

## What is in the set
34 cases = 10 soil + 10 rainfall + 10 production + 4 operations cases (empty file, non-data JSON, injected read fault,
injected load fault). 1,336 reference fields. Each case is a file the pipeline has never been tuned on **by name**, in a
different layout from the scraped sources: other nesting, header names, delimiters (`;`), decimal commas, month-as-column,
units (acres, quintals, kg, inches, cm), crop synonyms, summary rows, NA / '-' cells, JSON Lines in a `.txt`, and one case
with French headers that only the LLM header mapper can read.

Both source groups appear: `json` (soil, rainfall, production) and `csv`. There is **no non-English source group yet**
beyond the one French-header case; Manipuri-language documents are not covered.

## How the references were made (and what "checked" means here)
`build_cases.py` does **not** import the pipeline. It reads the real files (`manipur_soil_health.json`,
`manipur_rainfall_monthly.json`, `Data/CSV_Agent.csv`) with its own loaders, re-shapes a few records, and writes the
**original value in canonical units** as the reference. At the end it re-checks that every reference value (or value / unit
factor) literally appears in the case file. This guards against builder bugs; it does **not** replace a human reading the
source. Before the demo, someone should open ~10% of `eval/reference/*.json` next to the case files and the original sources
and tick them off.

## Declared normalisation (PDF: "declare in eval/README.md")
| Field | Rule |
|---|---|
| district | upper-case name from `config/manipur_districts.json`; aliases map to it (`NONEI`->`NONEY`, `Imphal-East`->`IMPHAL EAST`). Codes are the SLUSI portal codes, **not** LGD |
| crop | `config/crop_synonyms.json` (Paddy->Rice, Redish->Radish, Tapioca->Cassava, Bhindi->Okra, Dry chillies->Chilli (dry), ...) |
| period | soil cycle `2017-19`; agricultural year `YYYY-YY` (`1999 - 2000` -> `1999-00`); month `YYYY-MM` |
| units | rainfall mm; area ha; production tonnes; yield t/ha. Soil: N/P/K kg/ha, OC %, EC dS/m, S/Zn/Fe/Cu/Mn/B ppm, pH unitless (standard SHC units, not stated by the source) |
| missing | text -> `UNSPECIFIED`, numbers -> NULL. Soil pH of 0.0 is treated as missing |
| season | part of the production key only when the file has a season column |

## Match rule (PDF)
A field is correct only if district, period, crop (and village for soil / season for production) match after the
normalisation above, the value is exact for integers and within +-1% for decimals after unit conversion, **and the stored
unit equals the expected unit**. Precision = correct / extracted, recall = correct / reference, F1 = 2PR/(P+R), computed
per agent and per source group; the scored figure is the **macro-average over agents**.

## Metric status
| Metric | Status |
|---|---|
| M3 field F1, M9 completion, M11 workflow, M12 automation, M13 redundancy | measured by `run_eval.py` |
| M4 provenance, M10 groundedness | **automated proxies**: the locator in each stored row is opened in the case file and every numeric field must appear there (after unit conversion). The evaluator's live check is the scored one. No LLM judge is used, so there is no judge to calibrate |
| M6 / M7 / M8 tool selection / input / trajectory | compared with reference trajectories **we wrote from our own design** (strict match, positional). They show the agent behaves as designed, not that the design is optimal |
| M14 - M17 | SQL on `db/manipur.db` (real data); M17 uses a seeded sample of 3 rows traced automatically |
| M1, M2 | measured from `docs/source_agent_map.csv` against the 54-row inventory in `TEAM_28_Sources.xlsx`: M1 = 54/54, **M2 = 3/54 = 0.056, below the 0.20 floor (0 of 2 points)** because only 3 sources have data loaded; 51 are declared pending |
| M5 | **pending**: needs the instructor's unseen items and answer key |
| M18 | **pending**: no adapter trained; extraction is rule-based with an LLM only for header mapping |

`phase2_metrics.csv` columns: `metric_id, metric_name, scope, value, numerator, denominator, floor, target, points,
max_points, status, notes`. The PDF says its CSV template is "attached" but the file is not inside the PDF, so these columns
are our own; send the template and the writer in `run_eval.py` is a few lines to adapt.

## Read this before quoting the numbers
* **First run** (`results/first_run_metrics.csv`): macro F1 **0.60**, task completion 0.62. Failures were real pipeline gaps
  (wrapper keys such as `"districts"` read as a crop, decimal commas in `;` files, inches in a header, leaf rows with blank
  cells, headers like `Distr.`) plus two mistakes in my own references (season key; a missing tehsil field).
* The pipeline was then fixed and the references corrected; the current run scores **1.00 on every metric we can measure**.
  That is a development result on cases I wrote while seeing the failures, so it is optimistic. Expect the instructor's
  surprise documents (likely PDFs / scans, which the pipeline cannot read yet) to score lower.
* The cases share their underlying values with the files already loaded into `db/manipur.db`. No adapter exists, so there is
  no training overlap today; once one is trained, hold these cases out by file hash (`results/` already records hashes via
  `source_hash` in the database).
* Deliberately-broken inputs (`ops_01`, `ops_02`) are excluded from the automation rate.
