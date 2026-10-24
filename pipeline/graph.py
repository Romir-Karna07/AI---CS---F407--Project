"""
LangGraph orchestration.

    inspect -> read --(error)--> recover -> read ...          (max 2 recoveries, then finish as failed)
                 |
                 v
              classify -> soil_agent -> rainfall_agent -> production_agent -> load -> finish
                          (conditional: only agents that have a matching record group are visited)

Fixed code decides: file type, structure, which columns mean what, units, normalisation, validation, routing.
The LLM (optional, see llm.py) is only asked when headers/groups cannot be classified deterministically.
Every tool call is traced into state["trajectory"] (used later for the tool-selection / trajectory metrics).
"""
import csv
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from . import agents as agent_mod
from .concepts import DOMAIN_CORE, map_columns, register_synonym, score_domains
from .db import connect, insert_rows
from .llm import get_llm, llm_classify, llm_map_headers
from .normalize import normalize_district
from .readers import YEAR_RE, read_csv_file, read_json, to_number
from .schemas import TABLES, validate_row

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.getenv("AGRI_LOG_DIR") or ROOT / "logs")   # tests point this at a temp dir
MAX_RECOVERIES = 2
AGENT_ORDER = ["soil", "rainfall", "production"]


class State(TypedDict, total=False):
    input_path: str
    db_path: str
    run_id: str
    started_at: str
    source_hash: str
    source_type: str
    force_format: str | None
    inject_fault: str | None
    manual_note: str
    parsed: dict
    groups: list
    queue: list
    rows: dict
    trajectory: list
    error: str | None
    retries: int
    agent_errors: list
    result: dict
    status: str


# ----------------------------------------------------------------------------- tracing
def _trace(state, step, tool=None, args=None, status="ok", detail=None, t0=None):
    entry = {"step": step, "tool": tool, "args": args or {}, "status": status, "detail": detail,
             "ms": round((time.perf_counter() - t0) * 1000, 1) if t0 else None}
    return state.get("trajectory", []) + [entry]


def _maybe_fault(state, where):
    """Fault injection for demos (--inject-fault): raise in the named node on the first attempt only."""
    if state.get("inject_fault") == where and state.get("retries", 0) == 0:
        raise RuntimeError(f"injected fault in {where}")


# ----------------------------------------------------------------------------- nodes
def inspect_node(state: State) -> State:
    t0 = time.perf_counter()
    p = Path(state["input_path"])
    run_id = state.get("run_id") or f"run_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    out = {"run_id": run_id, "started_at": datetime.now().isoformat(timespec="seconds"), "retries": 0,
           "agent_errors": [], "rows": {}, "error": None}
    try:
        data = p.read_bytes()
        out["source_hash"] = hashlib.sha256(data).hexdigest()
        head = data[:300].lstrip()
        ext = p.suffix.lower()
        if head.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM
            head = head[3:]
        looks_json = head.lstrip()[:1] in (b"{", b"[")
        out["source_type"] = ("json" if ext in (".json", ".geojson", ".jsonl", ".ndjson") or looks_json else "csv")
        out["trajectory"] = _trace(state, "inspect", "inspect_file", {"file": p.name}, "ok",
                                   {"bytes": len(data), "type": out["source_type"]}, t0)
    except Exception as e:
        out["error"] = f"cannot open input: {e}"
        out["trajectory"] = _trace(state, "inspect", "inspect_file", {"file": p.name}, "error", str(e), t0)
    return out


def read_node(state: State) -> State:
    t0 = time.perf_counter()
    stype = state.get("force_format") or state["source_type"]
    tool = "read_json_data" if stype == "json" else "read_csv_data"
    args = {"file_path": Path(state["input_path"]).name}
    try:
        _maybe_fault(state, "read")
        parsed = (read_json if stype == "json" else read_csv_file)(Path(state["input_path"]))
        n = sum(len(g["rows"]) for g in parsed["groups"])
        return {"parsed": parsed, "error": None, "source_type": stype,
                "trajectory": _trace(state, "read", tool, args, "ok",
                                     {"records": n, "groups": len(parsed["groups"]), "notes": parsed["notes"]}, t0)}
    except Exception as e:
        return {"error": f"read failed ({stype}): {e}",
                "trajectory": _trace(state, "read", tool, args, "error", str(e), t0)}


def recover_node(state: State) -> State:
    """1st recovery: retry the same parser (transient error). 2nd: try the other parser (mislabelled file)."""
    t0 = time.perf_counter()
    attempt = state.get("retries", 0) + 1
    current = state.get("force_format") or state["source_type"]
    fmt = current if attempt == 1 else ("csv" if current == "json" else "json")
    return {"force_format": fmt, "retries": attempt, "error": None,
            "trajectory": _trace(state, "recover", None, {"attempt": attempt, "retry_with": fmt}, "ok", None, t0)}


def classify_node(state: State) -> State:
    t0 = time.perf_counter()
    llm = get_llm()
    traj = state.get("trajectory", [])
    groups = []
    for g in state["parsed"]["groups"]:
        rows = g["rows"]
        sample = rows[:300]
        keys = list(dict.fromkeys(k for r in sample for k in r["data"]))
        mapping, unmapped = map_columns(keys)
        mcount = len(agent_mod.month_columns(keys))
        scores = score_domains(mapping.values())
        if mcount >= 6:
            scores["rainfall"] += 3
        method = "rules"
        has_numeric = any(to_number(r["data"].get(k)) is not None for k in unmapped for r in sample[:50])
        numeric_unmapped = unmapped[:40] if has_numeric else []   # text columns too: district / year names vary as well
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
        if llm and numeric_unmapped and (max(scores.values()) < 3 or gaps):
            t1 = time.perf_counter()
            proposal = llm_map_headers(llm, numeric_unmapped, sample[0]["data"])
            trial = {**mapping, **{k: v for k, v in proposal.items() if v not in mapping.values()}}
            trial_scores = score_domains(trial.values())
            if mcount >= 6:
                trial_scores["rainfall"] += 3
            # trust the LLM only if it makes the group a recognisable domain or fills a missing district / time column
            accepted = max(trial_scores.values()) > max(scores.values()) or                 len(missing_identity(trial.values(), trial_scores)) < len(gaps)
            traj = traj + [{"step": "classify", "tool": "llm_map_headers", "args": {"columns": numeric_unmapped[:10]},
                            "status": "ok" if accepted else "rejected", "detail": proposal,
                            "ms": round((time.perf_counter() - t1) * 1000, 1)}]
            if accepted:
                for k, v in proposal.items():
                    register_synonym(k, v)
                mapping, unmapped = map_columns(keys)
                scores = score_domains(mapping.values())
                if mcount >= 6:
                    scores["rainfall"] += 3
                method = "rules+llm_headers"
        best = max(scores.values())
        domain = None
        if best > 0:
            tops = [d for d in AGENT_ORDER if scores[d] == best]
            domain = tops[0]
            if len(tops) > 1 and llm:
                t1 = time.perf_counter()
                d = llm_classify(llm, keys, sample[0]["data"])
                traj = traj + [{"step": "classify", "tool": "llm_classify", "args": {"tie": tops}, "status": "ok",
                                "detail": d, "ms": round((time.perf_counter() - t1) * 1000, 1)}]
                domain, method = (d if d in tops else domain), "rules+llm_tiebreak"
        elif llm and has_numeric:   # nothing numeric -> nothing to extract; do not spend an LLM call
            t1 = time.perf_counter()
            d = llm_classify(llm, keys, sample[0]["data"])
            traj = traj + [{"step": "classify", "tool": "llm_classify", "args": {}, "status": "ok", "detail": d,
                            "ms": round((time.perf_counter() - t1) * 1000, 1)}]
            has_core = d in DOMAIN_CORE and (set(mapping.values()) & DOMAIN_CORE[d])
            domain, method = (d if has_core else None), ("llm" if has_core else "llm_rejected_no_measurement_columns")
        groups.append({"pattern": g["pattern"], "domain": domain, "records": len(rows), "scores": scores,
                       "method": method, "mapped_headers": {k: v for k, v in mapping.items()},
                       "unmapped_headers": [k for k in unmapped][:30]})
    queue = [d for d in AGENT_ORDER if any(g["domain"] == d for g in groups)]
    traj = traj + [{"step": "classify", "tool": None, "args": {}, "status": "ok",
                    "detail": {"groups": [(g["pattern"], g["domain"], g["records"]) for g in groups], "queue": queue},
                    "ms": round((time.perf_counter() - t0) * 1000, 1)}]
    return {"groups": groups, "queue": queue, "trajectory": traj}


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
            for r in rows:
                r.update(source_file=Path(state["input_path"]).name, source_hash=state["source_hash"],
                         source_type=state["source_type"], agent=agent_name, run_id=state["run_id"])
            return {"rows": {**state.get("rows", {}), agent_name: rows}, "queue": queue,
                    "trajectory": _trace(state, agent_name, f"{domain}_extract", {"records_in": len(rows_in)}, "ok",
                                         {"rows_out": len(rows)}, t0)}
        except Exception as e:  # one agent failing must not stop the others
            return {"queue": queue, "agent_errors": state.get("agent_errors", []) + [f"{agent_name}: {e}"],
                    "trajectory": _trace(state, agent_name, f"{domain}_extract", {"records_in": len(rows_in)},
                                         "error", str(e), t0)}
    return node


def load_node(state: State) -> State:
    t0 = time.perf_counter()
    summary = {"by_agent": {}, "rejected": []}
    try:
        _maybe_fault(state, "load")
        con = connect(state.get("db_path") or ROOT / "db" / "manipur.db")
        started = state["started_at"]
        con.execute("INSERT OR REPLACE INTO runs (run_id,input_file,source_hash,started_at,status) VALUES (?,?,?,?,?)",
                    (state["run_id"], Path(state["input_path"]).name, state["source_hash"], started, "running"))
        tot = dict(extracted=0, inserted=0, duplicate=0, invalid=0, rejected=0)
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
            ins, dup = insert_rows(con, agent, good)
            inv = sum(g["validation_status"] == "invalid" for g in good)
            summary["by_agent"][agent] = {"table": table, "extracted": len(rows), "inserted": ins, "duplicates": dup,
                                          "invalid": inv, "rejected": len(rows) - len(good)}
            tot["extracted"] += len(rows); tot["inserted"] += ins; tot["duplicate"] += dup
            tot["invalid"] += inv; tot["rejected"] += len(rows) - len(good)
        summary["totals"] = tot
        con.commit()
        con.close()
        return {"result": summary, "error": None,
                "trajectory": _trace(state, "load", "sqlite_load", {"db": "manipur.db"}, "ok", tot, t0)}
    except Exception as e:
        return {"error": f"load failed: {e}", "retries": state.get("retries", 0) + 1, "result": summary,
                "trajectory": _trace(state, "load", "sqlite_load", {}, "error", str(e), t0)}


def finish_node(state: State) -> State:
    res = state.get("result") or {}
    tot = res.get("totals", {})
    queue_unprocessed = [g for g in state.get("groups", []) if g["domain"] is None]  # e.g. metadata-only groups
    if state.get("error") or not state.get("rows"):
        status = "failed"
    elif state.get("agent_errors"):
        status = "partial"
    else:
        status = "complete"
    finished = datetime.now().isoformat(timespec="seconds")
    error = state.get("error")
    if not error and not state.get("rows") and not state.get("agent_errors"):
        error = "no recognised soil / rainfall / production records (unclassified: " +                 ", ".join(g["pattern"] for g in queue_unprocessed) + ")"
    notes = {"error": error, "agent_errors": state.get("agent_errors"),
             "unclassified_groups": [g["pattern"] for g in queue_unprocessed]}
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
    report = {"run_id": state["run_id"], "input": Path(state["input_path"]).name, "status": status,
              "source_type": state.get("source_type"), "groups": state.get("groups"), "result": res,
              "notes": notes, "retries": state.get("retries", 0), "trajectory": state.get("trajectory")}
    (runs_dir / f"{state['run_id']}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log = LOG_DIR / "automation_log.csv"
    new = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["run_id", "timestamp", "input_file", "status", "manual_intervention", "manual_note",
                        "rows_extracted", "rows_inserted", "retries", "notes"])
        w.writerow([state["run_id"], finished, Path(state["input_path"]).name, status,
                    "yes" if state.get("manual_note") else "no", state.get("manual_note", ""),
                    tot.get("extracted", 0), tot.get("inserted", 0), state.get("retries", 0),
                    (error or "; ".join(state.get("agent_errors", []))) or ""])
    return {"status": status, "result": {**res, "notes": notes}}


# ----------------------------------------------------------------------------- routing
def after_inspect(state):
    return "finish" if state.get("error") else "read"


def after_read(state):
    if not state.get("error"):
        return "classify"
    return "recover" if state.get("retries", 0) < MAX_RECOVERIES else "finish"


def next_agent(state):
    q = state.get("queue") or []
    if q:
        return agent_mod.AGENTS[q[0]][0]
    return "load" if state.get("rows") else "finish"   # nothing extracted: no point opening the database


def after_load(state):
    if state.get("error") and state.get("retries", 0) < MAX_RECOVERIES:
        return "load"
    return "finish"


def build_graph():
    g = StateGraph(State)
    g.add_node("inspect", inspect_node)
    g.add_node("read", read_node)
    g.add_node("recover", recover_node)
    g.add_node("classify", classify_node)
    for d in AGENT_ORDER:
        g.add_node(agent_mod.AGENTS[d][0], _make_agent_node(d))
    g.add_node("load", load_node)
    g.add_node("finish", finish_node)
    g.add_edge(START, "inspect")
    g.add_conditional_edges("inspect", after_inspect, ["read", "finish"])
    g.add_conditional_edges("read", after_read, ["classify", "recover", "finish"])
    g.add_edge("recover", "read")
    targets = [agent_mod.AGENTS[d][0] for d in AGENT_ORDER] + ["load", "finish"]
    g.add_conditional_edges("classify", next_agent, targets)
    for d in AGENT_ORDER:
        g.add_conditional_edges(agent_mod.AGENTS[d][0], next_agent, targets)
    g.add_conditional_edges("load", after_load, ["load", "finish"])
    g.add_edge("finish", END)
    return g.compile()


def run_file(path, db_path=None, inject_fault=None, manual_note="") -> dict:
    graph = build_graph()
    final = graph.invoke({"input_path": str(path), "db_path": str(db_path) if db_path else None,
                          "inject_fault": inject_fault, "manual_note": manual_note, "trajectory": []})
    return {"run_id": final["run_id"], "status": final["status"], "source_type": final.get("source_type"),
            "groups": final.get("groups"), "result": final.get("result"), "retries": final.get("retries", 0),
            "trajectory": final.get("trajectory")}
