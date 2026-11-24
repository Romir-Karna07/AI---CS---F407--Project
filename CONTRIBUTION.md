# Contributions - Team 28, Manipur AgriAgent

This file is built from the **git history** (`git log --all`), so it states what the commits show. Anything the history cannot show (who proposed
what, time spent, work done outside git) is left for each member to add in their own section: do not edit another member's section.

## What the git history shows (as of 2026-11-24, branch `phase-3`)

| Author (git identity) | Commits | Dates | What the commits contain |
|---|---|---|---|
| Romir-Karna07 | 14 | 2026-09-26 .. 2026-11-02 | uploads of the scrapers (`scrapers/`, soil / IMD rainfall / NASA POWER scraping), the Phase 1 source inventory (`TEAM_28_Sources.xlsx`), data and evaluation assets (`Update project data and evaluation assets`, 2026-11-02) |
| Angad Singh Marwaha / Angad Singh (two identities, same person) | 5 | 2026-10-24 .. 2026-11-20 | LangGraph ingestion pipeline (`pipeline/`, `run.py`), evaluation harness and held-out evaluation sets (`eval/`), PDF document agent and tool benchmark (`tools/`, `docs/challenge.md`), source-to-agent map, schema, the AI layer (`pipeline/ai.py`, `context.py`, `crops.py`, `llm.py`), LangSmith setup |

| Commit | Author | Date | Subject |
|---|---|---|---|
| `7e98dbb` | Angad Singh | 2026-10-24 | Manipur agri pipeline: LangGraph ingestion, eval harness, Phase 1 docs |
| `3bc5c73` | Angad Singh Marwaha | 2026-11-02 | Phase 2: held-out evaluation, PDF document agent, complete source map |
| `5ddd2b2` | Romir-Karna07 | 2026-11-02 | Update project data and evaluation assets |
| `bcfbb77` | Angad Singh Marwaha | 2026-11-20 | AI integrations in file processing |
| `c381c67`, `94c01e0` | Angad Singh Marwaha | 2026-11-20 | Improved AI integration and LangSmith setup |
| `9e89f51`, `7dc0b97` .. `a73ef3f`, `bf53460`, `831d71e`, `aa2cedb` | Romir-Karna07 | 2026-09-26 .. 2026-10-27 | scraper files, source inventory and data uploads |

Regenerate with `git shortlog -sne --all` and `git log --all --format='%h|%an|%ad|%s' --date=short`.

## Work areas and where to find them

| Area | Files | Git-recorded author |
|---|---|---|
| Source inventory (54 sources) and source-to-agent map | `TEAM_28_Sources.xlsx`, `docs/source_agent_map.csv`, `docs/build_source_agent_map.py` | Romir-Karna07 (inventory), Angad Singh (map) |
| Scrapers / raw corpus | `scrapers/`, `manipur_*.json`, `Data/CSV_Agent.csv` | Romir-Karna07 |
| Ingestion pipeline (LangGraph), schema, validation | `pipeline/`, `run.py`, `db/schema.sql`, `config/` | Angad Singh |
| PDF document agent, tool benchmark | `pipeline/pdf_reader.py`, `tools/`, `docs/challenge.md`, `docs/pdf_tool_tests/` | Angad Singh |
| Held-out evaluation sets, scorer, metrics | `eval/` | Angad Singh |
| AI layer (header mapping, context facts, crop filter, state detection, plausibility) | `pipeline/ai.py`, `context.py`, `crops.py`, `llm.py`, `docs/ai_layer.md` | Angad Singh |
| Database dictionary, dump, sample queries; file renaming | `db/dictionary.md`, `db/manipur_dump.sql`, `db/sample_queries.py` | prepared with Claude Code (see below) |

## AI assistance (disclosure)
Parts of the code, tests and documentation were written with Claude Code (Anthropic), working at the instruction of a team member in that member's checkout.
Commits made from such sessions carry the trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Every metric in `eval/` was produced by running
the code; no number in the documents was typed in from memory. The members remain responsible for being able to explain the code attributed to them.

## Member sections (each member fills in their own)

### Romir Karna (`Romir-Karna07`)
- Responsibilities: Source Lead, Model Lead
- Files / commits I can explain in the viva: _to be written by the member_

### Angad Singh Marwaha
- Responsibilities: Data Engineering Lead and Evaluation Lead
- Files / commits I can explain in the viva: _to be written by the member_

### Other team members (if any)
Team 28 may have members who have no commit under their own identity. They should be added here, with the work and, if it was done elsewhere, where it can be seen.
