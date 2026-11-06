"""Hierarchical (partially pooled) beta-binomial model with posterior predictive checks.

Why this exists
---------------
The peer z-score (metrics.py) needs >= 50 comparable precincts, because spread
estimates from small groups are unreliable. It is also symmetric, which is a bad
fit near 0% or 100% (a 5%-Democratic county has a long right tail, not a
bell curve). This model fixes both:

  * Small counties borrow strength from their district (and districts from the
    whole dataset) instead of being dropped. Only districts with fewer than
    HB_MIN_DISTRICT assessable precincts are left out.
  * The predictive distribution is a (skewed) beta-binomial, not a normal curve.

Model, for one metric (e.g. Democratic share) in precinct i of county c, district d:

    k_i ~ Binomial(n_i, theta_i)
    theta_i ~ Beta with mean mu_c and variance tau2_c            (precinct-to-precinct variation)
    mu_c    = w_c * (county centre) + (1 - w_c) * (district centre)  (partial pooling of the centre)
    log tau2_c shrunk toward log tau2_d, and log tau2_d toward the dataset median

The pooling weights are empirical-Bayes estimates:
    w_c = s2_between / (s2_between + tau2_d / m_c)
where s2_between is the (robust) variance of county centres within the district
and m_c the number of precincts in the county. Spread estimates are pooled on the
log scale with prior strengths HB_TAU_PRIOR_N (in "precincts").

Posterior predictive check: given the fitted model, how surprising is the observed
k_i? Two-sided p-value p_i = 2 * min(P(K <= k_i), P(K >= k_i)), computed from a
moment-matched Beta approximation of the beta-binomial (fast, keeps the skew).
The fitted parameters are plug-in estimates, so uncertainty in the centre is added
to the predictive variance; calibration is checked by simulation (calibrate.py).
"""
import numpy as np
from scipy import stats

import config
from metrics import robust_spread, wmedian


def _groups(gid, rows):
    """Yield (group id, row indices) for the given subset of rows."""
    if len(rows) == 0:
        return
    order = rows[np.argsort(gid[rows], kind="stable")]
    g = gid[order]
    cuts = np.flatnonzero(np.diff(g)) + 1
    for s, e in zip(np.r_[0, cuts], np.r_[cuts, len(order)]):
        yield g[s], order[s:e]


def _within_spread2(pp, nn, c, tau_min):
    """Robust between-precinct variance (sampling noise removed), floored."""
    cc = min(max(c, 0.005), 0.995)
    s2 = np.median(cc * (1 - cc) / nn)
    return max(tau_min**2, robust_spread(pp) ** 2 - s2)


def fit(num, den, mask, gid_c, gid_d, tau_min):
    """Fit the hierarchy. Returns dict of per-row arrays: mu, v_between (tau2 + centre uncertainty), level, m_c."""
    num = np.asarray(num, float)
    den = np.asarray(den, float)
    n_rows = len(num)
    ok = np.asarray(mask, bool) & (den > 0)
    p = np.divide(num, den, out=np.full(n_rows, np.nan), where=den > 0)
    out = {
        "mu": np.full(n_rows, np.nan),
        "v": np.full(n_rows, np.nan),
        "level": np.zeros(n_rows, "int8"),  # 0 not assessed, 1 county pooled with district
        "m_c": np.zeros(n_rows, "int32"),
    }
    rows = np.flatnonzero(ok)
    if len(rows) == 0:
        return out

    # ---- county level (county = county x local election authority)
    county = {}
    for g, idx in _groups(gid_c, rows):
        m = len(idx)
        own = m >= config.HB_MIN_COUNTY_OWN
        cc = wmedian(p[idx], den[idx]) if own else np.nan
        t2 = _within_spread2(p[idx], den[idx], cc, tau_min) if own else np.nan
        county[g] = {"idx": idx, "d": gid_d[idx[0]], "m": m, "c": cc, "t2": t2}

    # ---- district level.
    #   centre        : vote-weighted median of all its precincts
    #   t2_total      : spread of ALL its precincts (includes differences BETWEEN counties)
    #   t2 (within)   : typical spread INSIDE its counties = precinct-weighted median of county
    #                   spreads. This, not t2_total, is what a county's spread is pooled toward:
    #                   pooling toward t2_total would make every small county look as diverse
    #                   as the whole state and hide real outliers.
    dist = {}
    for g, idx in _groups(gid_d, rows):
        if len(idx) < config.HB_MIN_DISTRICT:
            continue
        c = wmedian(p[idx], den[idx])
        dist[g] = {"c": c, "t2_total": _within_spread2(p[idx], den[idx], c, tau_min), "m": len(idx), "counties": []}
    if not dist:
        return out
    for g, x in county.items():
        if x["d"] in dist:
            dist[x["d"]]["counties"].append(g)
    for d in dist.values():
        own = [county[g] for g in d["counties"] if not np.isnan(county[g]["t2"])]
        if len(own) >= 2:
            d["t2"] = float(np.exp(wmedian(np.log([x["t2"] for x in own]), np.array([x["m"] for x in own], float))))
            d["m_within"] = sum(x["m"] for x in own)
        else:  # no county big enough to measure within-county spread: use the (wider) total spread
            d["t2"] = d["t2_total"]
            d["m_within"] = 0
    # dataset-level prior for district within-spreads
    log_t2_all = np.median([np.log(d["t2"]) for d in dist.values()])
    for d in dist.values():
        lam = d["m_within"] / (d["m_within"] + config.HB_TAU_PRIOR_N) if d["m_within"] else 0.0
        d["t2"] = max(tau_min**2, float(np.exp(lam * np.log(d["t2"]) + (1 - lam) * log_t2_all)))
        if not d["m_within"]:
            d["t2"] = max(d["t2"], d["t2_total"])

    # ---- between-county variance of centres, per district (robust)
    for dg, d in dist.items():
        cs = [county[g] for g in d["counties"] if not np.isnan(county[g]["c"])]
        if len(cs) >= 3:
            centres = np.array([x["c"] for x in cs])
            raw = robust_spread(centres) ** 2
            noise = np.median([d["t2"] / x["m"] for x in cs])
            d["s2b"] = max(raw - noise, (0.25 * tau_min) ** 2)
        else:
            d["s2b"] = np.nan
        # variance of an UNKNOWN county's centre around the district centre
        d["v_unknown"] = d["s2b"] if not np.isnan(d["s2b"]) else max(d["t2_total"] - d["t2"], 0.0)

    # ---- pool each county toward its district
    for g, x in county.items():
        if x["d"] not in dist:
            continue
        d = dist[x["d"]]
        m = x["m"]
        if np.isnan(x["c"]):
            # too few precincts for its own centre: use the district centre, with the full
            # between-county uncertainty added
            mu = d["c"]
            t2 = d["t2"]
            v_mu = d["v_unknown"]
        else:
            if np.isnan(d["s2b"]):
                w = m / (m + config.HB_CENTER_PRIOR_N)
                lam = m / (m + config.HB_TAU_PRIOR_N)
                t2 = np.exp(lam * np.log(x["t2"]) + (1 - lam) * np.log(d["t2"]))
                v_mu = (1 - w) ** 2 * d["v_unknown"] + w**2 * t2 / m
            else:
                lam = m / (m + config.HB_TAU_PRIOR_N)
                t2 = np.exp(lam * np.log(x["t2"]) + (1 - lam) * np.log(d["t2"]))
                w = d["s2b"] / (d["s2b"] + t2 / m)
                v_mu = 1.0 / (1.0 / d["s2b"] + m / t2)
            mu = w * x["c"] + (1 - w) * d["c"]
        t2 = max(t2, tau_min**2)
        idx = x["idx"]
        out["mu"][idx] = mu
        out["v"][idx] = t2 + v_mu
        out["level"][idx] = 1
        out["m_c"][idx] = m
    return out


def predictive_p(num, den, mu, v):
    """Two-sided posterior predictive p-value and signed z-equivalent.

    k/n is approximated by a Beta distribution with the beta-binomial's mean and variance:
        Var(k/n) = mu(1-mu)/n + v (n-1)/n
    """
    num = np.asarray(num, float)
    den = np.asarray(den, float)
    ok = ~np.isnan(mu) & (den > 0)
    p = np.full(len(num), np.nan)
    z = np.full(len(num), np.nan)
    if not ok.any():
        return p, z
    k, n = num[ok], den[ok]
    m = np.clip(mu[ok], 0.002, 0.998)
    var = m * (1 - m) / n + v[ok] * (n - 1) / n
    var = np.minimum(var, 0.95 * m * (1 - m))
    nu = m * (1 - m) / var - 1.0
    a, b = m * nu, (1 - m) * nu
    lo = stats.beta.cdf(np.clip((k + 0.5) / n, 0, 1), a, b)  # P(K <= k)
    hi = stats.beta.sf(np.clip((k - 0.5) / n, 0, 1), a, b)  # P(K >= k)
    pv = np.clip(2 * np.minimum(lo, hi), 1e-300, 1.0)
    p[ok] = pv
    z[ok] = np.sign(k / n - m) * stats.norm.isf(pv / 2)
    return p, z


def simulate(rng, den, mu, v):
    """Draw counts from the fitted predictive distribution (used for calibration)."""
    m = np.clip(mu, 0.002, 0.998)
    var = np.minimum(np.maximum(v, 1e-8), 0.9 * m * (1 - m))
    nu = m * (1 - m) / var - 1
    theta = rng.beta(m * nu, (1 - m) * nu)
    return rng.binomial(den.astype("int64"), theta)
