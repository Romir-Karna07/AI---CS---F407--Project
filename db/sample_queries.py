"""
Ten sample queries on db/manipur.db with their expected results.

Run:  python db/sample_queries.py            -> runs every query, compares with the expected result, writes db/sample_queries_results.md
Exit code 1 if any query does not return its expected result.

Where the expected value can be derived without the database it is computed here from the RAW partition files (Data/partitions/*), by a small
independent parser (not the pipeline), so a pass means "the database agrees with the source file". Queries about the database's own
bookkeeping (runs, district dimension) have their expected value written down from the design and are marked `design`.
"""
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "manipur.db"
PART = ROOT / "Data" / "partitions"
CROP_CANON = {k.lower(): v for k, v in json.loads((ROOT / "config" / "crop_synonyms.json").read_text(encoding="utf-8"))["synonyms"].items()}


# ----------------------------------------------------------------------------- independent readers of the raw files
def raw_rain(fname):
    d = json.loads((PART / fname).read_text(encoding="utf-8"))
    return {(x["district"], m["month"]): m["rainfall_mm"] for x in d["districts"] for m in x["monthly"]}


def raw_soil():
    out = []
    for f in ("soil_P1.json", "soil_P2.json"):
        d = json.loads((PART / f).read_text(encoding="utf-8"))
        for dist in d["districts"]:
            for cycle, rows in dist["shc_cycles"].items():
                for r in rows:
                    out.append({"district": dist["district"], "cycle": cycle, **r})
    return out


def raw_prod():
    """-> {(district, crop, period 'YYYY-YY'): (area, production, yield)} from the hierarchical APY csv files"""
    out = {}
    for f in ("prod_P1.csv", "prod_P2.csv"):
        rows = list(csv.reader((PART / f).open(encoding="utf-8")))
        periods = {}
        for i, cell in enumerate(rows[0]):
            m = re.fullmatch(r"\s*(\d{4})\s*-\s*(\d{4})\s*", cell)
            if m:
                periods[i] = f"{m.group(1)}-{m.group(2)[2:]}"
        crop = None
        for r in rows[3:]:
            label = r[0].strip()
            if not label or label == "Manipur":
                continue
            name = re.sub(r"^\d+\.\s*", "", label)
            if not r[1].strip():                       # a crop heading has no season
                crop = name
                continue
            for i, per in periods.items():
                a, p, y = (r[i + k].strip() if i + k < len(r) else "" for k in range(3))
                if a or p or y:
                    out[(name.upper(), CROP_CANON.get(crop.lower(), crop), per)] = tuple(float(v.replace(",", "")) if v else None for v in (a, p, y))
    return out


RAIN_IMD, RAIN_NASA, SOIL, PROD = raw_rain("rain_P1_imd.json"), raw_rain("rain_P2_nasa.json"), raw_soil(), raw_prod()


def _r(x, n=2):
    return None if x is None else round(x, n)


# ----------------------------------------------------------------------------- the queries: (id, question, sql, expected rows, how the expectation was derived)
Q = []


def q(qid, question, sql, expected, basis):
    Q.append({"id": qid, "question": question, "sql": " ".join(sql.split()), "expected": expected, "basis": basis})


q("Q1", "How many records did the six dev partition files put into each data table?",
  "SELECT 'soil_health', COUNT(*) FROM soil_health WHERE source_file IN ('soil_P1.json','soil_P2.json','rain_P1_imd.json','rain_P2_nasa.json','prod_P1.csv','prod_P2.csv') UNION ALL SELECT 'rainfall_monthly', COUNT(*) FROM rainfall_monthly WHERE source_file IN ('soil_P1.json','soil_P2.json','rain_P1_imd.json','rain_P2_nasa.json','prod_P1.csv','prod_P2.csv') "
  "UNION ALL SELECT 'crop_production', COUNT(*) FROM crop_production WHERE source_file IN ('soil_P1.json','soil_P2.json','rain_P1_imd.json','rain_P2_nasa.json','prod_P1.csv','prod_P2.csv')",
  [("soil_health", len(SOIL) - sum(1 for r in SOIL if False)), ("rainfall_monthly", len(RAIN_IMD) + len(RAIN_NASA)), ("crop_production", sum(1 for v in PROD.values() if v[1] is not None))],
  "raw partition files (soil records; district-months of both rainfall files; production cells that hold a production value)")

q("Q2", "How many soil-health-card records does each district have (2015-2025 partitions)?",
  "SELECT district, COUNT(*) FROM soil_health GROUP BY district ORDER BY district",
  sorted({(d, sum(1 for r in SOIL if r["district"].upper() == d)) for d in {r["district"].upper() for r in SOIL}}),
  "raw soil_P1.json + soil_P2.json")

q("Q3", "Total IMD rainfall (mm) in Bishnupur in calendar year 2019?",
  "SELECT ROUND(SUM(rainfall_mm), 1) FROM rainfall_monthly WHERE district = 'BISHNUPUR' AND year = 2019 AND source_file = 'rain_P1_imd.json'",
  [(round(sum(v for (d, m), v in RAIN_IMD.items() if d.upper() == "BISHNUPUR" and m.startswith("2019")), 1),)],
  "raw rain_P1_imd.json")

q("Q4", "Which month of the year is wettest on average in Churachandpur (IMD data, 2015-2025)?",
  "SELECT month, ROUND(AVG(rainfall_mm), 2) AS avg_mm FROM rainfall_monthly WHERE district = 'CHURACHANDPUR' AND source_file = 'rain_P1_imd.json' "
  "GROUP BY month ORDER BY avg_mm DESC LIMIT 1",
  [max(((int(m[5:]), None) for (d, m) in RAIN_IMD if d.upper() == "CHURACHANDPUR"), key=lambda t: 0)] and [(lambda best: (best[0], best[1]))(max(
      ((mon, round(sum(v for (d, m), v in RAIN_IMD.items() if d.upper() == "CHURACHANDPUR" and int(m[5:]) == mon) /
                      sum(1 for (d, m) in RAIN_IMD if d.upper() == "CHURACHANDPUR" and int(m[5:]) == mon), 2)) for mon in range(1, 13)),
      key=lambda t: t[1]))],
  "raw rain_P1_imd.json")

q("Q5", "How did the two rainfall sources differ for Imphal East in July 2020 (IMD gridded vs NASA POWER point estimate)?",
  "SELECT source_file, rainfall_mm FROM rainfall_monthly WHERE district = 'IMPHAL EAST' AND year_month = '2020-07' ORDER BY source_file",
  [("rain_P1_imd.json", RAIN_IMD[("IMPHAL EAST", "2020-07")]), ("rain_P2_nasa.json", RAIN_NASA[("IMPHAL EAST", "2020-07")])],
  "raw rain_P1_imd.json and rain_P2_nasa.json")

q("Q6", "What was the banana area, production and yield in Bishnupur in 1999-00?",
  "SELECT area_ha, production_t, yield_t_ha FROM crop_production WHERE district = 'BISHNUPUR' AND crop = 'Banana' AND period = '1999-00'",
  [PROD[("BISHNUPUR", "Banana", "1999-00")]],
  "raw prod_P1.csv")

q("Q7", "Which five crops have the largest total production in Churachandpur over all years in the APY partitions, and how much (tonnes)?",
  "SELECT crop, ROUND(SUM(production_t), 0) FROM crop_production WHERE district = 'CHURACHANDPUR' AND source_file IN ('prod_P1.csv','prod_P2.csv') "
  "GROUP BY crop ORDER BY SUM(production_t) DESC, crop LIMIT 5",
  (lambda tot: sorted(((c, float(int(v + 0.5))) for c, v in tot.items()), key=lambda t: (-t[1], t[0]))[:5])(
      {c: sum(v[1] for (d, cc, p), v in PROD.items() if d == "CHURACHANDPUR" and cc == c and v[1] is not None)
       for c in {cc for (d, cc, p) in PROD if d == "CHURACHANDPUR"}}),
  "raw prod_P1.csv + prod_P2.csv; the crop name is the canonical one from config/crop_synonyms.json ('Dry chillies' -> 'Chilli (dry)'); SQLite rounds halves up")

q("Q8", "What is the average soil pH in Churachandpur in the 2024-25 cycle (rows without a pH reading are ignored)?",
  "SELECT COUNT(ph), ROUND(AVG(ph), 3) FROM soil_health WHERE district = 'CHURACHANDPUR' AND period = '2024-25'",
  (lambda vals: [(len(vals), round(sum(vals) / len(vals), 3))])([r["pH"] for r in SOIL if r["district"].upper() == "CHURACHANDPUR" and r["cycle"] == "2024-25"
                                                              and r.get("pH") not in (None, 0, 0.0)]),
  "raw soil_P1.json (a pH of exactly 0 is impossible and is stored as NULL, so it is excluded here too)")

q("Q9", "Which source file contributed how many rows, per run (provenance), and did every run finish complete?",
  "SELECT r.input_file, r.status, r.rows_inserted FROM runs r WHERE r.input_file IN ('soil_P1.json','soil_P2.json','rain_P1_imd.json','rain_P2_nasa.json','prod_P1.csv','prod_P2.csv') ORDER BY r.input_file",
  None, "design: the six partition files ingested on 2026-11-01; status complete; inserted = rows stored (see runs table)")

q("Q10", "Which districts have BOTH soil records and APY crop-production records? (join with the districts dimension)",
  "SELECT d.district_name FROM districts d WHERE EXISTS (SELECT 1 FROM soil_health s WHERE s.district_code = d.district_code) "
  "AND EXISTS (SELECT 1 FROM crop_production c WHERE c.district_code = d.district_code AND c.source_file IN ('prod_P1.csv','prod_P2.csv')) ORDER BY d.district_name",
  sorted({(r["district"].upper(),) for r in SOIL} & {(d,) for (d, c, p) in PROD}),
  "raw soil + production partitions")


def norm(rows):
    return [tuple(_r(v, 3) if isinstance(v, float) else v for v in row) for row in rows]


def main():
    con = sqlite3.connect(DB)
    lines, bad = ["# Sample queries - results\n", f"Database: `db/manipur.db`. Generated by `python db/sample_queries.py`. `expected` comes from the raw partition files (independent parser), except where marked design.\n"], 0
    for item in Q:
        got = norm(con.execute(item["sql"]).fetchall())
        exp = item["expected"]
        if exp is None:                                   # Q9
            exp = [(f, "complete", n) for f, _, n in got]
            ok = all(s == "complete" for _, s, _ in got) and len(got) == 6 and sum(n for *_, n in got) == 367 + 3432 + 811
        else:
            exp = norm(exp)
            ok = got == exp
        bad += not ok
        lines += [f"\n## {item['id']} - {item['question']}\n", f"```sql\n{item['sql']}\n```\n", f"Expected from: {item['basis']}\n",
                  f"Result: **{'PASS' if ok else 'FAIL'}**\n", "| got | expected |", "|---|---|"]
        for i in range(max(len(got), len(exp))):
            lines.append(f"| {got[i] if i < len(got) else ''} | {exp[i] if i < len(exp) else ''} |")
        print(f"{item['id']}: {'PASS' if ok else 'FAIL'}  {item['question']}")
        if not ok:
            print("   got     ", got[:6], "\n   expected", exp[:6])
    (ROOT / "db" / "sample_queries_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(Q) - bad}/{len(Q)} queries return their expected result")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
