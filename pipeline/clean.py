"""Step 1: raw MEDSL candidate-level CSV  ->  one clean row per precinct per contest.

What this module takes care of (all of these are real gotchas in MEDSL files):
  * blank padding rows (lines of only commas) left behind by spreadsheet exports
  * keeping only general-election rows
  * OVER VOTES / UNDER VOTES / WRITEIN rows that are stored as if they were candidates
  * the same candidate on several party lines (fusion), grouped by candidate name
  * states that report a TOTAL row, per-mode rows, or both (never double counted)
  * absentee / provisional / county-total "precincts" that are not real places
  * contests that a two-party comparison cannot handle (uncontested, top-two, ...)
"""
from collections import Counter
import re

import numpy as np
import pandas as pd

import config

CONTEST = ["state_po", "office", "district", "special"]
UNIT = CONTEST + ["county_fips", "county_name", "jurisdiction_fips", "precinct"]
VCLASSES = ["dem", "rep", "other", "writein", "over", "under"]

REQUIRED = ["precinct", "office", "party_simplified", "votes", "candidate", "district", "state_po"]
OPTIONAL = ["mode", "county_name", "county_fips", "jurisdiction_fips", "stage", "special", "writein", "state", "party_detailed", "jurisdiction_name"]

# Offices elected statewide. Some states report these by congressional district (e.g. Virginia);
# the district is dropped so each statewide race is ONE contest.
STATEWIDE_OFFICES = {"US SENATE", "GOVERNOR", "PRESIDENT", "US PRESIDENT", "SECRETARY OF STATE", "ATTORNEY GENERAL"}

# Party labels that are the Democratic or Republican party under a state-specific name.
DEM_ALIASES = r"^(?:DEMOCRAT(?:IC)?(?:\s+PARTY)?|DEMOCRATIC[\s\-]*FARMER[\s\-]*LABOR|DFL|DEMOCRATIC[\s\-]*NPL|DEMOCRATIC[\s\-]*NONPARTISAN[\s\-]*LEAGUE)$"
REP_ALIASES = r"^(?:REPUBLICAN(?:\s+PARTY)?|GOP)$"


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _norm_fips(s):
    s = s.fillna("").str.strip().str.replace(r"\.0+$", "", regex=True)
    return s.mask(s.str.upper().isin(["NAN", "NA", "NONE", "NULL"]), "")


def read_raw(path, chunksize=250_000, only_states=None):
    """Read the raw CSV in chunks. Returns (candidate-level DataFrame, stats Counter)."""
    header = pd.read_csv(path, nrows=0).columns.tolist()
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise SystemExit(
            f"The input file is missing required column(s): {', '.join(missing)}.\n"
            f"Columns found: {', '.join(header)}"
        )
    cols = [c for c in REQUIRED + OPTIONAL if c in header]
    stats = Counter()
    parts = []
    adjust_units = set()

    reader = pd.read_csv(
        path, usecols=cols, dtype=str, keep_default_na=False, na_values=[""], chunksize=chunksize
    )
    for chunk in reader:
        stats["raw_rows"] += len(chunk)

        # 1. blank padding rows
        blank = chunk[["precinct", "candidate", "votes", "state_po"]].isna().all(axis=1)
        stats["blank_rows"] += int(blank.sum())
        chunk = chunk[~blank]

        # 2. rows with no state are unusable
        no_state = chunk["state_po"].isna()
        stats["rows_without_state"] += int(no_state.sum())
        chunk = chunk[~no_state]

        # 3. general election only
        if "stage" in chunk:
            keep = chunk["stage"].fillna("").str.upper().str.strip() == config.STAGE
            stats["rows_not_general"] += int((~keep).sum())
            chunk = chunk[keep]

        if only_states:
            chunk = chunk[chunk["state_po"].str.upper().isin(only_states)]

        # 4. votes must be a non-negative number. -1 is used as a "not reported" code in some
        #    states and is simply dropped. Larger negative numbers are adjustment rows (e.g.
        #    Michigan's county-level "9999" precincts): the whole unit is later marked
        #    non-geographic so it is never compared with real precincts.
        v = pd.to_numeric(chunk["votes"], errors="coerce")
        bad = v.isna()
        neg = v < 0
        adj = v < -1
        stats["rows_invalid_votes"] += int(bad.sum())
        stats["rows_negative_votes"] += int(neg.sum())
        if adj.any():
            a = chunk[adj]
            for k in zip(a["state_po"].str.upper().str.strip(), _norm_fips(a["county_fips"]) if "county_fips" in a else [""] * len(a),
                         a["precinct"].fillna("(no name)").str.strip()):
                adjust_units.add(k)
        chunk = chunk[~(bad | neg)].assign(votes=v[~(bad | neg)].astype("float64"))
        if chunk.empty:
            continue

        out = pd.DataFrame(index=chunk.index)
        out["state_po"] = chunk["state_po"].str.upper().str.strip()
        out["state"] = chunk["state"].fillna(chunk["state_po"]).str.title() if "state" in chunk else out["state_po"]
        out["office"] = chunk["office"].fillna("").str.upper().str.strip()
        d = chunk["district"].fillna("0").str.strip()
        dn = pd.to_numeric(d, errors="coerce")
        out["district"] = np.where(dn.notna(), dn.fillna(0).astype("int64").astype(str), d.str.upper())
        out.loc[out["office"].isin(STATEWIDE_OFFICES), "district"] = "STATEWIDE"
        out["special"] = (
            chunk["special"].fillna("FALSE").str.upper().str.strip().eq("TRUE") if "special" in chunk else False
        )
        out["county_fips"] = _norm_fips(chunk["county_fips"]) if "county_fips" in chunk else ""
        out["county_name"] = chunk["county_name"].fillna("").str.strip() if "county_name" in chunk else ""
        out["jurisdiction_fips"] = _norm_fips(chunk["jurisdiction_fips"]) if "jurisdiction_fips" in chunk else ""
        out["jurisdiction_name"] = chunk["jurisdiction_name"].fillna("").str.strip() if "jurisdiction_name" in chunk else ""
        out["precinct"] = chunk["precinct"].fillna("(no name)").str.strip()
        out["mode"] = chunk["mode"].fillna("TOTAL").str.upper().str.strip() if "mode" in chunk else "TOTAL"
        out["candidate"] = chunk["candidate"].fillna("").str.strip()
        out["party_simplified"] = chunk["party_simplified"].fillna("").str.upper().str.strip()
        out["party_detailed"] = (
            chunk["party_detailed"].fillna("").str.upper().str.strip() if "party_detailed" in chunk else ""
        )
        out["writein"] = (
            chunk["writein"].fillna("FALSE").str.upper().str.strip().eq("TRUE") if "writein" in chunk else False
        )
        out["votes"] = chunk["votes"]
        parts.append(out)

    if not parts:
        raise SystemExit("No usable general-election rows were found in the input file.")
    df = pd.concat(parts, ignore_index=True)
    df.attrs["adjust_units"] = adjust_units
    stats["rows_kept"] = len(df)
    return df, stats


# --------------------------------------------------------------------------- #
# Classifying rows
# --------------------------------------------------------------------------- #
def classify(df, stats):
    attrs = dict(df.attrs)
    cand = df["candidate"].str.upper()
    over = cand.str.match(r"^OVER[\s\-]*VOTES?$")
    under = cand.str.match(r"^(?:UNDER[\s\-]*VOTES?|BLANK[\s\-]*VOTES?|BLANKS?)$")
    meta = cand.str.contains(
        r"REGISTERED|BALLOTS?\s+CAST|TURNOUT|TOTAL\s+VOTES|VOTES\s+CAST|TOTAL\s+BALLOTS", regex=True
    ) & ~(over | under)
    write = df["writein"] | cand.str.contains(r"WRITE[\s\-]*INS?|SCATTER", regex=True)
    cat = np.select([meta, over, under, write], ["meta", "over", "under", "writein"], default="cand")
    df = df.assign(cat=cat)
    stats["rows_meta_dropped"] = int((cat == "meta").sum())

    # Candidate -> major party, decided per contest by candidate NAME so that a
    # candidate listed on two party lines (fusion voting) counts once, as a
    # Democrat or Republican.
    c = df.loc[df["cat"] == "cand", CONTEST + ["candidate", "party_simplified", "votes"]]
    pd_ = df.loc[df["cat"] == "cand", "party_detailed"]
    c = c.assign(
        is_dem=c["party_simplified"].eq("DEMOCRAT") | pd_.str.match(DEM_ALIASES),
        is_rep=c["party_simplified"].eq("REPUBLICAN") | pd_.str.match(REP_ALIASES),
    )
    cand_tab = (
        c.groupby(CONTEST + ["candidate"], sort=False)
        .agg(is_dem=("is_dem", "max"), is_rep=("is_rep", "max"), votes=("votes", "sum"))
        .reset_index()
    )
    cand_tab["cls"] = np.select(
        [cand_tab["is_dem"] & ~cand_tab["is_rep"], cand_tab["is_rep"] & ~cand_tab["is_dem"]],
        ["dem", "rep"],
        default="other",
    )
    df = df.merge(cand_tab[CONTEST + ["candidate", "cls"]], on=CONTEST + ["candidate"], how="left")
    df.attrs.update(attrs)
    df["vclass"] = np.where(df["cat"] == "cand", df["cls"], df["cat"])

    # Voting mode
    mu = df["mode"]
    df["mclass"] = np.select(
        [
            mu.eq("TOTAL"),
            mu.str.contains(r"ELECTION\s*DAY|POLLING|E-?DAY|^IN[\s\-]*PERSON$|PRECINCT", regex=True),
            mu.str.contains("PROVISIONAL"),
        ],
        ["total", "election_day", "provisional"],
        default="early_absentee",
    )
    return df, cand_tab


def contest_table(cand_tab):
    """One row per contest: how many Democrats / Republicans, and who they are."""
    n = cand_tab.pivot_table(index=CONTEST, columns="cls", values="candidate", aggfunc="nunique", fill_value=0)
    for k in ("dem", "rep", "other"):
        if k not in n:
            n[k] = 0
    n = n.rename(columns={"dem": "n_dem", "rep": "n_rep", "other": "n_other"})[["n_dem", "n_rep", "n_other"]]

    top = cand_tab.sort_values("votes", ascending=False).drop_duplicates(CONTEST + ["cls"])
    for k in ("dem", "rep"):
        names = top[top["cls"] == k].set_index(CONTEST)["candidate"].str.title().rename(f"{k}_name")
        n = n.join(names)
    n[["dem_name", "rep_name"]] = n[["dem_name", "rep_name"]].fillna("")
    n["status"] = np.select(
        [
            (n["n_dem"] > 1) | (n["n_rep"] > 1),  # top-two / same-party runoff: checked first
            (n["n_dem"] == 1) & (n["n_rep"] == 1),
        ],
        ["multi_major", "two_party"],
        default="single_major_party",
    )
    return n.reset_index()


# --------------------------------------------------------------------------- #
# Precinct table
# --------------------------------------------------------------------------- #
def build_precinct_table(df, cand_tab, stats):
    contests = contest_table(cand_tab)

    # ---- totals per precinct: use TOTAL rows if the precinct has them, else sum the modes
    df["is_total_mode"] = df["mclass"].eq("total")
    df["has_total"] = df.groupby(UNIT, sort=False)["is_total_mode"].transform("max")
    use = df["vclass"].ne("meta") & (df["is_total_mode"] | ~df["has_total"])
    wide = df[use].groupby(UNIT + ["vclass"], sort=False)["votes"].sum().unstack("vclass", fill_value=0)
    for k in VCLASSES:
        if k not in wide:
            wide[k] = 0
    wide = wide[VCLASSES].round().astype("int64")

    # ---- mode split (only where election-day AND another mode are reported for the precinct)
    split = df[(~df["is_total_mode"]) & df["vclass"].isin(["dem", "rep", "other", "writein"])]
    if len(split):
        votes = split.groupby(UNIT + ["mclass"], sort=False)["votes"].sum().unstack("mclass", fill_value=0)
        present = split.groupby(UNIT + ["mclass"], sort=False).size().unstack("mclass", fill_value=0) > 0
        for k in ("election_day", "early_absentee", "provisional"):
            if k not in votes:
                votes[k] = 0
                present[k] = False
        modes = pd.DataFrame(
            {
                "v_ed": votes["election_day"].round().astype("int64"),
                "v_non": (votes["early_absentee"] + votes["provisional"]).round().astype("int64"),
                "mode_split": present["election_day"] & (present["early_absentee"] | present["provisional"]),
            }
        )
        wide = wide.join(modes, how="left")
    else:
        wide["v_ed"] = 0
        wide["v_non"] = 0
        wide["mode_split"] = False
    wide["mode_split"] = wide["mode_split"].fillna(False).astype(bool)
    wide[["v_ed", "v_non"]] = wide[["v_ed", "v_non"]].fillna(0).astype("int64")

    p = wide.reset_index()
    p["cand_votes"] = p["dem"] + p["rep"] + p["other"] + p["writein"]
    p["two_party"] = p["dem"] + p["rep"]
    p["ballots"] = p["cand_votes"] + p["over"] + p["under"]

    # ---- contest-level facts
    under_total = p.groupby(CONTEST)["under"].sum().rename("under_total").reset_index()
    contests = contests.merge(under_total, on=CONTEST, how="left")
    contests["under_reported"] = contests["under_total"].fillna(0) > 0
    p = p.merge(
        contests[CONTEST + ["status", "under_reported"]], on=CONTEST, how="left"
    )

    # ---- data-quality flags
    pats = [re.compile(x, re.I) for x in config.NON_GEO_PATTERNS]
    names = pd.Series(p["precinct"].unique())
    nongeo_names = set(names[names.map(lambda s: any(r.search(s) for r in pats))])
    adj = df.attrs.get("adjust_units", set())
    adj_mask = pd.Series([k in adj for k in zip(p["state_po"], p["county_fips"], p["precinct"])], index=p.index) if adj else False
    # jurisdiction names are checked too (e.g. Rhode Island's "FEDERAL PRECINCT" 9001/9002)
    jn = df.groupby(UNIT, sort=False)["jurisdiction_name"].first().reindex(pd.MultiIndex.from_frame(p[UNIT])).to_numpy()
    jnames = pd.Series(pd.unique(jn.astype(str)))
    nongeo_j = set(jnames[jnames.map(lambda s: any(r.search(s) for r in pats))])
    p["nongeo"] = p["precinct"].isin(nongeo_names) | adj_mask | pd.Series(jn.astype(str), index=p.index).isin(nongeo_j)
    stats["adjustment_units"] = int(np.sum(adj_mask)) if adj else 0
    p["zero_votes"] = p["cand_votes"] == 0
    vec = ["dem", "rep", "other", "writein", "over", "under"]
    p["dup_vector"] = p.duplicated(subset=CONTEST + vec, keep=False) & (p["two_party"] >= config.DUP_MIN_VOTES)

    stats["precincts"] = len(p)
    stats["contests"] = len(contests)
    stats["non_geographic_units"] = int(p["nongeo"].sum())
    stats["zero_vote_units"] = int(p["zero_votes"].sum())
    stats["duplicate_vector_units"] = int(p["dup_vector"].sum())
    for s, n in contests["status"].value_counts().items():
        stats[f"contests_{s}"] = int(n)
    return p, contests
