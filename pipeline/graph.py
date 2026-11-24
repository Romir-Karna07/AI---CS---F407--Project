"""
LangGraph orchestration.

    inspect(triage) -> plan -> read --(error)--> recover -> read ...        (recover: at most MAX_RECOVERIES = 2 per file, strict)
                               |
                               v
                           classify -> soil_agent -> rainfall_agent -> production_agent -> load -> finish
                               |            (only agents that have a matching record group are visited)
                               +--(nothing recognised, budget left)--> recover -> read ...   (repair the structure / read it as prose)

Fixed code decides: reading, column mapping, units, normalisation, validation, routing. The model is consulted only where the code is
uncertain, and every answer is verified by code (see pipeline/ai.py):
  inspect   : triage of an ambiguous file (deterministic magic bytes / content profile first)
  plan      : which pages of a long PDF / which sheets of a workbook to read (page selection)
  read      : structure repair of irregular tables (title lines, repeated headers, transposed / melted layouts), prose extraction, OCR
  recover   : which recovery strategy to try next, from a fixed menu, never repeating one, within the retry budget
  classify  : header meaning in any language, translation of non-English categorical values, units / district / period from titles
  load      : plausibility check - impossible values are set aside in `review_queue`, never edited
A file that is clean costs zero model calls. Every step, model call and decision is traced into state["trajectory"] (and to LangSmith
when configured: see pipeline/trace.py).
"""
import csv
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from . import agents as agent_mod
from . import context as context_mod
from . import lang, plausibility
from . import triage as triage_mod
from .ai import AIContext, ai_on
from . import crops as crops_mod
from .concepts import DOMAIN_CORE, map_columns, register_synonym, score_domains
from .db import connect, insert_review, insert_rows
from .llm import classify_prompt, headers_prompt, parse_domain, parse_headers, verify_header_proposal
from .normalize import normalize_district
from .readers import YEAR_RE, read_json, to_number
from .schemas import TABLES, validate_row
from .trace import run_config

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.getenv("AGRI_LOG_DIR") or ROOT / "logs")   # tests point this at a temp dir
MAX_RECOVERIES = 2
AGENT_ORDER = ["soil", "rainfall", "production"]
MAX_PDF_PAGES = int(os.getenv("AGRI_PDF_MAX_PAGES", "40"))
READ_TOOL = {"json": "read_json_data", "pdf": "read_pdf_data", "pdf_ocr": "read_pdf_ocr", "xlsx": "read_xlsx_data", "html": "read_html_data",
             "text": "read_text_data", "image": "read_image_data", "csv": "read_csv_data"}
_AI: dict = {}          # run_id -> AIContext (budget, model handle); removed when the run finishes


class State(TypedDict, total=False):
    input_path: str
    db_path: str
    run_id: str
    started_at: str
    source_hash: str
    source_type: str
    force_format: str | None
    strategy: str | None
    tried: list
    ai_enabled: bool
    plan: dict
    inject_fault: str | None
    manual_note: str
    parsed: dict
    groups: list
    queue: list
    rows: dict
    review: list
    trajectory: list
    error: str | None
    retries: int
    agent_errors: list
    result: dict
    status: str


# ----------------------------------------------------------------------------- tracing helpers
def _ai(state) -> AIContext:
    rid = state.get("run_id") or "unknown"
    if rid not in _AI:
        _AI[rid] = AIContext(rid, enabled=state.get("ai_enabled", True))
    return _AI[rid]


def _trace(state, step, tool=None, args=None, status="ok", detail=None, t0=None, ai=None, traj=None):
    """append this step to the trajectory, preceded by the model calls made while it ran"""
    base = list(traj if traj is not None else state.get("trajectory", []))
    if ai is not None:
        base += ai.drain()
    base.append({"step": step, "tool": tool, "args": args or {}, "status": status, "detail": detail,
                 "ms": round((time.perf_counter() - t0) * 1000, 1) if t0 else None})
    return base


def _maybe_fault(state, where):
    """Fault injection for demos (--inject-fault): raise in the named node on the first attempt only."""
    if state.get("inject_fault") == where and state.get("retries", 0) == 0:
        raise RuntimeError(f"injected fault in {where}")


def _use_ai(state, ai) -> bool:
    return bool(state.get("ai_enabled", True)) and ai.available


# ----------------------------------------------------------------------------- inspect (triage)
def inspect_node(state: State) -> State:
    t0 = time.perf_counter()
    p = Path(state["input_path"])
    run_id = state.get("run_id") or f"run_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    out = {"run_id": run_id, "started_at": datetime.now().isoformat(timespec="seconds"), "retries": 0, "tried": [],
           "agent_errors": [], "rows": {}, "error": None}
    ai = _AI[run_id] = AIContext(run_id, enabled=state.get("ai_enabled", True))
    st = {**state, "run_id": run_id}
    try:
        data = p.read_bytes()
        out["source_hash"] = hashlib.sha256(data).hexdigest()
        info = triage_mod.sniff(p)
        stype, how = info["type"], info["how"]
        if info["confidence"] == "low" and _use_ai(state, ai):
            route, reason = triage_mod.llm_triage(ai, info, p.name)
            if route == "unsupported":
                stype, info["reason"] = "unsupported", reason or "the model judged the file unreadable"
            elif route:
                stype, how = {"prose": "text"}.get(route, route), "model"
        stype = {"prose": "text"}.get(stype, stype)
        if stype == "unsupported":
            raise ValueError(f"unsupported file type: {info['reason']}. Supported: {triage_mod.SUPPORTED}")
        out["source_type"] = stype
        out["trajectory"] = _trace(st, "inspect", "inspect_file", {"file": p.name}, "ok", {"bytes": len(data), "type": stype, "how": how}, t0, ai)
    except Exception as e:
        out["error"] = f"cannot open input: {e}"
        out["trajectory"] = _trace(st, "inspect", "inspect_file", {"file": p.name}, "error", str(e), t0, ai)
    return out


# ----------------------------------------------------------------------------- plan (page / sheet selection)
def plan_node(state: State) -> State:
    """decide WHAT to read before reading: pages of a long PDF, sheets of a workbook. Short / simple inputs pass straight through."""
    t0 = time.perf_counter()
    ai = _ai(state)
    stype = state["source_type"]
    path = Path(state["input_path"])
    plan, detail = {}, None
    try:
        if stype == "pdf" and state.get("ai_enabled", True) and ai_on():
            override = os.getenv("AGRI_PDF_PAGES", "").strip()
            if not override:
                from .page_select import select_pages
                r = select_pages(path, ai if ai.available else None, MAX_PDF_PAGES)
                if r["method"] != "all pages (short document)":
                    plan = {"pages": r["pages"], "prose_pages": r["prose_pages"]}
                    detail = {"total_pages": r["total"], "selected": len(r["pages"]), "method": r["method"], "pages": r["pages"][:40],
                              "prose_pages": r["prose_pages"][:20], "notes": r["notes"]}
        elif stype == "xlsx" and _use_ai(state, ai):
            from .tabular import xlsx_overview
            ov = xlsx_overview(path)
            if len(ov) >= 3:
                ans = ai.ask("plan", "You choose which sheets of a workbook contain data tables about soil tests, rainfall or crop area / "
                             "production / yield (any language). Answer ONLY with JSON: {\"sheets\": [\"exact sheet name\", ...]}",
                             "Sheets:\n" + "\n".join(f"- {s['name']!r} ({s['dims']}): {' / '.join(s['head'])}" for s in ov), {"sheets": len(ov)})
                names = {s["name"] for s in ov}
                chosen = [n for n in (ans.get("sheets") if isinstance(ans, dict) else []) or [] if n in names]
                if chosen:
                    plan = {"sheets": chosen}
                    detail = {"sheets_total": len(ov), "selected": chosen}
    except Exception as e:  # planning must never stop a run: fall back to reading everything
        plan, detail = {}, {"error": str(e)[:160]}
    if detail is None and not ai.steps:
        return {"plan": plan}
    return {"plan": plan, "trajectory": _trace(state, "plan", "plan_read", {"type": stype}, "ok" if not (detail or {}).get("error") else "error",
                                               detail, t0, ai)}


# ----------------------------------------------------------------------------- read
def _prose_text(path, orig_type):
    from .readers import read_text
    if orig_type == "html":
        from .tabular import html_prose
        return html_prose(path)
    if orig_type == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        return "\n".join(" ".join(str(c) for c in row if c is not None) for ws in wb.worksheets for row in ws.iter_rows(values_only=True))
    return read_text(path)[0]


def _cover_lines(path, n=25) -> list:
    """the first lines of page 1 (title / cover): where a document says which state it is about. Never fails a read."""
    try:
        from .pdf_reader import page_texts
        lines = []
        for _, text in page_texts(path, [1]):
            for l in text.splitlines():
                l = re.sub(r"\s+", " ", l).strip()
                if l and l not in lines:
                    lines.append(l[:120])
        return lines[:n]
    except Exception:
        return []


def read_node(state: State) -> State:
    t0 = time.perf_counter()
    ai = _ai(state)
    use_ai = _use_ai(state, ai)
    stype = state.get("force_format") or state["source_type"]
    strategy = state.get("strategy")
    tool = READ_TOOL.get(stype, "read_csv_data")
    path = Path(state["input_path"])
    plan = state.get("plan") or {}
    args = {"file_path": path.name}
    try:
        _maybe_fault(state, "read")
        if stype in ("pdf", "pdf_ocr", "image"):
            from .pdf_reader import page_texts, read_pdf_file
            backend = "ocr" if stype in ("pdf_ocr", "image") else "auto"
            groups, notes, parsed = [], [], None
            try:
                parsed = read_pdf_file(path, backend=backend, pages=plan.get("pages"))
                groups, notes = list(parsed["groups"]), list(parsed["notes"])
            except ValueError as e:
                if not (use_ai and (plan.get("prose_pages") or strategy == "as_prose")):
                    raise
                notes.append(f"no tables: {e}")
            table_pages = {int(m.group(1)) for g in groups for m in [re.match(r"pdf:p(\d+)\.", g["pattern"])] if m}
            prose_pages = plan.get("prose_pages") or ([p for p in (plan.get("pages") or range(1, 9))] if strategy == "as_prose" else [])
            prose_pages = [p for p in prose_pages if p not in table_pages]      # a page whose table was read is not also read as sentences
            if use_ai and prose_pages:
                from .prose import extract
                for pno, text in page_texts(path, prose_pages[:12]):
                    g, n = extract(ai, text, f"p{pno} ")
                    groups += g
                    notes += n
            if not groups:
                raise ValueError("no tables or verifiable facts found in the PDF")
            parsed = {"format": "pdf", "encoding": "pdf", "notes": notes, "groups": groups, "doc_context": _cover_lines(path)}
        elif stype == "json":
            parsed = read_json(path)
        elif stype in ("csv", "xlsx", "html"):
            from .tabular import read_tabular
            parsed = read_tabular(path, stype, ai=ai if use_ai else None, sheets=plan.get("sheets"),
                                  force_structure=(strategy == "structure_repair"),
                                  reason="no domain was recognised from the headers" if strategy == "structure_repair" else None)
        elif stype == "text":
            if not use_ai:
                raise ValueError("free text needs the language model (prose extraction); no model is available")
            from .prose import extract
            text = _prose_text(path, state["source_type"])
            groups, notes = extract(ai, text, "")
            if not groups:
                raise ValueError("no verifiable agricultural facts found in the text" + (f" ({'; '.join(notes)})" if notes else ""))
            parsed = {"format": "prose", "encoding": "text", "notes": notes, "groups": groups}
        else:
            raise ValueError(f"no reader for type {stype}")
        n = sum(len(g["rows"]) for g in parsed["groups"])
        keep_type = "pdf" if stype.startswith("pdf") else ("text" if stype == "text" else stype)
        return {"parsed": parsed, "error": None, "source_type": keep_type,
                "trajectory": _trace(state, "read", tool, args, "ok", {"records": n, "groups": len(parsed["groups"]), "notes": parsed["notes"]}, t0, ai)}
    except Exception as e:
        return {"error": f"read failed ({stype}): {e}", "trajectory": _trace(state, "read", tool, args, "error", str(e), t0, ai)}


# ----------------------------------------------------------------------------- recover (strict budget)
def recovery_options(state, post_classify: bool):
    """the fixed menu of strategies for this situation (never repeating one already tried)"""
    stype = state.get("force_format") or state["source_type"]
    tried = set(state.get("tried", []))
    if post_classify:
        base = {"csv": ["structure_repair", "as_prose"], "xlsx": ["structure_repair", "as_prose"], "html": ["structure_repair", "as_prose"],
                "pdf": ["as_prose", "force_ocr"], "image": ["as_prose"]}.get(stype if stype != "pdf_ocr" else "pdf", [])
    else:
        base = ["retry_same", "other_reader"]
        if stype.startswith("pdf") or stype == "image":
            base.append("force_ocr")
        if stype in ("csv", "xlsx", "html"):
            base += ["structure_repair", "as_prose"]
    if post_classify and (state.get("parsed") or {}).get("structure_confirmed"):
        base = [s for s in base if s != "as_prose"]      # the layout was checked and is a normal table: it is simply not our data
    return [s for s in base if s not in tried]


def recover_node(state: State) -> State:
    """Strict budget: at most MAX_RECOVERIES per file. The first recovery after a READ error is always 'retry the same reader' (transient errors);
    afterwards, and after a failed classification, the model may choose the next strategy from the menu (or, without a model, the first unused one)."""
    t0 = time.perf_counter()
    ai = _ai(state)
    attempt = state.get("retries", 0) + 1
    current = state.get("force_format") or state["source_type"]
    post = not state.get("error")
    options = recovery_options(state, post)
    why = "deterministic order"
    strategy = options[0] if options else "retry_same"
    nontrivial = Path(state["input_path"]).stat().st_size > 0
    if options and _use_ai(state, ai) and nontrivial and (post or attempt >= 2):
        ans = ai.ask("recover", "You choose the next recovery strategy for a data-extraction pipeline that failed on a file. Choose exactly one "
                     "strategy from the given list. retry_same = read it again; other_reader = try the other parser (csv<->json) or OCR; "
                     "force_ocr = OCR every page; structure_repair = ask for a table structure plan (title lines, transposed, entities as "
                     "columns); as_prose = read the text as sentences with figures. Answer ONLY with JSON: {\"strategy\": \"...\", \"why\": \"short\"}",
                     f"File type: {current}\nProblem: {(state.get('error') or 'read fine but no soil / rainfall / production columns were recognised')[:300]}\n"
                     f"Strategies already tried: {state.get('tried', [])}\nAllowed now: {options}", {"allowed": options})
        if isinstance(ans, dict) and ans.get("strategy") in options:
            strategy, why = ans["strategy"], str(ans.get("why", ""))[:100] or "model choice"
    fmt = current
    if strategy == "other_reader":
        fmt = "pdf_ocr" if current.startswith("pdf") else ("csv" if current == "json" else "json" if current == "csv" else current)
        if current.startswith("pdf"):
            strategy = "force_ocr"
    elif strategy == "force_ocr":
        fmt = "pdf_ocr"
    elif strategy == "as_prose":
        fmt = "text"
    if not _use_ai(state, ai) and not post:      # legacy deterministic behaviour without a model
        if current.startswith("pdf"):
            fmt = "pdf" if attempt == 1 and current == "pdf" else "pdf_ocr"
        else:
            fmt = current if attempt == 1 else ("csv" if current == "json" else "json")
        strategy = "retry_same" if attempt == 1 else "other_reader"
    return {"force_format": fmt, "strategy": strategy, "tried": state.get("tried", []) + [strategy], "retries": attempt, "error": None,
            "rows": {}, "queue": [], "groups": [], "result": {}, "agent_errors": [],         # start the next attempt from a clean slate
            "trajectory": _trace(state, "recover", None, {"attempt": attempt, "budget": MAX_RECOVERIES, "retry_with": fmt, "strategy": strategy},
                                 "ok", {"why": why}, t0, ai)}


# ----------------------------------------------------------------------------- classify
# what a model-proposed column meaning may be, once the table's domain is known (the data check already ran in verify_header_proposal)
_COMMON_OK = {"district", "district_code", "state", "state_code", "period", "record_id", "latitude", "longitude"}
_DEFERRED_OK = {"production": _COMMON_OK | {"crop", "season", "area", "production", "yield", "unit"},
                "soil": _COMMON_OK | {"crop", "village", "tehsil", "block", "category", "n", "p", "k", "oc", "ph", "ec", "s", "zn", "fe", "cu", "mn", "b"},
                "rainfall": _COMMON_OK | {"rainfall_mm", "rainy_days", "unit"}}


def _context_needs(domain, rows, mapping, mcount, has_district_label, has_year_label):
    vals = set(mapping.values())
    src_for = {c: s for s, c in mapping.items()}
    var_map = {"production": ["area", "production", "yield"], "rainfall": ["rainfall_mm"]}.get(domain, [])
    units = []
    for canon in var_map:
        src = src_for.get(canon)
        if not src:
            continue
        has_unit = bool(re.search(r"\(([^)]*)\)", str(src))) or any(r["units"].get(src) for r in rows[:5]) or "unit" in vals
        if not has_unit:
            units.append(canon)
    want_district = not ({"district", "district_code"} & vals) and not has_district_label
    want_period = not ({"period", "year_month"} & vals) and not has_year_label and mcount < 6
    return units, want_district, want_period


def classify_node(state: State) -> State:
    t0 = time.perf_counter()
    ai = _ai(state)
    llm = ai.llm if state.get("ai_enabled", True) else None
    traj = list(state.get("trajectory", []))
    groups, recognised = [], []
    doc_lines = list(state["parsed"].get("doc_context") or []) + [Path(state["input_path"]).name]   # cover / title lines + file name
    for g in state["parsed"]["groups"]:
        rows = g["rows"]
        sample = rows[:300]
        ctx_lines = list(rows[0].get("context") or []) if rows else []
        prompt_ctx = [l for l in ctx_lines if not l.startswith("sheet name:")][:4] + doc_lines[:6]   # what the model is told about the table
        keys = list(dict.fromkeys(k for r in sample for k in r["data"]))
        mapping, unmapped = map_columns(keys)
        crop_by_vocab = False
        if "crop" not in mapping.values() and set(mapping.values()) & DOMAIN_CORE["production"]:
            # a crop measure but no crop column named: an unknown text column whose cells fixed code recognises as crop / livestock names IS the
            # crop column ("Particulars": Rice, Pulses, Milk ...). Otherwise the rows would be stored with crop UNSPECIFIED and never filtered.
            for col in unmapped:
                vals = [str(r["data"].get(col) or "").strip() for r in rows[:60]]
                vals = [v for v in vals if v and to_number(v) is None]
                if len(vals) >= 2 and sum(crops_mod.deterministic_verdict(v) is not None for v in vals) / len(vals) >= 0.5:
                    for r in rows:
                        r["hmap"] = {**(r.get("hmap") or {}), col: "crop"}
                    mapping = {**mapping, col: "crop"}
                    unmapped = [u for u in unmapped if u != col]
                    crop_by_vocab = True
                    break
        mcount = len(agent_mod.month_columns(keys))
        scores = score_domains(mapping.values())
        if mcount >= 6:
            scores["rainfall"] += 3
        method = "rules" + ("+crop_column_by_vocabulary" if crop_by_vocab else "")
        has_numeric = any(to_number(r["data"].get(k)) is not None for k in unmapped for r in sample[:50])
        numeric_unmapped = unmapped[:40] if has_numeric else []   # text columns too: district / year names vary as well
        has_measure = bool(set(mapping.values()) & set().union(*DOMAIN_CORE.values()))
        if not numeric_unmapped and has_measure and unmapped and max(scores.values()) == 0:
            numeric_unmapped = unmapped[:40]   # a measure column is known but nothing says what the rows are: ask about the text columns ("Particulars")
        label_texts = [str(lab) for r in sample[:30] for _, lab in r["labels"]]
        has_district_label = any(normalize_district(t)[2] in ("exact", "alias", "fuzzy") for t in label_texts)
        has_year_label = any(YEAR_RE.search(t) for t in label_texts)

        def missing_identity(mapped_fields, scr):
            """location / time fields a record needs but the headers (and labels) do not provide"""
            if max(scr.values()) == 0:
                return []
            need = []
            if not ({"district", "district_code"} & set(mapped_fields)) and not has_district_label:
                need.append("district")
            if not ({"period", "year_month"} & set(mapped_fields)) and not has_year_label and mcount < 6:
                need.append("time")
            return need

        gaps = missing_identity(mapping.values(), scores)
        proposal, deferred, extra_map = {}, {}, {}
        if llm and numeric_unmapped and (max(scores.values()) < 3 or gaps):
            sysm, usr = headers_prompt(numeric_unmapped, sample[0]["data"], [r["data"] for r in sample[:4]], prompt_ctx)
            memo_key = ("headers", tuple(numeric_unmapped))
            if memo_key in ai.memo:     # an earlier table in this file has exactly these columns: reuse the answer (the data check below still runs)
                raw_map = ai.memo[memo_key]
                ai.note("headers", "llm_map_headers", {"columns": numeric_unmapped[:10]}, "reused", {"answer": "same columns as an earlier table"})
            else:
                raw_map = ai.ask("headers", sysm, usr, {"columns": numeric_unmapped[:10]}, tool="llm_map_headers")
                if raw_map is not None:
                    ai.memo[memo_key] = raw_map
            proposal = verify_header_proposal(parse_headers(raw_map, numeric_unmapped), sample)
            trial = {**mapping, **{k: v for k, v in proposal.items() if v not in mapping.values()}}
            trial_scores = score_domains(trial.values())
            if mcount >= 6:
                trial_scores["rainfall"] += 3
            # trust the LLM only if it makes the group a recognisable domain or fills a missing district / time column
            accepted = max(trial_scores.values()) > max(scores.values()) or len(missing_identity(trial.values(), trial_scores)) < len(gaps)
            traj += ai.drain() + [{"step": "classify", "tool": None, "args": {}, "status": "ok" if accepted else "rejected",
                                   "detail": {"header_proposal": proposal, "accepted": accepted}, "ms": None}]
            if accepted:
                for k, v in proposal.items():
                    register_synonym(k, v)
                mapping, unmapped = map_columns(keys)
                scores = score_domains(mapping.values())
                if mcount >= 6:
                    scores["rainfall"] += 3
                method = "rules+llm_headers"
            else:
                deferred = proposal      # not enough to change the domain by itself: kept until the domain is known (see below)

        def ask_domain(extra_args):
            """one classification per distinct set of columns in this file (tables repeated across pages share the answer)"""
            mkey = ("classify", tuple(keys), tuple(extra_args.get("tie", ())))
            if mkey in ai.memo:
                ai.note("classify", "llm_classify", extra_args, "reused", {"answer": "same columns as an earlier table"})
                return ai.memo[mkey]
            s_, u_ = classify_prompt(keys, sample[0]["data"], [r["data"] for r in sample[:4]], prompt_ctx)
            ans = ai.ask("classify", s_, u_, extra_args, tool="llm_classify")
            if ans is not None:
                ai.memo[mkey] = ans
            return ans

        best = max(scores.values())
        domain = None
        if best > 0:
            tops = [d for d in AGENT_ORDER if scores[d] == best]
            domain = tops[0]
            if len(tops) > 1 and llm:
                d = parse_domain(ask_domain({"tie": tops}))
                traj += ai.drain()
                domain, method = (d if d in tops else domain), "rules+llm_tiebreak"
        elif llm and (has_numeric or has_measure):   # nothing numeric -> nothing to extract; do not spend an LLM call
            d = parse_domain(ask_domain({}))
            traj += ai.drain()
            has_core = d in DOMAIN_CORE and (set(mapping.values()) & DOMAIN_CORE[d])
            domain, method = (d if has_core else None), ("llm" if has_core else "llm_rejected_no_measurement_columns")
        # ---- the domain is known: now the model's other column meanings (crop name, state, season ...) can be judged. They need no "score" -
        # a crop-name column does not make a table a production table - they only need to fit the domain and the data (checked above).
        if domain and deferred:
            allowed = _DEFERRED_OK[domain] | ({"year_month"} if domain == "rainfall" and mcount < 6 else set())
            have = set(mapping.values())
            extra_map = {k: v for k, v in deferred.items() if v in allowed and v not in have and k in keys}
            if extra_map:
                for r in rows:
                    r["hmap"] = {**(r.get("hmap") or {}), **extra_map}
                mapping = {**mapping, **extra_map}
                unmapped = [u for u in unmapped if u not in extra_map]
                method += "+llm_dims"
        # ---- crops only: livestock, dairy, fishery (and other non-crop) rows never enter the crop-production table
        excluded = []
        if domain == "production" and rows:
            crop_src = {c: s for s, c in mapping.items()}.get("crop")
            model_named = (crop_src is not None and crop_src in extra_map) or method.startswith("llm")
            kept, excluded, unverified = crops_mod.filter_rows(ai, rows, crop_src, prompt_ctx, bool(llm) and model_named)
            traj += ai.drain()
            if excluded:
                rows[:] = kept       # in place: the agent reads this same list
                method += "+crops_only"
                traj.append({"step": "classify", "tool": None, "args": {"pattern": g["pattern"]}, "status": "ok",
                             "detail": {"excluded_non_crop": excluded, "kept": len(kept)}, "ms": None})
                if not kept:
                    domain = None
                    method = "no_crop_rows_after_filter"
            if unverified:
                method += "+crops_unverified"
        # ---- context facts: units / district / period stated in the title or caption, only where the table itself gives none
        # (fixed code reads the plain cases - "('000 MT)", "2023-24", "of Fruit Crops" - even without a model; the model is asked for the rest)
        if domain:
            ctx_lines = rows[0].get("context") or []
            units, want_d, want_p = _context_needs(domain, rows, mapping, mcount, has_district_label, has_year_label)
            meaningful = [l for l in ctx_lines if not l.startswith("sheet name:")] or (ctx_lines if (want_d or want_p) else [])
            # the crop of a table that names none (a state-wise "Fruit Crops" table): only from the caption, with its quote
            want_c = (domain == "production" and "crop" not in set(mapping.values()) and
                      not any(crops_mod.row_label(r, None) for r in rows[:20]))
            if meaningful and (units or want_d or want_p or want_c):
                facts = context_mod.facts_for(ai, ctx_lines, keys, units, want_d, want_p, want_c)
                if facts:
                    context_mod.apply_to_rows(rows, facts)
                    method += "+context"
        # ---- state: nothing in a table says "Manipur" - the cover, title or file name does. Looked up only when the table has no district /
        # state column (a district already implies the state); fixed code searches first, the model is asked at most once per document.
        if domain and rows and not ({"state", "district", "district_code"} & set(mapping.values())) and not has_district_label \
                and not (rows[0].get("ctx") or {}).get("district"):
            found = context_mod.find_state(ai if llm else None, doc_lines, ctx_lines)
            traj += ai.drain()
            if found:
                context_mod.apply_state(rows, found)
                method += "+state"
        if domain:
            recognised.append((rows, mapping))
        groups.append({"pattern": g["pattern"], "domain": domain, "records": len(rows), "scores": scores,
                       "method": method, "mapped_headers": {k: v for k, v in mapping.items()},
                       "unmapped_headers": [k for k in unmapped][:30], **({"excluded_non_crop": excluded} if excluded else {})})
    # ---- language: translate non-English district / month / crop / season values (model proposes, closed lists verify)
    lang_notes = lang.localize(ai, recognised) if (recognised and llm) else []
    queue = [d for d in AGENT_ORDER if any(g["domain"] == d for g in groups)]
    traj += ai.drain()
    traj.append({"step": "classify", "tool": None, "args": {}, "status": "ok",
                 "detail": {"groups": [(g["pattern"], g["domain"], g["records"]) for g in groups], "queue": queue, "language": lang_notes},
                 "ms": round((time.perf_counter() - t0) * 1000, 1)})
    return {"groups": groups, "queue": queue, "parsed": state["parsed"], "trajectory": traj}


def _make_agent_node(domain):
    agent_name, fn = agent_mod.AGENTS[domain]

    def node(state: State) -> State:
        t0 = time.perf_counter()
        rows_in = [r for g, gi in zip(state["parsed"]["groups"], state["groups"]) if gi["domain"] == domain
                   for r in g["rows"]]
        queue = [q for q in state["queue"] if q != domain]
        try:
            if state.get("inject_fault") == agent_name:
                raise RuntimeError(f"injected fault in {agent_name}")
            rows = fn(rows_in)
            other = sorted({r["_out_of_state"] for r in rows if "_out_of_state" in r})   # rows about other states / All India: not stored
            n_other = sum("_out_of_state" in r for r in rows)
            rows = [r for r in rows if "_out_of_state" not in r]
            for r in rows:
                r.update(source_file=Path(state["input_path"]).name, source_hash=state["source_hash"],
                         source_type=state["source_type"], agent=agent_name, run_id=state["run_id"])
            return {"rows": {**state.get("rows", {}), agent_name: rows}, "queue": queue,
                    "trajectory": _trace(state, agent_name, f"{domain}_extract", {"records_in": len(rows_in)}, "ok",
                                         {"rows_out": len(rows), **({"other_states_not_stored": n_other, "states": other[:20]} if n_other else {})}, t0)}
        except Exception as e:  # one agent failing must not stop the others
            return {"queue": queue, "agent_errors": state.get("agent_errors", []) + [f"{agent_name}: {e}"],
                    "trajectory": _trace(state, agent_name, f"{domain}_extract", {"records_in": len(rows_in)},
                                         "error", str(e), t0)}
    return node


# ----------------------------------------------------------------------------- load (+ plausibility)
def load_node(state: State) -> State:
    t0 = time.perf_counter()
    ai = _ai(state)
    summary = {"by_agent": {}, "rejected": [], "review": 0}
    try:
        _maybe_fault(state, "load")
        con = connect(state.get("db_path") or ROOT / "db" / "manipur.db")
        started = state["started_at"]
        con.execute("INSERT OR REPLACE INTO runs (run_id,input_file,source_hash,started_at,status) VALUES (?,?,?,?,?)",
                    (state["run_id"], Path(state["input_path"]).name, state["source_hash"], started, "running"))
        tot = dict(extracted=0, inserted=0, duplicate=0, invalid=0, rejected=0, quarantined=0)
        validated = {}
        for agent, rows in state.get("rows", {}).items():
            table, model = TABLES[agent]
            good = []
            for r in rows:
                notes = validate_row(agent, r)
                r["validation_status"] = "invalid" if notes else "valid"
                r["validation_notes"] = ";".join(notes)
                try:
                    good.append(model(**r).model_dump())
                except ValidationError as e:
                    summary["rejected"].append({"agent": agent, "locator": r.get("locator"),
                                                "reason": "; ".join(f"{'.'.join(map(str, x['loc']))}:{x['type']}"
                                                                    for x in e.errors())[:200]})
            validated[agent] = (rows, good)
        # plausibility: impossible values are set aside for a human, not stored in the data tables
        flagged, p_notes = plausibility.check(ai if (state.get("ai_enabled", True)) else None, {a: g for a, (_, g) in validated.items()})
        review_items = []
        for agent, (rows, good) in validated.items():
            table, model = TABLES[agent]
            keep = []
            for i, row in enumerate(good):
                info = flagged.get((agent, i))
                if info:
                    review_items.append(plausibility.review_row(agent, table, row, info))
                else:
                    keep.append(row)
            ins, dup = insert_rows(con, agent, keep)
            inv = sum(g["validation_status"] == "invalid" for g in keep)
            nq = len(good) - len(keep)
            summary["by_agent"][agent] = {"table": table, "extracted": len(rows), "inserted": ins, "duplicates": dup,
                                          "invalid": inv, "rejected": len(rows) - len(good), "quarantined": nq}
            tot["extracted"] += len(rows); tot["inserted"] += ins; tot["duplicate"] += dup
            tot["invalid"] += inv; tot["rejected"] += len(rows) - len(good); tot["quarantined"] += nq
        if review_items:
            summary["review"] = insert_review(con, state["run_id"], review_items)
        summary["totals"] = tot
        summary["plausibility"] = p_notes
        con.commit()
        con.close()
        return {"result": summary, "error": None,
                "trajectory": _trace(state, "load", "sqlite_load", {"db": "manipur.db"}, "ok", {**tot, "plausibility": p_notes}, t0, ai)}
    except Exception as e:
        return {"error": f"load failed: {e}", "retries": state.get("retries", 0) + 1, "result": summary,
                "trajectory": _trace(state, "load", "sqlite_load", {}, "error", str(e), t0, ai)}


def finish_node(state: State) -> State:
    res = state.get("result") or {}
    tot = res.get("totals", {})
    ai = _AI.pop(state.get("run_id"), None)
    queue_unprocessed = [g for g in state.get("groups", []) if g["domain"] is None]  # e.g. metadata-only groups
    # a run that stored nothing is not a success, whatever the agents reported
    stored_nothing = not state.get("error") and not state.get("agent_errors") and tot.get("inserted", 0) + tot.get("duplicate", 0) == 0 \
        and tot.get("quarantined", 0) == 0
    if state.get("error") or not state.get("rows"):
        status = "failed"
    elif state.get("agent_errors"):
        status = "partial"
    elif stored_nothing:
        status = "failed"
    else:
        status = "complete"
    finished = datetime.now().isoformat(timespec="seconds")
    error = state.get("error")
    if not error and not state.get("rows") and not state.get("agent_errors"):
        why = "no recognised soil / rainfall / production records (unclassified: " + ", ".join(g["pattern"] for g in queue_unprocessed) + ")"
        if state.get("retries", 0) >= MAX_RECOVERIES:
            why += f"; retry budget exhausted ({state.get('retries', 0)}/{MAX_RECOVERIES} recoveries, tried: {', '.join(state.get('tried', []))})"
        error = why
    if not error and stored_nothing and state.get("rows"):
        error = "every extracted row was rejected by the schema (nothing stored)"
    notes = {"error": error, "agent_errors": state.get("agent_errors"),
             "unclassified_groups": [g["pattern"] for g in queue_unprocessed],
             "retry_budget": {"max": MAX_RECOVERIES, "used": state.get("retries", 0), "strategies": state.get("tried", [])},
             "ai": ai.summary() if ai else None, "quarantined_for_review": tot.get("quarantined", 0),
             "excluded_non_crop": [x for g in state.get("groups", []) for x in g.get("excluded_non_crop", [])][:60]}
    try:
        con = connect(state.get("db_path") or ROOT / "db" / "manipur.db")
        con.execute("INSERT OR IGNORE INTO runs (run_id,input_file,source_hash,started_at,status) VALUES (?,?,?,?,?)",
                    (state["run_id"], Path(state["input_path"]).name, state.get("source_hash"), state["started_at"], status))
        con.execute("UPDATE runs SET finished_at=?, status=?, rows_extracted=?, rows_inserted=?, rows_duplicate=?, "
                    "rows_invalid=?, retries=?, notes=? WHERE run_id=?",
                    (finished, status, tot.get("extracted", 0), tot.get("inserted", 0), tot.get("duplicate", 0),
                     tot.get("invalid", 0), state.get("retries", 0), json.dumps(notes), state["run_id"]))
        con.commit()
        con.close()
    except Exception as e:
        notes["db_log_error"] = str(e)
    # run trace + automation log
    runs_dir = LOG_DIR / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    traj = list(state.get("trajectory") or [])
    if ai:
        traj += ai.drain()
    report = {"run_id": state["run_id"], "input": Path(state["input_path"]).name, "status": status,
              "source_type": state.get("source_type"), "groups": state.get("groups"), "result": res,
              "notes": notes, "retries": state.get("retries", 0), "trajectory": traj}
    (runs_dir / f"{state['run_id']}.json").write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    log = LOG_DIR / "automation_log.csv"
    new = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["run_id", "timestamp", "input_file", "status", "manual_intervention", "manual_note",
                        "rows_extracted", "rows_inserted", "retries", "notes"])
        extra = f"ai_calls={ai.calls}" if ai and ai.calls else ""
        w.writerow([state["run_id"], finished, Path(state["input_path"]).name, status,
                    "yes" if state.get("manual_note") else "no", state.get("manual_note", ""),
                    tot.get("extracted", 0), tot.get("inserted", 0), state.get("retries", 0),
                    "; ".join(x for x in [error or "; ".join(state.get("agent_errors", [])), extra] if x)])
    return {"status": status, "result": {**res, "notes": notes}, "trajectory": traj}


# ----------------------------------------------------------------------------- routing
def after_inspect(state):
    return "finish" if state.get("error") else "plan"


def after_read(state):
    if not state.get("error"):
        return "classify"
    return "recover" if state.get("retries", 0) < MAX_RECOVERIES and recovery_options(state, False) else "finish"


def next_agent(state):
    q = state.get("queue") or []
    if q:
        return agent_mod.AGENTS[q[0]][0]
    if state.get("rows"):
        return "load"
    # nothing recognised: with budget left, a model-chosen repair (structure / prose / OCR) may still rescue the file
    ai = _AI.get(state.get("run_id"))
    if (ai is not None and _use_ai(state, ai) and state.get("retries", 0) < MAX_RECOVERIES and not state.get("agent_errors")
            and recovery_options(state, True) and state.get("source_type") != "json"):
        return "recover"
    return "finish"


def after_load(state):
    if state.get("error") and state.get("retries", 0) < MAX_RECOVERIES:
        return "load"
    # rows were extracted but every one was rejected / nothing was stored: a repair strategy may still rescue the file
    tot = (state.get("result") or {}).get("totals") or {}
    ai = _AI.get(state.get("run_id"))
    if (not state.get("error") and tot and tot.get("inserted", 0) + tot.get("duplicate", 0) + tot.get("quarantined", 0) == 0
            and ai is not None and _use_ai(state, ai) and state.get("retries", 0) < MAX_RECOVERIES
            and recovery_options(state, True) and state.get("source_type") != "json"):
        return "recover"
    return "finish"


def build_graph():
    g = StateGraph(State)
    g.add_node("inspect", inspect_node)
    g.add_node("plan", plan_node)
    g.add_node("read", read_node)
    g.add_node("recover", recover_node)
    g.add_node("classify", classify_node)
    for d in AGENT_ORDER:
        g.add_node(agent_mod.AGENTS[d][0], _make_agent_node(d))
    g.add_node("load", load_node)
    g.add_node("finish", finish_node)
    g.add_edge(START, "inspect")
    g.add_conditional_edges("inspect", after_inspect, ["plan", "finish"])
    g.add_edge("plan", "read")
    g.add_conditional_edges("read", after_read, ["classify", "recover", "finish"])
    g.add_edge("recover", "read")
    targets = [agent_mod.AGENTS[d][0] for d in AGENT_ORDER] + ["load", "recover", "finish"]
    g.add_conditional_edges("classify", next_agent, targets)
    for d in AGENT_ORDER:
        g.add_conditional_edges(agent_mod.AGENTS[d][0], next_agent, targets)
    g.add_conditional_edges("load", after_load, ["load", "recover", "finish"])
    g.add_edge("finish", END)
    return g.compile()


def run_file(path, db_path=None, inject_fault=None, manual_note="", ai=None) -> dict:
    """ai: True / False to force the AI steps on / off for this run (default: AGRI_AI environment variable, on)"""
    graph = build_graph()
    enabled = ai_on() if ai is None else bool(ai)
    init = {"input_path": str(path), "db_path": str(db_path) if db_path else None, "inject_fault": inject_fault,
            "manual_note": manual_note, "trajectory": [], "ai_enabled": enabled}
    final = graph.invoke(init, config=run_config(Path(path).name, {"ai": enabled, "inject_fault": inject_fault or ""}))
    notes = (final.get("result") or {}).get("notes") or {}
    return {"run_id": final["run_id"], "status": final["status"], "source_type": final.get("source_type"),
            "groups": final.get("groups"), "result": final.get("result"), "retries": final.get("retries", 0),
            "trajectory": final.get("trajectory"), "ai": notes.get("ai")}
