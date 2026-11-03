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

import config
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
