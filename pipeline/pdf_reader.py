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
from contextlib import nullcontext
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


_LEGEND_RE = re.compile(r"(?<![A-Za-z])([A-Z])\s*[:=]\s*([A-Za-z][^,;|]*?)(?=\s*(?:[,;|]|\s[A-Z]\s*[:=]|$))")


def legend_of(lines) -> dict:
    """abbreviations a caption defines for the table's cells: "A: Area in '000 Ha  P: Production in '000 MT" -> {'A': "Area ('000 Ha)", ...}.
    Only single capital letters defined as a word are taken, so ordinary captions are untouched."""
    out = {}
    for l in lines or []:
        for m in _LEGEND_RE.finditer(str(l)):
            text = re.sub(r"\s+", " ", m.group(2)).strip(" .")
            text = re.sub(r"\s+in\s+(.+)$", r" (\1)", text)          # "Area in '000 Ha" -> "Area ('000 Ha)": the unit where the reader looks for it
            if len(text) >= 3:
                out[m.group(1)] = text
    return out


def expand_legend(table, legend):
    """replace cells that are exactly a defined abbreviation (a header 'A' under a year, a row code 'P') by what the caption says it means"""
    if not legend:
        return table
    return [[legend.get(c.strip(), c) if isinstance(c, str) else c for c in row] for row in table]


_COLNO_RE = re.compile(r"\(\s*\d{1,2}\s*\)")
_MEASURE_RE = re.compile(r"\b(Area|Production|Yield)\s+in\s*([^)\n,;]+)", re.I)


def drop_column_numbers(table):
    """drop the '(1) (2) (3) ...' row that numbers the columns under the header in Indian statistical tables (it is not data)"""
    out = []
    for r in table:
        cells = [str(c or "").strip() for c in r]
        filled = [c for c in cells if c]
        if len(filled) >= 2 and all(_COLNO_RE.fullmatch(c) for c in filled):
            continue
        out.append(r)
    return out


_SERIAL_HEAD = re.compile(r"(s\.?\s*l\.?|sr\.?|s\.?)\s*no\.?", re.I)


def drop_serial_column(table):
    """drop a leading 'Sl. No.' / 'S.No.' column whose cells are just 1, 2, 3 ... (a row counter, not data; it hides the label column)"""
    if len(table) < 3 or not table[0]:
        return table
    j = next((k for k, c in enumerate(table[0][:2]) if _SERIAL_HEAD.fullmatch(str(c or "").strip())), None)
    if j is None:
        return table
    cells = [str(r[j] or "").strip() for r in table[1:] if j < len(r)]
    filled = [c for c in cells if c]
    if not filled or not all(re.fullmatch(r"\d{1,3}\.?|[ivx]+\.?|\(\d+\)", c, re.I) for c in filled):
        return table
    return [r[:j] + r[j + 1:] for r in table]


def add_caption_measure(table, cap):
    """a table whose column headers are only years ('2022-23 | 2023-24 | 2024-25') takes its measure from the caption
    ('(Area in hectares)'): a second header row is added under the years, so the years stay the period and the caption names the
    variable and its unit. Only when the caption states exactly one measure."""
    if len(table) < 2:
        return table
    found = {m.group(1).title(): m.group(2).strip(" .'’") for m in _MEASURE_RE.finditer(" ".join(cap or []))}
    if len(found) != 1:
        return table
    head = [str(c or "").strip() for c in table[0]]
    years = [j for j, c in enumerate(head) if re.fullmatch(r"(19|20)\d{2}(\s*-\s*\d{2,4})?", c)]
    if len(years) < 2 or len(years) < len([c for c in head if c]) - 3:
        return table
    measure, unit = next(iter(found.items()))
    second = ["" for _ in head]
    for j in years:
        second[j] = f"{measure} ({unit})"
    return [table[0], second] + table[1:]


def _digits_only(v):
    """'0 . 7 0' (letter-spaced text layer) -> '0.70'; text is left alone"""
    s = str(v or "").strip()
    return re.sub(r"\s+", "", s) if re.fullmatch(r"[\d\s.,-]+", s) else s


def _col(j):
    from .readers import col_letter
    return col_letter(j)


_SEASON_RE = re.compile(r"\b(pre[\s-]*kharif|kharif|rabi|summer|zaid|autumn|winter|whole\s+year)\b", re.I)


def melt_coded_blocks(table, legend):
    """A table whose rows are coded by a legend and whose columns are districts:

        District        <blank>  Imphal East  Chandel ... Total State
        Kharif Maize    A          0.45        2.85   ...   14.42        (A = Area in 000 ha, P = Production in 000 Mt, Y = Yield)
                        P          0.70        5.38   ...   28.69
                        Y          1.56        1.89   ...    1.99

    becomes one row per (crop label, district column) with the coded variables as columns, so the ordinary reader takes it:
        Crop | Season | District | Area (000 ha) | Production (000 Mt) | Yield (MT/ha)
    -> (new table, original row number of each new row) or None when the table is not of this shape (nothing is guessed:
    the code column must hold only legend entries and most column headers must be district names)."""
    if not legend or len(table) < 3:
        return None
    from .normalize import STATE_NAME, normalize_district
    names = set(legend.values())
    head = [str(c or "").strip() for c in table[0]]
    ncol = len(head)
    code_col = next((j for j in range(min(3, ncol)) if
                     sum(str(r[j] or "").strip() in names for r in table[1:]) >= max(3, (len(table) - 1) // 2) and
                     all(str(r[j] or "").strip() in names or not str(r[j] or "").strip() for r in table[1:])), None)
    if code_col is None or code_col == 0:
        return None
    first = table[1]                       # a header word wrapped into the first data row ("Churachand" / "pur")
    for j in range(code_col + 1, ncol):
        cell = str(first[j] or "").strip()
        if head[j] and re.fullmatch(r"[a-z]+", cell):
            head[j], first[j] = head[j] + cell, ""
    places = {}
    for j in range(code_col + 1, ncol):
        if normalize_district(head[j])[2] in ("exact", "alias", "fuzzy"):
            places[j] = head[j]
        elif re.search(r"\btotal\b|\bstate\b", head[j], re.I):
            places[j] = STATE_NAME
    if len(places) < max(2, (ncol - code_col - 1) // 2):
        return None
    order = list(dict.fromkeys(legend.values()))
    out, lines, label, label_line, block = [], [], "", 0, {}

    def flush():
        if label and block and not re.match(r"^\s*total", label, re.I):       # "Total paddy", "TotalPulses": derived sums
            m = _SEASON_RE.search(label)
            season = m.group(1).title() if m else ""
            crop = re.sub(r"\s+", " ", _SEASON_RE.sub(" ", label)).strip() or label
            for j, place in places.items():
                vals = [_digits_only(block.get(v, {}).get(j, "")) for v in order]
                if any(str(x).strip() for x in vals):
                    out.append([crop, season, place] + vals)
                    lines.append(f"{label_line}, col {_col(j)} ({head[j]})")     # one source cell block per record: the locator stays unique

    for i, r in enumerate(table[1:], 2):
        lab, code = str(r[0] or "").strip(), str(r[code_col] or "").strip()
        if code not in names:
            flush(); label, block = "", {}
            continue                                                            # a row without a code ("Net Area"): not part of a block
        if lab:
            flush(); label, label_line, block = lab, i, {}
        block[code] = {j: str(r[j] or "").strip() for j in places}
    flush()
    if not out:
        return None
    return [["Crop", "Season", "District"] + order] + out, [1] + lines


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
        if len(cur) >= 3:
            # The OCR sometimes fuses two neighbouring NUMBERS into one box ("98.0 5.4"). Such a box, with only numeric tokens and spanning
            # two or more header cells, is split by token position BEFORE the columns are built; text cells are never split.
            hdr = [(c[0], c[2]) for c in cur[0]]
            fixed = [cur[0]]
            for row in cur[1:]:
                new_row = []
                for c in row:
                    toks = c[4].split()
                    if len(toks) >= 2 and sum(1 for x0, x1 in hdr if min(c[2], x1) - max(c[0], x0) > 0) >= 2 and all(to_number(t) is not None for t in toks):
                        width, total, pos = c[2] - c[0], max(len(c[4]), 1), 0
                        for t in toks:
                            at = c[4].find(t, pos)
                            pos = at + len(t)
                            new_row.append((c[0] + width * at / total, c[1], c[0] + width * (at + len(t)) / total, c[3], t))
                    else:
                        new_row.append(c)
                fixed.append(new_row)
            cur = fixed
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


def _sideways(fitz_page):
    """90 or 270 when most text lines run vertically (a landscape table printed on a portrait page), else 0"""
    dirs = {}
    for b in fitz_page.get_text("dict").get("blocks", []):
        for l in b.get("lines", []):
            d = tuple(round(x) for x in l.get("dir", (1, 0)))
            dirs[d] = dirs.get(d, 0) + 1
    total = sum(dirs.values()) or 1
    if dirs.get((0, -1), 0) / total > 0.6:
        return 90
    if dirs.get((0, 1), 0) / total > 0.6:
        return 270
    return 0


def _upright_tables(fdoc, pno, rot):
    """turn a sideways page upright (a one-page copy in a temporary file) and read it with camelot "stream", which also takes the rows that
    have no ruling of their own (tables shaded in bands). -> (tables, text lines of the original page: title and legend in reading order)"""
    import os
    import tempfile
    one = fdoc.__class__()
    one.insert_pdf(fdoc, from_page=pno - 1, to_page=pno - 1)
    one[0].set_rotation((one[0].rotation + rot) % 360)
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        one.save(tmp)
        tables = _camelot_tables(tmp, 1)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    lines = [l.strip() for l in fdoc[pno - 1].get_text().splitlines() if l.strip()]
    return tables, lines


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
def read_pdf_file(path, backend: str = "auto", max_pages: int | None = None, pages=None) -> dict:
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
    sel = pages if pages else os.getenv("AGRI_PDF_PAGES", "")     # read per call: several files in one process
    if isinstance(sel, (list, tuple)):
        page_list = sorted({int(p) for p in sel if 1 <= int(p) <= n_pages})
        if not page_list:
            raise ValueError("the selected pages are outside the document")
        notes.append(f"{len(page_list)} selected page(s)")
    elif str(sel).strip():
        page_list = []
        for part in re.split(r"\s*,\s*", str(sel).strip()):
            m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
            if not m:
                raise ValueError(f"bad page range {part!r}; use '53-60', '161' or '1,4,9-12'")
            page_list += [p for p in range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1) if 1 <= p <= n_pages]
        page_list = sorted(set(page_list))
        if not page_list:
            raise ValueError(f"page range {sel!r} is outside the document ({n_pages} pages)")
        notes.append(f"page range {sel}")
    else:
        page_list = list(range(1, min(n_pages, limit) + 1))
        if n_pages > limit:
            notes.append(f"only the first {limit} of {n_pages} pages were read (AGRI_PDF_MAX_PAGES)")
    groups, route_counts, last_header = [], {}, None
    try:
        pl_cm = pdfplumber.open(str(path))
    except Exception:                       # an image file: no text layer, no ruled-table detection, OCR only
        pl_cm = nullcontext(None)
    with pl_cm as pl:
        for pno in page_list:
            fpage = fdoc[pno - 1]
            has_text = len(fpage.get_text().strip()) >= 20
            found, route, captions, lines = [], None, [], []
            if has_text and backend != "ocr":
                lines = _page_lines_text(fpage)
                try:
                    cand = [t for t in _plumber_tables(pl.pages[pno - 1]) if _usable(t)] if pl is not None else []
                    if cand:
                        route = "pdfplumber"
                except Exception:
                    cand = []
                rot = _sideways(fpage) if not cand else 0
                if rot:                                   # a sideways table (landscape on a portrait page): read it upright
                    try:
                        up, up_lines = _upright_tables(fdoc, pno, rot)
                        up = [t for t in up if _usable(t)]
                        if up:
                            cand, lines, route = up, up_lines, "camelot_upright"
                    except Exception:
                        pass
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
                if not str(route).startswith("pdfplumber"):
                    table = merge_shifted_columns(table)
                cap = title + list(cap)
                legend = legend_of(cap + lines[-4:])                             # a legend is printed in the caption or as a page footnote
                table = expand_legend(drop_serial_column(drop_column_numbers(table)), legend)
                table = add_caption_measure(table, cap)
                melted = melt_coded_blocks(table, legend)
                if melted:
                    try:
                        parsed = parse_rows(melted[0], "pdf-table", "pdf", loc_prefix=f"p{pno} t{k} ", line_numbers=melted[1])
                    except ValueError:
                        continue
                    labels = caption_labels(cap)
                    rows = parsed["groups"][0]["rows"]
                    for r in rows:
                        r["pattern"] = f"pdf:p{pno}.t{k}"
                        r["labels"] = list(labels)
                        r["context"] = [c for c in cap if c][:6]
                    groups.append({"pattern": f"pdf:p{pno}.t{k}", "rows": rows})
                    route_counts[route + "+coded_blocks"] = route_counts.get(route + "+coded_blocks", 0) + 1
                    continue
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
                    r["context"] = [c for c in cap if c][:6]      # caption lines: the context-facts step reads units / district / period from them
                groups.append({"pattern": f"pdf:p{pno}.t{k}", "rows": rows})
                route_counts[route] = route_counts.get(route, 0) + 1
    if not groups:
        raise ValueError("no tables with numeric data found in the PDF" + (" (no text layer; OCR found none)" if backend == "ocr" else ""))
    notes.append("tables by route: " + ", ".join(f"{k}={v}" for k, v in route_counts.items()))
    return {"format": "pdf", "encoding": "pdf", "notes": notes, "groups": groups}


def page_texts(path, pages):
    """[(page number, text)] of the given pages' text layer (for prose extraction); pages without text are skipped"""
    import pymupdf
    doc = pymupdf.open(str(path))
    out = []
    for p in pages:
        if 1 <= int(p) <= len(doc):
            t = doc[int(p) - 1].get_text() or ""
            if t.strip():
                out.append((int(p), t))
    doc.close()
    return out
