#!/usr/bin/env python3
"""Pennsylvania Department of State bulk files (2024 General)  ->  files this project can read.

Inputs (free downloads from the Department of State "Election Data" page, see README):
    pa2024_general_returns.txt     "Election Returns Precinct Data": one row per precinct x candidate
    pa2024_voter_registration.txt  "Voter Registration Precinct Data": registered voters per precinct

Outputs (all in the same folder as the inputs unless --out is given):
    pa-2024-president.csv   MEDSL-style long file for President, with a registered_voters column
    pa-2024-senate.csv      the same for U.S. Senate (used as the cross-race companion)
    pa-2024-precincts.csv   one row per precinct: votes, registration, census voting-district id
    pa-2024-build-report.json  what was merged, dropped and how totals compare with the official ones

Split precincts. Some precincts are cut by a congressional or school-district line. The state lists
each piece separately, but reports the votes for a statewide office under ONE piece and the
registered voters under each piece. Left alone, one piece shows more votes than voters and the
other shows voters and no votes. Pieces are recognised by their name suffix ("(USC 12)",
"School District 1", ...) and merged back into one precinct. Nothing else is altered.
"""
import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

COUNTIES = """01 Adams|02 Allegheny|03 Armstrong|04 Beaver|05 Bedford|06 Berks|07 Blair|08 Bradford|09 Bucks|10 Butler|
11 Cambria|12 Cameron|13 Carbon|14 Centre|15 Chester|16 Clarion|17 Clearfield|18 Clinton|19 Columbia|20 Crawford|
21 Cumberland|22 Dauphin|23 Delaware|24 Elk|25 Erie|26 Fayette|27 Forest|28 Franklin|29 Fulton|30 Greene|31 Huntingdon|
32 Indiana|33 Jefferson|34 Juniata|35 Lackawanna|36 Lancaster|37 Lawrence|38 Lebanon|39 Lehigh|40 Luzerne|41 Lycoming|
42 McKean|43 Mercer|44 Mifflin|45 Monroe|46 Montgomery|47 Montour|48 Northampton|49 Northumberland|50 Perry|
51 Philadelphia|52 Pike|53 Potter|54 Schuylkill|55 Snyder|56 Somerset|57 Sullivan|58 Susquehanna|59 Tioga|60 Union|
61 Venango|62 Warren|63 Washington|64 Wayne|65 Westmoreland|66 Wyoming|67 York"""
COUNTY = {int(x.split(" ", 1)[0]): x.split(" ", 1)[1] for x in COUNTIES.replace("\n", "").split("|")}
MUNI_TYPE = {"2": "CITY", "4": "TWP", "5": "TOWN", "6": "BORO"}
PARTY = {"DEM": ("DEMOCRAT", "DEMOCRATIC"), "REP": ("REPUBLICAN", "REPUBLICAN"),
         "LIB": ("LIBERTARIAN", "LIBERTARIAN"), "GRN": ("OTHER", "GREEN"), "CST": ("OTHER", "CONSTITUTION")}
OFFICES = {"USP": "US PRESIDENT", "USS": "US SENATE"}

# Official statewide results (Pennsylvania Department of State, electionreturns.pa.gov, certified).
OFFICIAL = {"USP": {"REP": 3543308, "DEM": 3423042}}
# Totals in the data file used by Mebane (2025), Table 1 and notes to Table 2.
MEBANE = {"precincts": 9189, "analysed": 9157, "electors": 9175518, "votes_cast": 7040360,
          "trump": 3543308, "harris": 3423042}

# returns file columns (0-based), see the state's readme
E_COUNTY, E_PREC, E_DIST, E_OFFICE, E_PARTY = 2, 3, 5, 8, 9
E_LAST, E_FIRST, E_VOTES, E_CONG = 11, 12, 15, 18
E_MTYPE, E_MNAME, E_MCD, E_FIPS, E_VTD = 21, 22, 28, 29, 30
# registration file columns
R_COUNTY, R_PREC = 2, 3
R_PARTIES = ((5, 6), (8, 9), (11, 12), (14, 15), (17, 18), (20, 21))  # (abbreviation, count)

SPLIT_SUFFIX = re.compile(
    r"\s*(?:\((?:USC|CONG\.?|CONGR\.?|STH|STS)\s*\d+\)|SCHOOL\s+DISTRICT\s+\d+|(?<=\d)\(\d+\))\s*$", re.I)


def base_name(name):
    """Precinct name with a split-piece suffix removed (and spaces tidied)."""
    return re.sub(r"\s+", " ", SPLIT_SUFFIX.sub("", name)).strip()


def read_returns(path):
    info, votes = {}, defaultdict(Counter)
    cands = defaultdict(Counter)
    with open(path, encoding="latin-1", newline="") as f:
        for r in csv.reader(f):
            if len(r) < 31:
                continue
            key = (int(r[E_COUNTY]), int(r[E_PREC]))
            if key not in info:
                info[key] = {"name": r[E_MNAME].strip(), "mtype": r[E_MTYPE].strip(), "mcd": r[E_MCD].strip(),
                             "fips": r[E_FIPS].strip().zfill(3), "vtd": r[E_VTD].strip(), "cong": r[E_CONG].strip()}
            if r[E_OFFICE] in OFFICES:
                votes[key][(r[E_OFFICE], r[E_PARTY])] += int(r[E_VOTES] or 0)
                cands[r[E_OFFICE]][(r[E_PARTY], (r[E_FIRST].strip() + " " + r[E_LAST].strip()).strip())] += int(r[E_VOTES] or 0)
    return info, votes, cands


def read_registration(path):
    reg = {}
    with open(path, encoding="latin-1", newline="") as f:
        for r in csv.reader(f):
            if len(r) < 22 or int(r[R_COUNTY]) == 0:
                continue  # county 0 / precinct 99999 is a placeholder row
            c = Counter()
            for a, n in R_PARTIES:
                c[r[a].strip() or "NONE"] += int(r[n] or 0)
            reg[(int(r[R_COUNTY]), int(r[R_PREC]))] = c
    return reg


def build_units(info, votes, reg):
    """Merge split pieces; return list of unit dicts and a report."""
    groups = defaultdict(list)
    for key, i in info.items():
        groups[(key[0], i["mtype"], i["mcd"], base_name(i["name"]))].append(key)
    units, merged = [], []
    for (county, mtype, mcd, bname), keys in groups.items():
        keys = sorted(keys)
        is_split = len(keys) > 1 and any(base_name(info[k]["name"]) != info[k]["name"].strip() for k in keys)
        parts = [keys] if is_split else [[k] for k in keys]
        for part in parts:
            v, rg = Counter(), Counter()
            for k in part:
                v.update(votes.get(k, {}))
                rg.update(reg.get(k, {}))
            # the piece that carries the presidential votes names the unit (its codes are kept)
            lead = max(part, key=lambda k: sum(n for (o, _), n in votes.get(k, {}).items() if o == "USP"))
            i = info[lead]
            units.append({
                "county_code": county, "precinct_code": lead[1], "pieces": len(part),
                "piece_codes": " ".join(str(k[1]) for k in part),
                "name": bname if len(part) > 1 else i["name"], "mtype": mtype, "mcd": mcd,
                "fips": i["fips"], "vtd": i["vtd"], "cong": i["cong"],
                "has_registration": any(k in reg for k in part),
                "reg_total": sum(rg.values()), "reg_dem": rg.get("DEM", 0), "reg_rep": rg.get("REP", 0),
                "votes": v,
            })
            if len(part) > 1:
                merged.append({"county": COUNTY[county], "precinct": bname,
                               "pieces": [{"name": info[k]["name"], "code": k[1],
                                           "registered": sum(reg.get(k, {}).values()),
                                           "president_votes": sum(n for (o, _), n in votes.get(k, {}).items() if o == "USP")}
                                          for k in part]})
    # labels: unique within a county (boroughs and townships can share a name)
    seen = Counter((u["county_code"], u["name"]) for u in units)
    for u in units:
        u["label"] = u["name"] + (" " + MUNI_TYPE.get(u["mtype"], "") if seen[(u["county_code"], u["name"])] > 1 else "")
    again = Counter((u["county_code"], u["label"]) for u in units)
    for u in units:
        if again[(u["county_code"], u["label"])] > 1:
            u["label"] += f" ({u['precinct_code']})"
    units.sort(key=lambda u: (u["county_code"], u["precinct_code"]))
    return units, merged


def geoid(u):
    """Census 2020 voting-district id: state 42 + county FIPS + 6-character VTD code."""
    vtd = u["vtd"].strip()
    if not vtd or set(vtd) == {"0"}:
        return ""
    return "42" + u["fips"] + vtd.zfill(6)


def write_medsl(units, cands, office, path):
    names = {}
    for (party, name), _ in cands[office].most_common():
        names.setdefault(party, name)
    cols = ["precinct", "office", "party_detailed", "party_simplified", "mode", "votes", "county_name", "county_fips",
            "jurisdiction_name", "jurisdiction_fips", "candidate", "district", "dataverse", "year", "stage",
            "state", "special", "writein", "state_po", "registered_voters"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for u in units:
            cf = "42" + u["fips"]
            for party, name in names.items():
                simple, detail = PARTY.get(party, ("OTHER", party))
                w.writerow([u["label"], OFFICES[office], detail, simple, "TOTAL", u["votes"].get((office, party), 0),
                            COUNTY[u["county_code"]].upper(), cf, COUNTY[u["county_code"]].upper(), cf,
                            name.upper(), "statewide", "PRESIDENT" if office == "USP" else "SENATE", 2024, "GEN",
                            "PENNSYLVANIA", "FALSE", "FALSE", "PA",
                            u["reg_total"] if u["has_registration"] else ""])


def main(argv=None):
    here = Path(__file__).resolve().parent.parent / "raw" / "pa-2024"
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--returns", default=str(here / "pa2024_general_returns.txt"))
    ap.add_argument("--registration", default=str(here / "pa2024_voter_registration.txt"))
    ap.add_argument("--out", default=None, help="output folder (default: folder of the returns file)")
    a = ap.parse_args(argv)
    for p in (a.returns, a.registration):
        if not Path(p).exists():
            raise SystemExit(f"File not found: {p}\nSee README, 'Pennsylvania 2024', for where to download it.")
    out = Path(a.out) if a.out else Path(a.returns).parent
    out.mkdir(parents=True, exist_ok=True)

    info, votes, cands = read_returns(a.returns)
    reg = read_registration(a.registration)
    if not cands["USP"]:
        raise SystemExit("No presidential rows (office code USP) found: is this the General Election returns file?")
    units, merged = build_units(info, votes, reg)

    write_medsl(units, cands, "USP", out / "pa-2024-president.csv")
    write_medsl(units, cands, "USS", out / "pa-2024-senate.csv")

    cols = ["unit_id", "county", "precinct", "county_fips", "geoid20", "mcd_code", "cong_district", "pieces", "piece_codes",
            "registered", "registered_dem", "registered_rep",
            "pres_dem", "pres_rep", "pres_other", "sen_dem", "sen_rep", "sen_other"]
    with open(out / "pa-2024-precincts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for u in units:
            v = u["votes"]
            oth = lambda o: sum(n for (oo, p), n in v.items() if oo == o and p not in ("DEM", "REP"))  # noqa: E731
            w.writerow([f"{u['county_code']}-{u['precinct_code']}", COUNTY[u["county_code"]], u["label"], "42" + u["fips"],
                        geoid(u), u["mcd"], u["cong"], u["pieces"], u["piece_codes"],
                        u["reg_total"] if u["has_registration"] else "", u["reg_dem"], u["reg_rep"],
                        v.get(("USP", "DEM"), 0), v.get(("USP", "REP"), 0), oth("USP"),
                        v.get(("USS", "DEM"), 0), v.get(("USS", "REP"), 0), oth("USS")])

    tot = Counter()
    for u in units:
        tot.update(u["votes"])
    pres = lambda u: sum(n for (o, _), n in u["votes"].items() if o == "USP")  # noqa: E731
    report = {
        "source_precinct_rows": len(info),
        "registration_rows": len(reg),
        "units": len(units),
        "merged_split_precincts": len(merged),
        "pieces_merged": sum(len(m["pieces"]) for m in merged),
        "source_rows_more_votes_than_registered": sum(
            1 for k in info if k in reg and sum(n for (o, _), n in votes.get(k, {}).items() if o == "USP") > sum(reg[k].values())),
        "units_zero_president_votes": sum(1 for u in units if pres(u) == 0),
        "units_more_votes_than_registered": sum(1 for u in units if u["has_registration"] and pres(u) > u["reg_total"]),
        "units_without_registration_row": sum(1 for u in units if not u["has_registration"]),
        "registered_total": sum(u["reg_total"] for u in units),
        "president": {"trump": tot[("USP", "REP")], "harris": tot[("USP", "DEM")],
                      "all_candidates": sum(n for (o, _), n in tot.items() if o == "USP")},
        "senate": {"mccormick": tot[("USS", "REP")], "casey": tot[("USS", "DEM")],
                   "all_candidates": sum(n for (o, _), n in tot.items() if o == "USS")},
        "official": {"trump": OFFICIAL["USP"]["REP"], "harris": OFFICIAL["USP"]["DEM"]},
        "mebane_2025": MEBANE,
        "candidates": {o: [{"party": p, "name": n, "votes": v} for (p, n), v in c.most_common()] for o, c in cands.items()},
        "merged": merged,
    }
    (out / "pa-2024-build-report.json").write_text(json.dumps(report, indent=1))
    print(f"precinct rows in returns file : {len(info):,}")
    print(f"units after merging split precincts: {len(units):,} ({len(merged)} merged from {report['pieces_merged']} pieces)")
    print(f"registered voters : {report['registered_total']:,}   (Mebane 2025: {MEBANE['electors']:,})")
    print(f"Trump  : {report['president']['trump']:,}   (official {OFFICIAL['USP']['REP']:,})")
    print(f"Harris : {report['president']['harris']:,}   (official {OFFICIAL['USP']['DEM']:,})")
    print(f"zero-vote units {report['units_zero_president_votes']}, more votes than registered "
          f"{report['units_more_votes_than_registered']}")
    print(f"wrote {out / 'pa-2024-president.csv'}, pa-2024-senate.csv, pa-2024-precincts.csv, pa-2024-build-report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
