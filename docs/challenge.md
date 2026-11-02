# Challenge note: getting numbers out of PDFs

*Team 28, Manipur. Written 2026-11-01. Everything marked "measured" can be re-run: `python docs/pdf_tool_tests/run_pdf_tool_tests.py` (the result files are next to it) and `python eval/run_eval.py`.*

## 1. The challenge

Of the 54 inventory sources, **17 are PDF-first** (state and national statistical publications, district contingency plans, bank and ministry reports, a journal paper) and 16 more
list PDF among several formats (33 mention PDF in all). The CSV / JSON agents cannot read any of them. A PDF stores glyphs at positions, not a table: whether the page *has* a table, where its columns are, and which
number belongs under which header must be reconstructed. The documents in our own inventory vary on exactly the axes that break extractors:

| Property seen in our real PDFs | Example (page) |
|---|---|
| table drawn with ruling lines | District Agriculture Contingency Plan, Imphal West, p2 (S31) |
| table with **no** ruling lines, columns only implied by spacing | Horticulture Statistics at a Glance 2018, Table 3.1, p53 (S06) |
| **rotated** page (the whole table turned 90 degrees) | Agricultural Statistics at a Glance 2024-25, Table 2.28 yield by state, p161 (S04) |
| **bilingual** English + Hindi, dense, coloured cells, 9 tables on one page | NABARD State Focus Paper Manipur 2024-25, banking profile, p33 (S12) |
| merged header cells, negative numbers | journal table of ARIMA statistics, p10 (S53) |
| key / value table with units inside the cell ("22,327 sq. km") | MoFPI Manipur state profile, p4 (S54) |
| scrambled text layer (columns interleaved) | all-India final estimates, p1 (S05) |
| scan with no text layer | not in the 10 downloads; the PDF's evaluation protocol says to expect "a scanned table" |

The instructor's three surprise documents will be drawn from our source types and one will be in the state's language. So the question is not which tool is best on average but
**which tool, in which order, survives all of these layouts** and where it must admit defeat.

What is *not* in scope here: reading prose (reports, circulars, the journal text) to extract facts, which needs an LLM extractor; Manipuri script; figures.

## 2. What the literature covers, and what is research vs our implementation

| Topic | What the literature / tool documentation covers | What it does **not** settle for us |
|---|---|---|
| Table **structure recognition** by deep models | *PubTables-1M* (Smock et al., CVPR 2022): about a million tables from PubMed Central articles with row / column / cell annotations; DETR-style models trained on it. *TableFormer* (Nassar et al., CVPR 2022): a transformer that predicts cell structure and content boxes together, built for partial or missing borderlines, empty cells, spans and hierarchical headers. | Both are trained and scored on **scientific / financial** tables. Whether they transfer to a government statistical yearbook, a rotated page or a Hindi-English page is an open, *research* question; only our own run (section 4) answers it for our pages. |
| End-to-end open-source converter | *Docling Technical Report* (Auer et al., arXiv 2408.09869, 2024): layout detector (RT-DETR family, trained on DocLayNet) + TableFormer + optional OCR, on CPU or GPU. | The report quotes speed on its own hardware; **our** CPU-only laptop took a median 48 s per page. That is an implementation / hardware limit, not a property of the method. |
| Rule-based table extraction | Camelot's two modes: *Lattice* finds ruling lines with image processing; *Stream* looks for text alignment and guesses the column count (the Stream detector follows Nurminen's 2013 Tampere thesis, *Algorithmic extraction of data in tables in PDF documents*). pdfplumber and PyMuPDF `find_tables` use ruling lines or text-position heuristics in the same family. | The documentation lists the limits (Stream treats the page as one table; whitespace guesses fail on dense pages). How often that bites on **our** pages is measured, not read. |
| OCR | PP-OCR (Du et al., 2020), packaged as RapidOCR (ONNX): text detection + recognition. Hegghammer (2022, *J. Comput. Social Science*) benchmarks Tesseract, Textract and Document AI on English and Arabic scans: server OCR beats Tesseract, worst on noisy scans, English far above Arabic. | No OCR benchmark covers Meitei Mayek or Bengali-script Manipuri tables. Our OCR tests are English / Hindi-header only. |
| Layout-aware pre-trained models (LayoutLMv3, Nougat ...) | Used to read whole pages into fields or markup. | **Not tested.** Needs a GPU and labelled Manipur documents; listed as the Phase 3 route if rules plateau. |

*Citations were checked against the arXiv, CVF open-access and Springer pages; the Nurminen thesis was confirmed from its repository abstract and Camelot's documentation, not read in full.*

**Research limitation vs ours, kept apart.** Research limits: deep table models are validated on other kinds of tables; OCR benchmarks do not include Manipuri script; no published number tells us how
Camelot Stream or TableFormer behave on Indian statistical yearbooks. **Our implementation limits:** only CPU was available (Docling slow); we did not train or fine-tune any model; the ground truth is six pages
checked by eye (small); the OCR check for scans re-reads the page with the same OCR (partly circular, see section 5).

## 3. Tools tested

Ten configurations, all open source, all actually run on the real PDFs in `Data/pdf_samples/` (not committed to the repository: re-download them from the URLs in `Data/manifest.csv`) (ten files, every one from the Phase 1 inventory: S04 ×1, S05 ×2, S06, S12, S31 ×3, S53, S54).

| # | Tool (configuration) | Kind |
|---|---|---|
| 1 | **pypdf 6.x**, `extraction_mode="layout"` | text layer, line-based |
| 2 | **poppler `pdftotext -layout`** | text layer, line-based |
| 3 | **pdfplumber 0.11**, default (ruled-table) strategy | table detector, needs ruling lines |
| 4 | **pdfplumber**, text strategy | table detector, whitespace |
| 5 | **PyMuPDF 2.0** `find_tables()` | table detector |
| 6 | **PyMuPDF** words grouped into visual lines (own 15-line helper) | text layer, geometry |
| 7 | **Camelot 2.0 lattice** | table detector, ruled |
| 8 | **Camelot 2.0 stream** | table detector, whitespace |
| 9 | **Docling 2.145** (layout model + TableFormer, CPU) | deep model |
| 10 | **RapidOCR** on the page rendered at 200 dpi + words grouped into lines (two engines: `rapidocr-onnxruntime` 1.4.4 and `rapidocr` 3.10 / PP-OCRv6) | OCR |

### Ground truth
Six pages, one per layout type, with **119 numbers** read by eye from rendered page images (not by any tool): `docs/pdf_tool_tests/ground_truth.json`
(S06 p53, S53 p10, S31 Imphal West p2, S54 p4, S04 p161 rotated, S12 p33). A row counts as found when some extracted row contains the anchor text ("Manipur", "Gross cropped area" ...);
its score is how many of the expected numbers equal the **last k numbers of that row, in order**. The best candidate row per tool is taken (generous to the tool). Total: 119 numbers in 23 rows (S06 20, S53 24, S31 16, S54 5, S04 40, S12 14).

### Result 1: accuracy on the six ground-truth pages (measured)

| Tool | Numbers correct | Rows exact | S06 p53 unruled | S53 p10 journal | S31 p2 ruled | S54 p4 key/value | S04 p161 rotated | S12 p33 dense bilingual |
|---|---|---|---|---|---|---|---|---|
| pypdf layout | 78/119 = 0.655 | 20/23 | 20/20 | 24/24 | 15/16 | 5/5 | 0/40 | 14/14 |
| pdftotext -layout | 17/119 = 0.143 | 6/23 | 9/20 | 0/24 | 4/16 | 3/5 | 0/40 | 1/14 |
| pdfplumber (ruled) | 69/119 = 0.580 | 19/23 | 10/20 | 24/24 | 16/16 | 5/5 | 0/40 | 14/14 |
| pdfplumber (text) | 48/119 = 0.403 | 10/23 | 20/20 | 21/24 | 2/16 | 5/5 | 0/40 | 0/14 |
| PyMuPDF find_tables | 13/119 = 0.109 | 1/23 | 12/20 | 0/24 | 0/16 | 1/5 | 0/40 | 0/14 |
| PyMuPDF words to lines | 78/119 = 0.655 | 20/23 | 20/20 | 24/24 | 15/16 | 5/5 | 0/40 | 14/14 |
| Camelot lattice | 59/119 = 0.496 | 17/23 | 0/20 | 24/24 | 16/16 | 5/5 | 0/40 | 14/14 |
| **Camelot stream** | **104/119 = 0.874** | 20/23 | 20/20 | 24/24 | 15/16 | 5/5 | **40/40** | **0/14** |
| Docling (TableFormer) | 75/119 = 0.630 | 17/23 | 20/20 | 24/24 | 12/16 | 5/5 | 0/40 | 14/14 |
| RapidOCR 1.4 on page image | 61/119 = 0.513 | 17/23 | 20/20 | 14/24 | 15/16 | 5/5 | 0/40 | 7/14 |
| RapidOCR 3.10 (PP-OCRv6) on page image | 78/119 = 0.655 | 20/23 | 20/20 | 24/24 | 15/16 | 5/5 | 0/40 | 14/14 |
| **Cascade used by the agent** (pdfplumber ruled, else Camelot stream, + clean-up) | **114/119 = 0.958** | 18/23 | 20/20 | 24/24 | 16/16 | **0/5** | 40/40 | 14/14 |

What the table shows:
* **No single tool covers all layouts.** The best single tool (Camelot stream) fails the dense bilingual page completely (0/14: it returned four "tables", two of them two-column dumps of running text with a whole column of numbers in one cell) yet is the *only* tool that reads the rotated page;
  pdfplumber's ruled mode is exact on ruled pages and wrong on the unruled one (10/20); Docling's TableFormer was exact on four of six pages, **failed on the rotated page (0/40)** and was the slowest.
* Line-oriented text tools (`pdftotext`, PyMuPDF `find_tables`) are poor on these pages; the PyMuPDF *words* helper, which uses geometry, matches pypdf.
* **Combining the two best-complementary tools gives 0.958**, higher than any single tool. The one failing page is the key / value table whose cells hold units in the text ("22,327 sq. km"): no tool
  reads those as numbers and the cascade returns no table, which is the correct refusal for our tables but a miss against this ground truth.
* The cascade was assembled *after* seeing these six pages, so its 0.958 is optimistic as a prediction. The held-out PDF evaluation (section 5) is the unbiased test.

### Result 2: speed and robustness (measured, 5-page window per file, 10 files, CPU only, other jobs running)

| Tool | Median s / page | Slowest page | Seconds for all ~48 pages | Crashes / errors |
|---|---|---|---|---|
| PyMuPDF words to lines | 0.03 | 0.1 | 2 | 0 |
| pdftotext -layout | 0.07 | 0.4 | 5 | 0 |
| Camelot stream | 0.27 | 3.5 | 39 | 0 |
| pypdf layout | 0.39 | 1.9 | 31 | 0 |
| pdfplumber (ruled) | 0.84 | 2.9 | 52 | 0 |
| pdfplumber (text) | 0.88 | 4.9 | 63 | 0 |
| Camelot lattice | 1.14 | 6.9 | 84 | 0 |
| PyMuPDF find_tables | 1.58 | 3.5 | 73 | 0 |
| RapidOCR (page rendered at 200 dpi) | 21.3 | 51.5 | 1,163 | 0 |
| Docling (CPU) | 47.6 | 98.9 | 2,162 | 0 |

Docling's first call also downloaded and loaded its models (the very first page took 197 s against a 48 s median). No tool crashed on any of the ten files (the 490-page and 476-page books were only read over a 5-page window). The cascade used by the agent costs roughly the sum of
the first two rows it needs: about 0.3-1 s per text page; OCR (about 25 s per page here) only runs on pages without a text layer or after a failed read.

### Result 3: the two OCR engines on scans (measured on the dev PDFs `eval/dev/cases/pdf_04.pdf`, `pdf_10.pdf`: image-only, light noise, JPEG)

| Engine | Dev scan production F1 | Dev scan soil F1 | What went wrong |
|---|---|---|---|
| `rapidocr-onnxruntime` 1.4.4 | 0.700 | 0.592 | drops the spaces inside cells ("Imphal East" -> "ImphalEast", village "Wangoo Ahallup" -> "WangooAhallup"), so crop and village names no longer match |
| `rapidocr` 3.10 (PP-OCRv6) | **1.000** | **1.000** | keeps spaces; chosen for the pipeline |

## 4. Decision and how it is built into the project

`pipeline/pdf_reader.py` (document_agent), exposed as the LangChain tool `tools/pdf_tool.py::read_pdf_data` and as the `read_pdf_data` step of the LangGraph graph, per page:

1. **pdfplumber ruled tables**: exact where ruling lines exist and fast;
2. else **Camelot stream**: whitespace tables and **rotated pages**, then `split_title_rows` (the printed title returned as a row) and `merge_shifted_columns` (a right-aligned number column detached from its header);
3. else, or when the page has no text layer, **RapidOCR 3.x** with `tables_from_boxes` (rows from vertical position, columns from merged x-extents).

Rejected, with the reason from our own numbers: *Docling* (0/40 on the rotated page, median 48 s per page on CPU, large dependency; kept as a measured alternative, not in the pipeline), *Camelot lattice* (0/20 on the unruled page),
*PyMuPDF find_tables* (0.109), *pdftotext* (0.143). A table that spans two pages gets the previous page's header when its first row is data. A caption above a table ("... Thoubal district, 2019-20")
becomes a district and period label so tables that do not repeat them still get a location and time. Everything after reading is the shared code: the unchanged classifier, agents, validation and database.

**Who decides what.** The PDF route is fixed code and open-source models (OCR, no language model). The LLM header mapper is consulted only exactly as for CSV / JSON (unknown headers, gated).
**Recovery.** A failed PDF read is retried once, then re-read with OCR forced on every page (`read_pdf_ocr`); verified by the injected-fault smoke test.

## 5. What the evaluation shows

Evaluation set: 10 PDFs from the held-out districts (rendered with reportlab: ruled multi-page, unruled with caption context, bilingual Hindi headers, rotated page, two scans, wide months, ...),
built and scored like the CSV / JSON cases (`eval/README.md`), reader developed only on the dev twins and on the real PDFs, held-out files run once.

| | |
|---|---|
| Field F1 over the 10 held-out PDFs (macro of 4 agents includes it) | **0.947** (1,110 of 1,216 fields) |
| Files at F1 = 1.000 | 7 of 10 |
| Scans (OCR) | 0.933 (a digit misread, "Thbal" for Thoubal) and 0.957 ("Thbal") |
| Rotated unruled page | 0.629: Camelot cut the December column off and some district cells came back blank |
| Task completion (every reference record present) | 7 of 10 |

**Hard / error cases shown live:** the rotated page, a scan, the bilingual-header file, the corrupt PDF (fails cleanly after the OCR route; `tests/smoke_unseen.py`), an injected read fault (recovers).

**The real PDFs through the whole pipeline** (8 runs, logged in `logs/automation_log.csv` as manual because the page range was chosen by hand):

| PDF, pages | Outcome |
|---|---|
| S06 p53 population table | `failed`: table read correctly, but it is not soil / rainfall / production data ("no recognised records") |
| S53 p10 ARIMA statistics | `failed`: same |
| S04 p161 yield by state, rotated | `failed`: table read (17 rows), not a domain table with a district |
| S12 p33 banking profile | `failed`: same |
| S05 pp1-3 all-India estimates | `failed`: scrambled text layer, not a domain table |
| S54 p4 Manipur overview | `failed` after the OCR recovery route: values carry text units, no numeric table |
| S31 Imphal West pp1-4 | `complete` but **0 rows stored**: the "normal rainfall" table was classified as rainfall, yet it is seasonal, not monthly, so every row was rejected |
| S31 Chandel pp1-6 | `complete`, 3 rows stored **`invalid`** (district UNSPECIFIED): a land-use table was misclassified as production; validation flagged it |

**Honest limits:** the PDF tables our inventory actually holds are mostly **outside the three tables**, so no inventory source is marked done or partial for the document agent; the two `complete` runs show the
status can say "complete" while storing nothing or only invalid rows (a weakness in the shared loader, not fixed); the page-level provenance check for scans re-reads the page with the same OCR (partly circular);
no Manipuri-script text; the ground truth is small (119 numbers, six pages). Evidence for each: `docs/pdf_tool_tests/`, `docs/agents/document_agent.md`, `eval/results/first_run_with_pdf_metrics.csv`.
