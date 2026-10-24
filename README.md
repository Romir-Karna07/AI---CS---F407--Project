# Manipur AgriAgent - ingestion pipeline (LangGraph)

One command, any JSON or CSV file:

```
pip install -r requirements.txt
python run.py --input <file.json|file.csv> [--db db/manipur.db] [--verbose]
```

Rows land in SQLite (`db/manipur.db`, DDL in `db/schema.sql`) with provenance on every row
(`source_file, source_hash, source_type, agent, run_id, locator`), a validation status, and the
source fields that matched no column kept in `extra` (nothing is dropped).

## Orchestration

```
inspect -> read --error--> recover (retry same parser, then the other one; max 2) --> read
             |                                                             \-> finish (failed)
             v
          classify -> soil_agent -> rainfall_agent -> production_agent -> load -> finish
                      (only agents that have a matching record group are visited)
```

| Step | Decided by | What |
|---|---|---|
| file type, structure, header block, unpivoting, hierarchy | fixed code (`pipeline/readers.py`) | JSON: any nesting, ancestor fields (e.g. district) carried onto records, dict keys used as labels. CSV: multi-row headers, repeated year blocks, state > crop > district hierarchies, tidy tables, wide month columns, `,` `;` tab delimiters, decimal commas |
| which column means what | fixed code (`pipeline/concepts.py`: synonyms + substring rules) | `Organic Carbon (%)`, `OC`, `organic_carbon` -> `oc` |
| domain (soil / rainfall / production) | fixed code; **LLM only if no rule matches** (`pipeline/llm.py`, Groq `openai/gpt-oss-120b`; key in `.env` as `GROQ_API_KEY`) | |
| district, crop, period, month, units | fixed code (`pipeline/normalize.py`, `config/*.json`) | aliases (NONEI->NONEY), crop synonyms (Paddy->Rice), acres->ha, quintals->t |
| validation | fixed code (`pipeline/schemas.py`) | ranges, district in reference list, yield ~ production/area, >=3 nutrients, parsable period |

Failures are isolated: a failing agent is logged and the others still load (`status=partial`).
`--inject-fault read|load|soil_agent|rainfall_agent|production_agent` demonstrates this.

## Tables
`soil_health`, `rainfall_monthly`, `crop_production`, plus `districts` and `runs`. Column meanings: `data_headers.txt`.

## Tests
`python tests/smoke_unseen.py` - 22 checks on files the code was not written around (`tests/unseen/`), fault
injection, idempotent re-ingest, and the three real files. These are developer fixtures, **not** the Phase 2 eval set.

## Known limits
- District codes are the SLUSI/SHC portal codes, not verified against LGD (`config/manipur_districts.json`).
- Soil units are the standard SHC units (N/P/K kg/ha, micronutrients ppm, EC dS/m, OC %); the source does not state them.
- 44 Churachandpur 2024-25 soil rows have pH = 0.0 in the source file; pH 0 is impossible, so it is stored as NULL (original value kept in `extra.ph_source_value`).
- The LLM only maps headers / classifies when rules fail; it was tested live on one French-header soil CSV. It does not extract values.
- `Total (...)` rows in production CSVs are skipped as derived aggregates.

## Evaluation (Phase 2)
`python eval/run_eval.py` runs the pipeline on 34 cases (`eval/cases`, references in `eval/reference`) and writes
`eval/phase2_metrics.csv`. Details, declared normalisation, what is measured vs proxied vs pending: `eval/README.md`.

## Phase 1 inventory -> deliverables
`TEAM_28_Sources.xlsx` (54 sources) drives: `docs/source_agent_map.csv` (built by `docs/build_source_agent_map.py`),
`docs/agents/*.md` (agent cards + grouping rationale), `data/manifest.csv` (built by `data/build_manifest.py`).
