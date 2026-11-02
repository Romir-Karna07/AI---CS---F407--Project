"""
One-command entry point:   python run.py --input <file.json|file.csv|file.pdf> [--db db/manipur.db] [--json-out out.json]

Reads any JSON, CSV or PDF (tables; scans via OCR; AGRI_PDF_PAGES=53-60 limits the pages), routes it through the LangGraph pipeline (inspect -> read -> classify -> domain agents ->
load -> finish) and stores validated rows with provenance in SQLite. Nothing here is specific to a file name.
Demo options:  --inject-fault read|load|soil_agent|rainfall_agent|production_agent   (shows recovery / partial runs)
               --manual-note "text"   (declares a manual intervention; logged in logs/automation_log.csv)
"""
import argparse
import json
import sys

from pipeline.graph import run_file


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manipur agri data pipeline (LangGraph)")
    ap.add_argument("--input", required=True, help="path to a .json / .csv / .pdf file")
    ap.add_argument("--db", default=None, help="SQLite path (default db/manipur.db)")
    ap.add_argument("--json-out", default=None, help="also write the run report here")
    ap.add_argument("--inject-fault", default=None)
    ap.add_argument("--manual-note", default="")
    ap.add_argument("--verbose", action="store_true", help="print the full tool trajectory")
    a = ap.parse_args(argv)

    out = run_file(a.input, db_path=a.db, inject_fault=a.inject_fault, manual_note=a.manual_note)
    print(f"run {out['run_id']}  status={out['status']}  type={out['source_type']}  retries={out['retries']}")
    for g in out.get("groups") or []:
        print(f"  group {g['pattern']:<40} -> {str(g['domain']):<11} records={g['records']:<5} "
              f"unmapped={g['unmapped_headers'][:6]}")
    res = out.get("result") or {}
    for agent, s in (res.get("by_agent") or {}).items():
        print(f"  {agent:<17} -> {s['table']:<17} extracted={s['extracted']} inserted={s['inserted']} "
              f"duplicates={s['duplicates']} invalid={s['invalid']} rejected={s['rejected']}")
    for r in (res.get("rejected") or [])[:5]:
        print("  rejected:", r)
    if (res.get("notes") or {}).get("agent_errors"):
        print("  agent errors:", res["notes"]["agent_errors"])
    if a.verbose:
        for t in out["trajectory"]:
            print("  ", t)
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
    return 0 if out["status"] in ("complete", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
