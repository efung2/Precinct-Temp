"""Step 2: peer-relative anomaly scores.

Every metric is a proportion with a denominator:

    share   = Democratic votes / (Democratic + Republican votes)
    under   = under-votes / ballots
    mode    = early + absentee + provisional votes / all votes   (only where reported)

For each precinct we ask: "how unusual is this proportion compared with the
other precincts in the same county (or district, if the county is too small),
given how noisy a precinct of this size is expected to be?"

    z = (p_i - peer_center) / sqrt(tau^2 + p(1-p)/n_i)

  * peer_center : vote-weighted median of the peer group (robust to outliers)
  * tau^2       : real between-precinct spread, estimated robustly from the
                  peer group (median absolute deviation), minus typical
                  sampling noise, never below a small floor
  * p(1-p)/n_i  : sampling noise, which is large for small precincts

Honest heterogeneity (urban/rural, campus, retirement community) widens tau,
so heterogeneous peer groups produce *fewer* flags, not more.
"""
import numpy as np
from scipy import stats

import config
from clean import CONTEST


# --------------------------------------------------------------------------- #
def wmedian(x, w):
    order = np.argsort(x, kind="stable")
    cw = np.cumsum(w[order])
    pos = int(np.searchsorted(cw, 0.5 * cw[-1]))
    return x[order][min(pos, len(x) - 1)]


def robust_spread(pp):
    """A deliberately cautious estimate of the spread of a peer group.

    The largest of three robust estimates (interquartile range, 10-90 range,
    median absolute deviation), each scaled to equal the standard deviation for
    normal data. Taking the largest means that a group with two clusters of
    precincts (e.g. segregated neighbourhoods) gets a wide spread instead of
    having its smaller cluster mistaken for outliers.
    """
    q10, q25, q75, q90 = np.percentile(pp, [10, 25, 75, 90])
    mad = np.median(np.abs(pp - np.median(pp)))
    return max((q75 - q25) / 1.349, (q90 - q10) / 2.563, 1.4826 * mad)


def _fit_pass(p, n, gid, tau_min):
    """Fit centre / tau^2 per group and score every member. All inputs are 1-D arrays."""
    m = len(p)
    center = np.full(m, np.nan)
    tau2 = np.full(m, np.nan)
    z = np.full(m, np.nan)
    support = np.full(m, np.iinfo("int32").max, dtype="int64")
    if m == 0:
        return center, tau2, z, support
    order = np.argsort(gid, kind="stable")
    g = gid[order]
    cuts = np.flatnonzero(np.diff(g)) + 1
    starts = np.r_[0, cuts]
    ends = np.r_[cuts, m]
    for s, e in zip(starts, ends):
        idx = order[s:e]
        pp = p[idx]
        nn = n[idx]
        c = wmedian(pp, nn)
        cc = min(max(c, 0.005), 0.995)
        s2 = cc * (1.0 - cc) / nn
        resid = pp - c
        t2 = max(tau_min**2, robust_spread(pp) ** 2 - np.median(s2))
        center[idx] = c
        tau2[idx] = t2
        z[idx] = resid / np.sqrt(t2 + s2)
        # support: how many OTHER peers are statistically indistinguishable from this one,
        # i.e. lie within one local standard deviation of it?
        srt = np.sort(pp)
        r = config.SUPPORT_RADIUS_SD * np.sqrt(t2 + s2)
        raw = np.searchsorted(srt, pp + r, side="right") - np.searchsorted(srt, pp - r, side="left") - 1
        support[idx] = adjust_support(raw, z[idx], len(idx))
    return center, tau2, z, support


def adjust_support(raw, z, m):
    """Turn a raw neighbour count into 'excess' neighbours.

    A precinct at z = 3 in a county of 1,000 precincts has ~23 peers within one standard deviation
    of it by pure chance (normal tail), so counting neighbours alone would call every moderately
    unusual precinct in a big county a "cluster". We subtract what chance allows: the 99th
    percentile of a Poisson count with mean (m-1) * [Phi(|z|+r) - Phi(|z|-r)], minus SUPPORT_MAX.
    A cluster is then: more similar precincts than chance would put there.
    """
    r = config.SUPPORT_RADIUS_SD
    az = np.abs(np.nan_to_num(z, nan=0.0))
    expect = (m - 1) * (stats.norm.cdf(az + r) - stats.norm.cdf(az - r))
    allow = np.maximum(stats.poisson.ppf(0.99, expect) - config.SUPPORT_MAX, 0)
    return (raw - allow).astype("int64")


def score_metric(num, den, mask, gid_c, gid_d, min_peers=None, tau_min=0.03):
    """Score one metric.

    num, den : integer arrays (numerator / denominator counts)
    mask     : rows that may be assessed at all (right contest type, big enough, real place)
    gid_c    : county-level group id per row,  gid_d : district(contest)-level group id per row
    Returns dict of arrays: p, center, tau2, z, level (0 = not assessed, 1 = county, 2 = district)
    """
    min_peers = config.MIN_PEERS if min_peers is None else min_peers
    num = np.asarray(num, dtype="float64")
    den = np.asarray(den, dtype="float64")
    n_rows = len(num)
    ok = np.asarray(mask, dtype=bool) & (den > 0)
    p = np.divide(num, den, out=np.full(n_rows, np.nan), where=den > 0)

    out = {
        "p": p,
        "center": np.full(n_rows, np.nan),
        "tau2": np.full(n_rows, np.nan),
        "z": np.full(n_rows, np.nan),
        "support": np.full(n_rows, np.iinfo("int32").max, dtype="int64"),
        "level": np.zeros(n_rows, dtype="int8"),
    }
    if not ok.any():
        return out

    # county-level groups with enough assessable peers
    cnt_c = np.bincount(gid_c[ok], minlength=int(gid_c.max()) + 1)
    use_c = ok & (cnt_c[gid_c] >= min_peers)
    idx = np.flatnonzero(use_c)
    if len(idx):
        c, t, z, sup = _fit_pass(p[idx], den[idx], gid_c[idx], tau_min)
        out["center"][idx], out["tau2"][idx], out["z"][idx], out["support"][idx] = c, t, z, sup
        out["level"][idx] = 1

    # district-level fallback for the rest (fitted on ALL assessable rows of the district)
    need = ok & ~use_c
    if need.any():
        cnt_d = np.bincount(gid_d[ok], minlength=int(gid_d.max()) + 1)
        need_gid = np.unique(gid_d[need])
        fit_rows = ok & np.isin(gid_d, need_gid) & (cnt_d[gid_d] >= min_peers)
        fidx = np.flatnonzero(fit_rows)
        if len(fidx):
            c, t, z, sup = _fit_pass(p[fidx], den[fidx], gid_d[fidx], tau_min)
            take = need[fidx]
            tgt = fidx[take]
            out["center"][tgt], out["tau2"][tgt] = c[take], t[take]
            out["z"][tgt], out["support"][tgt] = z[take], sup[take]
            out["level"][tgt] = 2
    return out


def is_flagged(z, support, thr):
    """A precinct is flagged if it is extreme AND isolated (not part of a cluster of similar precincts)."""
    return (np.abs(np.nan_to_num(z, nan=0.0)) >= thr) & (support <= config.SUPPORT_MAX)


# --------------------------------------------------------------------------- #
def metric_inputs(p):
    """Numerators, denominators and masks for the three metrics."""
    base = (p["status"] == "two_party") & ~p["nongeo"] & ~p["zero_votes"]
    inputs = {
        "share": dict(
            num=p["dem"].to_numpy(),
            den=p["two_party"].to_numpy(),
            mask=(base & (p["two_party"] >= config.MIN_DEN)).to_numpy(),
        ),
        "under": dict(
            num=p["under"].to_numpy(),
            den=p["ballots"].to_numpy(),
            mask=(base & p["under_reported"] & (p["ballots"] >= config.MIN_DEN)).to_numpy(),
        ),
        "mode": dict(
            num=p["v_non"].to_numpy(),
            den=(p["v_ed"] + p["v_non"]).to_numpy(),
            mask=(base & p["mode_split"] & ((p["v_ed"] + p["v_non"]) >= config.MIN_DEN)).to_numpy(),
        ),
    }
    return inputs


def analyze(p, z_flag=None):
    """Add scores and flags to the precinct table. Returns (table, fits)."""
    z_flag = config.Z_FLAG if z_flag is None else z_flag
    p = p.sort_values(CONTEST + ["county_fips", "jurisdiction_fips", "county_name", "precinct"]).reset_index(drop=True)
    # Lowest-level peer group = county x local election authority (jurisdiction). In most states the
    # two are the same; where they differ (New England towns, Chicago vs suburban Cook County) the
    # authority sets equipment, ballot design and reporting practice, so it is the fairer comparison.
    gid_c = p.groupby(CONTEST + ["county_fips", "jurisdiction_fips"], sort=False).ngroup().to_numpy()
    gid_d = p.groupby(CONTEST, sort=False).ngroup().to_numpy()

    fits = {}
    for name, d in metric_inputs(p).items():
        fit = score_metric(d["num"], d["den"], d["mask"], gid_c, gid_d, tau_min=config.TAU_MIN[name])
        fit.update(num=d["num"], den=d["den"], gid_c=gid_c, gid_d=gid_d)
        fits[name] = fit
        p[f"{name}_p"] = fit["p"]
        p[f"{name}_peer"] = fit["center"]
        p[f"{name}_z"] = fit["z"]
        p[f"{name}_level"] = fit["level"]

    flagged = {k: is_flagged(fits[k]["z"], fits[k]["support"], z_flag) for k in fits}
    extreme = {k: np.abs(np.nan_to_num(fits[k]["z"], nan=0.0)) >= z_flag for k in fits}
    clustered = np.zeros(len(p), dtype=bool)
    for k in fits:
        p[f"flag_{k}"] = flagged[k]
        clustered |= extreme[k] & ~flagged[k]
    p["n_flags"] = sum(v.astype(int) for v in flagged.values())
    absz = np.vstack([np.abs(np.nan_to_num(fits[k]["z"], nan=0.0)) for k in fits])
    p["max_abs_z"] = absz.max(axis=0)

    letters = np.where(flagged["share"], "S", "")
    letters = np.char.add(letters, np.where(flagged["under"], "U", ""))
    letters = np.char.add(letters, np.where(flagged["mode"], "M", ""))
    p["flags"] = letters

    dq = np.where(p["zero_votes"], "Z", "")
    dq = np.char.add(dq, np.where(p["nongeo"], "N", ""))
    dq = np.char.add(dq, np.where((p["status"] == "two_party") & (p["two_party"] < config.MIN_DEN), "T", ""))
    dq = np.char.add(dq, np.where(p["dup_vector"], "D", ""))
    dq = np.char.add(dq, np.where(clustered, "C", ""))
    p["dq"] = dq
    return p, fits
