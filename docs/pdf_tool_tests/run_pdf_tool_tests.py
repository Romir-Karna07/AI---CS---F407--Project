"""
PDF tool tests for docs/challenge.md  (PDF Phase 2 rubric: "at least 2 open-source tools actually run on the team's sources, with numbers").

Runs 8 extraction configurations on the 10 real PDFs in Data/pdf_samples/ (all from the Phase 1 inventory) and writes
    docs/pdf_tool_tests/results_accuracy.csv   per tool x ground-truth page: row-exact and field accuracy
    docs/pdf_tool_tests/results_per_file.csv   per tool x file: seconds, text/table volume, errors (5-page window per file)
    docs/pdf_tool_tests/results_summary.csv    per-tool macro numbers used in challenge.md

Accuracy is measured against docs/pdf_tool_tests/ground_truth.json (values read by eye from page images, not by any tool).
A ground-truth ROW is found when some extracted row contains the anchor text; its score is the number of expected numbers that equal the
LAST k numbers of that row, in order. The best candidate row per tool is used (generous to the tool).

Run:  python docs/pdf_tool_tests/run_pdf_tool_tests.py [--skip-docling] [--skip-ocr]
"""
import argparse
import csv
import json
import re
import subprocess
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent.parent
PDFS = ROOT / "Data" / "pdf_samples"
OUT = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--skip-docling", action="store_true")
ap.add_argument("--skip-ocr", action="store_true")
ap.add_argument("--only", default="", help="comma-separated tool names to run (e.g. rapidocr3_lines)")
ap.add_argument("--tag", default="", help="suffix for the result files, so a subset run does not overwrite the full run")
ap.add_argument("--no-window", action="store_true", help="accuracy on the ground-truth pages only; skip the per-file window pass")
args = ap.parse_args()

NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")


def numbers(text):
    out = []
    for m in NUM.finditer(text.replace("−", "-")):
        s = m.group(0)
        s = s.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s) else s.replace(",", ".")
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


def lines_from_words(words):
    """words: (x0, y0, x1, y1, text); group into visual lines by vertical overlap, sort left to right"""
    ws = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines, cur, cy = [], [], None
    for w in ws:
        yc = (w[1] + w[3]) / 2
        h = max(w[3] - w[1], 1)
        if cur and abs(yc - cy) > 0.5 * h:
            lines.append(cur)
            cur = []
        cur.append(w)
        cy = yc if len(cur) == 1 else cy
    if cur:
        lines.append(cur)
    return [" ".join(w[4] for w in sorted(l, key=lambda w: w[0])) for l in lines]


# ------------------------------------------------------------------------------------------------ tools: (pdf, page_no) -> rows
def t_pypdf(pdf, page):
    import pypdf
    p = pypdf.PdfReader(str(pdf)).pages[page - 1]
    return [l for l in (p.extract_text(extraction_mode="layout") or "").splitlines() if l.strip()]


def t_pdftotext(pdf, page):
    r = subprocess.run(["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(pdf), "-"], capture_output=True, timeout=120)
    return [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]


def _plumber(pdf, page, settings):
    import pdfplumber
    with pdfplumber.open(str(pdf)) as d:
        pg = d.pages[page - 1]
        return [" | ".join((c or "").replace("\n", " ") for c in row) for t in pg.extract_tables(settings) for row in t]


def t_pdfplumber_lines(pdf, page):
    return _plumber(pdf, page, {})


def t_pdfplumber_text(pdf, page):
    return _plumber(pdf, page, {"vertical_strategy": "text", "horizontal_strategy": "text"})


def t_pymupdf_tables(pdf, page):
    import pymupdf
    d = pymupdf.open(str(pdf))
    tabs = d[page - 1].find_tables()
    return [" | ".join((c or "").replace("\n", " ") for c in row) for t in tabs.tables for row in t.extract()]


def t_pymupdf_words(pdf, page):
    import pymupdf
    d = pymupdf.open(str(pdf))
    return lines_from_words([w[:5] for w in d[page - 1].get_text("words")])


def _camelot(pdf, page, flavor):
    import camelot
    ts = camelot.read_pdf(str(pdf), pages=str(page), flavor=flavor)
    return [" | ".join(str(c).replace("\n", " ") for c in row) for t in ts for row in t.df.values.tolist()]


def t_camelot_lattice(pdf, page):
    return _camelot(pdf, page, "lattice")


def t_camelot_stream(pdf, page):
    return _camelot(pdf, page, "stream")


_DOCLING = {}


def t_docling(pdf, page):
    from docling.document_converter import DocumentConverter
    if "conv" not in _DOCLING:
        t0 = time.perf_counter()
        _DOCLING["conv"] = DocumentConverter()
        _DOCLING["load_s"] = time.perf_counter() - t0
    res = _DOCLING["conv"].convert(str(pdf), page_range=(page, page))
    doc = res.document
    rows = []
    for t in doc.tables:
        df = t.export_to_dataframe(doc)
        rows.append(" | ".join(str(c) for c in df.columns))
        rows += [" | ".join(str(c) for c in r) for r in df.values.tolist()]
    return rows


_OCR = {}


def t_ocr(pdf, page):
    import numpy as np
    import pymupdf
    from rapidocr_onnxruntime import RapidOCR
    if "ocr" not in _OCR:
        _OCR["ocr"] = RapidOCR()
    d = pymupdf.open(str(pdf))
    pix = d[page - 1].get_pixmap(dpi=200)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    res, _ = _OCR["ocr"](img)
    words = [(min(p[0] for p in box), min(p[1] for p in box), max(p[0] for p in box), max(p[1] for p in box), txt) for box, txt, _ in (res or [])]
    return lines_from_words(words)


def t_ocr3(pdf, page):
    """rapidocr 3.x (PP-OCRv6) - the OCR engine used by pipeline/pdf_reader.py (keeps the spaces inside cells)"""
    import logging
    import numpy as np
    import pymupdf
    from rapidocr import RapidOCR
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    if "ocr3" not in _OCR:
        _OCR["ocr3"] = RapidOCR(params={"Det.unclip_ratio": 1.0})
    d = pymupdf.open(str(pdf))
    pix = d[page - 1].get_pixmap(dpi=200)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    out = _OCR["ocr3"](img)
    words = [(min(p[0] for p in b), min(p[1] for p in b), max(p[0] for p in b), max(p[1] for p in b), t)
             for b, t in zip(out.boxes if out.boxes is not None else [], out.txts or [])]
    return lines_from_words(words)


def t_cascade(pdf, page):
    """the document agent's own cascade (pipeline/pdf_reader.py): pdfplumber ruled tables -> camelot stream, then title-row / shifted-column clean-up"""
    sys.path.insert(0, str(ROOT))
    import pdfplumber
    from pipeline import pdf_reader as pr
    with pdfplumber.open(str(pdf)) as d:
        tabs = [t for t in pr._plumber_tables(d.pages[page - 1]) if pr._usable(t)]
    route = "pdfplumber"
    if not tabs:
        route = "camelot_stream"
        tabs = [t for t in pr._camelot_tables(pdf, page) if pr._usable(t)]
    rows = []
    for t in tabs:
        t, _ = pr.split_title_rows(t)
        if route != "pdfplumber":
            t = pr.merge_shifted_columns(t)
        rows += [" | ".join(r) for r in t]
    return rows


TOOLS = {"pypdf_layout": t_pypdf, "pdftotext_layout": t_pdftotext, "pdfplumber_lines": t_pdfplumber_lines,
         "pdfplumber_text": t_pdfplumber_text, "pymupdf_tables": t_pymupdf_tables, "pymupdf_words": t_pymupdf_words,
         "camelot_lattice": t_camelot_lattice, "camelot_stream": t_camelot_stream, "docling_tableformer": t_docling,
         "rapidocr_lines": t_ocr, "rapidocr3_lines": t_ocr3, "cascade_pdfplumber_then_camelot": t_cascade}
if args.skip_docling:
    TOOLS.pop("docling_tableformer")
if args.skip_ocr:
    TOOLS.pop("rapidocr_lines")
    TOOLS.pop("rapidocr3_lines")
if args.only:
    TOOLS = {k: v for k, v in TOOLS.items() if k in args.only.split(",")}
elif not args.tag:
    TOOLS.pop("rapidocr3_lines")          # the default full run keeps the original tools; the OCR-3 and cascade runs are tagged
    TOOLS.pop("cascade_pdfplumber_then_camelot")


# ------------------------------------------------------------------------------------------------ accuracy against ground truth
def score_row(rows, anchor, expected):
    best = (0, None)
    for r in rows:
        if anchor.lower() not in r.lower():
            continue
        nums = numbers(r)
        got = nums[-len(expected):] if len(nums) >= 1 else []
        got = [None] * (len(expected) - len(got)) + got
        hit = sum(g is not None and abs(g - e) <= 1e-6 * max(1, abs(e)) for g, e in zip(got, expected))
        if hit >= best[0]:
            best = (hit, r)
    return best


gt = json.loads((OUT / "ground_truth.json").read_text(encoding="utf-8"))["items"]
acc_rows, raw = [], {}
for tool, fn in TOOLS.items():
    for item in gt:
        pdf = PDFS / item["file"]
        t0 = time.perf_counter()
        try:
            rows = fn(pdf, item["page"])
            err = ""
        except Exception as e:
            rows, err = [], f"{type(e).__name__}: {str(e)[:120]}"
        secs = time.perf_counter() - t0
        n_fields = sum(len(r["values"]) for r in item["rows"])
        hits = row_exact = row_found = 0
        for r in item["rows"]:
            h, line = score_row(rows, r["anchor"], r["values"])
            hits += h
            row_exact += int(h == len(r["values"]))
            row_found += int(line is not None)
        acc_rows.append({"tool": tool, "file": item["file"], "page": item["page"], "gt_rows": len(item["rows"]), "rows_found": row_found,
                         "rows_exact": row_exact, "gt_fields": n_fields, "fields_correct": hits,
                         "field_accuracy": round(hits / n_fields, 4), "seconds": round(secs, 2), "extracted_rows": len(rows), "error": err})
        raw[(tool, item["file"])] = rows
        print(f"{tool:20} {item['file'][:34]:34} p{item['page']:<4} fields {hits}/{n_fields}  rows exact {row_exact}/{len(item['rows'])}  {secs:5.1f}s {err}", flush=True)

# ------------------------------------------------------------------------------------------------ per-file stability over a 5-page window
WINDOW = 5
win = {i["file"]: (max(1, i["page"] - 2), i["page"] + 2) for i in gt}
file_rows = []
import pypdf
for pdf in ([] if args.no_window else sorted(PDFS.glob("*.pdf"))):
    n = len(pypdf.PdfReader(str(pdf)).pages)
    a, b = win.get(pdf.name, (1, min(n, WINDOW)))
    b = min(b, n)
    for tool, fn in TOOLS.items():
        if tool == "rapidocr_lines" and n > 0 and pdf.name not in win and False:
            continue
        t0 = time.perf_counter()
        tot_rows = tot_num = 0
        err = ""
        for p in range(a, b + 1):
            try:
                rows = fn(pdf, p)
                tot_rows += len(rows)
                tot_num += sum(len(numbers(r)) for r in rows)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:100]}"
        secs = time.perf_counter() - t0
        file_rows.append({"tool": tool, "file": pdf.name, "pages_total": n, "window": f"{a}-{b}", "rows": tot_rows, "numeric_tokens": tot_num,
                          "seconds": round(secs, 2), "sec_per_page": round(secs / (b - a + 1), 2), "error": err})
        print(f"[window] {tool:20} {pdf.name[:40]:40} {a}-{b}: rows={tot_rows:5} nums={tot_num:6} {secs:6.1f}s {err}", flush=True)

SUF = f"_{args.tag}" if args.tag else ""
for name, rows in ((f"results_accuracy{SUF}.csv", acc_rows), (f"results_per_file{SUF}.csv", file_rows)):
    if not rows:
        continue
    with (OUT / name).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

summary = []
for tool in TOOLS:
    a = [r for r in acc_rows if r["tool"] == tool]
    f = [r for r in file_rows if r["tool"] == tool] or [{"error": "", "sec_per_page": r["seconds"], "seconds": r["seconds"]} for r in a]
    summary.append({"tool": tool,
                    "field_accuracy_micro": round(sum(r["fields_correct"] for r in a) / sum(r["gt_fields"] for r in a), 4),
                    "field_accuracy_macro_pages": round(sum(r["field_accuracy"] for r in a) / len(a), 4),
                    "rows_exact": f"{sum(r['rows_exact'] for r in a)}/{sum(r['gt_rows'] for r in a)}",
                    "pages_all_fields_correct": f"{sum(r['fields_correct'] == r['gt_fields'] for r in a)}/{len(a)}",
                    "files_with_error": sum(bool(r["error"]) for r in f), "median_sec_per_page": sorted(r["sec_per_page"] for r in f)[len(f) // 2],
                    "total_seconds_window": round(sum(r["seconds"] for r in f), 1)})
for r in summary:
    pass
with (OUT / f"results_summary{SUF}.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(summary[0]))
    w.writeheader()
    w.writerows(summary)
print()
for s in summary:
    print(s)
if "docling_tableformer" in TOOLS and "load_s" in _DOCLING:
    print("docling model load seconds (one-off):", round(_DOCLING["load_s"], 1))
