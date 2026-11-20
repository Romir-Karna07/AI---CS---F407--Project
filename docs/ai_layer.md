# The AI layer: where the model is used, how it is checked, and what it cost

*Written 2026-11-18. Code: `pipeline/ai.py` (the one wrapper every model call goes through), `triage.py`, `page_select.py`, `structure.py` + `tabular.py`,
`prose.py`, `context.py`, `lang.py`, `plausibility.py`, and the changed `graph.py`. Evidence: `eval/ai/`, `eval/dev/ai/`, `eval/README.md`.*

## 1. The principle: deterministic first, the model only where the code is uncertain, and every answer verified

Before this change the language model was used for two things only (mapping unknown column headers and breaking a classification tie); everything else was
fixed code. That cannot read a file it was not written around (a title above the header, an Excel workbook, figures in a sentence, a Tamil table).
The model is now used at **eleven** points of a file's journey, but always under the same rules:

1. **Deterministic first.** Each stage has a cheap code check; the model is asked only when that check is uncertain. A clean file costs **zero** model calls and
   is parsed exactly as before (verified: 88 known-good CSV / text files give byte-identical output through the old and the new reader).
2. **The model proposes, code verifies.** Row numbers must exist, quoted words must occur in the source, every number must occur in its quote, units come from a
   fixed vocabulary, districts from the 16-name reference list, months are 1-12. An answer that fails the check is dropped, never trusted.
3. **Strict budgets.** At most **16 model calls per file** (`AGRI_LLM_MAX_CALLS`), a per-purpose cap, and at most **2 recoveries per file**. When the budget is
   spent the run fails with the reason; it does not loop.
4. **Everything is traced.** Every call is a step in the run trajectory (purpose, cached or fresh, seconds, the answer) and, with LangSmith on, a child run with the full prompt.
5. **Cached and switchable.** Answers are cached by prompt hash (`.llm_cache/`), so a repeated file is deterministic and free. `python run.py --no-ai` (or `AGRI_AI=0`) switches the layer off.

## 2. Stage by stage

| # | Stage (graph node) | What the model does | Fixed code gate (when it is asked) | Fixed code check (what it must pass) |
|---|---|---|---|---|
| 1 | **Triage** (`inspect`) | decides the route of an ambiguous text file: csv / prose / html / json / unsupported | magic bytes and a content profile decide almost every file; only a genuinely ambiguous one is asked | route must be one the code can read; unreadable binary / Office / zip files are refused with a reason |
| 2 | **Plan: page selection** (`plan`) | reads a one-line digest per candidate page of a long PDF (first line, table-line count, key words, a sample numeric line; any language) and returns the relevant pages and whether the figures are in a table or in prose | PDFs above 15 pages; every page's text layer is pre-scanned first (about 0.02 s per page) | returned pages must be candidates; at most 40 are read; without a model the deterministic score ranks them |
| 3 | **Plan: sheets** (`plan`) | chooses the relevant sheets of a workbook | 3 or more sheets | names must exist |
| 4 | **Structure repair** (`read`) | given a run-length summary of the **whole** table, returns header lines, data ranges, title / unit lines, rows to drop, and whether the table is transposed or has entities as columns (melt) | the gate fires on a title above the header, a header-like row inside the data, or a stray text row; also forced when no domain is recognised | row numbers must exist, ranges ordered, melt columns valid; the repaired table must contain numbers; original row numbers are kept for provenance |
| 5 | **Recovery** (`recover`) | picks the next strategy from a fixed menu (retry, other reader, force OCR, structure repair, read as prose) | after a failed read (the first retry is always deterministic) or when nothing was recognised or stored | must be on the menu and not yet tried; **2 recoveries maximum** |
| 6 | **Context facts** (`classify`) | reads the title / caption lines and reports units ("thousand hectares"), district, period, crop | only if the table has context lines and its own columns lack the information | every fact needs a **quote that occurs in the context text**; unit and multiplier from a fixed set; district through the reference list; used only where the table gives nothing |
| 7 | **Language** (`classify`) | interprets headers in any language (header prompt), translates district / crop / season / month values and unit words | only values the English lookups cannot resolve | district: one of the 16 or null; month 1-12; unit from a fixed vocabulary that fits the variable; the original text is kept in `extra` |
| 8 | **Prose extraction** (`read`) | extracts production / rainfall / soil facts from sentences, any language | free-text files and PDF pages the table reader could not read | the **verbatim quote must occur in the source** (its character offsets become the locator, `chars 120-260`); **every number must occur in its quote** (numbers written only in words are dropped); identity fields must occur in a Latin-script chunk |
| 9 | **Plausibility** (`load`) | judges only the flagged values: "physically possible?" | only values outside the declared range, physically impossible combinations, or extreme outliers (z > 25 and more than 10x the median) | only `implausible` rows are removed from the data tables and put in `review_queue` (`pending_review`) for a person; **a value is never edited**; with no model nothing is set aside |
| 10 | Header mapping | maps unknown column names to canonical fields (existing step, now through the shared wrapper) | unrecognised headers | the proposal is checked against the data (a column of plain years is the year, not the month) and must make the table a recognisable domain |
| 11 | Classification tie-break | existing step, now through the shared wrapper | rules tie or find nothing | accepted only with the matching measurement columns |

**Decided against (by design):** the model does not invent schemas or domains (the three tables are fixed; a price table fails cleanly instead of being forced
into one), does not "repair" OCR-garbled names, and does not write the run report.

### Why the structure repair is not limited to the first 20 lines
The model sees a **run-length summary of the whole table**: consecutive rows of the same shape (title / header-like / data / mixed) are grouped, short runs are shown
in full and long runs by their first and last rows. A repeated header at line 302 or a note at line 453 of a 600-row table appears as its own run with its text, however
far down it is (tested: `tests/unseen_ai/late_anomaly.csv`, evaluation case `ai_03`). A table of 50,000 rows with one stray line at row 31,207 produces the same size of summary.

## 3. Languages: nothing here knows a language

No word list, dictionary or prompt example in the code is specific to Hindi, Bengali, Manipuri or Tamil. What is generic: script detection from Unicode character names
(`lang.scripts_in`), digits and separators of any script (`readers.to_number`), header keys that keep any script (`concepts.norm_key`), encoding detection (`charset-normalizer`),
and the model's own multilingual ability for headers, names, months and unit words, with closed lists to verify its answers. The evaluation cases cover **Hindi**, **Bengali
script with native digits** (a script used for Manipuri) and **Tamil** (unrelated to Manipur, unknown to the code) in tables, and **Tamil** in prose.
**Not tested: Meitei Mayek** (the script Manipuri also uses): I did not have a sample and did not invent one. Scanned documents in non-Latin scripts also need an OCR model
for that script; the bundled OCR is tuned for Latin text and was not tested on them.

## 4. LangSmith: the path of a file, visually

Add to `.env` (git-ignored; see `.env.example`):
```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your key>
LANGSMITH_PROJECT=manipur-agriagent
```
Every `python run.py --input <file>` then appears in LangSmith as a trace named `agri-ingest: <file>`: the LangGraph nodes in order (`inspect -> plan -> read -> classify -> agents -> load ->
finish`, with every `recover` loop and the routing decisions), each model call as a child run with its prompt and answer, and the decorated helpers (PDF cascade, structure plan,
page selection, prose extraction, plausibility check) as sub-steps. Tags and metadata (file name, AI on / off, injected fault) make runs searchable.
**What was verified here:** the graph emits exactly this event stream (nodes, routers, model calls), checked with a local callback handler; `docs/orchestration.mmd` is a static
diagram of the graph (Mermaid). **What was not verified:** the upload to the LangSmith cloud, because it needs your API key. Note that prompts contain excerpts of the files.

## 5. Results (all numbers: `eval/README.md`, `eval/ai/`)

20 cases for the capabilities above (17 should succeed, 3 must fail cleanly), built from real records of the **held-out districts** in layouts the rule-based reader cannot take.
Three arms, the same files and the same scorer:

| Arm | cases fully correct | field F1 (micro) | impossible values that reached the tables |
|---|---|---|---|
| **before** (the committed pipeline, run from an extract of that commit) | 1 / 17 | 0.477 | 2 of 2 |
| new code, **no model calls** (`--no-ai`: the new deterministic readers for Excel, HTML, images) | 2 / 17 | 0.745 | 2 of 2 |
| **AI layer**, first cold run (gpt-oss-20b) | **16 / 17** | **0.974** | **0 of 2** (both set aside) |
| AI layer, second run (same files, same code) | 15 / 17 | 0.961 | 0 of 2 |
| AI layer, after a fix informed by that second run (*not cold*) | 16 / 17 | 0.974 | 0 of 2 |

All 3 must-fail files failed cleanly within the budget and stored nothing (no invented rows), in every arm. The original 44-case official set still scores field F1 **0.985**
(it was 0.987 before this work; see section 6 for why), and the 44-case dev set 1.000.

## 6. Limits and honest findings

* **The run-to-run variation is real.** Two runs of the same code on the same files gave 16 and 15 fully correct cases: in the second run the model labelled both the month and the
  year column `year_month`. Temperature is 0, but the model is not deterministic. The fix (a column of plain years is the year) is a code check on the model's proposal, added after seeing that
  failure, so the third run is **not** a cold number. Cached answers make a repeated run deterministic, which hides this variation unless the cache is cleared.
* **The model was `gpt-oss-20b` with low reasoning effort**, because the free tier's daily quota for `gpt-oss-120b` (200,000 tokens) was exhausted by development; the 120b model was not re-evaluated.
  Development of the AI set used about 100 model calls a day. The free tier is therefore a real constraint: a full AI evaluation costs tens of thousands of tokens. When the quota is hit the pipeline
  says so in the trace and continues without the model (`ai:* ... model rate limit reached`).
* **The plausibility check set aside real values.** On the original official set it removed one record each from `prod_h01` and `prod_h06` (field F1 1.000 -> 0.985 there): unusual but genuine
  source values. On the full production file it set aside a Bishnupur banana row (117,034 t at 133.6 t/ha, where banana normally gives 30-60 t/ha), which a person should indeed look at.
  Those rows sit in `review_queue`, not lost; but they are not in the data tables, so field F1 (which counts only stored rows) goes down. The judge also called a real 37,000 t sugarcane
  figure implausible until it was shown the rest of the row (area and yield), which is now part of its input.
* **OCR errors remain.** One held-out image case lost one record because a village name was misread ("Part II" read as "Part Il"). No step repairs OCR text (by decision).
* **Page selection was verified on a constructed 60-page report (the only table on page 47)** and run on the two real 476- and 490-page PDFs (4 model calls, about 80 s each), but for the real
  books there is no answer key, so its precision on them is not measured.
* **Cost and latency.** The AI cases took a median of a few seconds per file, up to about 40 s for an OCR image; long PDFs about 80 s for the selection alone. The old reader was instant on the files it could read.
* **Not supported:** legacy `.xls` / `.doc`, `.docx`, archives, audio. They are refused with a clear reason. **Prose** numbers written in words are skipped, not guessed.
