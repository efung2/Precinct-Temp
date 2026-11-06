#!/usr/bin/env python3
"""Convert ALARM Project census-2020 files (census voting districts with VEST election results)
into (1) MEDSL-format CSVs that run_pipeline.py can read and (2) a unit table for eforensics.

Data sources (please cite; see README):
  * ALARM Project, census-2020: 2020 Census P.L. 94-171 data by voting district (VTD),
    with VEST precinct election results re-allocated to census geography.
  * VEST (Voting and Election Science Team), 2020 precinct-level election results.

Each census voting district becomes one "precinct". Votes in the ALARM files are
ESTIMATES wherever VEST precincts and census VTDs differ (they are fractional); they are
rounded to whole votes here. Units are kept only if they have voting-age population,
at least one vote, and no more votes than voting-age residents (otherwise turnout > 100%,
an artefact of re-allocation). The SAME units are used for eforensics and for our pipeline.

States whose ALARM file is at census-BLOCK level (no VTDs: CA, HI, ME, OR) are skipped,
because blocks are far smaller than precincts. DC is skipped (its "Senate" race is a
shadow seat, not a U.S. Senate seat). Arkansas is skipped (no Democrat ran in 2020), and so is
Louisiana (multi-candidate "jungle" race).

    python3 pipeline/alarm_convert.py --alarm-dir raw/census-vest-2020 --out-dir raw
"""
import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

STATE_NAMES = {
    "AK": "Alaska", "AL": "Alabama", "AR": "Arkansas", "AZ": "Arizona", "CO": "Colorado", "DE": "Delaware",
    "GA": "Georgia", "IA": "Iowa", "ID": "Idaho", "IL": "Illinois", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "MA": "Massachusetts", "ME": "Maine", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MT": "Montana", "NC": "North Carolina", "NE": "Nebraska", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "OK": "Oklahoma", "OR": "Oregon", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "VA": "Virginia",
    "WV": "West Virginia", "WY": "Wyoming",
}
SPECIAL_2020 = {"AZ"}  # Arizona's 2020 Senate race was a special election
SKIP = {"DC": "shadow Senate seat, not a U.S. Senate race",
        "AR": "no Democratic candidate in 2020 (the file codes the Libertarian as the Democrat)"}

COLUMNS = ["precinct", "office", "party_detailed", "party_simplified", "mode", "votes", "county_name",
           "county_fips", "jurisdiction_name", "jurisdiction_fips", "candidate", "district", "magnitude",
           "dataverse", "year", "stage", "state", "special", "writein", "state_po"]


def _race_cols(cols, prefix):
    d = [c for c in cols if re.match(rf"^{prefix}_dem_", c)]
    r = [c for c in cols if re.match(rf"^{prefix}_rep_", c)]
    return (d[0], r[0]) if len(d) == 1 and len(r) == 1 else (None, None)


def _rows(u, office, dcol, rcol, special):
    parts = []
    for col, party in ((dcol, "DEMOCRAT"), (rcol, "REPUBLICAN")):
        cand = party[:3] + " " + col.split("_")[-1].upper()
        parts.append(pd.DataFrame({
            "precinct": u["precinct"], "office": office, "party_detailed": party, "party_simplified": party,
            "mode": "TOTAL", "votes": u[col], "county_name": u["county_name"], "county_fips": u["county_fips"],
            "jurisdiction_name": u["county_name"], "jurisdiction_fips": u["county_fips"], "candidate": cand,
            "district": "STATEWIDE", "magnitude": 1, "dataverse": office.split()[-1], "year": 2020, "stage": "GEN",
            "state": u["state"], "special": "TRUE" if special else "FALSE", "writein": "FALSE", "state_po": u["state_po"],
        }))
    return parts


def convert(alarm_dir, log=print):
    alarm_dir = Path(alarm_dir)
    sen, pre, units, report = [], [], [], []
    for f in sorted(alarm_dir.glob("*_2020_*.csv")):
        st = f.name[:2].upper()
        level = f.name.split("_")[2].split(".")[0]
        head = pd.read_csv(f, nrows=0).columns
        sd, sr = _race_cols(head, "uss_20")
        if st in SKIP:
            report.append({"state_po": st, "status": "skipped: " + SKIP[st], "units": 0})
            continue
        if sd is None:
            if any(c.startswith("uss_20") for c in head):
                report.append({"state_po": st, "status": "skipped: not a one-Democrat, one-Republican race", "units": 0})
            continue
        if level != "vtd":
            report.append({"state_po": st, "status": "skipped: census-block level only", "units": 0})
            continue
        pd_, pr = _race_cols(head, "pre_20")
        a = pd.read_csv(f, dtype={"GEOID20": str, "vtd": str})
        u = pd.DataFrame({
            "state_po": st,
            "state": STATE_NAMES.get(st, st),
            "county_fips": a["GEOID20"].str[:5],
            "county_name": a["county"].str.upper(),
            "precinct": "VTD " + a["vtd"].astype(str),
            "geoid": a["GEOID20"],
            "vap": np.rint(a["vap"].fillna(0)).astype(int),
            "sen_dem": np.rint(a[sd].fillna(0)).astype(int),
            "sen_rep": np.rint(a[sr].fillna(0)).astype(int),
        })
        if pd_:
            u["pre_dem"] = np.rint(a[pd_].fillna(0)).astype(int)
            u["pre_rep"] = np.rint(a[pr].fillna(0)).astype(int)
        frac = float(((a[sd] % 1) != 0).mean())
        v = u["sen_dem"] + u["sen_rep"]
        keep = (u["vap"] > 0) & (v > 0) & (v <= u["vap"])
        report.append({"state_po": st, "status": "ok", "units_in_file": int(len(u)), "units": int(keep.sum()),
                       "dropped_no_vap_or_votes": int(((u["vap"] <= 0) | (v <= 0)).sum()),
                       "dropped_turnout_over_100": int(((u["vap"] > 0) & (v > u["vap"])).sum()),
                       "share_fractional_votes": round(frac, 3),
                       "senate_dem": sd.split("_")[-1].upper(), "senate_rep": sr.split("_")[-1].upper()})
        u = u[keep].reset_index(drop=True)
        units.append(u)
        sen += _rows(u, "US SENATE", "sen_dem", "sen_rep", st in SPECIAL_2020)
        if pd_:
            pre += _rows(u, "US PRESIDENT", "pre_dem", "pre_rep", False)
    rep = pd.DataFrame(report)
    log(f"    {int((rep['status'] == 'ok').sum())} states converted, "
        f"{int(rep.get('units', pd.Series(dtype=int)).sum()):,} census districts kept")
    return (pd.concat(sen, ignore_index=True)[COLUMNS], pd.concat(pre, ignore_index=True)[COLUMNS],
            pd.concat(units, ignore_index=True), rep)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--alarm-dir", required=True, help="folder with the ALARM *_2020_vtd.csv files")
    ap.add_argument("--out-dir", default="raw")
    a = ap.parse_args(argv)
    sen, pre, units, rep = convert(a.alarm_dir)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sen.to_csv(out / "alarm-senate-2020-vtd.csv", index=False)
    pre.to_csv(out / "alarm-president-2020-vtd.csv", index=False)
    units.to_csv(out / "alarm-units-2020-vtd.csv", index=False)
    rep.to_csv(out / "alarm-conversion-report.csv", index=False)
    print(rep.to_string(index=False))
    print(f"wrote {out}/alarm-senate-2020-vtd.csv, alarm-president-2020-vtd.csv, alarm-units-2020-vtd.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
