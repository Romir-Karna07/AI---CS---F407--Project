"""
Phase 2 evaluation run.   python eval/run_eval.py [--skip-llm-cases] [--no-official-db]

Runs the real pipeline (run.py's graph) on every case in eval/cases/, compares the database rows it produced with
eval/reference/*.json, and writes
    eval/phase2_metrics.csv          one row per metric per scope (agent / source group / end_to_end / surprise)
    eval/results/per_case.csv        per-case numbers
    eval/results/history/*.csv       a timestamped copy of every metrics file (so first-run vs later runs stay visible)
Nothing is hard-coded to a case: the scorer only reads the reference files and what the pipeline actually stored.

Metrics that need things we do not have yet are written with an empty value and status "pending":
M1/M2 (Phase 1 inventory), M5 (instructor answer key), M18 (no adapter trained).
"""
import argparse
import csv
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser()
ap.add_argument("--skip-llm-cases", action="store_true", help="drop cases that need the Groq header mapper")
ap.add_argument("--no-official-db", action="store_true", help="skip the M14-M17 SQL checks on db/manipur.db")
ap.add_argument("--db", default=str(ROOT / "db" / "manipur.db"))
args = ap.parse_args()

WORK = Path(tempfile.mkdtemp(prefix="agri_eval_"))
os.environ["AGRI_LOG_DIR"] = str(WORK / "logs")
from pipeline.graph import run_file  # noqa: E402  (after AGRI_LOG_DIR is set)
from pipeline.llm import get_llm  # noqa: E402

TABLES = {"soil": "soil_health", "rainfall": "rainfall_monthly", "production": "crop_production"}
SOIL_NUTRIENTS = ["n", "p", "k", "oc", "ph", "ec", "s", "zn", "fe", "cu", "mn", "b"]
FACTORS = [1, 0.404686, 0.1, 0.001, 25.4, 10, 1000, 100, 1 / 0.404686]   # unit conversions a value may have gone through
FLOOR_TARGET = {  # metric: (floor, target)  - from Phase2_Eval.pdf
    "M1": (0.50, 1.00), "M2": (0.20, 0.60), "M3": (0.50, 0.85), "M4": (0.60, 0.95), "M5": (0.30, 0.75),
    "M6": (0.60, 0.90), "M7": (0.60, 0.90), "M8": (0.50, 0.80), "M9": (0.50, 0.90), "M10": (0.70, 0.95),
    "M11": (0.50, 0.90), "M12": (0.30, 0.80), "M13": (0.50, 0.10), "M14": (0.80, 1.00), "M15": (0.70, 0.98),
    "M16": (0.10, 0.00), "M17": (1, 3)}
WEIGHT = {"M1": 3, "M2": 2, "M3": 6, "M4": 3, "M5": 6, "M6": 2, "M7": 2, "M8": 2, "M9": 2, "M10": 2, "M11": 4,
          "M12": 6, "M13": 2, "M14": 3, "M15": 2, "M16": 2, "M17": 3}
NAMES = {"M1": "Source mapping coverage", "M2": "Implementation coverage", "M3": "Field-level F1", "M4": "Provenance accuracy",
         "M5": "Surprise-item F1", "M6": "Tool selection accuracy", "M7": "Tool input accuracy", "M8": "Trajectory match (strict)",
         "M9": "Task completion", "M10": "Groundedness", "M11": "End-to-end workflow success", "M12": "Automation rate",
         "M13": "Redundant call ratio", "M14": "Mandatory field completeness", "M15": "Validation pass rate",
         "M16": "Duplicate rate", "M17": "Live trace-back", "M18": "Adapter comparison"}


def points(metric, value):
    if value is None or metric not in FLOOR_TARGET:
        return None
    lo, hi = FLOOR_TARGET[metric]
    frac = (value - lo) / (hi - lo)
    return max(0.0, min(1.0, frac)) * WEIGHT[metric]


# ------------------------------------------------------------------------------------------------ provenance tracer
def _nums_from_node(node, out):
    if isinstance(node, dict):
        for v in node.values():
            _nums_from_node(v, out)
    elif isinstance(node, list):
        for v in node:
            _nums_from_node(v, out)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.append(float(node))
    elif isinstance(node, str):
        try:
            out.append(float(node.replace(",", ".")))
        except ValueError:
            pass


def _read_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass


def source_numbers(path, locator):
    """Numbers found in the source at `locator`, or None if the locator does not lead anywhere."""
    text = _read_text(path)
    if locator.startswith("$"):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            doc = [json.loads(l) for l in text.splitlines() if l.strip()]
        node = doc
        try:
            for m in re.finditer(r'\.(\w+)|\["([^"]+)"\]|\[(\d+)\]', locator[1:]):
                name, key, idx = m.groups()
                node = node[name] if name else node[key] if key else node[int(idx)]
        except (KeyError, IndexError, TypeError):
            return None
        out = []
        _nums_from_node(node, out)
        return out
    m = re.match(r"line (\d+)(?:, cols ([A-Z]+)-([A-Z]+))?", locator)
    if not m:
        return None
    ln = int(m.group(1))
    delim = ";" if text.count(";") > text.count(",") else ","
    rows = list(csv.reader(text.splitlines(), delimiter=delim))
    if ln - 1 >= len(rows):
        return None
    cells = rows[ln - 1]
    if m.group(2):
        def idx(s):
            n = 0
            for ch in s:
                n = n * 26 + ord(ch) - 64
            return n - 1
        cells = cells[idx(m.group(2)):idx(m.group(3)) + 1]
    out = []
    for c in cells:
        try:
            out.append(float(c.strip().replace(",", ".") if delim == ";" else c.strip().replace(",", "")))
        except ValueError:
            pass
    return out


def supported(value, nums):
    return any(abs(n * f - value) <= max(0.011 * abs(value), 0.006) for n in nums for f in FACTORS)


def trace_row(path, locator, numeric_fields: dict):
    """-> (n_supported, n_total). A locator that leads nowhere supports nothing."""
    nums = source_numbers(path, locator)
    if nums is None:
        return 0, len(numeric_fields)
    return sum(supported(v, nums) for v in numeric_fields.values()), len(numeric_fields)


# ------------------------------------------------------------------------------------------------ predicted fields
def unit_map(unit_text):
    return {k.strip(): v.strip() for k, v in (p.split(":", 1) for p in unit_text.split(";") if ":" in p)}


def predicted_records(db_path, agent, run_id):
    """-> {key: {field: (value, unit)}}, and per-row info for tracing"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(f"SELECT * FROM {TABLES[agent]} WHERE run_id=?", (run_id,)).fetchall()
    con.close()
    recs, info = {}, []
    for r in rows:
        r = dict(r)
        um = unit_map(r["unit"]) if ";" in r["unit"] else {}
        f = {}
        if agent == "soil":
            for k in SOIL_NUTRIENTS:
                if r[k] is not None:
                    f[k] = (r[k], um.get(k, r["unit"]))
            for k in ("tehsil", "category"):
                if r[k] not in (None, "UNSPECIFIED"):
                    f[k] = (r[k], "text")
            key = (r["district"], r["period"], r["crop"], r["village"])
        elif agent == "rainfall":
            if r["rainfall_mm"] is not None:
                f["rainfall_mm"] = (r["rainfall_mm"], r["unit"])
            if r["rainy_days_ge_2_5mm"] is not None:
                f["rainy_days_ge_2_5mm"] = (r["rainy_days_ge_2_5mm"], "days")
            key = (r["district"], r["year_month"], r["crop"], "")
        else:
            for k, ukey in (("area_ha", "area"), ("production_t", "production"), ("yield_t_ha", "yield")):
                if r[k] is not None:
                    f[k] = (r[k], um.get(ukey, r["unit"]))
            key = (r["district"], r["period"], r["crop"], r["season"])
        nkey = tuple(str(x).strip().casefold() for x in key)
        recs.setdefault(nkey, {}).update(f)
        info.append({"locator": r["locator"], "numeric": {k: v[0] for k, v in f.items() if v[1] != "text"}, "key": nkey})
    return recs, info


def value_ok(exp, got):
    ev, eu = exp["value"], exp["unit"]
    gv, gu = got
    if eu != gu:
        return False
    if eu == "text":
        return str(ev).strip().casefold() == str(gv).strip().casefold()
    if isinstance(ev, int):
        return gv == ev
    return abs(gv - ev) <= 0.01 * abs(ev) + 1e-9


# ------------------------------------------------------------------------------------------------ run the cases
refs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((EVAL / "reference").glob("*.json"))]
if args.skip_llm_cases:
    refs = [r for r in refs if not r["needs_llm"]]
print(f"{len(refs)} cases; LLM available: {get_llm() is not None}; work dir {WORK}")

results = []
for ref in refs:
    cid, agent = ref["case_id"], ref["agent"]
    is_ops = cid.startswith("ops_")
    db = WORK / f"{cid}.db"
    case_path = EVAL / ref["file"]
    t0 = time.perf_counter()
    out = run_file(case_path, db_path=db, inject_fault=ref["inject_fault"])
    secs = time.perf_counter() - t0
    traj = out["trajectory"] or []
    tools = [t["tool"] for t in traj if t["tool"]]
    retries = sum(1 for t in traj if t["step"] == "recover")
    res = {"case": ref, "id": cid, "agent": agent, "group": ref["source_group"], "ops": is_ops, "status": out["status"],
           "tools": tools, "retries": retries, "seconds": secs, "tp": 0, "extracted": 0, "ref_fields": 0,
           "complete": None, "traced_rows": 0, "ok_rows": 0, "sup_fields": 0, "tot_fields": 0, "wrong": [], "missing": []}
    if agent in TABLES and out["status"] in ("complete", "partial"):
        recs, info = predicted_records(db, agent, out["run_id"])
        expected = {}
        for r in ref["records"]:
            k = tuple(str(r["key"][x]).strip().casefold() for x in ("location", "period", "crop", "entity"))
            expected[k] = r["fields"]
        res["ref_fields"] = sum(len(v) for v in expected.values())
        for k, fields in recs.items():
            for f, got in fields.items():
                res["extracted"] += 1
                exp = expected.get(k, {}).get(f)
                if exp is not None and value_ok(exp, got):
                    res["tp"] += 1
                else:
                    res["wrong"].append((k, f, got, exp))
        for k, fields in expected.items():
            for f, exp in fields.items():
                if f not in recs.get(k, {}):
                    res["missing"].append((k, f, exp))
        required_ok = True
        for k in expected:
            row = recs.get(k)
            if not row:
                required_ok = False
            elif agent == "soil" and sum(f in row for f in SOIL_NUTRIENTS) < 3:
                required_ok = False
            elif agent == "rainfall" and "rainfall_mm" not in row:
                required_ok = False
            elif agent == "production" and not ({"area_ha", "production_t"} & set(row)):
                required_ok = False
        res["complete"] = out["status"] == "complete" and required_ok
        for i in info:
            if not i["numeric"]:
                continue
            s, t = trace_row(case_path, i["locator"], i["numeric"])
            res["traced_rows"] += 1
            res["ok_rows"] += int(s == t)
            res["sup_fields"] += s
            res["tot_fields"] += t
    elif agent in TABLES:
        res["ref_fields"] = sum(len(r["fields"]) for r in ref["records"])
        res["complete"] = False
    # trajectory scoring (strict, positional)
    exp_t = ref["expected_trajectory"]
    res["traj_match"] = tools == exp_t
    res["tool_correct"] = sum(1 for a, b in zip(tools, exp_t) if a == b)
    res["tool_calls"] = len(tools)
    bad_in = 0
    for t in traj:
        if not t["tool"]:
            continue
        if t["tool"] in ("inspect_file", "read_json_data", "read_csv_data"):
            if (t["args"].get("file") or t["args"].get("file_path")) != case_path.name:
                bad_in += 1
        elif t["tool"].endswith("_extract"):
            if not t["args"].get("records_in"):
                bad_in += 1
    res["tool_inputs_ok"] = res["tool_calls"] - bad_in
    res["redundant"] = max(0, len(tools) - len(exp_t))
    res["expected_status_ok"] = out["status"] == ref["expected_status"]
    results.append(res)
    flag = "ok " if res["expected_status_ok"] else "BAD"
    f1 = (2 * res["tp"] / (res["extracted"] + res["ref_fields"])) if (res["extracted"] + res["ref_fields"]) else float("nan")
    print(f"  {flag} {cid:<42} status={out['status']:<8} F1={f1:.3f} fields {res['tp']}/{res['extracted']} ext, {res['ref_fields']} ref  "
          f"traj={'=' if res['traj_match'] else '!='} {secs:.1f}s")


# ------------------------------------------------------------------------------------------------ aggregate
def f1_of(rs):
    tp, ex, rf = sum(r["tp"] for r in rs), sum(r["extracted"] for r in rs), sum(r["ref_fields"] for r in rs)
    p = tp / ex if ex else 0.0
    r_ = tp / rf if rf else 0.0
    return (2 * p * r_ / (p + r_) if p + r_ else 0.0), tp, ex, rf


rows_out = []   # metric rows


def emit(metric, scope, value, num=None, den=None, status="measured", notes=""):
    lo, hi = FLOOR_TARGET.get(metric, (None, None))
    pts = points(metric, value) if scope in ("end_to_end", "database") else None
    rows_out.append({"metric_id": metric, "metric_name": NAMES[metric], "scope": scope,
                     "value": "" if value is None else round(value, 4), "numerator": "" if num is None else round(num, 4),
                     "denominator": "" if den is None else round(den, 4), "floor": lo if lo is not None else "",
                     "target": hi if hi is not None else "", "points": "" if pts is None else round(pts, 2),
                     "max_points": WEIGHT.get(metric, ""), "status": status, "notes": notes})


scored = [r for r in results if not r["ops"] and r["agent"] in TABLES]
agents = sorted({r["agent"] for r in scored})
groups = sorted({r["group"] for r in scored})

# M3
per_agent = {}
for a in agents:
    f, tp, ex, rf = f1_of([r for r in scored if r["agent"] == a])
    per_agent[a] = f
    emit("M3", a, f, tp, ex, notes=f"P={tp / ex if ex else 0:.3f} R={tp / rf if rf else 0:.3f}; {rf} reference fields")
for g in groups:
    f, tp, ex, rf = f1_of([r for r in scored if r["group"] == g])
    emit("M3", g, f, tp, ex, notes="source group = file format")
macro = sum(per_agent.values()) / len(per_agent)
emit("M3", "end_to_end", macro, notes="macro-average of agent F1 (as the PDF requires); micro F1 overall = "
     f"{f1_of(scored)[0]:.3f}")

# M4 / M10 from the source tracer
for scope, rs in [(a, [r for r in scored if r["agent"] == a]) for a in agents] + [("end_to_end", scored)]:
    n, ok = sum(r["traced_rows"] for r in rs), sum(r["ok_rows"] for r in rs)
    emit("M4", scope, ok / n if n else None, ok, n, notes="automated: locator opened in the case file; every numeric field must appear "
         "at that locator (after unit conversion). The evaluator's live check is the scored one.")
    sf, tf = sum(r["sup_fields"] for r in rs), sum(r["tot_fields"] for r in rs)
    emit("M10", scope, sf / tf if tf else None, sf, tf, notes="numeric fields found in the source record; text fields not checked; "
         "no LLM judge used, so no judge calibration")

# M5 / M18 pending
emit("M5", "surprise", None, status="pending", notes="needs the instructor's 3 unseen items + answer key")
# M1 / M2 from docs/source_agent_map.csv, checked against the Phase 1 inventory workbook
map_rows = list(csv.DictReader((ROOT / "docs" / "source_agent_map.csv").open(encoding="utf-8")))
inv_ids = None
xlsx = ROOT / "TEAM_28_Sources.xlsx"
if xlsx.exists():
    try:
        import openpyxl
        inv_ids = [r[0] for r in openpyxl.load_workbook(xlsx, data_only=True)["Source Inventory"].iter_rows(min_row=2, values_only=True) if r[0]]
    except Exception as e:  # reporting only
        print("could not read inventory workbook:", e)
n_inv = len(inv_ids) if inv_ids else len(map_rows)
ids_match = inv_ids is None or sorted(inv_ids) == sorted(r["source_id"] for r in map_rows)
mapped = sum(1 for r in map_rows if r["agent"].strip() and (inv_ids is None or r["source_id"] in inv_ids))
impl = sum(1 for r in map_rows if r["status"] in ("done", "partial"))
emit("M1", "end_to_end", mapped / n_inv, mapped, n_inv, status="measured" if ids_match else "CHECK",
     notes="rows in docs/source_agent_map.csv with an agent / inventory rows" + ("" if ids_match else "; IDS DO NOT MATCH THE INVENTORY"))
emit("M2", "end_to_end", impl / n_inv, impl, n_inv,
     notes="sources with an agent marked done or partial / inventory rows; 'partial' only where rows are in db/manipur.db; spot-check live")
emit("M18", "end_to_end", None, status="pending", notes="no LoRA/QLoRA adapter trained yet; pipeline extraction is rule-based")

# M6-M9, M11, M13 (per agent + end_to_end)
traj_cases = [r for r in results]
for scope, rs in [(a, [r for r in results if r["agent"] == a and not r["ops"]]) for a in agents] + [("end_to_end", results)]:
    calls = sum(r["tool_calls"] for r in rs)
    emit("M6", scope, sum(r["tool_correct"] for r in rs) / calls if calls else None, sum(r["tool_correct"] for r in rs), calls,
         notes="vs the reference trajectory in eval/reference (authored from our own design, positional comparison)")
    emit("M7", scope, sum(r["tool_inputs_ok"] for r in rs) / calls if calls else None, sum(r["tool_inputs_ok"] for r in rs), calls,
         notes="file argument = the case file; extractor received >0 records")
    emit("M8", scope, sum(r["traj_match"] for r in rs) / len(rs) if rs else None, sum(r["traj_match"] for r in rs), len(rs),
         notes="match mode: strict (same tools, same order)")
    cs = [r for r in rs if r["complete"] is not None]
    emit("M9", scope, sum(bool(r["complete"]) for r in cs) / len(cs) if cs else None, sum(bool(r["complete"]) for r in cs), len(cs),
         notes="status complete AND every reference record present with its required fields")
    redundant = sum(r["redundant"] for r in rs)
    emit("M13", scope, redundant / calls if calls else None, redundant, calls,
         notes=f"retries reported separately: {sum(r['retries'] for r in rs)} recover step(s)")

expect_complete = [r for r in results if r["case"]["expected_status"] == "complete"]
reached = sum(r["status"] == "complete" for r in expect_complete)
emit("M11", "end_to_end", reached / len(expect_complete), reached, len(expect_complete),
     notes="runs that should complete and did (incl. 2 injected-fault runs)")
faults = [r for r in results if r["case"]["inject_fault"]]
emit("M11", "fault_injection", 1.0 if all(r["status"] == "complete" and r["retries"] >= 1 for r in faults) else 0.0,
     sum(r["status"] == "complete" and r["retries"] >= 1 for r in faults), len(faults),
     notes="1 = every injected fault (read, load) recovered and the run completed")
err = [r for r in results if r["case"]["expected_status"] == "failed"]
emit("M11", "bad_input_handling", sum(r["status"] == "failed" for r in err) / len(err), sum(r["status"] == "failed" for r in err),
     len(err), status="reported", notes="empty / non-data files fail cleanly instead of crashing (not part of M11 score)")

# M12 from the pipeline's own automation log
log = WORK / "logs" / "automation_log.csv"
lrows = list(csv.DictReader(log.open(encoding="utf-8"))) if log.exists() else []
real = [l for l in lrows if not l["input_file"].startswith(("ops_01", "ops_02"))]
done = [l for l in real if l["status"] == "complete" and l["manual_intervention"] == "no"]
emit("M12", "end_to_end", len(done) / len(real) if real else None, len(done), len(real),
     notes="eval inputs excluding the two deliberately-broken files; manual interventions = 0 (no code or config touched during the run)")

# M14-M17 on the real database
if not args.no_official_db and Path(args.db).exists():
    con = sqlite3.connect(args.db)
    tabs = [("soil_health", "period"), ("rainfall_monthly", "year_month"), ("crop_production", "period")]
    total = complete = valid = 0
    dup = 0
    for t, timecol in tabs:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        c = con.execute(f"""SELECT COUNT(*) FROM {t} WHERE source_file<>'' AND source_type<>'' AND agent<>'' AND run_id<>''
                            AND locator<>'' AND crop<>'' AND district<>'' AND {timecol} IS NOT NULL AND unit<>''""").fetchone()[0]
        v = con.execute(f"SELECT COUNT(*) FROM {t} WHERE validation_status='valid'").fetchone()[0]
        d = con.execute(f"""SELECT COALESCE(SUM(c),0) FROM (SELECT COUNT(*) c FROM {t}
                            GROUP BY source_file, locator, crop, district, {timecol} HAVING COUNT(*)>1)""").fetchone()[0]
        total += n; complete += c; valid += v; dup += d
        emit("M14", t, c / n if n else None, c, n, status="measured", notes="SQL on db/manipur.db")
        emit("M15", t, v / n if n else None, v, n, notes="rules: unit known, district in reference list, period parsable, value in plausible range")
        emit("M16", t, d / n if n else None, d, n)
    emit("M14", "database", complete / total, complete, total, notes="rows with source, source type, agent, run id, locator, crop, location, time, unit filled or coded UNSPECIFIED")
    emit("M15", "database", valid / total, valid, total)
    emit("M16", "database", dup / total, dup, total, notes="natural key = source_file, locator, crop, district, period")
    # M17: 3 random rows per run of the checker, traced back to the source file
    rng = random.Random(7)
    con.row_factory = sqlite3.Row
    allrows = []
    for t, _ in tabs:
        allrows += [(t, dict(r)) for r in con.execute(f"SELECT * FROM {t}")]
    sample = rng.sample(allrows, 3)
    SRC = {"manipur_soil_health.json": ROOT / "manipur_soil_health.json", "manipur_rainfall_monthly.json": ROOT / "manipur_rainfall_monthly.json",
           "CSV_Agent.csv": ROOT / "Data" / "CSV_Agent.csv"}
    ok3 = 0
    for t, r in sample:
        numeric = {k: r[k] for k in (SOIL_NUTRIENTS + ["rainfall_mm", "area_ha", "production_t", "yield_t_ha"]) if k in r and r[k] is not None}
        s, tt = trace_row(SRC.get(r["source_file"], r["source_file"]), r["locator"], numeric)
        ok3 += int(tt > 0 and s == tt)
    emit("M17", "database", ok3, ok3, 3, notes="automated stand-in for the evaluator's 3 random rows (seeded sample)")
    # whole-database trace-back for M4 (reported, not the scored sample)
    n = ok = 0
    for t, r in allrows:
        numeric = {k: r[k] for k in (SOIL_NUTRIENTS + ["rainfall_mm", "area_ha", "production_t", "yield_t_ha"]) if k in r and r[k] is not None}
        if not numeric:
            continue
        s, tt = trace_row(SRC.get(r["source_file"], r["source_file"]), r["locator"], numeric)
        n += 1
        ok += int(s == tt)
    emit("M4", "database", ok / n, ok, n, status="measured", notes="every row in db/manipur.db traced back automatically")
    con.close()

# ------------------------------------------------------------------------------------------------ write files
(EVAL / "results" / "history").mkdir(parents=True, exist_ok=True)
fields = ["metric_id", "metric_name", "scope", "value", "numerator", "denominator", "floor", "target", "points", "max_points",
          "status", "notes"]
for dest in (EVAL / "phase2_metrics.csv", EVAL / "results" / "history" / f"phase2_metrics_{datetime.now():%Y%m%d_%H%M%S}.csv"):
    with dest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)
with (EVAL / "results" / "per_case.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["case_id", "agent", "source_group", "difficulty", "status", "expected_status", "tp", "extracted", "ref_fields",
                "F1", "task_complete", "trajectory_match", "retries", "seconds", "tools"])
    for r in results:
        f1 = 2 * r["tp"] / (r["extracted"] + r["ref_fields"]) if (r["extracted"] + r["ref_fields"]) else ""
        w.writerow([r["id"], r["agent"], r["group"], r["case"]["difficulty"], r["status"], r["case"]["expected_status"], r["tp"],
                    r["extracted"], r["ref_fields"], f1 if f1 == "" else round(f1, 4), r["complete"], r["traj_match"],
                    r["retries"], round(r["seconds"], 2), " > ".join(r["tools"])])
with (EVAL / "results" / "errors.json").open("w", encoding="utf-8") as fh:
    json.dump({r["id"]: {"wrong": [(list(k), f, g, e) for k, f, g, e in r["wrong"]][:25],
                         "missing": [(list(k), f, e) for k, f, e in r["missing"]][:25]}
               for r in results if r["wrong"] or r["missing"]}, fh, indent=1, default=str)

# ------------------------------------------------------------------------------------------------ summary
print("\n=== headline (end_to_end / database scope) ===")
total_pts = 0.0
for m in rows_out:
    if m["scope"] in ("end_to_end", "database") and m["value"] != "":
        print(f"  {m['metric_id']:<4}{m['metric_name']:<32}{m['scope']:<11}{m['value']!s:<8} floor={m['floor']!s:<5} "
              f"target={m['target']!s:<5} points={m['points']!s:<5}/ {m['max_points']}")
shutil.rmtree(WORK, ignore_errors=True)
print("\nwrote eval/phase2_metrics.csv, eval/results/per_case.csv, eval/results/errors.json")
