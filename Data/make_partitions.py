"""
Splits the four corpus files into DEVELOPMENT partitions (what the agents run on / what the code may be tuned with) and a
HELD-OUT partition (used only to build the evaluation set; never ingested, never used to tune code).

Split rule (declared in eval/README.md): by DISTRICT.
    held out : IMPHAL WEST, THOUBAL, UKHRUL      (present in all four files)
    dev      : every other district

Dev files the agents run on (6)                       held-out raw material (4 files -> 30 evaluation files)
    Data/partitions/soil_P1.json, soil_P2.json          eval/heldout_source/soil_P3.json
    Data/partitions/rain_P1_imd.json (S22)              eval/heldout_source/rain_P3_imd.json
    Data/partitions/rain_P2_nasa.json (S21)             eval/heldout_source/rain_P3_nasa.json
    Data/partitions/prod_P1.csv, prod_P2.csv            eval/heldout_source/prod_P3.csv

The full corpus files in the repo root / Data/ stay untouched as the raw record of what was scraped.
Writes eval/heldout/split_manifest.json (SHA-256 of every partition) used by the contamination check.
Run:  python Data/make_partitions.py        (deterministic)
"""
import csv
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PART = ROOT / "Data" / "partitions"
HELD_DIR = ROOT / "eval" / "heldout_source"
MANIFEST = ROOT / "eval" / "heldout" / "split_manifest.json"
HELD = {"IMPHAL WEST", "THOUBAL", "UKHRUL"}

SOIL_P1 = {"CHURACHANDPUR", "KAMJONG", "KANGPOKPI", "NONEY"}      # remaining dev soil districts go to P2
PROD_P1 = {"BISHNUPUR", "CHANDEL", "CHURACHANDPUR"}               # remaining dev production districts go to P2


def dump(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def split_json(src, key_name, outputs, name_of=lambda n: n):
    """outputs: list of (path, predicate(district_upper) -> bool)"""
    doc = json.loads(src.read_text(encoding="utf-8"))
    for path, pred in outputs:
        part = {k: v for k, v in doc.items() if k != "districts"}
        part["districts"] = [d for d in doc["districts"] if pred(name_of(d["district"]).upper())]
        part["partition"] = path.name
        dump(path, part)


def split_prod(src):
    rows = list(csv.reader(src.open(encoding="utf-8-sig")))
    head, body = rows[:3], rows[3:]
    outs = {"prod_P1.csv": lambda d: d not in HELD and d in PROD_P1,
            "prod_P2.csv": lambda d: d not in HELD and d not in PROD_P1,
            "prod_P3.csv": lambda d: d in HELD}
    for fname, keep in outs.items():
        out = [r for r in head]
        out.append(body[0])                                   # the 'Manipur' state row
        crop_row, buf = None, []
        for r in body[1:]:
            if not r[1] and re.match(r"^\d+\.", r[0]):        # crop header row
                if crop_row and buf:
                    out += [crop_row] + buf
                crop_row, buf = r, []
            elif r[0].startswith("Total"):                    # per-crop total over ALL districts: wrong for a partition, dropped
                continue
            else:
                d = re.sub(r"^\d+\.\s*", "", r[0]).upper()
                if keep(d):
                    buf.append(r)
        if crop_row and buf:
            out += [crop_row] + buf
        dest = (HELD_DIR if fname == "prod_P3.csv" else PART) / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh, lineterminator="\n").writerows(out)


def main():
    soil = ROOT / "manipur_soil_health.json"
    split_json(soil, "districts", [
        (PART / "soil_P1.json", lambda d: d not in HELD and d in SOIL_P1),
        (PART / "soil_P2.json", lambda d: d not in HELD and d not in SOIL_P1),
        (HELD_DIR / "soil_P3.json", lambda d: d in HELD)])
    imd, nasa = ROOT / "manipur_rainfall_monthly.json", ROOT / "manipur_rainfall_nasa_power.json"
    split_json(imd, "districts", [(PART / "rain_P1_imd.json", lambda d: d not in HELD),
                                  (HELD_DIR / "rain_P3_imd.json", lambda d: d in HELD)])
    split_json(nasa, "districts", [(PART / "rain_P2_nasa.json", lambda d: d not in HELD),
                                   (HELD_DIR / "rain_P3_nasa.json", lambda d: d in HELD)])
    split_prod(ROOT / "Data" / "CSV_Agent.csv")

    man = {"rule": "split by district", "held_out_districts": sorted(HELD), "files": {}}
    for d, role in ((PART, "dev"), (HELD_DIR, "held_out")):
        for p in sorted(d.glob("*")):
            man["files"][p.relative_to(ROOT).as_posix()] = {"role": role, "sha256": sha(p), "bytes": p.stat().st_size}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(man, indent=1), encoding="utf-8")
    for k, v in man["files"].items():
        print(f"{v['role']:9} {k:48} {v['bytes']:>8} bytes")


if __name__ == "__main__":
    main()
