"""Step 3b: combine every method into one verdict per precinct and signal.

A precinct enters the review funnel for a signal if EITHER
    * the peer z-score flags it (|z| >= Z_FLAG and isolated), or
    * the hierarchical model finds it significant after Benjamini-Hochberg (q < FDR_Q).

It then has to survive, in order:
    T  multiple testing     hierarchical-model q-value < FDR_Q
    E  empirical null       Efron empirical-null FDR q < ENULL_Q (not ordinary real-world variation)
    C  cluster              not part of a group of similar precincts (isolation rule)
    L  other race           (vote share only, when a companion race is loaded)
                            the same precinct is NOT equally unusual in the other race
Verdict codes stored per signal:
    ""  not screened    T/E/C/L  ruled out at that check    P  persists: needs human review
"""
import numpy as np
from scipy import stats

import config
import fdr
import hierarchical as hb
from metrics import metric_inputs

SIGNALS = ("share", "under", "mode", "cross")
LETTER = {"share": "S", "under": "U", "mode": "M", "cross": "X"}
STAGES = ("screened", "after_T", "after_E", "after_C", "after_L")


def verdicts(screened, q, enq, support, cross_z=None):
    """Vectorised verdict codes."""
    n = len(screened)
    v = np.full(n, "", dtype=object)
    s = np.asarray(screened, bool)
    fail_T = ~(np.nan_to_num(q, nan=1.0) < config.FDR_Q)
    fail_E = np.isfinite(enq) & (enq >= config.ENULL_Q)
    fail_C = support > config.SUPPORT_MAX  # support is "infinite" when the peer z was not computed
    fail_C &= support < np.iinfo("int32").max
    if cross_z is not None:
        fail_L = np.isfinite(cross_z) & (np.abs(cross_z) < config.CROSS_EXPLAIN_Z)
    else:
        fail_L = np.zeros(n, bool)
    v[s] = "P"
    v[s & fail_L] = "L"
    v[s & fail_C] = "C"
    v[s & fail_E] = "E"
    v[s & fail_T] = "T"
    return v


def funnel(v, has_cross):
    v = np.asarray(v, dtype=object)
    scr = v != ""
    out = {"screened": int(scr.sum())}
    out["after_T"] = int((scr & (v != "T")).sum())
    out["after_E"] = int((scr & ~np.isin(v, ["T", "E"])).sum())
    out["after_C"] = int((scr & ~np.isin(v, ["T", "E", "C"])).sum())
    out["after_L"] = int((v == "P").sum()) if has_cross else None
    out["persist"] = int((v == "P").sum())
    return out


DF_GRID = np.r_[2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 50, 80, 120, 200, np.inf]
SMALL_COUNTY = 5


def _fit_df(absz):
    """Student-t degrees of freedom whose (unit-scale) tails best match simulated clean-data |z|.
    Only tail levels with at least 10 simulated exceedances are used. Never makes p-values
    smaller than the normal ones (df = inf when the simulation is already conservative)."""
    absz = absz[np.isfinite(absz)]
    if len(absz) < 1000:
        return np.inf, []
    levels = np.array([1e-2, 1e-3, 1e-4, 1e-5])
    zt = stats.norm.isf(levels / 2)
    obs = np.array([np.mean(absz >= t) for t in zt])
    use = obs * len(absz) >= 10
    diag = [{"nominal": float(l), "observed": float(o)} for l, o in zip(levels[use], obs[use])]
    if not use.any() or np.all(obs[use] <= levels[use]):
        return np.inf, diag
    best, best_err = np.inf, np.inf
    for df in DF_GRID:
        pred = 2 * stats.t.sf(zt[use], df) if np.isfinite(df) else levels[use]
        err = np.sum((np.log(obs[use]) - np.log(pred)) ** 2)
        if err < best_err:
            best, best_err = df, err
    return float(best), diag


def tail_calibration(f, num, den, gid_c, gid_d, tau_min, rng, reps=None):
    """Parametric-bootstrap calibration of the hierarchical p-values.

    Clean elections are simulated from the fitted model and re-fitted; the tails of the resulting
    clean-data z-scores show how much heavier than a bell curve they really are (mainly because
    spreads estimated from few precincts are noisy). A Student-t curve with matching tails is then
    used to turn z into p, separately for small counties (<= 5 precincts) and the rest.
    """
    reps = config.TAIL_CAL_REPS if reps is None else reps
    rows = np.flatnonzero(f["level"] > 0)
    out = {"small": np.inf, "large": np.inf, "diag": {}}
    if len(rows) < config.HB_MIN_DISTRICT:
        return out
    den = np.asarray(den, float)
    mask = np.zeros(len(den), bool)
    mask[rows] = True
    zs_small, zs_large = [], []
    for _ in range(reps):
        k = np.zeros(len(den))
        k[rows] = hb.simulate(rng, den[rows], f["mu"][rows], f["v"][rows])
        f2 = hb.fit(k, den, mask, gid_c, gid_d, tau_min)
        _, z2 = hb.predictive_p(k, den, f2["mu"], f2["v"])
        sm = f2["m_c"] <= SMALL_COUNTY
        zs_small.append(np.abs(z2[mask & sm]))
        zs_large.append(np.abs(z2[mask & ~sm]))
    for name, arr in (("small", zs_small), ("large", zs_large)):
        df, diag = _fit_df(np.concatenate(arr))
        out[name] = df
        out["diag"][name] = diag
    # small counties are never treated as better-behaved than large ones
    out["small"] = min(out["small"], out["large"])
    return out


def calibrated_p(z, m_c, dfs):
    p = np.full(len(z), np.nan)
    ok = np.isfinite(z)
    if not ok.any():
        return p
    df = np.where(m_c <= SMALL_COUNTY, dfs["small"], dfs["large"])
    az = np.abs(z)
    pn = 2 * stats.norm.sf(az)
    pt = 2 * stats.t.sf(az, np.where(np.isfinite(df), df, 1e9))
    p[ok] = np.where(np.isfinite(df[ok]), pt[ok], pn[ok])
    return np.clip(p, 1e-300, 1.0)


def score_metric_chain(num, den, mask, gid_c, gid_d, tau_min, peer_z, peer_support, z_flag, cross_z=None,
                       dfs=None):
    """Hierarchical p -> BH q -> empirical-null q -> verdicts, for one signal. Used for real data and simulations.
    dfs: tail calibration from tail_calibration(); None = uncalibrated (normal) p-values."""
    f = hb.fit(num, den, mask, gid_c, gid_d, tau_min)
    p, z = hb.predictive_p(num, den, f["mu"], f["v"])
    if dfs is not None:
        p = calibrated_p(z, f["m_c"], dfs)
    q = fdr.bh_qvalues(p)
    lf, info = fdr.empirical_null_q(z)
    peer_flag = (np.abs(np.nan_to_num(peer_z, nan=0.0)) >= z_flag) & (peer_support <= config.SUPPORT_MAX)
    screened = peer_flag | (np.nan_to_num(q, nan=1.0) < config.FDR_Q)
    v = verdicts(screened, q, lf, peer_support, cross_z)
    return {"fit": f, "p": p, "z": z, "q": q, "enq": lf, "enull_info": info, "v": v, "screened": screened}


def run(p, fits, z_flag, cross=None, rng=None, log=print):
    """Add hierarchical, FDR and verdict columns to the precinct table.

    p     : precinct table after metrics.analyze (sorted, same row order as fits)
    fits  : peer z fits from metrics.analyze
    cross : dict from crossrace.attach (or None)
    """
    inputs = metric_inputs(p)
    gid_c = fits["share"]["gid_c"]
    gid_d = fits["share"]["gid_d"]
    has_cross = cross is not None and "cross_z" in p
    results = {}
    for m in ("share", "under", "mode"):
        d = inputs[m]
        cz = p["cross_z"].to_numpy() if (has_cross and m == "share") else None
        rng = np.random.default_rng(config.SEED) if rng is None else rng
        f0 = hb.fit(d["num"], d["den"], d["mask"], gid_c, gid_d, config.TAU_MIN[m])
        dfs = tail_calibration(f0, d["num"], d["den"], gid_c, gid_d, config.TAU_MIN[m], rng)
        r = score_metric_chain(d["num"], d["den"], d["mask"], gid_c, gid_d, config.TAU_MIN[m],
                               fits[m]["z"], fits[m]["support"], z_flag, cz, dfs=dfs)
        r["dfs"] = dfs
        p[f"{m}_hbp"] = r["p"]
        p[f"{m}_hbz"] = r["z"]
        p[f"{m}_q"] = r["q"]
        p[f"{m}_enq"] = r["enq"]
        p[f"{m}_v"] = r["v"]
        p[f"{m}_hblevel"] = r["fit"]["level"]
        r["mask"], r["num"], r["den"] = d["mask"], d["num"], d["den"]
        results[m] = r
        fn = funnel(r["v"], has_cross and m == "share")
        log(f"    {m:6s}: hierarchical model assessed {int(np.isfinite(r['p']).sum()):,}; "
            f"screened {fn['screened']:,} -> persist {fn['persist']:,}  "
            f"[tail df: small {dfs['small']:g}, large {dfs['large']:g}]")

    if has_cross:
        cz = p["cross_z"].to_numpy()
        q = fdr.bh_qvalues(p["cross_p"].to_numpy())
        lf, info = fdr.empirical_null_q(cz)
        sup = p["cross_support"].to_numpy()
        screened = ((np.abs(np.nan_to_num(cz, nan=0.0)) >= z_flag) & (sup <= config.SUPPORT_MAX)) | \
            (np.nan_to_num(q, nan=1.0) < config.FDR_Q)
        v = verdicts(screened, q, lf, sup, None)
        p["cross_q"] = q
        p["cross_enq"] = lf
        p["cross_v"] = v
        results["cross"] = {"q": q, "enq": lf, "enull_info": info, "v": v, "screened": screened}
        fn = funnel(v, False)
        log(f"    cross : screened {fn['screened']:,} -> persist {fn['persist']:,}  [{info.get('method')}]")
    else:
        for col in ("cross_q", "cross_enq"):
            p[col] = np.nan
        p["cross_v"] = ""
        for col in ("comp_share", "cross_gap", "cross_peer", "cross_z"):
            if col not in p:
                p[col] = np.nan
        if "cross_status" not in p:
            p["cross_status"] = "n/a"

    # combined letters: screened signals and signals that persist
    scr = np.full(len(p), "", dtype=object)
    rem = np.full(len(p), "", dtype=object)
    for m in SIGNALS:
        v = p[f"{m}_v"].to_numpy(dtype=object)
        scr = np.where(v != "", scr + LETTER[m], scr)
        rem = np.where(v == "P", rem + LETTER[m], rem)
    p["screened"] = scr
    p["remain"] = rem
    return p, results
