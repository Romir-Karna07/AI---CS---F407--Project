# soil_agent

| | |
|---|---|
| **Table** | `soil_health` |
| **Sources** | S24 Soil Health Card portal (status: **partial**) - see `docs/source_agent_map.csv` |
| **Source group** | structured files (JSON from the portal's GraphQL + WMS layer; any tidy CSV/JSON with soil-test columns) |
| **Code** | `pipeline/agents.py::soil_agent`, shared readers in `pipeline/readers.py` |

## Responsibility
Turn soil-test records (one per village / sample point and sampling cycle) into rows of `soil_health`: district, village,
tehsil, cycle, category, N, P, K, organic carbon, pH, EC, S, Zn, Fe, Cu, Mn, B, with provenance (`source_file, source_hash,
locator, run_id`).

## Inputs -> outputs
* In: a `.json` / `.csv` file whose columns are recognised as soil nutrients (>= 3 of N, P, K, OC, pH, EC, S, Zn, Fe, Cu, Mn, B).
* Out: validated rows in SQLite; rejected rows (schema errors) are reported, never silently dropped; columns it cannot
  place are kept in `extra`.

## Decisions: fixed code vs model
| Decision | Made by |
|---|---|
| file type, nesting, header block | fixed code (`readers.py`) |
| which column is N / pH / district / period | fixed code: synonym dictionary + substring rules (`concepts.py`) |
| header it does not know (e.g. `Azote`, `Acidite_pH`) | **LLM** (Groq `openai/gpt-oss-120b`), only if rules leave the group unrecognised or a district/period column missing; the answer is accepted only if it makes the group a soil table or fills the missing column |
| district alias, period label, pH 0.0 -> missing, validation ranges | fixed code (`normalize.py`, `schemas.py`) |

## Tools (as logged in the trajectory)
`inspect_file` -> `read_json_data` / `read_csv_data` -> (`llm_map_headers`) -> `soil_extract` -> `sqlite_load`.

## Evaluation (dev set, see `eval/README.md` before quoting)
11 cases / 771 reference fields; macro-F1 contribution 1.00 on our own cases. This is optimistic: cases were written by us
while fixing failures. First run was lower (see `eval/results/first_run_metrics.csv`).

## Limitations
* S24 in the inventory is the portal's **aggregate** card data; what is loaded is the SLUSI **point** layer (438 records,
  13 of 16 districts; Jiribam, Senapati, Tamenglong have none).
* Units are the standard SHC units (N/P/K kg/ha, micronutrients ppm, EC dS/m, OC %); the portal does not print them.
* pH recorded as 0.0 (44 Churachandpur 2024-25 rows) is treated as missing; the original value stays in `extra`.
* District codes are the portal's, not LGD. No PDF / scanned soil-health cards, no Manipuri text.
* The scraper recovers points by rendering the map layer and clicking each point; records whose points overlap on the map
  could be missed (checked on one district only: the missing IDs did not exist on the server).
