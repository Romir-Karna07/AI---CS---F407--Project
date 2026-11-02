"""
PDF reader (document_agent's tool). Turns a PDF into the SAME neutral structure the CSV / JSON readers produce, so the unchanged
classifier and the soil / rainfall / production agents take it from there:

    {"format": "pdf", "encoding": "pdf", "groups": [Group, ...], "notes": [...]}      Group = one table of one page
    Row locator: "p53 t1 line 7, cols B-F"   (page 53, table 1 on that page, row 7 of that table)

Tool cascade, chosen from the numbers in docs/challenge.md (docs/pdf_tool_tests/):
    1. pdfplumber, ruled-table detection        - exact on tables drawn with ruling lines
    2. camelot "stream"                         - tables laid out with whitespace only, and rotated pages
    3. RapidOCR + word clustering               - pages with no text layer (scans)
A page uses the first tool that yields a table with >= 2 rows, >= 2 columns and >= 2 numeric cells. backend="ocr" forces step 3
for every page (used by the graph's recovery step when the text-layer route failed).

Fixed code decides everything here: no language model is involved. Captions above a table ("... Thoubal district - 2019-20") are
turned into district / period labels so a table that does not repeat them in a column still gets a location and a period.
"""
import os
import re
import unicodedata
import warnings
from pathlib import Path

from .normalize import _districts
from .readers import YEAR_RE, is_missing, parse_rows, to_number

warnings.filterwarnings("ignore")
MAX_PAGES = int(os.getenv("AGRI_PDF_MAX_PAGES", "40"))      # default: read at most the first 40 pages
PAGE_RANGE = os.getenv("AGRI_PDF_PAGES", "")               # optional "53-60" or "161": read exactly these pages (a tool input)
OCR_DPI = 200

_NAMES = []
for _d in _districts["districts"]:
    for _n in [_d["name"], *_d["aliases"]]:
        _NAMES.append((re.compile(r"(?<![A-Z])" + re.escape(_n.upper()).replace(r"\-", "[ -]").replace(r"\ ", "[ -]") + r"(?![A-Z])"), _d["name"]))
_PERIOD = re.compile(r"((?:19|20)\d{2})\s*[-–/]\s*((?:19|20)?\d{2})(?!\d)")


# ----------------------------------------------------------------------------- helpers
def _clean(c) -> str:
    return re.sub(r"\s+", " ", str(c).replace("\n", " ")).strip() if c is not None else ""


def _n_numeric(rows) -> int:
    return sum(1 for r in rows for c in r if to_number(c) is not None)


def _usable(rows) -> bool:
    return len(rows) >= 2 and max((len(r) for r in rows), default=0) >= 2 and _n_numeric(rows) >= 2


def caption_labels(lines) -> list:
    """district / period found in the text printed above a table -> labels understood by the domain agents."""
    text = " ".join(lines)
    labels = []
    up = text.upper()
    for rx, name in _NAMES:
        if rx.search(up):
            labels.append(("caption", name))
            break
    m = _PERIOD.search(text)
    if m:
        a, b = m.group(1), m.group(2)
        labels.append(("caption", f"{a}-{b[-2:]}"))
    else:
        m = YEAR_RE.search(text)
        if m:
            labels.append(("caption", m.group(1)))
    return labels


def split_title_rows(table):
    """Whitespace-table detectors often return the printed title as the first row(s) (one filled cell, the rest empty).
    -> (table without them, their text); the text still feeds the caption labels."""
    k = 0
    while k < len(table) - 2 and sum(bool(c) for c in table[k]) <= 1 and len(table[k]) >= 3:
        k += 1
    return table[k:], [c for row in table[:k] for c in row if c]


def merge_shifted_columns(table):
    """Whitespace-table detectors split a right-aligned number column away from its left-aligned header: the header sits in column k,
    the values in column k+1 (or the other way round). Merge two neighbouring columns when exactly one of them has a header cell and
    no data row has a value in both - a conservative test that leaves genuine columns (two headers) alone."""
    t = [list(r) for r in table]
    j = 0
    while t and j < len(t[0]) - 1:
        head = (bool(t[0][j]), bool(t[0][j + 1]))
        both = any(r[j] and r[j + 1] for r in t[1:])
        if head in ((True, False), (False, True)) and not both and any(r[j] or r[j + 1] for r in t[1:]):
            for r in t:
                r[j] = (r[j] + " " + r[j + 1]).strip()
                del r[j + 1]
        else:
            j += 1
    return t


def _word_lines(words):
    """words: (x0, y0, x1, y1, text) -> lines of word tuples (top to bottom, left to right)"""
    ws = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines, cur, cy = [], [], None
    for w in ws:
        yc, h = (w[1] + w[3]) / 2, max(w[3] - w[1], 1)
        if cur and abs(yc - cy) > 0.55 * h:
            lines.append(cur)
            cur = []
        if not cur:
            cy = yc
        cur.append(w)
    if cur:
        lines.append(cur)
    return [sorted(l, key=lambda w: w[0]) for l in lines]


# ----------------------------------------------------------------------------- OCR route: boxes -> table
def _ocr_words(page_pixmap):
    """-> [(x0, y0, x1, y1, text)] in pixel coordinates. rapidocr 3.x (PP-OCRv6) is preferred: it keeps the spaces inside multi-word
    cells ('Imphal East'); rapidocr-onnxruntime 1.x drops them. det unclip 1.0: the default 1.6 grows boxes until neighbouring columns fuse."""
    import numpy as np
    img = np.frombuffer(page_pixmap.samples, dtype=np.uint8).reshape(page_pixmap.height, page_pixmap.width, page_pixmap.n)[:, :, :3]
    try:
        import logging
        from rapidocr import RapidOCR
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)
        if not hasattr(_ocr_words, "engine3"):
            _ocr_words.engine3 = RapidOCR(params={"Det.unclip_ratio": 1.0})
        out = _ocr_words.engine3(img)
        items = [(b, t) for b, t in zip(out.boxes if out.boxes is not None else [], out.txts or [])]
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR as RapidOCR1
        if not hasattr(_ocr_words, "engine1"):
            _ocr_words.engine1 = RapidOCR1(det_unclip_ratio=1.0)
        res, _ = _ocr_words.engine1(img)
        items = [(b, t) for b, t, _ in (res or [])]
    return [(min(p[0] for p in box), min(p[1] for p in box), max(p[0] for p in box), max(p[1] for p in box),
             unicodedata.normalize("NFKC", str(txt))) for box, txt in items]   # OCR returns full-width brackets etc.


def tables_from_boxes(words):
    """Rebuild tables from positioned text boxes (OCR output). Rows = visual lines; a line with >= 2 cells is a table row; columns come
    from merging the x-extent of all cells of a table; cells are assigned to the column they overlap most.
    -> ([table rows], caption lines before each table)"""
    lines = _word_lines(words)
    out, cur, cap, pending_cap = [], [], [], []
    heights = sorted(w[3] - w[1] for l in lines for w in l)
    med_h = heights[len(heights) // 2] if heights else 10

    def cells_of(line):
        cells, last = [], None
        for w in line:
            if last is not None and w[0] - last[2] <= 0.15 * med_h:       # (almost) touching boxes: one cell; the OCR already keeps 'Imphal East' in one box
                last = (last[0], min(last[1], w[1]), w[2], max(last[3], w[3]), last[4] + " " + w[4])
                cells[-1] = last
            else:
                last = w
                cells.append(w)
        return cells

    def flush():
        nonlocal cur
        if len(cur) >= 2:
            ivs = sorted((c[0], c[2]) for row in cur for c in row)
            cols = []
            for a, b in ivs:
                if cols and a <= cols[-1][1]:
                    cols[-1][1] = max(cols[-1][1], b)
                else:
                    cols.append([a, b])
            table = []
            for row in cur:
                cells = [""] * len(cols)
                for c in row:
                    k = max(range(len(cols)), key=lambda i: min(c[2], cols[i][1]) - max(c[0], cols[i][0]))
                    cells[k] = (cells[k] + " " + c[4]).strip()
                table.append(cells)
            out.append((table, pending_cap[:]))
        pending_cap.clear()
        cur = []

    prev_bottom = None
    for line in lines:
        cells = cells_of(line)
        top = min(w[1] for w in line)
        if prev_bottom is not None and top - prev_bottom > 2.2 * med_h:
            flush()
            pending_cap.clear()
        if len(cells) >= 2:
            cur.append(cells)
        else:
            if cur:
                flush()
            pending_cap.append(" ".join(w[4] for w in line))
            del pending_cap[:-4]
        prev_bottom = max(w[3] for w in line)
    flush()
    return out


# ----------------------------------------------------------------------------- text-layer routes
def _plumber_tables(pdf_page):
    return [[[_clean(c) for c in row] for row in t] for t in pdf_page.extract_tables()]


def _camelot_tables(path, page_no):
    import camelot
    return [[[_clean(c) for c in row] for row in t.df.values.tolist()] for t in camelot.read_pdf(str(path), pages=str(page_no), flavor="stream")]


def _page_lines_text(fitz_page):
    return [" ".join(w[4] for w in l) for l in _word_lines([w[:5] for w in fitz_page.get_text("words")])]


def _caption_for(table, page_lines):
    first = next((c.lower() for c in table[0] if c), "")
    for i, ln in enumerate(page_lines):
        if first and first in ln.lower():
            return page_lines[max(0, i - 4):i]
    return page_lines[:3]


# ----------------------------------------------------------------------------- public reader
def read_pdf_file(path, backend: str = "auto", max_pages: int | None = None, pages: str | None = None) -> dict:
    import pdfplumber
    import pymupdf
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    limit = max_pages or MAX_PAGES
    fdoc = pymupdf.open(str(path))
    n_pages = len(fdoc)
    if n_pages == 0:
        raise ValueError("PDF has no pages")
    notes = [f"pages={n_pages}", f"backend={backend}"]
    spec = (pages or PAGE_RANGE).strip()
    if spec:
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", spec)
        if not m:
            raise ValueError(f"bad page range {spec!r}; use '53-60' or '161'")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        page_list = [p for p in range(first, last + 1) if 1 <= p <= n_pages]
        if not page_list:
            raise ValueError(f"page range {spec!r} is outside the document ({n_pages} pages)")
        notes.append(f"page range {spec}")
    else:
        page_list = list(range(1, min(n_pages, limit) + 1))
        if n_pages > limit:
            notes.append(f"only the first {limit} of {n_pages} pages were read (AGRI_PDF_MAX_PAGES)")
    groups, route_counts, last_header = [], {}, None
    with pdfplumber.open(str(path)) as pl:
        for pno in page_list:
            fpage = fdoc[pno - 1]
            has_text = len(fpage.get_text().strip()) >= 20
            found, route, captions = [], None, []
            if has_text and backend != "ocr":
                lines = _page_lines_text(fpage)
                try:
                    cand = [t for t in _plumber_tables(pl.pages[pno - 1]) if _usable(t)]
                    if cand:
                        route = "pdfplumber"
                except Exception:
                    cand = []
                if not cand:
                    try:
                        cand = [t for t in _camelot_tables(path, pno) if _usable(t)]
                        if cand:
                            route = "camelot_stream"
                    except Exception:
                        cand = []
                found = cand
                captions = [_caption_for(t, lines) for t in cand]
            if not found and (not has_text or backend == "ocr"):
                try:
                    ocr = tables_from_boxes(_ocr_words(fpage.get_pixmap(dpi=OCR_DPI)))
                except Exception as e:
                    raise RuntimeError(f"OCR failed on page {pno}: {e}")
                found = [t for t, _ in ocr if _usable(t)]
                captions = [c for t, c in ocr if _usable(t)]
                route = "ocr" if found else route
            for k, (table, cap) in enumerate(zip(found, captions), 1):
                table, title = split_title_rows(table)
                if route != "pdfplumber":
                    table = merge_shifted_columns(table)
                cap = title + list(cap)
                if _n_numeric(table[:1]) >= max(1, len(table[0]) // 2):          # first row is data, not a header
                    if last_header and len(last_header) == len(table[0]):
                        table = [last_header] + table                      # continuation page without a repeated header row
                else:
                    last_header = table[0]
                try:
                    parsed = parse_rows(table, "pdf-table", "pdf", loc_prefix=f"p{pno} t{k} ")
                except ValueError:
                    continue
                labels = caption_labels(cap)
                rows = parsed["groups"][0]["rows"]
                for r in rows:
                    r["pattern"] = f"pdf:p{pno}.t{k}"
                    r["labels"] = list(labels)
                groups.append({"pattern": f"pdf:p{pno}.t{k}", "rows": rows})
                route_counts[route] = route_counts.get(route, 0) + 1
    if not groups:
        raise ValueError("no tables with numeric data found in the PDF" + (" (no text layer; OCR found none)" if backend == "ocr" else ""))
    notes.append("tables by route: " + ", ".join(f"{k}={v}" for k, v in route_counts.items()))
    return {"format": "pdf", "encoding": "pdf", "notes": notes, "groups": groups}
