# Manipur AgriAgent - ingestion pipeline (LangGraph)

One command, any JSON, CSV, Excel, HTML, PDF, image or text file, in any language:

```
pip install -r requirements.txt
python run.py --input <file> [--db db/manipur.db] [--verbose] [--no-ai]
```

Rows land in SQLite (`db/manipur.db`, DDL in `db/schema.sql`) with provenance on every row
(`source_file, source_hash, source_type, agent, run_id, locator`), a validation status, and the
source fields that matched no column kept in `extra` (nothing is dropped).

## Orchestration

```
inspect (triage) -> plan (pages / sheets) -> read --error--> recover (max 2) --> read
                                              |
                                              v
   classify -> soil_agent -> rainfall_agent -> production_agent -> load (+ plausibility) -> finish
      |               (only agents that have a matching record group are visited)
      +--(nothing recognised or stored, budget left)--> recover (structure repair / read as prose / OCR) --> read
```
A static diagram of the real graph is `docs/orchestration.mmd`; with LangSmith on, every run is drawn live (see **AI layer** below).

| Step | Decided by | What |
|---|---|---|
| file type, structure, header block, unpivoting, hierarchy | fixed code (`pipeline/readers.py`) | JSON: any nesting, ancestor fields (e.g. district) carried onto records, dict keys used as labels. CSV: multi-row headers, repeated year blocks, state > crop > district hierarchies, tidy tables, wide month columns, `,` `;` tab delimiters, decimal commas |
| PDF: which tables, which cells | fixed code (`pipeline/pdf_reader.py`) | per page: pdfplumber ruled tables -> camelot "stream" (whitespace / rotated tables) -> RapidOCR + word clustering (scans). Captions above a table give district / period. `AGRI_PDF_PAGES=53-60` limits the pages. Chosen from `docs/challenge.md` |
| which column means what | fixed code (`pipeline/concepts.py`: synonyms + substring rules) | `Organic Carbon (%)`, `OC`, `organic_carbon` -> `oc` |
| domain (soil / rainfall / production) | fixed code; **LLM only if no rule matches** (`pipeline/llm.py`, Groq `openai/gpt-oss-120b`; key in `.env` as `GROQ_API_KEY`) | |
| district, crop, period, month, units | fixed code (`pipeline/normalize.py`, `config/*.json`) | aliases (NONEI->NONEY), crop synonyms (Paddy->Rice), acres->ha, quintals->t |
| validation | fixed code (`pipeline/schemas.py`) | ranges, district in reference list, yield ~ production/area, >=3 nutrients, parsable period |

Failures are isolated: a failing agent is logged and the others still load (`status=partial`).
`--inject-fault read|load|soil_agent|rainfall_agent|production_agent` demonstrates this.

## Tables
`soil_health`, `rainfall_monthly`, `crop_production`, plus `districts` and `runs`. Column meanings: `data_headers.txt`.

## Tests
`python tests/smoke_unseen.py` - 39 checks (22 CSV / JSON, 5 PDF, 11 AI-layer, 1 header-proposal) on files the code was not written around (`tests/unseen/`), fault
injection, idempotent re-ingest, and the three real files. These are developer fixtures, **not** the Phase 2 eval set.

## Known limits
- District codes are the SLUSI/SHC portal codes, not verified against LGD (`config/manipur_districts.json`).
- Soil units are the standard SHC units (N/P/K kg/ha, micronutrients ppm, EC dS/m, OC %); the source does not state them.
- 44 Churachandpur 2024-25 soil rows have pH = 0.0 in the source file; pH 0 is impossible, so it is stored as NULL (original value kept in `extra.ph_source_value`).
- The LLM only maps headers / classifies when rules fail; it was tested live on one French-header soil CSV. It does not extract values.
- `Total (...)` rows in production CSVs are skipped as derived aggregates.
- PDF input reads tables only (pdfplumber ruled -> camelot stream -> RapidOCR for scans). Not handled: Manipuri script, handwriting, key / value tables with units inside the cell text, prose.
  The real inventory PDFs we tried mostly hold data outside soil / rainfall / production, so none is loaded (`docs/challenge.md`).
- The loader can report `complete` while storing zero rows (all rows rejected) or only `invalid` rows; the invalid rows are flagged, the zero-row case is not.
- NASA POWER (S21) rainfall is a modelled point estimate per district, IMD (S22) a gridded estimate; neither is a gauge reading.
- Long PDFs are read over the first 40 pages unless `AGRI_PDF_PAGES` is set (setting it counts as a manual intervention).

## Evaluation (Phase 2)
The corpus is split by district: Imphal West, Thoubal and Ukhrul are **held out**. The agents run on 6 dev files (`Data/partitions/`) and
the official evaluation set (`eval/cases`: 30 CSV / JSON files = 10 per table agent, 10 PDF files for the document agent, 4 operations cases) is built only from the held-out districts.
`python eval/run_eval.py` runs it and writes `eval/phase2_metrics.csv`; `python eval/contamination_check.py` verifies zero overlap.
Details, declared normalisation, limits of the "never used for training" claim, measured vs proxied vs pending: `eval/README.md`.
The old 34 cases are a dev regression set in `eval/dev/` (`python eval/run_eval.py --set dev`).

## Phase 1 inventory -> deliverables
`TEAM_28_Sources.xlsx` (54 sources) drives: `docs/source_agent_map.csv` (built by `docs/build_source_agent_map.py`: all 54 sources, each with its agent, status, tool and evidence; `phase2_scope` marks the 6 being worked on now),
`docs/agents/*.md` (agent cards + grouping rationale), `Data/manifest.csv` (built by `Data/build_manifest.py`: corpus, dev partitions, held-out sources, 10 real PDFs, every evaluation case).

## Data, split and evidence (what lives where)
| Path | What |
|---|---|
| `manipur_soil_health.json`, `manipur_rainfall_monthly.json`, `manipur_rainfall_nasa_power.json`, `Data/CSV_Agent.csv` | the four raw corpus files (S24, S22, S21, S02); they contain the held-out districts and are **not** ingested whole |
| `Data/partitions/` | the 6 dev files the agents run on (`db/manipur.db`: 367 soil, 3,432 rainfall, 811 production rows) |
| `eval/heldout_source/` | the held-out districts' raw material (Imphal West, Thoubal, Ukhrul); never ingested, used only to build `eval/cases` |
| `eval/cases`, `eval/reference` | the official evaluation set: 30 CSV / JSON files + 10 PDF files + 4 operations cases |
| `eval/dev/` | the older 34 development cases + 10 dev PDFs (regression suite, not scored) |
| `Data/pdf_samples/` | 10 real PDFs from the inventory used for the tool comparison in `docs/challenge.md` (**git-ignored**, not redistributed; URLs and hashes in `Data/manifest.csv`) |
| `docs/pdf_tool_tests/` | the PDF tool benchmark, its ground truth and results |
| `docs/source_agent_map.csv` | all 54 inventory sources mapped to an agent (4 `partial`, 50 `pending`) |

## Status (2026-11-02)
Measured on the held-out set, first cold run: field F1 0.987 macro over four agents (soil, rainfall, production 1.000; document agent 0.947); source mapping 54/54; implementation coverage
**4/54** (below the 0.20 floor); surprise-item F1 and adapter comparison **pending**. Missing deliverables: `db/dictionary.md`, a database dump, `eval/adapter_comparison.csv`. See `eval/README.md`
for every metric and its limits.

## AI layer (new)
The model is used at eleven points of a file's journey (triage, page selection, structure repair, recovery, context facts, language, prose extraction, plausibility check, header mapping ...),
always **deterministic first, model only where the code is uncertain, every answer verified by code, strict budgets** (16 model calls and 2 recoveries per file). A clean file costs zero model calls.
Full design, per-stage table, evidence and limits: **`docs/ai_layer.md`**.

| Switch | Effect |
|---|---|
| `python run.py --no-ai` or `AGRI_AI=0` | rule-based behaviour (no model calls) |
| `GROQ_API_KEY` in `.env` | the model (default `openai/gpt-oss-120b`; `AGRI_LLM_MODEL=openai/gpt-oss-20b` has its own free-tier daily quota) |
| `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY=...` in `.env` | a visual trace of every run in LangSmith (see `.env.example`; prompts are uploaded) |
| `AGRI_PDF_PAGES=53-60` | read exactly these pages (otherwise the model / a deterministic score selects them) |

Values the plausibility check judges impossible are **not** stored in the data tables: they go to the `review_queue` table (`review_status = pending_review`) for a person.
Results on 20 files the old pipeline cannot read (held-out districts): fully correct 1/17 before, **16/17 with the AI layer** (`eval/README.md`).
