"""
Format readers. Each reader turns an arbitrary file into the same neutral structure:

    {"format": "json"|"csv", "encoding": ..., "groups": [Group, ...], "notes": [...]}
    Group = {"pattern": str, "rows": [Row, ...]}
    Row   = {"locator": str, "data": {key: scalar}, "labels": [(name, label), ...], "units": {key: unit_text}}

Nothing here knows about soil, rainfall or crops - it only discovers the *structure*:
  * JSON: finds every array/dict of records at any depth, carries ancestor scalars
    (e.g. district) down onto each record, and turns dict keys that act as labels
    (e.g. {"2017-19": [...]}) into `labels`.
  * CSV: finds the header block (one or many rows), label columns, value columns, repeated
    column blocks (e.g. one Area/Production/Yield trio per year) and row hierarchies
    (e.g. state -> crop -> district), and unpivots wide tables into one record per block.
"""
import codecs
import csv
import json
import re
from pathlib import Path

YEAR_RE = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")
ENCODINGS = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
MISSING_TOKENS = {"", "-", "--", "na", "n/a", "nan", "null", "none", "nil", "no data", "no data."}


# ----------------------------------------------------------------------------- helpers
def is_missing(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in MISSING_TOKENS)


def to_number(v):
    """float for numeric-looking input, else None.
    '1,234.5' -> 1234.5 (thousands comma) ; '280,5' -> 280.5 (decimal comma) ; '12 %' -> 12.0."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().rstrip("%").strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):      # thousands separators
        s = s.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d+", s):                        # decimal comma
        s = s.replace(",", ".")
    elif "," in s:
        return None                                             # ambiguous: refuse rather than guess
    try:
        return float(s)
    except ValueError:
        return None


def read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise ValueError("could not decode file with any of " + ", ".join(ENCODINGS))


def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


# ----------------------------------------------------------------------------- JSON
def _is_scalar(v):
    return not isinstance(v, (dict, list))


def _has_records(v) -> bool:
    if isinstance(v, list):
        return any(isinstance(x, dict) for x in v)
    if isinstance(v, dict):
        return any(not _is_scalar(x) for x in v.values())
    return False


def _flatten_small(d: dict, prefix="") -> dict:
    """scalar fields of d, plus nested dicts of scalars flattened as 'bbox.minx'."""
    out = {}
    for k, v in d.items():
        if _is_scalar(v):
            out[prefix + str(k)] = v
        elif isinstance(v, dict):
            out.update(_flatten_small(v, f"{prefix}{k}."))
        # lists of scalars are kept as JSON text
        elif isinstance(v, list) and not any(isinstance(x, dict) for x in v):
            out[prefix + str(k)] = json.dumps(v)
    return out


# Keys that name a collection ({"districts": [...]}) rather than label a record ({"Banana": [...]}).
_WRAPPER_KEYS = {"data", "records", "record", "rows", "items", "results", "result", "entries", "districts", "crops",
                 "villages", "blocks", "tehsils", "states", "monthly", "months", "values", "report", "reports", "list",
                 "samples", "readings", "observations", "table", "dataset", "payload", "content", "response"}


def _with_label(labels, name, key):
    """Add (container name, key) as a label unless the key is just a collection wrapper."""
    k = str(key).lower()
    if re.sub(r"[^a-z]", "", k) in _WRAPPER_KEYS or re.match(r"^by[_\- ]", k):   # "by_year", "by-district" ...
        return labels
    return labels + [(name, str(key))]


def _walk_json(node, path, pattern, name, ctx, labels, rows):
    if isinstance(node, list):
        for i, x in enumerate(node):
            if isinstance(x, dict):
                _walk_json(x, f"{path}[{i}]", pattern + "[]", name, ctx, labels, rows)
        return
    if not isinstance(node, dict):
        return
    scalars = {k: v for k, v in node.items() if _is_scalar(v)}
    collections = {k: v for k, v in node.items() if _has_records(v)}
    only_dicts = not scalars and node and all(isinstance(v, (dict, list)) for v in node.values()) \
        and any(isinstance(v, dict) and v for v in node.values())
    if only_dicts:  # {"Ukhrul": {...}, "Senapati": {...}}: keys are labels, values are records
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                _walk_json(v, f'{path}["{k}"]', pattern + "[*]", name, ctx, _with_label(labels, name, k), rows)
        return
    if not collections:  # leaf record
        data = dict(ctx)
        data.update(_flatten_small(node))
        rows.append({"locator": path, "pattern": pattern, "data": data, "labels": list(labels), "units": {}})
        return
    if not scalars:  # pure container: its keys are labels (e.g. {"2017-19": [...]})
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                _walk_json(v, f'{path}["{k}"]', pattern + "[*]", name, ctx, _with_label(labels, name, k), rows)
        return
    new_ctx = dict(ctx)
    new_ctx.update(scalars)
    for k, v in collections.items():
        _walk_json(v, f"{path}.{k}", f"{pattern}.{k}", k, new_ctx, labels, rows)
    # scalar-only children that are not records themselves are already in new_ctx


def read_json(path: Path) -> dict:
    text, enc = read_text(path)
    notes = []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        # JSON Lines fallback
        doc = [json.loads(line) for line in text.splitlines() if line.strip()]
        notes.append("parsed as JSON Lines")
    rows = []
    if isinstance(doc, list) and doc and all(_is_scalar(x) for x in doc):
        raise ValueError("JSON is a flat list of scalars - no records found")
    _walk_json(doc, "$", "$", "root", {}, [], rows)
    if not rows:
        raise ValueError("no record-like objects found in JSON")
    groups = {}
    for r in rows:
        groups.setdefault(r["pattern"], []).append(r)
    return {"format": "json", "encoding": enc, "notes": notes,
            "groups": [{"pattern": p, "rows": rs} for p, rs in groups.items()]}


# ----------------------------------------------------------------------------- CSV
def _strip_number_prefix(s: str) -> str:
    return re.sub(r"^\s*\d+\s*[\.\)]\s*", "", s).strip()


def _is_total(label: str) -> bool:
    return bool(re.match(r"^\s*((grand|sub)\s*-?\s*)?total\b|^\s*(average|mean|avg)\b", label, re.I))


def _numeric_count(row, start=1):
    return sum(1 for c in row[start:] if to_number(c) is not None)


def _header_block(rows) -> int:
    """number of header rows: row 0, plus following rows whose first cell is blank."""
    n = 1
    while n < len(rows) and not rows[n][0].strip() and _numeric_count(rows[n]) == 0:
        n += 1
    return n


def _label_columns(rows, start, ncols) -> list[int]:
    """leading columns that hold text labels (not numbers) in the data rows."""
    data_rows = [r for r in rows[start:] if _numeric_count(r, 0) >= 2]
    labels = []
    for j in range(ncols):
        cells = [r[j].strip() for r in data_rows if r[j].strip()]
        if cells and sum(to_number(c) is not None for c in cells) / len(cells) < 0.5:
            labels.append(j)
        else:
            break
    return labels or [0]


def _level_role(values) -> str:
    vals = [v for v in values if v.strip()]
    if not vals:
        return "empty"
    if sum(bool(YEAR_RE.search(v)) for v in vals) / len(vals) >= 0.5:
        return "period"
    if sum(bool(re.search(r"\(.*\)", v)) for v in vals) / len(vals) >= 0.5:
        return "unit"
    return "variable"


def read_csv_file(path: Path) -> dict:
    text, enc = read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    raw = [r for r in csv.reader(text.splitlines(), delimiter=delim)]
    if delim == ";":  # ';' files use the decimal comma: '1,215' is 1.215, never one thousand two hundred fifteen
        raw = [[re.sub(r"^(-?\d+),(\d+)$", r"\1.\2", c.strip()) for c in r] for r in raw]
    return parse_rows(raw, delim, enc)


def parse_rows(raw, delim, enc, loc_prefix="") -> dict:
    """Neutral-structure parser for any table given as a list of rows: a CSV file, or one table cut out of a PDF page
    (then `loc_prefix` makes the locators read like 'p53 t1 line 7, cols B-F')."""
    ncols = max((len(r) for r in raw), default=0)
    rows = [[c.strip() for c in r] + [""] * (ncols - len(r)) for r in raw]
    keep = [(i, r) for i, r in enumerate(rows) if any(c for c in r)]  # remember original line numbers
    if len(keep) < 2:
        raise ValueError("CSV has no data rows")
    lines = [i + 1 for i, _ in keep]
    rows = [r for _, r in keep]

    h = _header_block(rows)
    label_cols = _label_columns(rows, h, ncols)
    value_cols = [j for j in range(ncols) if j not in label_cols]
    notes = [f"delimiter={delim!r}", f"header_rows={h}", f"label_columns={label_cols}"]

    # ---- column specs ------------------------------------------------------------
    levels = [rows[i] for i in range(h)]
    filled = []
    for li, lev in enumerate(levels):
        out, last = list(lev), ""
        if li < h - 1:
            for j in value_cols:
                if out[j].strip():
                    last = out[j]
                elif last:
                    out[j] = last
        filled.append(out)
    roles = [_level_role([f[j] for j in value_cols]) for f in filled]
    if h > 1 and "variable" not in roles:  # e.g. only a years row + a units row: the last non-unit level is the variable
        cand = [i for i, r in enumerate(roles) if r != "unit"]
        if cand:
            roles[cand[-1]] = "variable"
    specs = {}
    for j in value_cols:
        spec = {"period": "", "variable": "", "unit": ""}
        for li, role in enumerate(roles):
            if role in spec:
                spec[role] = filled[li][j] if not spec[role] else spec[role] + " " + filled[li][j]
        if h == 1:
            spec["variable"] = rows[0][j]
        if spec["variable"].strip():
            specs[j] = spec
    # unit sometimes sits inside the variable text: "Area (Ha)"
    for spec in specs.values():
        m = re.search(r"\(([^)]*)\)", spec["variable"])
        if m and not spec["unit"]:
            spec["unit"] = f"({m.group(1)})"

    # ---- hierarchy names from the first header cell: "State/Crop/District" ------------
    names = [n.strip() for n in re.split(r"[/>|]", rows[0][label_cols[0]]) if n.strip()] if h >= 1 else []
    extra_label_names = [rows[0][j] for j in label_cols[1:]]

    out_rows, stack, prev_group, skipped_totals = [], [], False, 0
    for ri in range(h, len(rows)):
        r = rows[ri]
        label = r[label_cols[0]]
        has_values = any(to_number(r[j]) is not None or (r[j].strip() and not is_missing(r[j])) for j in value_cols)
        if h == 1:  # tidy table: every row is a record (except summary rows)
            if _is_total(label):
                skipped_totals += 1
                continue
            data = {rows[0][j]: r[j] for j in range(ncols) if rows[0][j].strip()}
            out_rows.append({"locator": f"{loc_prefix}line {lines[ri]}", "pattern": "csv", "data": data, "labels": [], "units": {}})
            continue
        is_leaf = has_values or any(r[j].strip() for j in label_cols[1:])   # e.g. a Season value marks a data row
        if not has_values and is_leaf:   # a data row whose value cells are all blank: nothing to record, hierarchy unchanged
            prev_group = False
            continue
        if not has_values:  # hierarchy header row
            clean = _strip_number_prefix(label)
            if not clean:
                continue
            if prev_group:
                stack.append(clean)
            elif stack:
                stack[-1] = clean
            else:
                stack = [clean]
            prev_group = True
            continue
        prev_group = False
        if _is_total(label):
            skipped_totals += 1
            continue
        ctx = {}
        for d, anc in enumerate(stack):
            ctx[names[d] if d < len(names) else f"level{d + 1}"] = anc
        ctx[names[len(stack)] if len(stack) < len(names) else "entity"] = _strip_number_prefix(label)
        for j, nm in zip(label_cols[1:], extra_label_names):
            if r[j].strip():
                ctx[nm or f"col{j}"] = r[j]
        blocks = {}
        for j, spec in specs.items():
            blocks.setdefault(spec["period"], []).append(j)
        for period, cols in blocks.items():
            vals = {specs[j]["variable"]: r[j] for j in cols if not is_missing(r[j])}
            if not vals:
                continue
            data = dict(ctx)
            if period:
                data["period"] = period
            data.update(vals)
            units = {specs[j]["variable"]: specs[j]["unit"] for j in cols if specs[j]["unit"]}
            out_rows.append({"locator": f"{loc_prefix}line {lines[ri]}, cols {col_letter(min(cols))}-{col_letter(max(cols))}"
                                        + (f", period {period}" if period else ""),
                             "pattern": "csv", "data": data, "labels": [], "units": units})
    if skipped_totals:
        notes.append(f"skipped {skipped_totals} aggregate 'Total' rows (derived, would double-count)")
    if not out_rows:
        raise ValueError("no data records found in CSV")
    return {"format": "csv", "encoding": enc, "notes": notes, "groups": [{"pattern": "csv", "rows": out_rows}]}


# ----------------------------------------------------------------------------- dispatch
def _looks_like_json(p: Path) -> bool:
    head = p.read_bytes()[:200]
    if head.startswith(codecs.BOM_UTF8):
        head = head[len(codecs.BOM_UTF8):]
    return head.lstrip()[:1] in (b"{", b"[")


def read_any(path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    ext = p.suffix.lower()
    if ext in (".json", ".geojson", ".jsonl", ".ndjson"):
        return read_json(p)
    if ext in (".csv", ".tsv", ".txt"):
        return read_csv_file(p)
    return read_json(p) if _looks_like_json(p) else read_csv_file(p)
