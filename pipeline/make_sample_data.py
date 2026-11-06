#!/usr/bin/env python3
"""Generate a SYNTHETIC precinct file in the MEDSL layout, with known planted anomalies.

Used for testing (and for timing the pipeline on a file the size of the real one).
Nothing in here is real election data.

    python pipeline/make_sample_data.py --out raw/synthetic.csv --planted-out raw/synthetic_planted.csv \
        --companion-out raw/synthetic_senate.csv
    python pipeline/make_sample_data.py --out /tmp/big.csv --copies 100 --pad-blank 5000   # ~900k rows
"""
import argparse
import sys

import numpy as np
import pandas as pd

COLUMNS = [
    "precinct", "office", "party_detailed", "party_simplified", "mode", "votes", "county_name",
    "county_fips", "jurisdiction_name", "jurisdiction_fips", "candidate", "district", "magnitude",
    "dataverse", "year", "stage", "state", "special", "writein", "state_po", "state_fips",
    "state_cen", "state_ic", "date", "readme_check",
]
COUNTS = ["dem", "rep", "other", "writein", "over", "under"]

BASE_STATES = [
    # mode: "total" = TOTAL rows only, "split" = ELECTION DAY/EARLY/ABSENTEE rows, "both" = TOTAL and split rows
    dict(name="ALPHA", mode="total", contests=[("1", "normal"), ("2", "normal"), ("3", "uncontested")]),
    dict(name="BETA", mode="split", contests=[("1", "normal"), ("2", "bimodal")]),
    dict(name="GAMMA", mode="total", contests=[("1", "small_counties"), ("2", "top_two")]),
    dict(name="DELTA", mode="both", contests=[("1", "normal")]),
]


def _beta(rng, mean, sd, size):
    mean = np.clip(mean, 0.02, 0.98)
    var = np.minimum(sd**2, 0.9 * mean * (1 - mean))
    nu = mean * (1 - mean) / var - 1
    return rng.beta(mean * nu, (1 - mean) * nu, size)


def _code(i):
    return chr(65 + (i // 26) % 26) + chr(65 + i % 26)


def _contest_precincts(rng, kind, scale):
    # precincts per county. Counties of 50+ precincts are compared with themselves;
    # smaller counties fall back to the whole district.
    sizes = {
        "normal": [70, 70, 30, 30],
        "bimodal": [70, 40, 40],
        "small_counties": [4] * 14,
        "top_two": [30] * 4,
        "uncontested": [30] * 3,
    }[kind]
    frames = []
    for ci, base_k in enumerate(sizes):
        k = base_k if kind == "small_counties" else max(12, int(round(base_k * scale)))
        n_two = np.maximum(10, rng.lognormal(6.0, 0.8, k)).astype(int)
        if kind == "bimodal" and ci == 0:
            hi = rng.random(k) < 0.5
            share = np.where(hi, _beta(rng, 0.82, 0.06, k), _beta(rng, 0.18, 0.06, k))
        else:
            share = _beta(rng, rng.uniform(0.3, 0.7), 0.07, k)
        dem = rng.binomial(n_two, share)
        rep = n_two - dem
        other = rng.binomial(n_two, 0.02)
        writein = rng.poisson(0.3, k)
        cand = dem + rep + other + writein
        under_rate = rng.uniform(0.02, 0.05)
        under = rng.binomial(cand, under_rate / (1 - under_rate))
        over = rng.poisson(0.2, k)
        frames.append(
            pd.DataFrame(
                {
                    "county_idx": ci,
                    "precinct": [f"PCT {j + 1:03d}" for j in range(k)],
                    "dem": dem, "rep": rep, "other": other, "writein": writein, "over": over, "under": under,
                    "ed_frac": rng.uniform(0.5, 0.7),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _plant(rng, pc, state_po, district, kind, planted, mode):
    """Modify precinct counts in place and record what was planted."""
    def pick(county_idx, n, min_two=600):
        c = pc[(pc.county_idx == county_idx) & ((pc.dem + pc.rep) >= min_two)]
        return list(c.index[: n]) if len(c) else []

    def rec(i, what, detail):
        planted.append(dict(state_po=state_po, district=district, county_idx=int(pc.at[i, "county_idx"]),
                            precinct=pc.at[i, "precinct"], kind=what, detail=detail))

    if kind == "normal" and district == "1" and mode == "total":
        for i in pick(1, 3):  # strong share shifts
            n = pc.at[i, "dem"] + pc.at[i, "rep"]
            pc.at[i, "dem"] = min(n, pc.at[i, "dem"] + int(round(0.35 * n)))
            pc.at[i, "rep"] = n - pc.at[i, "dem"]
            rec(i, "share_shift", "+0.35")
    if kind == "normal" and district == "2" and mode == "total":
        idx = pick(0, 2, min_two=100)
        if len(idx) == 2:  # duplicated vote vector
            for col in COUNTS:
                pc.at[idx[1], col] = pc.at[idx[0], col]
            rec(idx[1], "duplicate", "copy of " + pc.at[idx[0], "precinct"])
        z = pick(2, 1, min_two=0)
        if z:  # zero-vote precinct
            for col in COUNTS:
                pc.at[z[0], col] = 0
            rec(z[0], "zero_votes", "all zero")
    if kind == "normal" and mode == "split" and district == "1":
        u = pick(0, 1, min_two=400)
        if u:
            i = u[0]
            pc.at[i, "under"] = int((pc.at[i, "dem"] + pc.at[i, "rep"]) * 0.35)
            rec(i, "undervote_spike", "rate ~0.26")
        m = pick(1, 1, min_two=400)
        if m:
            pc.at[m[0], "ed_frac"] = 0.03
            rec(m[0], "mode_shift", "ed_frac 0.03")


def _mode_frames(pc, mode, rng):
    """Return list of (mode label, DataFrame of counts)."""
    out = []
    total = pc[["precinct", "county_idx"] + COUNTS].copy()
    if mode in ("total", "both"):
        out.append(("TOTAL", total))
    if mode in ("split", "both"):
        ed = {c: rng.binomial(pc[c].to_numpy(), pc["ed_frac"].to_numpy()) for c in COUNTS}
        rest = {c: pc[c].to_numpy() - ed[c] for c in COUNTS}
        early = {c: rng.binomial(rest[c], 0.6) for c in COUNTS}
        absn = {c: rest[c] - early[c] for c in COUNTS}
        for label, d in (("ELECTION DAY", ed), ("EARLY", early), ("ABSENTEE", absn)):
            f = pc[["precinct", "county_idx"]].copy()
            for c in COUNTS:
                f[c] = d[c]
            out.append((label, f))
    return out


def generate(seed=7, scale=1.0, copies=1):
    rng = np.random.default_rng(seed)
    raw_parts, planted, truth = [], [], []
    si = 0
    for _ in range(copies):
        for base in BASE_STATES:
            po = _code(si)
            state_fips = 60 + si
            si += 1
            for district, kind in base["contests"]:
                pc = _contest_precincts(rng, kind, scale)
                pre = pc[["dem", "rep"]].copy()  # before any planted change (used for the companion race)
                pc["cons_shift"] = 0.0
                n_before = len(planted)
                if copies == 1:
                    _plant(rng, pc, po, district, kind, planted, base["mode"])
                    if kind == "normal" and district == "1" and base["mode"] == "total":
                        # "special population": unusual in BOTH races (should be explained by the cross-race check)
                        c0 = pc[(pc.county_idx == 0) & ((pc.dem + pc.rep) >= 600)].index[:2]
                        for i in c0:
                            n = pc.at[i, "dem"] + pc.at[i, "rep"]
                            pc.at[i, "dem"] = min(n, pc.at[i, "dem"] + int(round(0.35 * n)))
                            pc.at[i, "rep"] = n - pc.at[i, "dem"]
                            pc.at[i, "cons_shift"] = 0.35
                            planted.append(dict(state_po=po, district=district, county_idx=0,
                                                precinct=pc.at[i, "precinct"], kind="special_pop",
                                                detail="+0.35 in both races"))
                if po == _code(2) and district == "1" and kind == "small_counties" and copies == 1:
                    # a non-geographic pool of absentee ballots that must be excluded from peer comparison
                    extra = pd.DataFrame(
                        [dict(county_idx=1, precinct="ABSENTEE", dem=4500, rep=500, other=40, writein=2, over=3,
                              under=100, ed_frac=0.6)]
                    )
                    pc = pd.concat([pc, extra], ignore_index=True)
                    planted.append(dict(state_po=po, district=district, county_idx=1, precinct="ABSENTEE",
                                        kind="nongeo", detail="absentee pool"))

                # counties are numbered per district so that districts do not share precinct names
                offset = (int(district) - 1) * 20
                pc["cons_shift"] = pc["cons_shift"].fillna(0.0)
                pc["county_idx"] = pc["county_idx"] + offset
                for e in planted[n_before:]:
                    e["county_idx"] = int(e["county_idx"]) + offset
                tr = pc.assign(state_po=po, district=district, kind=kind, state_fips=state_fips,
                               state=base["name"] + (f" {si}" if copies > 1 else ""),
                               dem_pre=pre["dem"].reindex(pc.index).fillna(pc["dem"]).to_numpy(),
                               rep_pre=pre["rep"].reindex(pc.index).fillna(pc["rep"]).to_numpy())
                truth.append(tr)

                for label, mc in _mode_frames(pc, base["mode"], rng):
                    cands = [
                        ("dem", "DEMOCRAT", f"{po} DEM D{district}", False),
                        ("rep", "REPUBLICAN", f"{po} REP D{district}", False),
                        ("other", "LIBERTARIAN", f"{po} LIB D{district}", False),
                        ("writein", "", "WRITEIN", True),
                        ("over", "", "OVER VOTES", False),
                        ("under", "", "UNDER VOTES", False),
                    ]
                    if kind == "top_two":  # two Democrats, no Republican
                        cands[1] = ("rep", "DEMOCRAT", f"{po} DEM2 D{district}", False)
                        cands = [c for c in cands if c[0] != "other"]
                    if kind == "uncontested":
                        cands = [c for c in cands if c[0] not in ("dem", "other")]
                    for col, party, cname, wi in cands:
                        part = pd.DataFrame(
                            {
                                "precinct": mc["precinct"].to_numpy(),
                                "county_idx": mc["county_idx"].to_numpy(),
                                "votes": mc[col].to_numpy(),
                            }
                        )
                        part["party_detailed"] = party
                        part["party_simplified"] = party
                        part["candidate"] = cname
                        part["writein"] = "TRUE" if wi else "FALSE"
                        part["mode"] = label
                        part["district"] = district
                        part["state_po"] = po
                        part["state"] = base["name"] + (f" {si}" if copies > 1 else "")
                        part["state_fips"] = state_fips
                        raw_parts.append(part)

    raw = pd.concat(raw_parts, ignore_index=True)
    raw["county_name"] = raw["state_po"] + " COUNTY " + (raw["county_idx"] + 1).astype(str)
    raw["county_fips"] = 90000 + raw["state_fips"] * 100 + raw["county_idx"]
    raw["jurisdiction_name"] = raw["county_name"]
    raw["jurisdiction_fips"] = raw["county_fips"]
    raw["office"] = "US HOUSE"
    raw["magnitude"] = 1
    raw["dataverse"] = "HOUSE"
    raw["year"] = 2018
    raw["stage"] = "GEN"
    raw["special"] = "FALSE"
    raw["state_cen"] = 0
    raw["state_ic"] = 0
    raw["date"] = "11/6/18"
    raw["readme_check"] = "FALSE"
    raw = raw[COLUMNS]

    truth = pd.concat(truth, ignore_index=True)
    planted = pd.DataFrame(planted)
    return raw, planted, truth


def generate_companion(truth, seed=8):
    """A second race on the same ballot (a statewide "Senate" race) for every synthetic precinct.

    Its Democratic share follows the precinct's House share BEFORE any planted change, plus a
    district-level candidate effect and a little noise. Precincts planted as "special_pop" are
    shifted in both races; "share_shift" precincts are shifted in the House race only.
    """
    rng = np.random.default_rng(seed)
    t = truth.copy()
    n_pre = (t["dem_pre"] + t["rep_pre"]).astype(float)
    base = np.where((n_pre > 0) & t["kind"].isin(["normal", "bimodal", "small_counties"]),
                    t["dem_pre"] / np.maximum(n_pre, 1), rng.uniform(0.3, 0.6, len(t)))
    eff = {k: rng.normal(0, 0.04) for k in t[["state_po", "district"]].drop_duplicates().itertuples(index=False)}
    delta = np.array([eff[(a, b)] for a, b in zip(t["state_po"], t["district"])])
    sh = np.clip(base + delta + rng.normal(0, 0.02, len(t)) + t["cons_shift"].to_numpy(), 0.01, 0.99)
    n = np.maximum(n_pre, 30).astype(int) + rng.poisson(5, len(t))
    dem = rng.binomial(n, sh)
    rep = n - dem
    under = rng.binomial(n, 0.02)
    parts = []
    for col, party, name, votes in (("dem", "DEMOCRAT", "SEN DEM", dem), ("rep", "REPUBLICAN", "SEN REP", rep),
                                    ("under", "", "UNDER VOTES", under)):
        d = pd.DataFrame({"precinct": t["precinct"].to_numpy(), "county_idx": t["county_idx"].to_numpy(),
                          "state_po": t["state_po"].to_numpy(), "state": t["state"].to_numpy(),
                          "state_fips": t["state_fips"].to_numpy(), "votes": votes})
        d["candidate"] = d["state_po"] + " " + name if party else name
        d["party_detailed"] = party
        d["party_simplified"] = party
        parts.append(d)
    raw = pd.concat(parts, ignore_index=True)
    raw["county_name"] = raw["state_po"] + " COUNTY " + (raw["county_idx"] + 1).astype(str)
    raw["county_fips"] = 90000 + raw["state_fips"] * 100 + raw["county_idx"]
    raw["jurisdiction_name"] = raw["county_name"]
    raw["jurisdiction_fips"] = raw["county_fips"]
    raw["office"] = "US SENATE"
    raw["district"] = "STATEWIDE"
    raw["mode"] = "TOTAL"
    raw["writein"] = "FALSE"
    raw["magnitude"] = 1
    raw["dataverse"] = "SENATE"
    raw["year"] = 2018
    raw["stage"] = "GEN"
    raw["special"] = "FALSE"
    raw["state_cen"] = 0
    raw["state_ic"] = 0
    raw["date"] = "11/6/18"
    raw["readme_check"] = "FALSE"
    return raw[COLUMNS]


def write_csv(raw, path, pad_blank=0):
    raw.to_csv(path, index=False)
    if pad_blank:
        with open(path, "a", encoding="utf-8") as f:
            f.write((("," * (len(COLUMNS) - 1)) + "\n") * pad_blank)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--planted-out", default=None)
    ap.add_argument("--companion-out", default=None, help="also write a second race (statewide Senate) for the same precincts")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scale", type=float, default=1.0, help="multiplies precincts per county")
    ap.add_argument("--copies", type=int, default=1, help="replicate the 4 synthetic states this many times")
    ap.add_argument("--pad-blank", type=int, default=0, help="append this many comma-only blank lines")
    a = ap.parse_args(argv)
    raw, planted, truth = generate(a.seed, a.scale, a.copies)
    write_csv(raw, a.out, a.pad_blank)
    if a.companion_out:
        comp = generate_companion(truth, a.seed + 1)
        write_csv(comp, a.companion_out)
        print(f"wrote {len(comp):,} companion-race rows to {a.companion_out}")
    print(f"wrote {len(raw):,} data rows (+{a.pad_blank:,} blank) to {a.out}")
    if a.planted_out:
        planted.to_csv(a.planted_out, index=False)
        print(f"wrote {len(planted)} planted anomalies to {a.planted_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
