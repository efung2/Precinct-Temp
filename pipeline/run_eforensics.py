#!/usr/bin/env python3
"""eforensics vs. our checks, 2020 U.S. Senate.

Three parts (all write into docs/data/eforensics-senate-2020/):

  census   eforensics on every usable state, using census voting districts (ALARM census-2020
           files: voting-age population + VEST Senate votes). Our pipeline must already have been
           run on the SAME districts (dataset senate-2020-vtd), so the two can be compared unit by unit.
  real     robustness check: eforensics on REAL MEDSL counts in states where census districts can be
           matched to MEDSL precincts (vap_match.py); compared with our senate-2020 dataset.
  sim      head-to-head on simulated elections with KNOWN truth (clean, and with planted fraud):
           how often does each method flag clean precincts, and how often does it catch real ones?

Typical use (after alarm_convert.py and the two run_pipeline.py runs; see README):

    python3 pipeline/run_eforensics.py --parts census real sim

Results for each state are cached in raw/eforensics-cache/, so an interrupted run can be resumed.
"""
import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import eforensics as ef  # noqa: E402

ROOT = HERE.parent
VERDICT_KEYS = ("T", "E", "C", "L")


def _dump(obj, path):
    path.parent.mkdir(parents=True, exist_ok=True)

    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating, float)):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, np.bool_):
            return bool(o)
        return o

    path.write_text(json.dumps(clean(obj), separators=(",", ":")))


def our_results(dataset_dir, st):
    """Our pipeline's verdicts for one state, keyed by (county, precinct)."""
    f = Path(dataset_dir) / "states" / f"{st}.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text())
    out = {}
    for c in d["contests"].values():
        if "rows" not in c:
            continue
        idx = {n: i for i, n in enumerate(d["fields"])}
        for r in c["rows"]:
            out[(r[idx["county"]], r[idx["precinct"]])] = {
                "screened": r[idx["screened"]], "remain": r[idx["remain"]],
                "share_v": r[idx["share_v"]], "cross_v": r[idx["cross_v"]],
            }
    return out


def compare(units, post, ours):
    """Unit-level overlap between eforensics flags and our verdicts."""
    n = len(units)
    o = [ours.get((c, p)) if ours else None for c, p in zip(units["county_name"], units["precinct"])]
    found = np.array([x is not None for x in o])
    remain = np.array([bool(x and x["remain"]) for x in o])
    screened = np.array([bool(x and x["screened"]) for x in o])
    sv = np.array([(x or {}).get("share_v", "") or "" for x in o])
    f05 = post["pfraud"] > 0.5
    f09 = post["pfraud"] > 0.9
    why = {k: int((f05 & (sv == k)).sum()) for k in VERDICT_KEYS}
    why["not screened"] = int((f05 & found & ~screened).sum())
    return {
        "units_matched_to_ours": int(found.sum()),
        "ours_screened": int(screened.sum()),
        "ours_remain": int(remain.sum()),
        "ef05_and_ours_remain": int((f05 & remain).sum()),
        "ef09_and_ours_remain": int((f09 & remain).sum()),
        "ef05_outcome_in_ours": why,
    }, remain, screened


def leader_split(dem, rep):
    """eforensics' 'leader' is the statewide winner; returns W (leader votes) and the leader's party."""
    if dem.sum() >= rep.sum():
        return dem, "DEM"
    return rep, "REP"


def run_state(label, units, cache_dir, args, log):
    key = cache_dir / f"{label}.json"
    keyn = cache_dir / f"{label}.npz"
    if args.resume and key.exists() and keyn.exists():
        s = json.loads(key.read_text())
        z = np.load(keyn)
        return s, {k: z[k] for k in z.files}
    N = units["vap"].to_numpy(int)
    W, lead = leader_split(units["dem"].to_numpy(int), units["rep"].to_numpy(int))
    A = N - (units["dem"] + units["rep"]).to_numpy(int)
    s, post = ef.run_units(N, A, W, seed=args.seed, warmup=args.warmup, samples=args.samples,
                           chains=args.chains, log=log, label=label)
    s["leader"] = lead
    cache_dir.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps(s))
    np.savez_compressed(keyn, **{k: v for k, v in post.items() if k != "fraud_votes_total_draws"})
    return s, post


def part_units(mode, units_all, ours_dir, out_dir, args, log, unit_files):
    cache = ROOT / "raw" / "eforensics-cache"
    rows = []
    for st, u in units_all.groupby("state_po", sort=True):
        if args.only_states and st not in args.only_states:
            continue
        u = u.reset_index(drop=True)
        s, post = run_state(f"{mode}_{st}", u, cache, args, log)
        ours = our_results(ours_dir, st)
        cmp_, remain, screened = compare(u, post, ours)
        s.update(state_po=st, state=u["state"].iloc[0] if "state" in u else st, **cmp_)
        rows.append(s)
        if unit_files:
            V = (u["dem"] + u["rep"]).to_numpy(float)
            W, _ = leader_split(u["dem"].to_numpy(int), u["rep"].to_numpy(int))
            _dump({
                "fields": ["precinct", "county", "N", "turnout", "leader_share", "pfraud", "fraud_votes", "ours_remain", "ours_screened"],
                "leader": s["leader"],
                "rows": [[p, c, int(n), round(v / n, 4), round(w / max(v, 1), 4), round(float(pf), 3), round(float(fv), 1), bool(r), bool(sc)]
                         for p, c, n, v, w, pf, fv, r, sc in zip(u["precinct"], u["county_name"], u["vap"], V, W,
                                                                 post["pfraud"], post["fraud_votes"], remain, screened)],
            }, out_dir / "units" / f"{st}.json")
    tot = {
        "states": len(rows),
        "units": int(sum(r["n"] for r in rows)),
        "flag_05": int(sum(r["flag_05"] for r in rows)),
        "flag_09": int(sum(r["flag_09"] for r in rows)),
        "flag_05_min_chain": int(sum(min(r["flag_05_by_chain"]) for r in rows)),
        "flag_05_max_chain": int(sum(max(r["flag_05_by_chain"]) for r in rows)),
        "fraud_votes": float(sum(r["fraud_votes_mean"] for r in rows)),
        "leader_votes": int(sum(r["leader_votes"] for r in rows)),
        "converged_states": int(sum(r["converged"] for r in rows)),
        "ours_screened": int(sum(r["ours_screened"] for r in rows)),
        "ours_remain": int(sum(r["ours_remain"] for r in rows)),
        "ef05_and_ours_remain": int(sum(r["ef05_and_ours_remain"] for r in rows)),
    }
    return {"states": rows, "totals": tot}


# --------------------------------------------------------------------------- #
# Head-to-head simulations
# --------------------------------------------------------------------------- #
def _our_pipeline_on(units, tmp, log):
    """Run our full pipeline (no companion race) on a simulated unit table; return remain/screened arrays."""
    import run_pipeline
    rows = []
    for col, party in (("dem", "DEMOCRAT"), ("rep", "REPUBLICAN")):
        rows.append(pd.DataFrame({
            "precinct": units["precinct"], "office": "US SENATE", "party_detailed": party, "party_simplified": party,
            "mode": "TOTAL", "votes": units[col], "county_name": units["county_name"], "county_fips": units["county_fips"],
            "jurisdiction_fips": units["county_fips"], "candidate": party[:3] + " SIM", "district": "STATEWIDE",
            "stage": "GEN", "state": units["state"], "special": "FALSE", "writein": "FALSE", "state_po": units["state_po"],
        }))
    csv = Path(tmp) / "sim.csv"
    pd.concat(rows).to_csv(csv, index=False)
    run_pipeline.main(["--input", str(csv), "--dataset-id", "sim", "--out", str(Path(tmp) / "out"), "--skip-calibration"])
    ours = our_results(Path(tmp) / "out" / "sim", units["state_po"].iloc[0])
    o = [ours.get((c, p)) for c, p in zip(units["county_name"], units["precinct"])]
    return (np.array([bool(x and x["remain"]) for x in o]), np.array([bool(x and x["screened"]) for x in o]))


def part_sim(units_all, args, log):
    """Simulated elections on a real state's precinct structure (sizes and counties)."""
    tmpl = units_all[units_all["state_po"] == args.sim_state].reset_index(drop=True)
    rng = np.random.default_rng(args.seed + 100)
    N = tmpl["vap"].to_numpy(int)
    # realistic levels and spreads from the real data (logit scale)
    V = (tmpl["dem"] + tmpl["rep"]).to_numpy(float)
    W, _ = leader_split(tmpl["dem"].to_numpy(int), tmpl["rep"].to_numpy(int))
    lg = lambda x: np.log(np.clip(x, 0.01, 0.99) / (1 - np.clip(x, 0.01, 0.99)))  # noqa: E731
    l_nu, l_tau = lg(W / np.maximum(V, 1)), lg(V / N)
    mu = dict(nu=float(np.median(l_nu)), tau=float(np.median(l_tau)), iM=-1.0, iS=-1.0, uM=0.0, uS=0.0)
    s_nu, s_tau = float(np.std(l_nu)), float(np.std(l_tau))
    county = tmpl["county_fips"].to_numpy()
    scen = [
        ("clean_model", "Clean election that follows eforensics' own no-fraud assumptions",
         dict(pi=[1, 0, 0], sd=dict(nu=s_nu, tau=s_tau), corr=0.0, group=None)),
        ("clean_real", "Clean election with real-world structure: precincts cluster by county, and turnout is correlated with support",
         dict(pi=[1, 0, 0], sd=dict(nu=0.6 * s_nu, tau=0.6 * s_tau), corr=0.4, group=dict(nu=0.8 * s_nu, tau=0.8 * s_tau))),
        ("fraud_real", "Same real-world structure, plus incremental fraud planted in 2% of precincts",
         dict(pi=[0.98, 0.02, 0.0], sd=dict(nu=0.6 * s_nu, tau=0.6 * s_tau), corr=0.4, group=dict(nu=0.8 * s_nu, tau=0.8 * s_tau))),
    ]
    out = {"template_state": args.sim_state, "units": int(len(N)), "scenarios": []}
    for key, desc, sp in scen:
        sd = dict(nu=sp["sd"]["nu"], tau=sp["sd"]["tau"], iM=0.3, iS=0.3, uM=0.3, uS=0.3)
        A, Wsim, Z = ef.simulate(rng, N, np.array(sp["pi"], float), mu, sd, corr_nu_tau=sp["corr"],
                                 groups=county if sp["group"] else None, group_sd=sp["group"])
        Vs = N - A
        u = tmpl[["state_po", "state", "county_fips", "county_name", "precinct"]].copy()
        u["vap"], u["dem"], u["rep"] = N, Wsim, Vs - Wsim
        ok = (Vs > 0).to_numpy() if hasattr(Vs, "to_numpy") else Vs > 0
        u, Z = u[ok].reset_index(drop=True), Z[ok]
        log(f"    simulation [{key}]: {len(u):,} precincts, {int((Z > 0).sum())} with planted fraud")
        s, post = ef.run_units(u["vap"].to_numpy(int), (u["vap"] - u["dem"] - u["rep"]).to_numpy(int),
                               u["dem"].to_numpy(int), seed=args.seed + 7, warmup=args.warmup, samples=args.samples,
                               chains=args.chains, log=log, label=f"sim {key}")
        with tempfile.TemporaryDirectory() as tmp:
            remain, screened = _our_pipeline_on(u, tmp, lambda *_: None)
        truth = Z > 0
        f05, f09 = post["pfraud"] > 0.5, post["pfraud"] > 0.9
        res = {"key": key, "description": desc, "units": int(len(u)), "planted": int(truth.sum()),
               "eforensics": {"flag_05": int(f05.sum()), "flag_09": int(f09.sum()),
                              "false_05": int((f05 & ~truth).sum()), "false_09": int((f09 & ~truth).sum()),
                              "caught_05": int((f05 & truth).sum()), "caught_09": int((f09 & truth).sum()),
                              "pi_mean": s["pi_mean"], "pi_by_chain": s["pi_by_chain"], "converged": s["converged"],
                              "rhat_pi": s["rhat_pi"], "flag_05_by_chain": s["flag_05_by_chain"]},
               "ours": {"screened": int(screened.sum()), "remain": int(remain.sum()),
                        "false_remain": int((remain & ~truth).sum()), "caught_remain": int((remain & truth).sum()),
                        "caught_screened": int((screened & truth).sum())}}
        log(f"      -> eforensics flags {res['eforensics']['flag_05']} (false {res['eforensics']['false_05']}), "
            f"ours remain {res['ours']['remain']} (false {res['ours']['false_remain']})")
        out["scenarios"].append(res)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parts", nargs="+", default=["census", "real", "sim"], choices=["census", "real", "sim"])
    ap.add_argument("--units", default=str(ROOT / "raw" / "alarm-units-2020-vtd.csv"))
    ap.add_argument("--alarm-dir", default=str(ROOT / "raw" / "census-vest-2020"))
    ap.add_argument("--medsl", default=str(ROOT / "raw" / "2020-SENATE-precinct-general.csv"))
    ap.add_argument("--ours-census", default=str(ROOT / "docs" / "data" / "senate-2020-vtd"))
    ap.add_argument("--ours-real", default=str(ROOT / "docs" / "data" / "senate-2020"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "data" / "eforensics-senate-2020"))
    ap.add_argument("--only-states", default=None)
    ap.add_argument("--sim-state", default="GA")
    ap.add_argument("--warmup", type=int, default=400)
    ap.add_argument("--samples", type=int, default=250, help="draws kept per chain")
    ap.add_argument("--chains", type=int, default=4)
    ap.add_argument("--seed", type=int, default=2020)
    ap.add_argument("--resume", action="store_true", help="reuse cached per-state results")
    args = ap.parse_args(argv)
    args.only_states = {s.strip().upper() for s in args.only_states.split(",")} if args.only_states else None
    t0 = time.time()

    def log(m):
        print(f"[{time.time() - t0:7.1f}s] {m}", flush=True)

    out_dir = Path(args.out)
    path = out_dir / "comparison.json"
    result = json.loads(path.read_text()) if path.exists() else {}
    result.update(generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  settings={"warmup": args.warmup, "samples_per_chain": args.samples, "chains": args.chains,
                            "seed": args.seed, "k": ef.K, "flag_rules": [0.5, 0.9]})

    units = pd.read_csv(args.units, dtype={"county_fips": str, "geoid": str})
    units = units.rename(columns={"sen_dem": "dem", "sen_rep": "rep"})
    rep_path = Path(args.units).with_name("alarm-conversion-report.csv")
    if rep_path.exists():
        result["conversion_report"] = pd.read_csv(rep_path).replace({np.nan: None}).to_dict("records")

    if "census" in args.parts:
        log("eforensics on census voting districts")
        result["census"] = part_units("census", units, args.ours_census, out_dir, args, log, unit_files=True)
    if "real" in args.parts:
        log("eforensics on real MEDSL counts (matched states)")
        import clean
        import vap_match
        df, st_ = clean.read_raw(args.medsl)
        df, ct = clean.classify(df, st_)
        p, _ = clean.build_precinct_table(df, ct, st_)
        p = p[(p["status"] == "two_party") & ~p["nongeo"] & (p["two_party"] > 0)]
        p = p[~p["special"] | (p["state_po"] == "AZ")]
        mm, rep = vap_match.match_all(p, args.alarm_dir, log=log)
        mm["state"] = mm["state_po"]
        res = part_units("real", mm, args.ours_real, out_dir, args, log, unit_files=False)
        res["match_report"] = rep.to_dict("records")
        result["real"] = res
    if "sim" in args.parts:
        log(f"head-to-head simulations on {args.sim_state}'s precinct structure")
        result["sim"] = part_sim(units, args, log)
    _dump(result, path)
    log(f"done: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
