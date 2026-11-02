"""
Contamination check (PDF: "overlap with training data = 0 by file hash"; "evaluation cases found in the training data -> 0").

Checks that the official evaluation set (eval/cases, eval/reference) shares nothing with what the agents were run on / tuned on:
  1. file hashes   : no evaluation file has the same SHA-256 as any development file (Data/partitions/*, eval/dev/cases/*)
  2. database      : no row in db/manipur.db comes from an evaluation or held-out file (runs.source_hash / source_file), and no
                     row belongs to a held-out district
  3. record keys   : no (agent, location, period, crop, entity) key appears in both the development references and the
                     evaluation references
  4. manifest      : every evaluation file and every dev partition still matches the hash frozen in eval/heldout/split_manifest.json
Run:  python eval/contamination_check.py          (exit code 1 if anything overlaps)
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
HELD = {"IMPHAL WEST", "THOUBAL", "UKHRUL"}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def keys(ref_dir):
    out = set()
    for f in Path(ref_dir).glob("*.json"):
        r = json.loads(f.read_text(encoding="utf-8"))
        for rec in r["records"]:
            k = rec["key"]
            out.add((r["agent"], str(k["location"]).casefold(), str(k["period"]).casefold(), str(k["crop"]).casefold(),
                     str(k["entity"]).casefold()))
    return out


def check(db_path=ROOT / "db" / "manipur.db"):
    res = {}
    test_files = [p for p in (EVAL / "cases").glob("*") if p.is_file()]
    test_hash = {sha(p): p.name for p in test_files}
    dev_files = [p for p in (ROOT / "Data" / "partitions").glob("*")] + [p for p in (EVAL / "dev" / "cases").glob("*") if p.is_file()]
    dev_hash = {sha(p) for p in dev_files}
    held_hash = {sha(p) for p in (EVAL / "heldout_source").glob("*")}
    res["eval_files"] = len(test_files)
    res["file_hash_overlap_with_dev"] = len(set(test_hash) & dev_hash)
    res["file_hash_overlap_with_heldout_source_files"] = len(set(test_hash) & held_hash)   # should be 0: tests are re-shaped, not copies

    bad_rows, bad_district, bad_hash = 0, 0, 0
    if Path(db_path).exists():
        con = sqlite3.connect(db_path)
        for t in ("soil_health", "rainfall_monthly", "crop_production"):
            bad_district += con.execute(f"SELECT COUNT(*) FROM {t} WHERE UPPER(district) IN ({','.join('?' * len(HELD))})", tuple(HELD)).fetchone()[0]
            for (h,) in con.execute(f"SELECT DISTINCT source_hash FROM {t}"):
                bad_hash += int(h in test_hash or h in held_hash)
            bad_rows += con.execute(f"SELECT COUNT(*) FROM {t} WHERE source_file LIKE 'soil_h%' OR source_file LIKE 'rain_h%' OR source_file LIKE 'prod_h%' OR source_file LIKE 'pdf_%' OR source_file LIKE 'ops_%' OR source_file LIKE '%P3%'").fetchone()[0]
        con.close()
    res["db_rows_from_eval_or_heldout_files"] = bad_rows
    res["db_source_hashes_equal_to_eval_or_heldout"] = bad_hash
    res["db_rows_in_held_out_districts"] = bad_district

    dev_ref = EVAL / "dev" / "reference"
    res["record_key_overlap_dev_vs_eval"] = len(keys(dev_ref) & keys(EVAL / "reference")) if dev_ref.exists() else 0

    man = json.loads((EVAL / "heldout" / "split_manifest.json").read_text(encoding="utf-8"))
    changed = [k for k, v in man["files"].items() if (ROOT / k).exists() and sha(ROOT / k) != v["sha256"]]
    missing = [k for k in man["files"] if not (ROOT / k).exists()]
    res["frozen_files_changed_or_missing"] = len(changed) + len(missing)
    if "eval_cases" in man:
        res["frozen_eval_files_changed"] = sum(sha(EVAL / "cases" / n) != h for n, h in man["eval_cases"].items() if (EVAL / "cases" / n).exists())
    res["overlap_total"] = sum(v for k, v in res.items() if k != "eval_files")
    return res


if __name__ == "__main__":
    r = check()
    for k, v in r.items():
        print(f"{k:50} {v}")
    print("CONTAMINATION CHECK:", "PASS (no overlap)" if r["overlap_total"] == 0 else "FAIL")
    sys.exit(0 if r["overlap_total"] == 0 else 1)
