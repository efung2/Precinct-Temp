"""Attach census voting-age population (ALARM census-2020, by voting district) to MEDSL precincts.

Census voting districts (VTDs) only carry a code, MEDSL precincts a name that sometimes contains
that code. For each state the matching rule that links the most precincts one-to-one is chosen
from a few candidates (first token, whole name, last token, all digits, digits after the county
code, trailing number). A link is KEPT only if the two sources agree on the votes: total
two-party votes within 5% (or 10 votes) and Democratic share within 2 points. That vote check
is what makes the match trustworthy: in Virginia, for example, codes match 99% of precincts but
votes never agree (MEDSL reports absentee ballots in county-wide pools).

Only states where at least MIN_USABLE of precincts survive are used.
"""
import re

import numpy as np
import pandas as pd

MIN_USABLE = 0.45
RULES = ("first_token", "whole", "last_token", "digits", "strip_county", "trailing_num")


def _norm(s):
    s = re.sub(r"[^A-Z0-9]", "", str(s).upper())
    return s.lstrip("0") or "0"


def _code(name, cfips, rule):
    n = str(name).upper().strip()
    toks = re.split(r"[\s\-_/]+", n)
    digits = re.sub(r"\D", "", n)
    if rule == "first_token":
        return _norm(toks[0])
    if rule == "whole":
        return _norm(n)
    if rule == "last_token":
        return _norm(toks[-1])
    if rule == "digits":
        return _norm(digits) if digits else None
    if rule == "strip_county":
        c3 = str(cfips)[-3:]
        return _norm(digits[len(c3):]) if c3 and digits.startswith(c3) and len(digits) > len(c3) else None
    if rule == "trailing_num":
        m = re.search(r"(\d+)\s*$", n)
        return _norm(m.group(1)) if m else None


def match_state(m, a, sen_dem_col, sen_rep_col):
    """m: MEDSL precinct rows of one state (dem, rep, county_fips, precinct); a: ALARM VTD file."""
    a = a.copy()
    a["cf"] = a["GEOID20"].str[:5]
    a["code"] = a["vtd"].map(_norm)
    a["vdem"] = a[sen_dem_col].fillna(0)
    a["vrep"] = a[sen_rep_col].fillna(0)
    cnt = a.groupby(["cf", "code"])["code"].transform("size")
    a1 = a[cnt == 1].set_index(["cf", "code"])
    best = None
    for rule in RULES:
        keys = [(cf, _code(nm, cf, rule)) for cf, nm in zip(m["county_fips"], m["precinct"])]
        ks = pd.Series(keys)
        dup = ks.duplicated(keep=False).to_numpy()
        hit = np.array([(k[1] is not None) and (not d) and (k in a1.index) for k, d in zip(keys, dup)])
        if best is None or hit.sum() > best[1].sum():
            best = (rule, hit, keys)
    rule, hit, keys = best
    mm = m[hit].copy()
    if mm.empty:
        return rule, mm
    av = a1.loc[[keys[i] for i in np.flatnonzero(hit)]]
    tm = (mm["dem"] + mm["rep"]).to_numpy(float)
    ta = (av["vdem"] + av["vrep"]).to_numpy(float)
    agree = (np.abs(tm - ta) <= np.maximum(0.05 * tm, 10)) & \
        (np.abs(mm["dem"].to_numpy() / np.maximum(tm, 1) - av["vdem"].to_numpy() / np.maximum(ta, 1)) <= 0.02)
    mm["vap"] = np.rint(av["vap"].fillna(0).to_numpy()).astype(int)
    mm["geoid"] = av["GEOID20"].to_numpy() if "GEOID20" in av else av.index.get_level_values(0)
    mm = mm[agree]
    mm = mm[(mm["vap"] > 0) & ((mm["dem"] + mm["rep"]) <= mm["vap"])]
    return rule, mm


def match_all(p, alarm_dir, log=print):
    """p: MEDSL precinct table (clean.build_precinct_table) for a two-party race per state."""
    from pathlib import Path
    out, report = [], []
    for st in sorted(p["state_po"].unique()):
        f = Path(alarm_dir) / f"{st.lower()}_2020_vtd.csv"
        if not f.exists():
            report.append({"state_po": st, "rule": None, "medsl": int((p.state_po == st).sum()), "usable": 0,
                           "usable_pct": 0.0, "used": False, "note": "no VTD-level census file"})
            continue
        a = pd.read_csv(f, dtype={"GEOID20": str, "vtd": str})
        dc = [c for c in a.columns if c.startswith("uss_20_dem")]
        rc = [c for c in a.columns if c.startswith("uss_20_rep")]
        m = p[p["state_po"] == st]
        if len(dc) != 1 or len(rc) != 1:
            continue
        rule, mm = match_state(m, a, dc[0], rc[0])
        frac = len(mm) / max(len(m), 1)
        used = frac >= MIN_USABLE and len(mm) >= 300
        report.append({"state_po": st, "rule": rule, "medsl": int(len(m)), "usable": int(len(mm)),
                       "usable_pct": round(100 * frac, 1), "used": bool(used)})
        if used:
            out.append(mm)
    rep = pd.DataFrame(report)
    log(f"    real-count matching: {int(rep['used'].sum())} states usable "
        f"({', '.join(rep.loc[rep['used'], 'state_po'])})")
    return (pd.concat(out, ignore_index=True) if out else pd.DataFrame()), rep
