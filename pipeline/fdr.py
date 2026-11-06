"""Multiple-testing control.

Two complementary tools, both applied to ALL precincts tested for a metric at once:

1. Benjamini-Hochberg (BH) q-values from the hierarchical model's p-values.
   Controls the expected share of false discoveries among the precincts declared
   significant. With ~100,000 tests, p = 0.0001 is NOT remarkable: about 10
   precincts would reach it by chance alone. BH accounts for that.

2. Efron's EMPIRICAL null false discovery rate.
   BH assumes the p-values are exactly right under "nothing unusual". Real
   precincts are more varied than any model, so their z-scores spread wider than
   N(0,1) even when nothing is wrong. Efron's method estimates the null curve from
   the bulk of the data itself (central matching) and re-tests every precinct
   against that wider curve. A precinct that is extreme on a textbook bell curve
   but ordinary on the empirical one is "consistent with real-world variation".

   Steps (Efron 2004, 2010; Lindsey's method):
     * histogram of z-scores, Poisson regression of counts on a natural cubic
       spline (7 degrees of freedom) -> smooth density f(z)
     * central matching: fit a quadratic to log f(z) over the central half of the
       data -> null mean mu0, null sd sigma0 (never below 1), null proportion pi0
     * tail-area FDR: q_i = pi0 * BH(p-values under N(mu0, sigma0))
   The density-based LOCAL fdr is also implemented (local_fdr) but not used for
   verdicts; see its docstring for why.
"""
import numpy as np
from scipy import stats

import config


def bh_qvalues(p):
    """Benjamini-Hochberg adjusted p-values (q-values); NaN-aware."""
    p = np.asarray(p, float)
    q = np.full(len(p), np.nan)
    ok = ~np.isnan(p)
    m = int(ok.sum())
    if m == 0:
        return q
    pv = p[ok]
    order = np.argsort(pv)
    ranked = pv[order] * m / np.arange(1, m + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(ranked, 1.0)
    q[ok] = out
    return q


def _ns_basis(x, knots):
    """Natural cubic spline basis (Hastie, Tibshirani & Friedman, eq. 5.4-5.5), without intercept.
    knots must include the two boundary knots."""
    k = np.asarray(knots, float)
    K = len(k)

    def d(j):
        return (np.maximum(x - k[j], 0) ** 3 - np.maximum(x - k[-1], 0) ** 3) / (k[-1] - k[j])

    cols = [x]
    dK1 = d(K - 2)
    for j in range(K - 2):
        cols.append(d(j) - dK1)
    return np.column_stack(cols)


def _poisson_glm(X, y, iters=60):
    X1 = np.column_stack([np.ones(len(y)), X])
    # scale columns for stability
    sc = np.maximum(np.abs(X1).max(axis=0), 1e-12)
    Xs = X1 / sc
    beta = np.zeros(Xs.shape[1])
    beta[0] = np.log(max(y.mean(), 1e-6))
    for _ in range(iters):
        eta = np.clip(Xs @ beta, -30, 30)
        mu = np.exp(eta)
        W = mu
        z = eta + (y - mu) / np.maximum(mu, 1e-12)
        A = Xs.T @ (W[:, None] * Xs) + 1e-8 * np.eye(Xs.shape[1])
        new = np.linalg.solve(A, Xs.T @ (W * z))
        if np.max(np.abs(new - beta)) < 1e-9:
            beta = new
            break
        beta = new
    return np.exp(np.clip(Xs @ beta, -30, 30))


def empirical_null(z, min_n=None):
    """Efron's empirical null by central matching. Returns info dict with mu0, sigma0, pi0
    (and the fitted density on a grid for local fdr), or method='not run' when too few tests."""
    min_n = config.LFDR_MIN_N if min_n is None else min_n
    zs = np.asarray(z, float)
    zs = zs[np.isfinite(zs)]
    info = {"n": int(len(zs)), "method": None}
    if len(zs) < min_n:
        info["method"] = "not run (fewer than %d tests)" % min_n
        return info
    L = config.LFDR_RANGE
    width = config.LFDR_BIN
    edges = np.arange(-L, L + width / 2, width)
    centers = 0.5 * (edges[1:] + edges[:-1])
    y, _ = np.histogram(np.clip(zs, -L + 1e-9, L - 1e-9), bins=edges)
    inner = np.quantile(np.clip(zs, centers[0], centers[-1]), np.linspace(0, 1, config.LFDR_DF)[1:-1])
    X = _ns_basis(centers, np.r_[centers[0], inner, centers[-1]])
    f = _poisson_glm(X, y.astype(float)) / (len(zs) * width)
    q25, q75 = np.quantile(zs, [0.25, 0.75])
    sel = (centers >= q25) & (centers <= q75) & (f > 0)
    if sel.sum() < 3:
        sel = (np.abs(centers - np.median(zs)) <= 1.0) & (f > 0)
    c2, c1, c0 = np.polyfit(centers[sel], np.log(f[sel]), 2)
    if c2 >= 0:
        mu0, sigma0, pi0 = float(np.median(zs)), float((q75 - q25) / 1.349), 1.0
    else:
        sigma0 = float(np.sqrt(-1.0 / (2 * c2)))
        mu0 = float(c1 * sigma0**2)
        pi0 = float(min(1.0, np.exp(c0 + np.log(np.sqrt(2 * np.pi) * sigma0) + mu0**2 / (2 * sigma0**2))))
    info.update(method="empirical null (Efron central matching)", mu0=mu0,
                sigma0_fitted=sigma0, sigma0=max(sigma0, 1.0), pi0=pi0,
                _centers=centers, _f=f)
    return info


def empirical_null_q(z, min_n=None):
    """Tail-area false discovery rate under Efron's EMPIRICAL null (Efron 2010, ch. 6).

    Each z is re-scored against N(mu0, sigma0) estimated from the bulk of the data, and the
    Benjamini-Hochberg procedure is applied with the estimated null proportion pi0:
        q_i = BH( 2 * P0(|Z - mu0| >= |z_i - mu0|) ) * pi0
    Unlike the local (density-based) fdr, this does not depend on estimating the density in
    the sparse far tails, so a handful of genuinely extreme precincts are not washed out.
    Returns (q array with NaN where z is NaN or the method was not run, info dict).
    """
    z = np.asarray(z, float)
    q = np.full(len(z), np.nan)
    info = empirical_null(z, min_n)
    if info.get("method", "").startswith("not run"):
        return q, info
    ok = np.isfinite(z)
    p0 = 2 * stats.norm.sf(np.abs(z[ok] - info["mu0"]) / info["sigma0"])
    q[ok] = np.minimum(1.0, bh_qvalues(p0) * info["pi0"])
    info = {k: v for k, v in info.items() if not k.startswith("_")}
    return q, info


def local_fdr(z, min_n=None):
    """Efron local fdr (density-based) under the empirical null. NOT used for verdicts: in testing it
    missed most strong-but-rare anomalies (e.g. 30 precincts at z = 6 among 70,000), because the
    smoothed density cannot follow a few points in the far tails. Kept for reference/diagnostics."""
    z = np.asarray(z, float)
    lfdr = np.full(len(z), np.nan)
    info = empirical_null(z, min_n)
    if info.get("method", "").startswith("not run"):
        return lfdr, info
    centers, f = info["_centers"], info["_f"]
    f0c = info["pi0"] * stats.norm.pdf(centers, info["mu0"], info["sigma0"])
    lf_c = np.minimum(1.0, f0c / np.maximum(f, 1e-300))
    right = centers >= info["mu0"]
    lf_c[right] = np.minimum.accumulate(lf_c[right])
    lf_c[~right] = np.minimum.accumulate(lf_c[~right][::-1])[::-1]
    ok = np.isfinite(z)
    lfdr[ok] = np.interp(np.clip(z[ok], centers[0], centers[-1]), centers, lf_c)
    return lfdr, {k: v for k, v in info.items() if not k.startswith("_")}
