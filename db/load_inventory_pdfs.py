"""
Loads the real inventory PDFs whose tables the pipeline reads correctly into db/manipur.db (Phase 2 implementation coverage, M2).

    python db/load_inventory_pdfs.py            # all files below
    python db/load_inventory_pdfs.py --dry-run  # into a temporary database instead, to check first

Every file goes through the ordinary one-command pipeline (`run.py` / pipeline.graph.run_file); nothing is typed in by hand. Two things are
set on purpose and are written to the run log:
  * the page range (AGRI_PDF_PAGES) where the table sits in a long report: a manual choice, so these runs count as manual interventions;
  * AGRI_EXCLUDE_DISTRICTS = the three districts held out for evaluation (Imphal West, Thoubal, Ukhrul): their rows are reported and NOT stored,
    so the real database still holds no held-out record (eval/contamination_check.py checks this).
The PDFs themselves are in Data/pdf_samples/ (git-ignored, not redistributed); URLs and SHA-256 are in Data/manifest.csv.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HELD_OUT = "IMPHAL WEST,THOUBAL,UKHRUL"
# (source id, file, pages or None, what the table is)
FILES = [
    ("S54", "S54_mofpi_manipur_state_profile.pdf", "7", "MoFPI Manipur state profile, Tables 2, 4-6: crop production 2023-24 (state level)"),
    ("S06", "S06_nhb_horticulture_at_a_glance_2018.pdf", "187,192", "NHB Horticultural Statistics at a Glance 2018, Tables 7.2.2 / 7.2.5: fruits and spices, "
                                                                    "area and production 2013-14..2017-18, Manipur row (state level)"),
    ("S10", "S10_economic_survey_manipur_2024-25.pdf", "90", "Economic Survey Manipur 2024-25 (DES Manipur, 369 pp; only the agriculture chapter was examined), "
                                                             "Table 5.12: area under 12 fruits and vegetables, 2022-23..2024-25 (state level)"),
    ("S08", "S08_agri_dept_manipur_apy_2018-19.pdf", None, "Agriculture Department Manipur: area, production and yield by district, 2018-19"),
    ("S08", "S08_agri_dept_manipur_apy_2019-20.pdf", None, "Agriculture Department Manipur: area, production and yield by district, 2019-20"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = Path(tempfile.mkdtemp()) / "check.db" if args.dry_run else ROOT / "db" / "manipur.db"
    os.environ["AGRI_EXCLUDE_DISTRICTS"] = HELD_OUT
    from pipeline.graph import run_file
    for sid, name, pages, what in FILES:
        path = ROOT / "Data" / "pdf_samples" / name
        if not path.exists():
            print(f"{sid} {name}: MISSING (download it first; URL in Data/manifest.csv)")
            continue
        if pages:
            os.environ["AGRI_PDF_PAGES"] = pages
        else:
            os.environ.pop("AGRI_PDF_PAGES", None)
        out = run_file(path, db_path=db, manual_note=f"{sid}: pages chosen by hand ({pages})" if pages else "")
        tot = ((out.get("result") or {}).get("totals") or {})
        print(f"{sid} {name}: status={out['status']} inserted={tot.get('inserted', 0)} invalid={tot.get('invalid', 0)} "
              f"duplicates={tot.get('duplicate', 0)}  ({what})")
    print(f"database: {db}")


if __name__ == "__main__":
    main()
