#!/usr/bin/env python3
"""Pennsylvania 2024 study: does accounting for legitimate campaign activity change the picture?

Run after pa_convert.py, pa_features.py and (optionally) pa_eforensics.py. Writes
docs/data/pa-2024-study/study.json, which the "PA 2024 study" page reads.

What it does
------------
1. Runs the normal pipeline (same code and seed as the "PA 2024" tab) to get the review list.
2. Explains deviations. For turnout and for vote share, three regressions on the logit scale:
       county            county effects only (what "compared with its county" means)
       + history         + the precinct's 2020 presidential result and turnout, race/ethnicity of its
                           voting-age population and its 2024 party registration
       + campaign        + exposure to each side's campaign events (FEC venue rentals)
   Each precinct gets a z-score from each model: residual / sqrt(spread^2 + chance noise).
3. Two extra rule-out checks for precincts that survived the pipeline (turnout or vote share):
       H  explained by history and make-up   |z| < EXPLAIN_Z once history is in the model
       A  explained by campaign activity     |z| >= EXPLAIN_Z with history, < EXPLAIN_Z with campaign added
4. Counts "votes above expectation" for the statewide leader in the precincts that remain, the
   closest analogue in our approach to eforensics' "fraudulent votes".
5. Collects the eforensics results (Mebane's published figures and our re-implementation with and
   without the same covariates).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scipy import stats  # noqa: E402

import config  # noqa: E402
import fdr  # noqa: E402
import pa_eforensics as pe  # noqa: E402
import run_pipeline  # noqa: E402
from metrics import robust_spread  # noqa: E402

ROOT = HERE.parent
PA = ROOT / "raw" / "pa-2024"
EXPLAIN_Z = config.CROSS_EXPLAIN_Z  # same rule as the cross-race check
HISTORY = ["trump_2020_logit", "turnout_2020_logit", "white_share", "black_share", "hisp_share",
           "reg_rep_share", "reg_dem_share", "log_registered"]
CAMPAIGN = ["events_dem_log", "events_rep_log"]
LABELS = {
    "trump_2020_logit": "2020 Trump share of two-party vote (logit)",
    "turnout_2020_logit": "2020 votes per voting-age resident (logit)",
    "white_share": "White share of voting-age population",
    "black_share": "Black share of voting-age population",
    "hisp_share": "Hispanic share of voting-age population",
    "reg_rep_share": "Republican share of registered voters, 2024",
    "reg_dem_share": "Democratic share of registered voters, 2024",
    "log_registered": "Registered voters (log)",
    "events_dem_log": "Democratic campaign events nearby (log exposure)",
    "events_rep_log": "Republican campaign events nearby (log exposure)",
}
# Mebane (2025), "eforensics Analysis of the 2024 President Election in Pennsylvania", June 2 version.
MEBANE = {
    "precincts": 9157, "margin": 120266,
    "table2": {"label": "County effects for turnout and vote choice (Table 2)", "pi": [0.733, 0.265, 0.00169],
               "fraud_precincts": 1820, "incremental": 1811, "extreme": 9,
               "fraud_votes": 225440.2, "lo": 207757.1, "hi": 252978.1, "manufactured": 111917.4,
               "multimodal": True},
    "table3": {"label": "County effects also in the fraud magnitudes (Table 3)", "pi": [0.732, 0.267, 0.00192],
               "fraud_precincts": 1804, "incremental": 1796, "extreme": 8,
               "fraud_votes": 210392.4, "lo": 190749.6, "hi": 236940.0, "manufactured": 88600.0,
               "multimodal": True},
    "narrowed": 25374.8,
    "interval_note": "posterior mean [99.5% credible interval]",
}


def logit(x):
    return np.log(x / (1 - x))


def expit(x):
    return 1 / (1 + np.exp(-x))


def _design(d, cols, counties):
    """County dummies (no intercept) + standardised covariates."""
    D = np.zeros((len(d), counties.max() + 1))
    D[np.arange(len(d)), counties] = 1.0
    if not cols:
        return D, [], []
    Z = np.zeros((len(d), len(cols)))
    scale = []
    for j, c in enumerate(cols):
        x = d[c].to_numpy(float)
        m, s = x.mean(), x.std() or 1.0
        Z[:, j] = (x - m) / s
        scale.append((float(m), float(s)))
    return np.hstack([D, Z]), cols, scale


def _huber(X, y, iters=30, c=1.345):
    """Huber-robust least squares (so the anomalies themselves do not steer the fit)."""
    w = np.ones(len(y))
    beta = None
    for _ in range(iters):
        sw = np.sqrt(w)
        beta = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)[0]
        r = y - X @ beta
        s = 1.4826 * np.median(np.abs(r - np.median(r))) or 1.0
        u = np.abs(r) / (c * s)
        w_new = np.where(u <= 1, 1.0, 1 / np.maximum(u, 1e-12))
        if np.max(np.abs(w_new - w)) < 1e-6:
            w = w_new
            break
        w = w_new
    return beta, w


def _cluster_se(X, r, w, groups, k_cov):
    """County-clustered standard errors for the last k_cov coefficients (weighted least squares sandwich)."""
    Xw = X * w[:, None]
    bread = np.linalg.pinv(X.T @ Xw)
    meat = np.zeros((X.shape[1], X.shape[1]))
    for g in np.unique(groups):
        sel = groups == g
        s = Xw[sel].T @ r[sel]
        meat += np.outer(s, s)
    G = len(np.unique(groups))
    V = bread @ meat @ bread * (G / (G - 1))
    return np.sqrt(np.diag(V))[-k_cov:] if k_cov else np.array([])


def fit_models(d, num, den, counties, tau_min_logit):
    """Fit the three models for one signal on the rows of d. Returns per-model dicts with z and predictions."""
    p_obs = (num + 0.5) / (den + 1.0)
    y = logit(p_obs)
    out = {}
    for name, cols in (("county", []), ("history", HISTORY), ("campaign", HISTORY + CAMPAIGN)):
        X, cols_, scale = _design(d, cols, counties)
        beta, w = _huber(X, y)
        fitted = X @ beta
        r = y - fitted
        pf = np.clip(expit(fitted), 0.005, 0.995)
        s2 = 1.0 / (den * pf * (1 - pf))  # chance noise of a logit for a precinct of this size
        tau2 = np.zeros(len(y))
        glob = max(tau_min_logit**2, robust_spread(r) ** 2 - np.median(s2))
        for g in np.unique(counties):
            sel = counties == g
            tau2[sel] = max(tau_min_logit**2, robust_spread(r[sel]) ** 2 - np.median(s2[sel])) \
                if sel.sum() >= config.MIN_PEERS else glob
        z = r / np.sqrt(tau2 + s2)
        k = len(cols_)
        se = _cluster_se(X, r, w, counties, k)
        # the same two checks the pipeline uses, applied to this model's z-scores
        q = fdr.bh_qvalues(2 * stats.norm.sf(np.abs(z)))
        enq, _ = fdr.empirical_null_q(z)
        bh = q < config.FDR_Q
        both = bh & (np.nan_to_num(enq, nan=0.0) < config.ENULL_Q)
        ss_tot = np.sum((y - np.array([y[counties == g].mean() for g in range(counties.max() + 1)])[counties]) ** 2)
        out[name] = {
            "z": z, "pred": expit(fitted), "resid_sd": float(robust_spread(r)),
            "r2_within": float(1 - np.sum(r**2) / ss_tot) if cols_ else 0.0,
            "n_abs_z_ge_4": int((np.abs(z) >= config.Z_FLAG).sum()), "n_abs_z_ge_3": int((np.abs(z) >= EXPLAIN_Z).sum()),
            "n_bh": int(bh.sum()), "n_bh_enull": int(both.sum()),
            "n_bh_enull_high": int((both & (z > 0)).sum()), "n_bh_enull_low": int((both & (z < 0)).sum()),
            "coef": [{"name": c, "label": LABELS[c], "beta": float(b), "se": float(s_), "z": float(b / s_) if s_ > 0 else None,
                      "mean": m, "sd": sd}
                     for c, b, s_, (m, sd) in zip(cols_, beta[-k:] if k else [], se, scale)],
        }
    return y, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--president", default=str(PA / "pa-2024-president.csv"))
    ap.add_argument("--senate", default=str(PA / "pa-2024-senate.csv"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "data" / "pa-2024-study"))
    a = ap.parse_args(argv)

    quiet = lambda *_: None  # noqa: E731
    r = run_pipeline.analyse(a.president, a.senate, log=quiet)
    p, chain = r["p"], r["chain"]

    u = pe.load_units()
    u["log_registered"] = np.log(u["registered"].clip(lower=1))
    u["key"] = u["county"].str.upper() + "|" + u["precinct"]
    p["key"] = p["county_name"] + "|" + p["precinct"]
    assert p["key"].is_unique and u["key"].is_unique and set(p["key"]) == set(u["key"]), "precinct keys do not line up"
    u = u.set_index("key").loc[p["key"]].reset_index()
    has_base = (u["no_baseline"] == 0).to_numpy()
    counties_all = np.unique(u["county"], return_inverse=True)[1]

    # ------------------------------------------------------------------ models
    signals = {}
    zcols = {}
    for m, tau_floor in (("turnout", 0.15), ("share", 0.15)):
        res = chain[m]
        mask = np.asarray(res["mask"], bool) & has_base
        idx = np.flatnonzero(mask)
        d = u.iloc[idx].reset_index(drop=True)
        num, den = np.asarray(res["num"], float)[idx], np.asarray(res["den"], float)[idx]
        y, fits = fit_models(d, num, den, counties_all[idx], tau_floor)
        for name, f in fits.items():
            z = np.full(len(p), np.nan)
            pred = np.full(len(p), np.nan)
            z[idx], pred[idx] = f["z"], f["pred"]
            zcols[(m, name)] = (z, pred)
        signals[m] = {
            "assessed_by_pipeline": int(np.asarray(res["mask"], bool).sum()), "in_models": int(len(idx)),
            "models": {name: {k: v for k, v in f.items() if k not in ("z", "pred")} for name, f in fits.items()},
        }
        # campaign effect in plain units: 25th -> 75th percentile of exposure, at the average level
        base = float(np.mean(num / den))
        for c in signals[m]["models"]["campaign"]["coef"]:
            if c["name"] in CAMPAIGN:
                # a typical WITHIN-county difference in exposure (25th to 75th percentile), since the
                # model compares precincts with others in the same county
                x = d[c["name"]].to_numpy(float)
                cidx = counties_all[idx]
                x = x - np.array([x[cidx == g].mean() if (cidx == g).any() else 0.0 for g in range(cidx.max() + 1)])[cidx]
                q25, q75 = np.percentile(x, [25, 75])
                c["within_county_iqr"] = float(q75 - q25)
                dlogit = c["beta"] * (q75 - q25) / c["sd"]
                c["iqr_effect_points"] = float(100 * (expit(logit(base) + dlogit) - base))
                c["iqr_effect_se_points"] = float(100 * abs(c["se"] * (q75 - q25) / c["sd"]) * base * (1 - base))

    # ------------------------------------------------------------------ extra checks on the review list
    dem, rep = p["dem"].to_numpy(float), p["rep"].to_numpy(float)
    two = dem + rep
    ballots = p["ballots"].to_numpy(float)
    reg = p["registered"].to_numpy(float)
    t_obs = np.divide(ballots, reg, out=np.full(len(p), np.nan), where=reg > 0)
    sD_obs = np.divide(dem, two, out=np.full(len(p), np.nan), where=two > 0)
    mu_t = chain["turnout"]["fit"]["mu"]
    mu_s = chain["share"]["fit"]["mu"]
    comp = pd.to_numeric(p["comp_share"], errors="coerce").to_numpy(float)
    cross_peer = pd.to_numeric(p["cross_peer"], errors="coerce").to_numpy(float)
    cross_z = pd.to_numeric(p["cross_z"], errors="coerce").to_numpy(float)

    def extra_verdict(i, m):
        """'' not applicable, N no census match, H history, A campaign activity, P remains."""
        zh, zc = zcols[(m, "history")][0][i], zcols[(m, "campaign")][0][i]
        if not np.isfinite(zh):
            return "N"
        if abs(zh) < EXPLAIN_Z:
            return "H"
        if abs(zc) < EXPLAIN_Z:
            return "A"
        return "P"

    def excess(i, t_exp, sD_exp):
        """Leader (Republican) votes above expectation. t_exp / sD_exp = None keeps the observed value."""
        f = two[i] / ballots[i] if ballots[i] > 0 else 0.0
        t = t_obs[i] if t_exp is None else t_exp
        sL = 1 - (sD_obs[i] if sD_exp is None else sD_exp)
        base = reg[i] * t if (reg[i] > 0 and np.isfinite(t)) else ballots[i]
        return float(rep[i] - base * f * sL)

    rows = []
    stage_sets = {"pipeline": [], "history": [], "campaign": []}
    stage_votes = {k: {"above": 0.0, "below": 0.0} for k in stage_sets}
    screened_votes = {"above": 0.0, "below": 0.0, "precincts": 0}
    for i in np.flatnonzero(p["screened"].to_numpy(dtype=object) != ""):
        tv, sv, xv = p["turnout_v"].iat[i], p["share_v"].iat[i], p["cross_v"].iat[i]
        # looser count: every screened precinct, expectation from the hierarchical model / other race
        e_scr = excess(i, mu_t[i] if tv else None,
                       (comp[i] + cross_peer[i]) if (xv and not sv) else (mu_s[i] if sv else None))
        screened_votes["above" if e_scr > 0 else "below"] += e_scr
        screened_votes["precincts"] += 1
        if p["remain"].iat[i] == "":
            continue
        ev = {m: extra_verdict(i, m) if v == "P" else "" for m, v in (("turnout", tv), ("share", sv))}
        t_flag, s_flag, x_flag = tv == "P", sv == "P", xv == "P"
        sD_cross = comp[i] + cross_peer[i] if x_flag else None
        e0 = excess(i, mu_t[i] if t_flag else None, mu_s[i] if s_flag else sD_cross)
        # after the history check: signals explained by history drop out; the rest use the history model's expectation
        keepH = {m: ev[m] in ("A", "P", "N") for m in ev}
        remH = (t_flag and keepH["turnout"]) or (s_flag and keepH["share"]) or x_flag
        tH = (zcols[("turnout", "history")][1][i] if ev["turnout"] != "N" else mu_t[i]) if (t_flag and keepH["turnout"]) else None
        sH = (zcols[("share", "history")][1][i] if ev["share"] != "N" else mu_s[i]) if (s_flag and keepH["share"]) else sD_cross
        e1 = excess(i, tH, sH) if remH else 0.0
        keepC = {m: ev[m] in ("P", "N") for m in ev}
        remC = (t_flag and keepC["turnout"]) or (s_flag and keepC["share"]) or x_flag
        tC = (zcols[("turnout", "campaign")][1][i] if ev["turnout"] != "N" else mu_t[i]) if (t_flag and keepC["turnout"]) else None
        sC = (zcols[("share", "campaign")][1][i] if ev["share"] != "N" else mu_s[i]) if (s_flag and keepC["share"]) else sD_cross
        e2 = excess(i, tC, sC) if remC else 0.0
        for k, on, e in (("pipeline", True, e0), ("history", remH, e1), ("campaign", remC, e2)):
            if on:
                stage_sets[k].append(i)
                stage_votes[k]["above" if e > 0 else "below"] += e
        rows.append({
            "county": u["county"].iat[i], "precinct": p["precinct"].iat[i], "signals": p["remain"].iat[i],
            "registered": int(reg[i]) if reg[i] > 0 else None, "votes": int(ballots[i]),
            "turnout": float(t_obs[i]) if np.isfinite(t_obs[i]) else None,
            "turnout_expected": float(mu_t[i]) if np.isfinite(mu_t[i]) else None,
            "leader_share": float(1 - sD_obs[i]), "other_race_leader_share": float(1 - comp[i]) if np.isfinite(comp[i]) else None,
            "z": {m: {name: (None if not np.isfinite(zcols[(m, name)][0][i]) else round(float(zcols[(m, name)][0][i]), 2))
                      for name in ("county", "history", "campaign")} for m in ("turnout", "share")},
            "cross_z": None if not np.isfinite(cross_z[i]) else round(float(cross_z[i]), 2),
            "extra": ev, "remains_after_history": bool(remH), "remains_after_campaign": bool(remC),
            "excess_pipeline": round(e0, 1), "excess_history": round(e1, 1), "excess_campaign": round(e2, 1),
            "events_dem_25km": int(u["events_dem_25km"].iat[i]), "events_rep_25km": int(u["events_rep_25km"].iat[i]),
            "nearest_event_km": float(np.nanmin([u["nearest_event_dem_km"].iat[i], u["nearest_event_rep_km"].iat[i]])),
            "location_from": u["location_from"].iat[i],
        })
    rows.sort(key=lambda x: (-abs(x["excess_pipeline"])))
    leader_votes = int(rep.sum())
    ours = {
        "leader": "Trump", "leader_votes": leader_votes, "margin_in_file": int(rep.sum() - dem.sum()),
        "screened": int((p["screened"] != "").sum()),
        "stages": [
            {"key": k, "precincts": len(stage_sets[k]), "votes_above": round(stage_votes[k]["above"], 1),
             "votes_below": round(stage_votes[k]["below"], 1)}
            for k in ("pipeline", "history", "campaign")],
        "screened_looser": {k: (round(v, 1) if k != "precincts" else v) for k, v in screened_votes.items()},
        "explain_z": EXPLAIN_Z,
    }

    # ------------------------------------------------------------------ eforensics (cached runs)
    remain_ids = set(u["unit_id"].to_numpy()[p["remain"].to_numpy(dtype=object) != ""])
    ef_specs, pf_main = [], None
    for name, sp in pe.SPECS.items():
        f = PA / "eforensics-cache" / f"{name}.json"
        if not f.exists():
            continue
        s = json.loads(f.read_text())
        ids, pf = s["unit_id"], np.asarray(s["pfraud"])
        if name == "county":
            pf_main = dict(zip(ids, s["pfraud"]))
        ef_specs.append({
            "spec": name, "label": sp["label"], "county_effects": sp["county"], "history": sp["history"],
            "campaign": sp["campaign"], "n": s["n"], "pi_mean": s["pi_mean"], "pi_by_chain": s["pi_by_chain"],
            "flag_05": s["flag_05"], "flag_09": s["flag_09"], "flag_05_by_chain": s["flag_05_by_chain"],
            "fraud_votes": s["fraud_votes_mean"], "fraud_votes_lo": s["fraud_votes_lo"], "fraud_votes_hi": s["fraud_votes_hi"],
            "fraud_votes_by_chain": s["fraud_votes_by_chain"], "leader_votes": s["leader_votes"],
            "converged": s["converged"], "rhat_pi_max": float(np.nanmax(s["rhat_pi"])), "seconds": s["seconds"],
            "settings": s["settings"], "coef": s.get("coef"), "covariates": s.get("covariates"),
            "ours_remain_also_flagged": int(sum(1 for i_, x in zip(ids, pf) if x > 0.5 and i_ in remain_ids)),
        })

    # ------------------------------------------------------------------ chart data (one row per precinct)
    status = np.where(p["remain"].to_numpy(dtype=object) != "", 2, np.where(p["screened"].to_numpy(dtype=object) != "", 1, 0))
    usable = (reg > 0) & (ballots > 0) & (ballots <= reg)
    pts = [[u["county"].iat[i], p["precinct"].iat[i], round(float(u["lat"].iat[i]), 4), round(float(u["lon"].iat[i]), 4),
            round(float(t_obs[i]), 4), round(float(rep[i] / ballots[i]), 4), int(status[i]),
            None if pf_main is None else pf_main.get(u["unit_id"].iat[i]),
            round(float(u["events_dem"].iat[i]), 2), round(float(u["events_rep"].iat[i]), 2)]
           for i in np.flatnonzero(usable)]
    events = pd.read_csv(PA / "pa-2024-events.csv").replace({np.nan: None}).to_dict("records")

    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "build": {k: v for k, v in json.loads((PA / "pa-2024-build-report.json").read_text()).items() if k != "candidates"},
        "features": json.loads((PA / "pa-2024-features-report.json").read_text()),
        "units_usable": int(usable.sum()), "registered_usable": int(reg[usable].sum()),
        "votes_usable": int(ballots[usable].sum()), "leader_votes_usable": int(rep[usable].sum()),
        "signals": signals, "ours": ours, "review": rows,
        "mebane": MEBANE, "eforensics": ef_specs,
        "points": {"fields": ["county", "precinct", "lat", "lon", "turnout", "leader_share", "status", "pfraud",
                              "events_dem", "events_rep"], "rows": pts},
        "events": events,
        "labels": LABELS,
    }

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

    dest = Path(a.out)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "study.json").write_text(json.dumps(clean(out), separators=(",", ":"), allow_nan=False))
    print(f"review list: {len(rows)} precincts; after history check {len(stage_sets['history'])}; "
          f"after campaign check {len(stage_sets['campaign'])}")
    for s in ours["stages"]:
        print(f"  {s['key']:9s}: {s['precincts']:3d} precincts, leader votes above expectation {s['votes_above']:,.0f}, "
              f"below {s['votes_below']:,.0f}")
    for m in signals:
        print(f"  {m}: " + "; ".join(f"{k} |z|>=4: {v['n_abs_z_ge_4']}, resid sd {v['resid_sd']:.3f}, within-R2 {v['r2_within']:.3f}"
                                     for k, v in signals[m]["models"].items()))
    print(f"wrote {dest / 'study.json'} ({(dest / 'study.json').stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
