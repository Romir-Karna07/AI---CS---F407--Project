"""
Tabular intake for CSV / TSV, Excel (.xlsx) and HTML tables.

Every format is first turned into the same thing - a "grid": a list of rows of strings plus the ORIGINAL row numbers and any context text found
around the table (sheet name, HTML caption and headings). `read_tabular` then
  1. runs the deterministic irregularity gate (structure.anomalies),
  2. only if the gate fires (or repair is forced after a failed classification) asks the model for a structure plan (structure.plan_structure),
  3. applies the validated plan in code and hands each resulting table to the shared parser (readers.parse_rows).
A clean table never reaches the model and is parsed exactly as before.
"""
import csv
import re
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

from . import structure
from .readers import col_letter, parse_rows, read_text

MAX_SHEET_ROWS = 200_000


# ----------------------------------------------------------------------------- CSV
def csv_grids(path):
    text, enc = read_text(path)
    try:
        delim = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|").delimiter
    except csv.Error:
        delim = ","
    raw = list(csv.reader(text.splitlines(), delimiter=delim))
    if delim == ";":  # ';' files use the decimal comma: '1,215' is 1.215
        raw = [[re.sub(r"^(-?\d+),(\d+)$", r"\1.\2", c.strip()) for c in r] for r in raw]
    ncols = max((len(r) for r in raw), default=0)
    rows, numbers = [], []
    for i, r in enumerate(raw, 1):
        r = [c.strip() for c in r] + [""] * (ncols - len(r))
        if any(r):
            rows.append(r)
            numbers.append(i)
    return [{"kind": "csv", "name": "", "rows": rows, "numbers": numbers, "context": [], "delim": delim, "enc": enc, "prefix": ""}]


# ----------------------------------------------------------------------------- Excel
def _cell_text(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, datetime):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return str(int(v)) if v == int(v) and abs(v) < 1e15 else repr(v)
    return str(v).strip()


def xlsx_overview(path, rows=3):
    """[{name, dims, head}] for every visible sheet (cheap; used to choose sheets)"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        head = []
        for r in ws.iter_rows(values_only=True):
            cells = [_cell_text(c) for c in r if _cell_text(c)]
            if cells:
                head.append(" | ".join(cells)[:120])
            if len(head) >= rows:
                break
        try:
            dims = ws.calculate_dimension()
        except Exception:
            dims = "?"
        out.append({"name": ws.title, "dims": dims, "head": head})
    wb.close()
    return out


def xlsx_grids(path, sheets=None):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    grids = []
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible" or (sheets and ws.title not in sheets):
            continue
        maxc = ws.max_column
        values = {}
        for r in ws.iter_rows(min_row=1, max_row=min(ws.max_row, MAX_SHEET_ROWS)):
            for c in r:
                if c.value is not None:
                    values[(c.row, c.column)] = _cell_text(c.value)
        for mr in ws.merged_cells.ranges:                       # merged cells: only the top-left holds the value
            v = values.get((mr.min_row, mr.min_col), "")
            full_width_title = mr.min_row == mr.max_row and (mr.max_col - mr.min_col + 1) >= maxc > 2
            if not v or full_width_title:                        # a title merged across the whole sheet stays a single cell
                continue
            for rr in range(mr.min_row, mr.max_row + 1):
                for cc in range(mr.min_col, mr.max_col + 1):
                    values.setdefault((rr, cc), v)
        rows, numbers = [], []
        for rn in range(1, min(ws.max_row, MAX_SHEET_ROWS) + 1):
            r = [values.get((rn, cn), "") for cn in range(1, maxc + 1)]
            if any(r):
                rows.append(r)
                numbers.append(rn)
        if rows:
            grids.append({"kind": "xlsx", "name": ws.title, "rows": rows, "numbers": numbers, "context": [f"sheet name: {ws.title}"],
                          "delim": "xlsx", "enc": "xlsx", "prefix": f"sheet '{ws.title}' "})
    wb.close()
    return grids


# ----------------------------------------------------------------------------- HTML
class _TableParser(HTMLParser):
    BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "li", "caption", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.recent, self.prose = [], [], []
        self._depth = 0
        self._skip = 0
        self._block = []
        self._cur = None
        self._row = None
        self._cell = None
        self._cap = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._cur = {"rows": [], "caption": "", "ctx": list(self.recent[-2:]), "spans": {}}
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._depth == 1 and tag in ("td", "th") and self._row is not None:
            self._cell = {"text": [], "colspan": int(a.get("colspan", 1) or 1) if str(a.get("colspan", "1")).isdigit() else 1,
                          "rowspan": int(a.get("rowspan", 1) or 1) if str(a.get("rowspan", "1")).isdigit() else 1}
        elif tag == "caption":
            self._cap = []
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._cur is not None:
            self._finish_row()
            self._row = None
        elif tag == "caption" and self._cap is not None and self._cur is not None:
            self._cur["caption"] = re.sub(r"\s+", " ", "".join(self._cap)).strip()
            self._cap = None
        elif tag == "table":
            if self._depth == 1 and self._cur is not None:
                self.tables.append(self._cur)
                self._cur = None
            self._depth = max(0, self._depth - 1)
        elif tag in self.BLOCKS and self._depth == 0:
            t = re.sub(r"\s+", " ", "".join(self._block)).strip()
            if t:
                self.recent.append(t[:300])
                self.prose.append(t)
            self._block = []

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell["text"].append(data)
        elif self._cap is not None:
            self._cap.append(data)
        elif self._depth == 0:
            self._block.append(data)

    def _finish_row(self):
        spans = self._cur["spans"]
        out, col, cells = [], 0, list(self._row)
        while cells or col in spans:
            if col in spans and spans[col][0] > 0:
                out.append(spans[col][1])
                spans[col][0] -= 1
                col += 1
                continue
            if not cells:
                break
            c = cells.pop(0)
            txt = re.sub(r"\s+", " ", "".join(c["text"])).strip()
            for k in range(c["colspan"]):
                out.append(txt)
                if c["rowspan"] > 1:
                    spans[col] = [c["rowspan"] - 1, txt]
                col += 1
        self._cur["rows"].append(out)


def html_parse(path):
    text, _ = read_text(path)
    p = _TableParser()
    p.feed(text)
    return p


def html_grids(path):
    p = html_parse(path)
    grids = []
    for k, t in enumerate(p.tables, 1):
        ncols = max((len(r) for r in t["rows"]), default=0)
        rows, numbers = [], []
        for i, r in enumerate(t["rows"], 1):
            r = r + [""] * (ncols - len(r))
            if any(c.strip() for c in r):
                rows.append(r)
                numbers.append(i)
        if len(rows) < 2 or ncols < 2:                           # layout tables
            continue
        ctx = ([t["caption"]] if t["caption"] else []) + t["ctx"]
        grids.append({"kind": "html", "name": f"table {k}", "rows": rows, "numbers": numbers, "context": ctx, "delim": "html",
                      "enc": "html", "prefix": f"table {k} "})
    return grids


def html_prose(path):
    return "\n".join(html_parse(path).prose)


# ----------------------------------------------------------------------------- shared reader
def read_tabular(path, kind, ai=None, sheets=None, force_structure=False, reason=None, only_tables=None):
    """-> parsed {format, encoding, notes, groups}; raises ValueError when no table with data is found"""
    grids = {"csv": csv_grids, "xlsx": lambda p: xlsx_grids(p, sheets), "html": html_grids}[kind](Path(path))
    if only_tables is not None and kind == "html":
        grids = [g for g in grids if g["name"] in only_tables]
    groups, notes, n_repaired, confirmed = [], [], 0, []
    for g in grids:
        rows, numbers = g["rows"], g["numbers"]
        if len(rows) < 1:
            continue
        found = structure.anomalies(rows, numbers, structure.segments(rows))
        tables = None
        if (found or force_structure) and ai is not None and ai.available:
            why = reason or ("structure looks irregular" if found else "no domain recognised from the headers")
            plan = structure.plan_structure(ai, g, why, found)
            if plan is not None and not plan["tables"]:           # the model looked at the whole sheet and found no data table in it
                notes.append(f"{g['kind']} {g['name'] or 'main'}: no data table (model)")
                continue
            if plan and len(plan["tables"]) == 1 and not plan["transposed"] and not plan["melt"] and not found and force_structure:
                confirmed.append(g["name"] or "main")      # the model looked and the layout is a normal table
            if plan:
                tables = structure.apply_plan(g, plan) or None
                if tables:
                    n_repaired += 1
                    notes.append(f"{g['kind']} {g['name']}: structure repaired by model ({'; '.join(found)[:120] or why})")
            elif found:
                notes.append(f"{g['kind']} {g['name']}: irregular ({'; '.join(found)[:120]}) but no usable repair plan")
        elif found:
            notes.append(f"{g['kind']} {g['name']}: irregular ({'; '.join(found)[:120]}); AI repair unavailable")
        if not tables:
            tables = [{"rows": rows, "numbers": numbers, "line_word": "line", "context": []}]
        for ti, t in enumerate(tables, 1):
            prefix = g["prefix"] + (f"t{ti} " if len(tables) > 1 else "")
            try:
                parsed = parse_rows(t["rows"], g["delim"], g["enc"], loc_prefix=prefix, line_numbers=t["numbers"], line_word=t["line_word"])
            except ValueError:
                continue
            pattern = g["kind"] if (len(grids) == 1 and len(tables) == 1) else f"{g['kind']}:{g['name'] or 'main'}#{ti}"
            ctx = list(dict.fromkeys(g["context"] + t["context"]))
            for r in parsed["groups"][0]["rows"]:
                r["pattern"] = pattern
                r["context"] = ctx
            groups.append({"pattern": pattern, "rows": parsed["groups"][0]["rows"]})
    if not groups:
        raise ValueError(f"no data records found in the {kind} file")
    n = len(grids)
    notes.insert(0, f"{kind}: {n} grid(s), {len(groups)} table(s) read" + (f", {n_repaired} repaired" if n_repaired else ""))
    return {"format": kind, "encoding": grids[0]["enc"], "notes": notes, "groups": groups, "structure_confirmed": bool(confirmed)}
