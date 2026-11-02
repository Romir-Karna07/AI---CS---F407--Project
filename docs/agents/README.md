# Agents and why the sources are grouped this way

Sources come from the Phase 1 inventory (`TEAM_28_Sources.xlsx`, 54 rows). Each is mapped to exactly one agent in `docs/source_agent_map.csv` (54 rows; columns `agent`, `status`, `tool_status`, `tool`, `phase2_scope`, `evidence_or_barrier`). `phase2_scope = in_scope` marks the 6 sources the implemented table agents can read and the team is working on now (S24, S22, S02, S21 loaded; S01, S03 waiting for a manual download); the other 48 are `on_hold`: mapped, declared pending, not started.
Grouping follows **how a source has to be read**, because that decides the tools: format
(table / PDF / HTML dashboard / free text), language (English vs Manipuri / Hindi) and access method (download vs API vs scrape).

| Agent | Reads | Sources | Status |
|---|---|---|---|
| [soil_agent](soil_agent.md) | soil-test tables | 1 (S24) | implemented, **partial** |
| [rainfall_agent](rainfall_agent.md) | weather / rainfall tables | 3 (S21, S22, S23) | implemented, S21 + S22 **partial** |
| [production_agent](production_agent.md) | crop area / production / yield tables | 7 (S01-S04, S06, S07, S13) | implemented, S02 **partial** |
| [document_agent](document_agent.md) | PDF tables: text layer, scans (OCR), rotated pages, English + Hindi headers | 17 | reader **implemented** (`pipeline/pdf_reader.py`), 10 + 10 PDF cases; **no inventory source is marked partial yet**: none of the real PDF tables tried loads into soil / rainfall / production. Manipuri script not covered |
| price_agent | mandi / retail / MSP feeds (HTML, API) | 4 | **pending** |
| advisory_agent | free-text advisories, KCC, news (multilingual) | 5 | **pending** |
| web_registry_agent | HTML dashboards and registries (warehouses, schemes, trade) | 12 | **pending** |
| reference_geo_agent | LGD, census, boundaries, OSM | 5 | **pending** |

Why structured agents are separate from `document_agent`: a table with columns is read by deterministic code and
validated by rules; a PDF needs layout / OCR extraction first and an LLM for the language-heavy parts, with different
errors and different evaluation. Why they are not one agent per source: the three implemented agents share one reader,
one normaliser and one database loader (`pipeline/`), and differ only in what the columns mean.

Coverage today: 4 of 54 sources partial (S24, S22, S02, S21), 50 pending: `M1 = 54/54` mapped, `M2 = 4/54 = 0.074` implemented (below the 0.20 floor). Within the 6 sources in scope now it is 4/6. Pending sources are declared, not hidden; real PDFs of S04, S05, S06, S12, S31, S53, S54 were downloaded and run through the document agent (evidence in the map and in `docs/challenge.md`).
declared, not hidden.

All agents run inside one LangGraph graph (`pipeline/graph.py`): `inspect -> read -> classify -> {soil | rainfall |
production}_agent -> load -> finish`, with retry / recovery on read and load failures and isolation of a failing agent.
