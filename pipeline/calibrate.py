"""Step 3: how trustworthy are the flags?

Two experiments, both run on the same peer-group structure as the real data:

1. NULL SIMULATION  - Re-draw every assessed precinct's vote counts from the fitted
   model (peer centre + between-precinct spread + binomial noise), run the detector
   again, and count how many precincts it flags. By construction nothing is wrong
   in these simulated elections, so every flag is a false positive.

2. INJECTION        - In the simulated elections, secretly move X points of
   two-party share in one precinct per contest and see how often the detector
   catches it, by precinct size. This is the detector's sensitivity ("power").

Limits (also shown on the site): the null model is the detector's own model, so
this checks calibration of the procedure, not whether real elections are
better or worse behaved than the model. The per-state "model fit" gauge compares
real flag counts with the null expectation to show where reality has fatter
tails than the model.
"""
import numpy as np
from scipy import stats

import config
import fdr
import hierarchical as hb
from metrics import is_flagged, score_metric


def _simulate_counts(rng, den, center, tau2):
    c = np.clip(center, 0.005, 0.995)
    var = np.clip(tau2, 1e-8, 0.9 * c * (1 - c))
    nu = c * (1 - c) / var - 1.0
    a, b = c * nu, (1 - c) * nu
    p = rng.beta(a, b)
    return rng.binomial(den.astype("int64"), p)


def _subset(fit, state_idx):
    keep = np.flatnonzero(~np.isnan(fit["z"]))
    return {
        "den": fit["den"][keep],
        "center": fit["center"][keep],
        "tau2": fit["tau2"][keep],
        "gid_c": fit["gid_c"][keep],
        "gid_d": fit["gid_d"][keep],
        "state": state_idx[keep],
    }


def run_calibration(fits, states, rng, log=print):
    """fits: dict metric -> fit dict from metrics.analyze; states: array of state codes per precinct row."""
    state_names, state_idx = np.unique(np.asarray(states), return_inverse=True)
    thresholds = tuple(sorted(set(config.Z_THRESHOLDS_REPORTED) | {config.Z_FLAG}))
    result = {
        "threshold": config.Z_FLAG,
        "thresholds": list(thresholds),
        "null": {},
        "expected_by_state": {},
        "power": None,
        "notes": [],
    }

    for metric, fit in fits.items():
        s = _subset(fit, state_idx)
        n = len(s["den"])
        if n < config.MIN_PEERS:
            result["null"][metric] = {"assessed": int(n), "reps": 0, "fpr": None}
            continue
        reps = int(np.clip(np.ceil(config.CALIB_TARGET_DRAWS / n), config.CALIB_MIN_REPS, config.CALIB_MAX_REPS))
        flagged = np.zeros(len(thresholds))
        by_state = np.zeros(len(state_names))
        ones = np.ones(n, dtype=bool)
        for _ in range(reps):
            k = _simulate_counts(rng, s["den"], s["center"], s["tau2"])
            sim = score_metric(k, s["den"], ones, s["gid_c"], s["gid_d"], tau_min=config.TAU_MIN[metric])
            for i, t in enumerate(thresholds):
                flagged[i] += is_flagged(sim["z"], sim["support"], t).sum()
            hit = is_flagged(sim["z"], sim["support"], config.Z_FLAG)
            by_state += np.bincount(s["state"][hit], minlength=len(state_names))
        draws = n * reps
        fpr = flagged / draws
        result["null"][metric] = {
            "assessed": int(n),
            "reps": reps,
            "fpr": {f"{t:g}": float(f) for t, f in zip(thresholds, fpr)},
            "expected_flags_per_10k": float(fpr[list(thresholds).index(config.Z_FLAG)] * 1e4)
            if config.Z_FLAG in thresholds
            else None,
        }
        result["expected_by_state"][metric] = {
            str(st): float(v / reps) for st, v in zip(state_names, by_state)
        }
        log(f"    null simulation [{metric}]: {n:,} assessed precincts x {reps} reps")

    # ---- injection (share metric only: it is the one with a direct "votes moved" meaning)
    fit = fits.get("share")
    s = _subset(fit, state_idx) if fit is not None else None
    if s is not None and len(s["den"]) >= config.MIN_PEERS:
        n = len(s["den"])
        reps = int(np.clip(np.ceil(config.CALIB_TARGET_DRAWS / n), config.CALIB_MIN_REPS, config.CALIB_MAX_REPS))
        reps = max(reps, 10)
        buckets = config.SIZE_BUCKETS
        hits = np.zeros((len(config.SHIFTS), len(buckets)))
        tot = np.zeros((len(config.SHIFTS), len(buckets)))
        ones = np.ones(n, dtype=bool)
        bucket_of = np.full(n, -1)
        for bi, (lo, hi, _) in enumerate(buckets):
            bucket_of[(s["den"] >= lo) & (s["den"] < hi)] = bi
        for si, shift in enumerate(config.SHIFTS):
            for _ in range(reps):
                k = _simulate_counts(rng, s["den"], s["center"], s["tau2"])
                # one random precinct per contest gets the injected shift
                r = rng.random(n)
                order = np.lexsort((r, s["gid_d"]))
                first = np.r_[True, s["gid_d"][order][1:] != s["gid_d"][order][:-1]]
                pick = order[first]
                sign = rng.choice([-1, 1], size=len(pick))
                moved = np.rint(shift * s["den"][pick]).astype("int64") * sign
                k2 = k.copy()
                k2[pick] = np.clip(k2[pick] + moved, 0, s["den"][pick])
                sim = score_metric(k2, s["den"], ones, s["gid_c"], s["gid_d"], tau_min=config.TAU_MIN["share"])
                det = is_flagged(sim["z"][pick], sim["support"][pick], config.Z_FLAG)
                b = bucket_of[pick]
                for bi in range(len(buckets)):
                    sel = b == bi
                    tot[si, bi] += sel.sum()
                    hits[si, bi] += det[sel].sum()
        with np.errstate(invalid="ignore", divide="ignore"):
            rate = np.where(tot > 0, hits / tot, np.nan)
        result["power"] = {
            "metric": "share",
            "shifts": list(config.SHIFTS),
            "buckets": [b[2] for b in buckets],
            "detect_rate": [[None if np.isnan(x) else float(x) for x in row] for row in rate],
            "trials": [[int(x) for x in row] for row in tot],
        }
        log(f"    injection test: {len(config.SHIFTS)} shifts x {reps} reps")
    else:
        result["notes"].append("Too few assessed precincts to run the injection test.")
    return result


# --------------------------------------------------------------------------- #
# Full-chain calibration (hierarchical model + FDR + verdicts)
# --------------------------------------------------------------------------- #
def run_chain_calibration(p, fits, chain, cross, rng, z_flag, reps=None, log=print):
    """Simulate clean elections from the HIERARCHICAL model and push them through every check.

    Reports, per signal:
      * hierarchical p-value calibration (share of clean precincts with p below 0.01, 0.001, 1e-4),
        overall and for small counties (<= 5 precincts) that the old 50-peer rule skipped
      * how many precincts a clean election puts into the funnel, and how many survive to the end
      * detection power for injected vote-share shifts at each stage of the funnel
    """
    import checks
    from metrics import score_metric

    reps = config.CHAIN_REPS if reps is None else reps
    out = {"reps": reps, "signals": {}, "power": None,
           "note": "Clean elections are simulated from the fitted hierarchical model, so this "
                   "checks that each step is calibrated; it cannot show that real precincts follow the model."}
    gid_c = fits["share"]["gid_c"]
    gid_d = fits["share"]["gid_d"]

    for m in ("share", "under", "mode"):
        r = chain[m]
        f = r["fit"]
        rows = np.flatnonzero(f["level"] > 0)
        if len(rows) < config.HB_MIN_DISTRICT:
            out["signals"][m] = {"assessed": int(len(rows)), "reps": 0}
            continue
        den = r["den"].astype(float)
        mask = np.zeros(len(den), bool)
        mask[rows] = True
        small = mask & (f["m_c"] <= 5)
        thr = (0.01, 0.001, 1e-4)
        cnt = np.zeros(len(thr))
        cnt_small = np.zeros(len(thr))
        scr = 0
        per = 0
        for _ in range(reps):
            k = np.zeros(len(den))
            k[rows] = hb.simulate(rng, den[rows], f["mu"][rows], f["v"][rows])
            peer = score_metric(k, den, mask & (den >= config.MIN_DEN), gid_c, gid_d, tau_min=config.TAU_MIN[m])
            res = checks.score_metric_chain(k, den, mask, gid_c, gid_d, config.TAU_MIN[m],
                                            peer["z"], peer["support"], z_flag, None, dfs=r.get("dfs"))
            pv = res["p"]
            for i, t in enumerate(thr):
                cnt[i] += np.nansum(pv[mask] < t)
                cnt_small[i] += np.nansum(pv[small] < t)
            scr += int(res["screened"].sum())
            per += int((res["v"] == "P").sum())
        n_all, n_small = mask.sum() * reps, max(small.sum() * reps, 1)
        out["signals"][m] = {
            "tail_df": {k: (None if not np.isfinite(v) else v) for k, v in r.get("dfs", {}).items() if k != "diag"},
            "assessed": int(mask.sum()),
            "small_county_precincts": int(small.sum()),
            "reps": reps,
            "p_below": {f"{t:g}": {"nominal": t, "observed": float(cnt[i] / n_all),
                                   "observed_small": float(cnt_small[i] / n_small) if small.any() else None}
                        for i, t in enumerate(thr)},
            "expected_screened": round(scr / reps, 2),
            "expected_persist": round(per / reps, 2),
        }
        log(f"    full-chain null [{m}]: {mask.sum():,} precincts x {reps} reps; "
            f"clean data screened {scr / reps:.1f}, persisted {per / reps:.2f}")

    # ---- cross-race gap under the null
    if cross is not None:
        res = cross["fit"]
        rows = np.flatnonzero(np.isfinite(res["z"]))
        if len(rows) >= config.CROSS_MIN_PEERS:
            import crossrace
            scr = per = 0
            mask = np.zeros(len(cross["gap"]), bool)
            mask[rows] = True
            for _ in range(reps):
                g = np.full(len(mask), np.nan)
                g[rows] = rng.normal(res["center"][rows], np.sqrt(res["tau2"][rows] + cross["s2"][rows]))
                sres = crossrace.score_gap(g, cross["s2"], mask, cross["gid_c"], cross["gid_d"])
                z = sres["z"]
                q = fdr.bh_qvalues(np.where(np.isfinite(z), 2 * stats.norm.sf(np.abs(z)), np.nan))
                lf, _ = fdr.empirical_null_q(z)
                screened = ((np.abs(np.nan_to_num(z)) >= z_flag) & (sres["support"] <= config.SUPPORT_MAX)) | \
                    (np.nan_to_num(q, nan=1.0) < config.FDR_Q)
                v = checks.verdicts(screened, q, lf, sres["support"])
                scr += int(screened.sum())
                per += int((v == "P").sum())
            out["signals"]["cross"] = {"assessed": int(len(rows)), "reps": reps,
                                       "expected_screened": round(scr / reps, 2),
                                       "expected_persist": round(per / reps, 2)}
            log(f"    full-chain null [cross]: screened {scr / reps:.1f}, persisted {per / reps:.2f}")

    # ---- power: inject a vote-share shift into one precinct per race, follow it through the funnel
    r = chain["share"]
    f = r["fit"]
    rows = np.flatnonzero(f["level"] > 0)
    if len(rows) >= config.HB_MIN_DISTRICT:
        den = r["den"].astype(float)
        mask = np.zeros(len(den), bool)
        mask[rows] = True
        stages = ("screened", "after_T", "after_E", "after_C")
        hits = {s: np.zeros(len(config.SHIFTS)) for s in stages}
        tot = np.zeros(len(config.SHIFTS))
        preps = max(2, reps // 2)
        for si, shift in enumerate(config.SHIFTS):
            for _ in range(preps):
                k = np.zeros(len(den))
                k[rows] = hb.simulate(rng, den[rows], f["mu"][rows], f["v"][rows])
                rr = rng.random(len(rows))
                order = rows[np.lexsort((rr, gid_d[rows]))]
                first = np.r_[True, gid_d[order][1:] != gid_d[order][:-1]]
                pick = order[first]
                pick = pick[den[pick] >= 100]
                sign = rng.choice([-1, 1], size=len(pick))
                k[pick] = np.clip(k[pick] + np.rint(shift * den[pick]) * sign, 0, den[pick])
                peer = score_metric(k, den, mask & (den >= config.MIN_DEN), gid_c, gid_d, tau_min=config.TAU_MIN["share"])
                res = checks.score_metric_chain(k, den, mask, gid_c, gid_d, config.TAU_MIN["share"],
                                                peer["z"], peer["support"], z_flag, None, dfs=r.get("dfs"))
                v = res["v"][pick]
                tot[si] += len(pick)
                hits["screened"][si] += (v != "").sum()
                hits["after_T"][si] += (~np.isin(v, ["", "T"])).sum()
                hits["after_E"][si] += (~np.isin(v, ["", "T", "E"])).sum()
                hits["after_C"][si] += (v == "P").sum()
        out["power"] = {
            "shifts": list(config.SHIFTS),
            "stages": list(stages),
            "rate": {s: [float(h / t) if t else None for h, t in zip(hits[s], tot)] for s in stages},
            "trials": [int(t) for t in tot],
            "note": "Shift applied to one precinct (100+ votes) per race, in one race only. The cross-race "
                    "check is not simulated: it only removes precincts that look the same in BOTH races, "
                    "so a single-race shift would not be removed by it.",
        }
        log(f"    full-chain power: {len(config.SHIFTS)} shifts x {preps} reps")
    return out
