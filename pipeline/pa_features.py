#!/usr/bin/env python3
"""Pennsylvania 2024: give every precinct a location, a 2020 baseline and a campaign-activity measure.

Inputs (defaults are in raw/pa-2024/ and raw/census-vest-2020/, see README):
    pa-2024-precincts.csv   from pa_convert.py
    pa_vtd_2020.zip         U.S. Census Bureau TIGER/Line 2020 voting districts (only the .dbf table
                            is read: each district's id and internal point)
    pa_2020_vtd.csv         ALARM Project census-2020 file: 2020 voting-age population by race and
                            the 2020 presidential vote (VEST) for the same districts
    oppexp24.zip, cm24.zip  FEC bulk data 2023-24: operating expenditures and the committee list

Outputs (raw/pa-2024/):
    pa-2024-features.csv    one row per precinct
    pa-2024-events.csv      the campaign event venues found in the FEC data
    pa-2024-features-report.json

Campaign activity = venue rentals. The FEC file gives the payee's city and ZIP code, not a street
address. Payroll and staff reimbursements are all recorded at the campaign's Philadelphia office,
and field-office leases do not appear in the presidential committees' Pennsylvania spending, so
neither can be located. Venue rentals for public events CAN be located: the payee is the venue. A
precinct's exposure to a side's events is the sum over that side's venues of exp(-distance / 15 km).
Only the two presidential campaigns and their parties' national committees are counted.
"""
import argparse
import csv
import io
import json
import math
import re
import struct
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PA = ROOT / "raw" / "pa-2024"

# committee id -> (side, name fragment that MUST appear in the FEC committee list, label)
COMMITTEES = {
    "C00703975": ("dem", "HARRIS FOR PRESIDENT", "Harris for President"),
    "C00744946": ("dem", "HARRIS VICTORY FUND", "Harris Victory Fund"),
    "C00010603": ("dem", "DEMOCRATIC NATIONAL COMMITTEE", "Democratic National Committee"),
    # the Trump campaign committee (Donald J. Trump for President 2024, Inc.) was renamed after the election
    "C00828541": ("rep", "NEVER SURRENDER", "Never Surrender, Inc. (Trump campaign committee)"),
    "C00867937": ("rep", "TRUMP 47 COMMITTEE", "Trump 47 Committee, Inc."),
    "C00873893": ("rep", "TRUMP NATIONAL COMMITTEE", "Trump National Committee JFC, Inc."),
    "C00003418": ("rep", "REPUBLICAN NATIONAL COMMITTEE", "Republican National Committee"),
}
TRUMP_CANDIDATE_ID = "P80001571"
VENUE = re.compile(r"(?:SITE|FACILITY|VENUE|EVENT\s+SPACE)\s+RENTAL", re.I)
NOT_PUBLIC = re.compile(r"FUNDRAIS|CREDIT", re.I)
WINDOW = (date(2024, 7, 22), date(2024, 12, 31))  # Harris-Trump contest; venues are often paid after the event
DECAY_KM = 15.0
OPPEXP_COLS = ["CMTE_ID", "AMNDT_IND", "RPT_YR", "RPT_TP", "IMAGE_NUM", "LINE_NUM", "FORM_TP_CD", "SCHED_TP_CD", "NAME",
               "CITY", "STATE", "ZIP_CODE", "TRANSACTION_DT", "TRANSACTION_AMT", "TRANSACTION_PGI", "PURPOSE",
               "CATEGORY", "CATEGORY_DESC", "MEMO_CD", "MEMO_TEXT", "ENTITY_TP", "SUB_ID", "FILE_NUM", "TRAN_ID",
               "BACK_REF_TRAN_ID"]


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def read_vtd_points(zip_path):
    """GEOID20 -> (lat, lon) from the .dbf table inside the Census shapefile zip (no GIS library needed)."""
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".dbf"))
        b = z.read(name)
    n, hlen, rlen = struct.unpack("<IHH", b[4:12])
    fields, o, pos = {}, 32, 1
    while b[o] != 0x0D:
        fname = b[o:o + 11].split(b"\0")[0].decode()
        flen = b[o + 16]
        fields[fname] = (pos, flen)
        pos += flen
        o += 32
    out = {}
    for i in range(n):
        rec = b[hlen + i * rlen: hlen + (i + 1) * rlen]
        get = lambda f: rec[fields[f][0]: fields[f][0] + fields[f][1]].decode("latin-1").strip()  # noqa: E731
        out[get("GEOID20")] = (float(get("INTPTLAT20")), float(get("INTPTLON20")))
    return out


def read_events(oppexp_zip, cm_zip, log=print):
    import zipcodes  # small pure-Python package with ZIP-code centroids

    with zipfile.ZipFile(cm_zip) as z:
        cm = {}
        for line in io.TextIOWrapper(z.open(z.namelist()[0]), encoding="latin-1"):
            f = line.rstrip("\n").split("|")
            cm[f[0]] = f
    for cid, (_, must, _) in COMMITTEES.items():
        if cid not in cm or must not in cm[cid][1].upper():
            raise SystemExit(f"Committee {cid} is not '{must}' in the FEC committee list (found: "
                             f"{cm.get(cid, ['', 'missing'])[1]}). The committee ids need checking.")
    if cm["C00828541"][14] != TRUMP_CANDIDATE_ID:
        raise SystemExit("C00828541 is no longer linked to Donald Trump's candidate id; check the committee ids.")

    ix = {c: i for i, c in enumerate(OPPEXP_COLS)}
    pay = []
    seen = set()
    with zipfile.ZipFile(oppexp_zip) as z:
        for line in io.TextIOWrapper(z.open(z.namelist()[0]), encoding="latin-1"):
            f = line.rstrip("\n").split("|")
            if len(f) < 22 or f[ix["CMTE_ID"]] not in COMMITTEES or f[ix["STATE"]] != "PA":
                continue
            purpose = f[ix["PURPOSE"]]
            if not VENUE.search(purpose) or NOT_PUBLIC.search(purpose) or f[ix["ENTITY_TP"]] != "ORG":
                continue
            try:
                m, d, y = (int(x) for x in f[ix["TRANSACTION_DT"]].split("/"))
                when = date(y, m, d)
                amt = float(f[ix["TRANSACTION_AMT"]])
            except ValueError:
                continue
            if not (WINDOW[0] <= when <= WINDOW[1]) or amt <= 0 or f[ix["SUB_ID"]] in seen:
                continue
            seen.add(f[ix["SUB_ID"]])
            pay.append({"committee": f[0], "side": COMMITTEES[f[0]][0], "payee": re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7E]|\?", " ", f[ix["NAME"]])).strip(),
                        "city": f[ix["CITY"]].strip(), "zip": f[ix["ZIP_CODE"]][:5], "date": when, "amount": amt,
                        "purpose": purpose.strip()})
    venues = {}
    no_zip = 0
    for p in pay:
        z = zipcodes.matching(p["zip"]) if re.fullmatch(r"\d{5}", p["zip"]) else []
        if not z or z[0]["state"] != "PA":
            no_zip += 1
            continue
        key = (p["side"], p["payee"].upper(), p["zip"])
        v = venues.setdefault(key, {"side": p["side"], "venue": p["payee"], "city": p["city"], "zip": p["zip"],
                                    "lat": float(z[0]["lat"]), "lon": float(z[0]["long"]), "payments": 0, "amount": 0.0,
                                    "first_payment": p["date"], "last_payment": p["date"], "committees": set()})
        v["payments"] += 1
        v["amount"] += p["amount"]
        v["first_payment"] = min(v["first_payment"], p["date"])
        v["last_payment"] = max(v["last_payment"], p["date"])
        v["committees"].add(COMMITTEES[p["committee"]][2])
    out = sorted(venues.values(), key=lambda v: (v["side"], v["first_payment"], v["venue"]))
    log(f"FEC: {len(pay)} venue-rental payments -> {len(out)} venues "
        f"({sum(v['side'] == 'dem' for v in out)} Democratic, {sum(v['side'] == 'rep' for v in out)} Republican); "
        f"{no_zip} payments without a usable Pennsylvania ZIP code")
    return out, {"payments": len(pay), "payments_without_zip": no_zip}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--precincts", default=str(PA / "pa-2024-precincts.csv"))
    ap.add_argument("--vtd", default=str(PA / "pa_vtd_2020.zip"))
    ap.add_argument("--alarm", default=str(ROOT / "raw" / "census-vest-2020" / "pa_2020_vtd.csv"))
    ap.add_argument("--oppexp", default=str(PA / "oppexp24.zip"))
    ap.add_argument("--committees", default=str(PA / "cm24.zip"))
    ap.add_argument("--out", default=str(PA))
    a = ap.parse_args(argv)
    for p in (a.precincts, a.vtd, a.alarm, a.oppexp, a.committees):
        if not Path(p).exists():
            raise SystemExit(f"File not found: {p}\nSee README, 'Pennsylvania 2024', for where each file comes from.")
    out = Path(a.out)

    units = list(csv.DictReader(open(a.precincts, encoding="utf-8")))
    pts = read_vtd_points(a.vtd)
    alarm = {r["GEOID20"]: r for r in csv.DictReader(open(a.alarm, encoding="utf-8"))}
    events, ev_report = read_events(a.oppexp, a.committees)

    # ---- location: census district point; else the mean of the municipality's located precincts; else the county's
    by_muni, by_county = defaultdict(list), defaultdict(list)
    for u in units:
        if u["geoid20"] in pts:
            by_muni[(u["county_fips"], u["mcd_code"])].append(pts[u["geoid20"]])
            by_county[u["county_fips"]].append(pts[u["geoid20"]])
    mean = lambda xs: (sum(x[0] for x in xs) / len(xs), sum(x[1] for x in xs) / len(xs))  # noqa: E731
    src = defaultdict(int)
    rows = []
    for u in units:
        g = u["geoid20"]
        if g in pts:
            loc, how = pts[g], "district"
        elif by_muni.get((u["county_fips"], u["mcd_code"])):
            loc, how = mean(by_muni[(u["county_fips"], u["mcd_code"])]), "municipality"
        else:
            loc, how = mean(by_county[u["county_fips"]]), "county"
        src[how] += 1
        r = {"unit_id": u["unit_id"], "county": u["county"], "precinct": u["precinct"], "geoid20": g,
             "lat": round(loc[0], 5), "lon": round(loc[1], 5), "location_from": how}
        al = alarm.get(g)
        vap = float(al["vap"]) if al else 0.0
        if al and vap > 0:
            d20, r20 = float(al["pre_20_dem_bid"] or 0), float(al["pre_20_rep_tru"] or 0)
            r.update(vap_2020=int(vap), white_share=round(float(al["vap_white"]) / vap, 4),
                     black_share=round(float(al["vap_black"]) / vap, 4), hisp_share=round(float(al["vap_hisp"]) / vap, 4),
                     dem_2020=round(d20, 1), rep_2020=round(r20, 1))
        else:
            r.update(vap_2020="", white_share="", black_share="", hisp_share="", dem_2020="", rep_2020="")
        for side in ("dem", "rep"):
            ds = [km(loc[0], loc[1], e["lat"], e["lon"]) for e in events if e["side"] == side]
            r[f"events_{side}"] = round(sum(math.exp(-d / DECAY_KM) for d in ds), 4)
            r[f"events_{side}_25km"] = sum(d <= 25 for d in ds)
            r[f"nearest_event_{side}_km"] = round(min(ds), 1) if ds else ""
        rows.append(r)

    with open(out / "pa-2024-features.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(out / "pa-2024-events.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["side", "venue", "city", "zip", "lat", "lon", "payments", "amount", "first_payment", "last_payment", "committees"]
        w = csv.writer(f)
        w.writerow(cols)
        for e in events:
            w.writerow([e["side"], e["venue"], e["city"], e["zip"], e["lat"], e["lon"], e["payments"], round(e["amount"], 2),
                        e["first_payment"].isoformat(), e["last_payment"].isoformat(), "; ".join(sorted(e["committees"]))])
    with_base = sum(1 for r in rows if r["vap_2020"] != "")
    report = {"units": len(rows), "location_from": dict(src), "with_2020_baseline": with_base,
              "events": {"venues": len(events), "dem": sum(e["side"] == "dem" for e in events),
                         "rep": sum(e["side"] == "rep" for e in events), **ev_report,
                         "window": [WINDOW[0].isoformat(), WINDOW[1].isoformat()], "decay_km": DECAY_KM,
                         "committees": {k: v[2] for k, v in COMMITTEES.items()}}}
    (out / "pa-2024-features-report.json").write_text(json.dumps(report, indent=1))
    print(f"precincts: {len(rows):,}; located by census district {src['district']:,}, municipality {src['municipality']:,}, "
          f"county {src['county']:,}; with 2020 baseline {with_base:,}")
    print(f"wrote {out / 'pa-2024-features.csv'}, pa-2024-events.csv, pa-2024-features-report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
