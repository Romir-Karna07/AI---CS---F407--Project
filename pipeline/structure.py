"""
Structure repair for tables that are not "one header row, then data rows".

Why this exists: the CSV parser assumes row 1 is the header. Real exports have titles and "Source:" lines above the table, repeated headers or
notes in the middle, several tables on one sheet, months in rows with districts as columns (or the reverse), merged cells ...

How it works - and why it is NOT limited to the first N lines
  1. Every non-blank row gets a shape class (T1 one text cell, N1 one number, H several text cells, D mostly numbers, M mixed) and a width bucket.
  2. Consecutive rows with the same shape are grouped into runs (run-length encoding) over the WHOLE table, however long it is.
     A 50,000-row table with one stray note at row 31,207 therefore shows up as its own run, with its text.
  3. `anomalies()` is a cheap deterministic gate: only a table with a title above the header, a header-like row inside the data, or stray text rows
     is sent to the model. A clean table costs no model call.
  4. `plan_structure()` sends the run summary (long runs: first / last rows only; short runs: every row) and asks for a JSON plan:
     which rows are header / data / context (title, unit, source lines) / to drop, whether the table is transposed, or has entities as columns.
  5. `validate_plan()` rejects any plan that points at rows that do not exist or yields no numeric data; `apply_plan()` then reshapes the grid in
     code. Original row numbers are kept so every stored value still traces back to its line in the file.
"""
import re
from itertools import zip_longest

from .readers import col_letter, to_number
from .trace import traceable

LONG_NOTE_CHARS = 45


# ----------------------------------------------------------------------------- shapes
def _cells(row):
    return [c for c in row if str(c).strip()]


def classify_row(row):
    cells = _cells(row)
    n = len(cells)
    if n == 0:
        return "B", 0
    nums = sum(1 for c in cells if to_number(c) is not None)
    if n == 1:
        return ("N1" if nums else "T1"), 1
    if nums == 0:
        return "H", n
    return ("D" if nums / n >= 0.5 else "M"), n


def _bucket(n):
    return "1" if n == 1 else "2" if n == 2 else "3-5" if n <= 5 else "6+"


def segments(rows):
    segs = []
    for i, r in enumerate(rows):
        cls, n = classify_row(r)
        key = (cls, _bucket(n))
        if segs and segs[-1]["key"] == key:
            segs[-1]["end"] = i
            segs[-1]["n"] += 1
        else:
            segs.append({"key": key, "cls": cls, "wb": key[1], "start": i, "end": i, "n": 1})
    return segs


def anomalies(rows, numbers, segs):
    """deterministic gate: reasons why this table needs a closer look ([] = clean, the normal parser can take it)"""
    out = []
    wide = [i for i, r in enumerate(rows) if len(_cells(r)) >= 2]
    if not wide:
        return ["no row has two or more cells"]
    if wide[0] > 0:
        out.append(f"{wide[0]} row(s) above the first multi-cell row (lines {numbers[0]}-{numbers[wide[0] - 1]}) look like a title or notes")
    hdr_end = wide[0]
    first_is_header = classify_row(rows[wide[0]])[0] == "H"
    while first_is_header and hdr_end + 1 < len(rows) and classify_row(rows[hdr_end + 1])[0] == "H":
        hdr_end += 1
    header_rows = (hdr_end - wide[0] + 1) if first_is_header else 0
    for s in segs:
        if s["start"] <= hdr_end:
            continue
        line = numbers[s["start"]]
        if s["cls"] == "H":
            # in a layout that already has a multi-row header, a 2-cell text row is a label row with blank values (handled by the parser)
            if header_rows <= 1 or s["wb"] in ("3-5", "6+"):
                out.append(f"header-like text row inside the table at line {line}")
        elif s["cls"] == "T1":
            text = _cells(rows[s["start"]])[0]
            if header_rows <= 1:
                out.append(f"stray single-cell text row at line {line}: {text[:50]!r}")
            elif len(text) >= LONG_NOTE_CHARS:
                out.append(f"long note at line {line}: {text[:50]!r}")
    return out[:8]


# ----------------------------------------------------------------------------- summary for the model
def _row_text(row, numbers_i, maxlen=150):
    cells = [str(c).strip()[:32] for c in row if str(c).strip()]
    t = " | ".join(cells)
    return f"L{numbers_i}: {t[:maxlen]}"


def summarize(rows, numbers, segs, max_chars=6500):
    """one line per run + sample rows; shrinks until it fits. Covers the whole table."""
    for samples in (2, 1, 0):
        lines = []
        for s in segs:
            a, b = s["start"], s["end"]
            head = f"L{numbers[a]}-L{numbers[b]} [{s['cls']} x{s['n']}, {s['wb']} cells]" if s["n"] > 1 else f"L{numbers[a]} [{s['cls']}, {s['wb']} cells]"
            if s["n"] <= 4:
                body = [_row_text(rows[i], numbers[i]) for i in range(a, b + 1)]
            elif samples == 0:
                body = []
            else:
                body = [_row_text(rows[i], numbers[i]) for i in range(a, a + samples)] + ["   ..."] + \
                       [_row_text(rows[i], numbers[i]) for i in range(b - samples + 1, b + 1)]
            lines.append(head + ("\n   " + "\n   ".join(body) if body else ""))
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
    return text[:max_chars] + "\n...(summary clipped)"


SYSTEM = (
    "You repair the structure of ONE table that was exported from a CSV, spreadsheet, web page or PDF. The text may be in any language or script.\n"
    "You receive a run-length summary of the WHOLE table: consecutive rows of the same shape are grouped; short runs are shown in full, long runs by "
    "their first and last rows. Rows are identified by line numbers like L12. Shape codes: T1 = one text cell, N1 = one number, H = several text "
    "cells (header-like), D = mostly numbers (data), M = mixed.\n"
    "Decide, using ONLY line numbers that appear in the summary:\n"
    "- tables: each table's header line(s) and data line ranges. A repeated header inside the data, a footnote or a 'Source' line is NOT data.\n"
    "- context: lines holding a title, units, a district / period / crop name or a source note (they describe the table; keep them).\n"
    "- drop: other lines to ignore.\n"
    "- transposed: true when records run ACROSS columns (the first column holds variable names, the header row holds the entities).\n"
    "- melt: when the header cells are VALUES of one dimension (e.g. district names or month names) and the cells below are one measure, give "
    "{\"id_cols\": [1-based column numbers that identify the row, e.g. the month], \"dimension\": \"English name of what the header cells are\", "
    "\"measure\": \"English name of what the cells measure, with unit if shown\"}. Otherwise null.\n"
    "Header = the line(s) at the TOP of a table. A header line repeated further down is not a header: put it in drop.\n"
    "How to tell the layouts apart (look at what the FIRST COLUMN and the HEADER ROW contain):\n"
    "  * Normal: header cells are variable names (District | Month | Rainfall (mm)); every row is one record.\n"
    "  * transposed=true: the first column holds VARIABLE names and the header row holds ENTITIES, e.g.\n"
    "      Parameter | Ukhrul | Thoubal   /   Rainfall (mm) | 12.5 | 8.0   /   Rain days | 3 | 2   -> corner_label \"District\".\n"
    "  * melt: the header row holds ENTITIES (districts) while the first column holds identifiers such as months or years and every cell is the "
    "same measure, e.g.  Month | Ukhrul | Thoubal  /  2020-01 | 12.5 | 8.0  -> id_cols [1], dimension \"District\", measure \"Rainfall (mm)\".\n"
    "Answer ONLY with JSON of this shape:\n"
    "{\"tables\":[{\"header\":[4],\"data\":[[5,904]]}],\"context\":[1,2],\"drop\":[],\"transposed\":false,\"corner_label\":null,"
    "\"melt\":null,\"confidence\":0.9}\n"
    "corner_label: for a transposed table, the English name of the entities in the header row (e.g. \"District\"); else null.")


def _ints(x):
    return [int(v) for v in x if isinstance(v, (int, float)) and not isinstance(v, bool)] if isinstance(x, list) else []


def validate_plan(plan, numbers, ncols):
    """-> cleaned plan or None. Row numbers must exist, ranges must be ordered, melt columns must exist."""
    if not isinstance(plan, dict):
        return None
    have = set(numbers)
    tables = []
    for t in plan.get("tables") or []:
        if not isinstance(t, dict):
            continue
        header = sorted(n for n in _ints(t.get("header")) if n in have)
        ranges = []
        for r in t.get("data") or []:
            if isinstance(r, list) and len(r) == 2 and all(isinstance(v, (int, float)) for v in r) and r[0] <= r[1]:
                ranges.append((int(r[0]), int(r[1])))
        if not header or not ranges:
            continue
        first_data = min(a for a, _ in ranges)
        lead = [n for n in header if n < first_data]
        extra = [n for n in header if n >= first_data]           # a header repeated inside the data: ignore it, do not fail the plan
        if not lead:
            continue
        tables.append({"header": lead, "data": ranges, "repeated_headers": extra})
    if not tables:
        if isinstance(plan.get("tables"), list) and not plan["tables"]:
            return {"tables": [], "context": [n for n in _ints(plan.get("context")) if n in have], "drop": set(), "transposed": False,
                    "corner_label": None, "melt": None, "confidence": plan.get("confidence")}   # the model says: no data table here
        return None
    melt = plan.get("melt")
    if isinstance(melt, dict):
        ids = [c for c in _ints(melt.get("id_cols")) if 1 <= c <= ncols]
        dim, meas = str(melt.get("dimension") or "").strip(), str(melt.get("measure") or "").strip()
        melt = {"id_cols": ids, "dimension": dim[:60], "measure": meas[:60]} if ids and dim and meas else None
    else:
        melt = None
    corner = plan.get("corner_label")
    drop = set(_ints(plan.get("drop"))) | {n for t in tables for n in t["repeated_headers"]}
    return {"tables": tables, "context": [n for n in _ints(plan.get("context")) if n in have], "drop": drop,
            "transposed": bool(plan.get("transposed")), "corner_label": str(corner)[:40] if corner else None, "melt": melt,
            "confidence": plan.get("confidence")}


@traceable("structure_plan", "chain")
def plan_structure(ai, grid, reason, found):
    """ask the model for a structure plan for this grid -> validated plan or None"""
    rows, numbers = grid["rows"], grid["numbers"]
    segs = segments(rows)
    user = (f"Why you are asked: {reason}\n" + (("Deterministic checks found: " + "; ".join(found) + "\n") if found else "") +
            (f"Source: {grid['kind']} {grid.get('name') or ''}\n" if grid.get("kind") else "") +
            f"Columns: {max((len(r) for r in rows), default=0)}; rows: {len(rows)}\n\nTABLE SUMMARY\n{summarize(rows, numbers, segs)}")
    plan = ai.ask("structure", SYSTEM, user, {"rows": len(rows), "runs": len(segs), "reason": reason[:80]})
    return validate_plan(plan, numbers, max((len(r) for r in rows), default=0))


# ----------------------------------------------------------------------------- apply a plan
def _transpose(region, labels_from):
    cols = list(zip_longest(*region, fillvalue=""))
    return [list(c) for c in cols], [f"col {col_letter(j)}" for j in range(len(cols))]


def apply_plan(grid, plan):
    """-> list of tables {rows, numbers, line_word, context}; [] if the plan gives nothing usable"""
    rows, numbers = grid["rows"], grid["numbers"]
    pos = {n: i for i, n in enumerate(numbers)}
    ctx = [" | ".join(_cells(rows[pos[n]])) for n in plan["context"] if n in pos]
    out = []
    for t in plan["tables"]:
        hdr = [n for n in t["header"] if n in pos]
        body = [n for n in numbers if any(a <= n <= b for a, b in t["data"]) and n not in plan["drop"] and n not in hdr]
        if not hdr or not body:
            continue
        region_nums = hdr + body
        region = [list(rows[pos[n]]) for n in region_nums]
        line_word = "line"
        if plan["transposed"]:
            region, region_nums = _transpose(region, None)
            line_word = "transposed"
            if plan.get("corner_label") and region and region[0]:
                region[0][0] = plan["corner_label"]
        elif plan["melt"]:
            m = plan["melt"]
            header_cells = region[len(hdr) - 1]
            ids = [c - 1 for c in m["id_cols"]]
            val_cols = [j for j in range(len(header_cells)) if j not in ids and str(header_cells[j]).strip()]
            new = [[(str(header_cells[j]).strip() or "id") for j in ids] + [m["dimension"], m["measure"]]]
            labels = [region_nums[0]]
            for rn, r in zip(region_nums[len(hdr):], region[len(hdr):]):
                for j in val_cols:
                    v = r[j] if j < len(r) else ""
                    if str(v).strip():
                        new.append([r[k] if k < len(r) else "" for k in ids] + [str(header_cells[j]).strip(), v])
                        labels.append(f"{rn} col {col_letter(j)}")
            region, region_nums = new, labels
        if len(region) < 2 or not any(to_number(c) is not None for r in region[1:] for c in r):
            continue
        out.append({"rows": region, "numbers": region_nums, "line_word": line_word, "context": ctx})
    return out
