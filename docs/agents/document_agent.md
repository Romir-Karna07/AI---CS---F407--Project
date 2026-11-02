# document_agent

| | |
|---|---|
| **Tables** | none of its own: it produces the same neutral records as the CSV / JSON readers, and the soil / rainfall / production agents store them in `soil_health`, `rainfall_monthly`, `crop_production` |
| **Sources** | the 17 PDF-first inventory sources (S05, S08-S12, S14, S19, S20, S25, S27, S30, S31, S45, S50, S53, S54). **No inventory source is marked done or partial yet** (see Limitations) |
| **Source group** | Document (PDF with a text layer, scanned PDF) |
| **Code** | `pipeline/pdf_reader.py` (`read_pdf_file`), `tools/pdf_tool.py` (`read_pdf_data`), graph nodes `read` / `recover` in `pipeline/graph.py` |
| **Evidence** | `docs/challenge.md` (tool comparison), `eval/cases/pdf_*.pdf` + `eval/reference/pdf_*.json` (10 held-out cases), `eval/dev/cases/pdf_*.pdf` (10 dev twins) |

## Responsibility
Read the **tables** in a PDF and hand them to the existing pipeline: `inspect -> read_pdf_data -> classify -> {soil|rainfall|production}_extract -> sqlite_load`.
Each table of each page becomes one record group; each row keeps a locator such as `p53 t1 line 7, cols B-F` (page 53, table 1 on that page, row 7), so a
stored value can be opened at its page.

## Inputs -> outputs
* In: a `.pdf` (detected by extension or the `%PDF-` header). Reads the first 40 pages by default; `AGRI_PDF_PAGES=53-60` (or `read_pdf_data(..., pages="53-60")`)
  reads exactly those.
* Out: records in the same structure as `read_csv_data` / `read_json_data` (`locator`, `data`, `labels`, `units`). Everything after that (column meaning, units,
  normalisation, validation, loading) is the shared code, unchanged.

## Tool cascade (per page; chosen from the numbers in `docs/challenge.md`)
| Step | Tool | Used when | Why |
|---|---|---|---|
| 1 | **pdfplumber**, ruled-table detection | the page has a text layer and a table drawn with ruling lines | exact on ruled tables, fastest of the table tools |
| 2 | **camelot** "stream" | no ruled table: tables laid out with whitespace, **rotated pages** | the only tool that read the rotated yearbook table (S04 p161, 40/40 fields) |
| 3 | **RapidOCR** (PP-OCRv6) + word clustering (`tables_from_boxes`) | no text layer (scan), or `backend="ocr"` | keeps the spaces inside cells, which the older 1.x engine did not |
A page accepts the first route that returns a table with >= 2 rows, >= 2 columns and >= 2 numeric cells. Post-processing, all fixed code:
`split_title_rows` (a printed title returned as a table row), `merge_shifted_columns` (a right-aligned number column split away from its left-aligned header),
caption labels (a district and a period read from the text above the table, used when the table has no such column), header carry-over for a table that continues
on the next page without a header.

## Decisions: fixed code vs model
| Decision | Made by |
|---|---|
| is this a PDF; which route reads a page; where the tables and their columns are | fixed code (`pdf_reader.py`) |
| what a column means, units, district / crop / period normalisation, validation | the shared fixed code (`concepts.py`, `normalize.py`, `schemas.py`) |
| a header the dictionary cannot map (English or Hindi header words are handled by the English token; other languages are not) | **LLM** header mapper, exactly as for CSV / JSON, gated the same way |
| the OCR text itself | the OCR model (RapidOCR); no language model reads the PDF |

## Tools (as logged in the trajectory)
`inspect_file` -> `read_pdf_data` (recovery: retry, then `read_pdf_ocr`) -> (`llm_map_headers`) -> `soil_extract` / `rainfall_extract` / `production_extract` -> `sqlite_load`.

## Evaluation (see `eval/README.md`; held-out districts, cold run)
10 PDF cases, 1,216 extracted-or-reference fields: **F1 0.947**. Seven files at 1.000. The two scans score 0.933 and 0.957 (OCR misread one digit and one district name);
the physically rotated page scores 0.629 (December column lost, some district cells blank). M6 0.927, M8 0.900, **M9 0.700**. The reader was developed on the dev twins
(10 / 10 at F1 1.0 after development) and on the real PDFs, which is why the held-out result is lower: it is the honest number.

## Limitations
* **No real PDF source is loaded.** The 10 real PDFs from the inventory were run through the pipeline (`logs/automation_log.csv`, 8 rows marked as manual because the page
  range was chosen by hand): most tables hold data outside the three domains (population, ARIMA statistics, banking profile, land-use and irrigation tables) and the run
  fails cleanly with "no recognised ... records"; the DACP "normal rainfall" table is classified as rainfall but is seasonal, not monthly, so all rows are rejected, and the
  run still reports `complete`; one DACP land-use table was misclassified as production and stored as `invalid` rows (district UNSPECIFIED).
* Key / value tables with units inside the cell text ("22,327 sq. km") are not read as numbers.
* A table that spans two pages loses nothing only if the second page has the same column count; multi-row headers are not merged across pages.
* OCR: digits and similar names can be misread (606 vs 909; "Thbal" for Thoubal). The page-level provenance check for scans is partly circular (it re-reads the page with the same OCR).
* **Manipuri script (Meitei Mayek / Bengali script) is not handled**; Hindi headers work only because the English word beside them is mapped. No handwriting, no skewed scans, no figures.
* The 17 PDF-first inventory sources are mostly narrative text (reports, circulars, journals); extracting facts from prose needs an LLM extractor and an adapter comparison (M18), which does not exist.
* Speed: OCR is about 25 s per page on this CPU; camelot about 1-4 s per page; the 400-page books are read only over a chosen page range.
