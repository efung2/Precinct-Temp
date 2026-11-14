#!/usr/bin/env python3
"""eforensics (our Python re-implementation) on Pennsylvania 2024, President, with and without covariates.

The same model is fitted several times; ONLY the turnout / vote-choice covariates change:

    plain              intercepts only (the default qbl call)
    county             + county effects                      (as in Mebane 2025, Table 2)
    county_campaign    + county effects + campaign events    <- the test of the hypothesis
    county_history     + county effects + precinct history and make-up
    county_all         + county effects + history + campaign events

Units: precincts with registered voters, at least one vote and no more votes than registered
voters. Eligible voters N = registered voters; V = votes for all presidential candidates;
leader = the statewide winner (Trump). Results are cached per specification in
raw/pa-2024/eforensics-cache/, so an interrupted run continues where it stopped.

    python3 pipeline/pa_eforensics.py                 # all specifications
    python3 pipeline/pa_eforensics.py --specs county county_campaign
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eforensics as ef  # noqa: E402

ROOT = HERE.parent
PA = ROOT / "raw" / "pa-2024"
SPECS = {
    "plain": dict(county=False, history=False, campaign=False, label="Intercepts only"),
    "county": dict(county=True, history=False, campaign=False, label="County effects"),
    "county_campaign": dict(county=True, history=False, campaign=True, label="County effects + campaign events"),
    "county_history": dict(county=True, history=True, campaign=False, label="County effects + precinct history and make-up"),
    "county_all": dict(county=True, history=True, campaign=True, label="County effects + history + campaign events"),
}
CAMPAIGN = ["events_dem_log", "events_rep_log"]
HISTORY = ["trump_2020_logit", "turnout_2020_logit", "white_share", "black_share", "hisp_share",
           "reg_rep_share", "reg_dem_share", "no_baseline"]


def logit(x, lo=0.01, hi=0.99):
    x = np.clip(x, lo, hi)
    return np.log(x / (1 - x))


def load_units(precincts=PA / "pa-2024-precincts.csv", features=PA / "pa-2024-features.csv"):
    """Merged precinct + feature table with the model covariates (also used by pa_study.py)."""
    u = pd.read_csv(precincts, dtype={"unit_id": str, "geoid20": str, "county_fips": str})
    f = pd.read_csv(features, dtype={"unit_id": str, "geoid20": str})
    d = u.merge(f.drop(columns=["county", "precinct", "geoid20"]), on="unit_id", how="left", validate="one_to_one")
    d["registered"] = pd.to_numeric(d["registered"], errors="coerce").fillna(0)
    d["votes"] = d["pres_dem"] + d["pres_rep"] + d["pres_other"]
    d["usable"] = (d["registered"] > 0) & (d["votes"] > 0) & (d["votes"] <= d["registered"])
    d["events_dem_log"] = np.log1p(d["events_dem"])
    d["events_rep_log"] = np.log1p(d["events_rep"])
    two20 = d["dem_2020"] + d["rep_2020"]
    has = d["vap_2020"].notna() & (two20 > 0)
    d["no_baseline"] = (~has).astype(float)
    d["trump_2020_logit"] = np.where(has, logit(d["rep_2020"] / two20.where(two20 > 0)), np.nan)
    d["turnout_2020_logit"] = np.where(has, logit(two20 / d["vap_2020"].where(d["vap_2020"] > 0)), np.nan)
    reg = d["registered"].where(d["registered"] > 0)
    d["reg_rep_share"] = d["registered_rep"] / reg
    d["reg_dem_share"] = d["registered_dem"] / reg
    return d


def design(d, cols):
    """Standardised covariate matrix; missing values are set to the mean (0) after standardising."""
    X = np.zeros((len(d), len(cols)))
    for j, c in enumerate(cols):
        x = d[c].to_numpy(float)
        ok = np.isfinite(x)
        sd = x[ok].std()
        X[:, j] = np.where(ok, (x - x[ok].mean()) / (sd if sd > 0 else 1.0), 0.0)
    return X


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--specs", nargs="+", default=list(SPECS), choices=list(SPECS))
    ap.add_argument("--warmup", type=int, default=400)
    ap.add_argument("--samples", type=int, default=250)
    ap.add_argument("--chains", type=int, default=4)
    ap.add_argument("--seed", type=int, default=2024)
    ap.add_argument("--force", action="store_true", help="refit even if a cached result exists")
    a = ap.parse_args(argv)
    cache = PA / "eforensics-cache"
    cache.mkdir(parents=True, exist_ok=True)

    d = load_units()
    d = d[d["usable"]].reset_index(drop=True)
    N = d["registered"].to_numpy(int)
    V = d["votes"].to_numpy(int)
    W = d["pres_rep"].to_numpy(int)
    groups = np.unique(d["county"], return_inverse=True)[1]
    print(f"{len(d):,} precincts; registered {N.sum():,}; votes {V.sum():,}; leader (Trump) {W.sum():,}", flush=True)

    for name in a.specs:
        out = cache / f"{name}.json"
        if out.exists() and not a.force:
            print(f"  {name}: cached", flush=True)
            continue
        sp = SPECS[name]
        cols = (HISTORY if sp["history"] else []) + (CAMPAIGN if sp["campaign"] else [])
        X = design(d, cols) if cols else None
        summ, post = ef.run_units(N, N - V, W, seed=a.seed, warmup=a.warmup, samples=a.samples, chains=a.chains,
                                  label=name, X=X, groups=groups if sp["county"] else None)
        summ.update(spec=name, label=sp["label"], covariates=cols, county_effects=sp["county"],
                    registered=int(N.sum()), votes=int(V.sum()),
                    unit_id=d["unit_id"].tolist(),
                    pfraud=np.round(post["pfraud"], 4).tolist(), p3=np.round(post["p3"], 4).tolist(),
                    fraud_votes_unit=np.round(post["fraud_votes"], 2).tolist())
        out.write_text(json.dumps(summ))
        print(f"  {name}: flags>0.5 {summ['flag_05']:,}; fraudulent votes {summ['fraud_votes_mean']:,.0f} "
              f"[{summ['fraud_votes_lo']:,.0f}, {summ['fraud_votes_hi']:,.0f}]; converged {summ['converged']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
