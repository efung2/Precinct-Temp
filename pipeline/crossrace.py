"""Cross-race comparison: the same precinct, two races on the same ballot.

Idea: a precinct full of Democrats is Democratic in EVERY race. If a precinct looks
unusual in the House race but shows the same pattern in the Senate race, the
explanation is almost certainly the voters, not the count. If it is unusual in
one race only, that is a more specific lead (though candidate effects such as a
home-town candidate are common, harmless causes).

    gap_i = Democratic two-party share in the primary race
          - Democratic two-party share in the companion race (same precinct, same election)

Each precinct's gap is compared with the gaps of its peers: other precincts in the
same county with the SAME PAIR of races (so candidate-pair effects such as a strong
incumbent are absorbed), falling back to all precincts with that pair of races.

    z = (gap_i - peer median gap) / sqrt(spread^2 + noise_i)
    noise_i = pA(1-pA)/nA + pB(1-pB)/nB   (conservative: ignores the positive covariance
                                           between two races voted by the same people)

Precincts are matched across the two files on (state, county, jurisdiction, cleaned
precinct name). Precincts split between districts (they appear more than once in
either file) are not compared, because the two totals would not cover the same voters.
"""
import numpy as np
import pandas as pd
from scipy import stats

import clean
import config
from metrics import adjust_support, robust_spread, wmedian


def _norm_name(s):
    return s.str.upper().str.replace(r"[^A-Z0-9]+", " ", regex=True).str.strip()


def load_companion(path, log=print):
    """Clean the companion file with the same rules as the primary file; return precinct table."""
    df, stats_ = clean.read_raw(path)
    df, cand_tab = clean.classify(df, stats_)
    c, contests = clean.build_precinct_table(df, cand_tab, stats_)
    log(f"    companion: {len(c):,} precincts in {len(contests)} contests "
        f"({int((contests['status'] == 'two_party').sum())} two-party)")
    return c, contests


def _prepare(t, tag):
    t = t.copy()
    t["k_full"] = list(zip(t["state_po"], t["county_fips"], t["jurisdiction_fips"], _norm_name(t["precinct"])))
    t["k_cty"] = list(zip(t["state_po"], t["county_fips"], _norm_name(t["precinct"])))
    t[f"contest_{tag}"] = t["office"] + "|" + t["district"].astype(str) + "|" + t["special"].astype(str)
    return t


def _split_keys(t, key):
    """Keys whose precinct appears in more than one district of the file (any race status):
    the precinct is split between districts, so one row does not cover all of its voters."""
    d = t[[key, "district"]].drop_duplicates()
    vc = d[key].value_counts()
    return set(vc.index[vc > 1])


def match(p, comp):
    """Return companion columns aligned to p's rows, plus a status per row.

    status: 'matched' / 'matched_by_name' (second pass ignoring jurisdiction),
            'unmatched', 'split' (precinct split between districts in either file),
            'n/a' (row not eligible: not a two-party race, non-geographic, too small)
    """
    out = pd.DataFrame(
        {"comp_dem": np.nan, "comp_rep": np.nan, "comp_contest": None, "cross_status": "n/a"}, index=p.index
    )
    elig = (p["status"] == "two_party") & ~p["nongeo"] & (p["two_party"] >= config.MIN_DEN)
    if not elig.any():
        return out
    P = _prepare(p, "p")
    C = _prepare(comp, "c")
    split_p = _split_keys(P, "k_full")
    split_c = _split_keys(C, "k_full")
    split_p2 = _split_keys(P, "k_cty")
    split_c2 = _split_keys(C, "k_cty")

    # usable companion rows; when a precinct has a regular AND a special race of the companion
    # office (e.g. 2018 Minnesota Senate), the regular one is used
    c = C[(C["status"] == "two_party") & ~C["nongeo"] & (C["two_party"] >= config.MIN_DEN)]
    c = c.sort_values("special", kind="stable")
    c_full = c.drop_duplicates("k_full", keep="first").set_index("k_full")
    c_cty = c.drop_duplicates("k_cty", keep="first")
    c_cty = c_cty[~c_cty.duplicated("k_cty", keep=False)].set_index("k_cty")

    pp = P[elig]
    status = pd.Series("unmatched", index=pp.index, dtype=object)
    is_split = pp["k_full"].isin(split_p | split_c)
    hit = pp["k_full"].isin(c_full.index) & ~is_split
    status[is_split] = "split"
    status[hit] = "matched"
    idx = status.index[hit.to_numpy()]
    src = c_full.loc[pp.loc[idx, "k_full"]]
    out.loc[idx, "comp_dem"] = src["dem"].to_numpy()
    out.loc[idx, "comp_rep"] = src["rep"].to_numpy()
    out.loc[idx, "comp_contest"] = src["contest_c"].to_numpy()

    # second pass ignoring jurisdiction (filled differently in some files); names must be
    # unique within the county on both sides
    rest = status.index[(status == "unmatched").to_numpy()]
    if len(rest):
        r = pp.loc[rest]
        ok2 = r["k_cty"].isin(c_cty.index) & ~r["k_cty"].isin(split_p2 | split_c2)
        idx2 = rest[ok2.to_numpy()]
        status[idx2] = "matched_by_name"
        src2 = c_cty.loc[pp.loc[idx2, "k_cty"]]
        out.loc[idx2, "comp_dem"] = src2["dem"].to_numpy()
        out.loc[idx2, "comp_rep"] = src2["rep"].to_numpy()
        out.loc[idx2, "comp_contest"] = src2["contest_c"].to_numpy()

    out.loc[pp.index, "cross_status"] = status
    return out


def _fit_gauss(x, s2, gid, tau_min):
    """Robust centre/spread per group, z-scores and isolation support (as metrics._fit_pass)."""
    m = len(x)
    center = np.full(m, np.nan)
    tau2 = np.full(m, np.nan)
    z = np.full(m, np.nan)
    support = np.full(m, np.iinfo("int32").max, dtype="int64")
    order = np.argsort(gid, kind="stable")
    g = gid[order]
    cuts = np.flatnonzero(np.diff(g)) + 1
    for s, e in zip(np.r_[0, cuts], np.r_[cuts, m]):
        idx = order[s:e]
        xx = x[idx]
        w = 1.0 / s2[idx]
        c = wmedian(xx, w)
        t2 = max(tau_min**2, robust_spread(xx) ** 2 - np.median(s2[idx]))
        sd = np.sqrt(t2 + s2[idx])
        center[idx] = c
        tau2[idx] = t2
        z[idx] = (xx - c) / sd
        srt = np.sort(xx)
        r = config.SUPPORT_RADIUS_SD * sd
        raw = np.searchsorted(srt, xx + r, side="right") - np.searchsorted(srt, xx - r, side="left") - 1
        support[idx] = adjust_support(raw, z[idx], len(idx))
    return center, tau2, z, support


def score_gap(gap, s2, mask, gid_c, gid_d, min_peers=None, tau_min=None):
    """Peer z-scores of the gap with county -> race-pair fallback."""
    min_peers = config.CROSS_MIN_PEERS if min_peers is None else min_peers
    tau_min = config.CROSS_TAU_MIN if tau_min is None else tau_min
    n = len(gap)
    res = {k: np.full(n, np.nan) for k in ("center", "tau2", "z")}
    res["support"] = np.full(n, np.iinfo("int32").max, dtype="int64")
    res["level"] = np.zeros(n, "int8")
    ok = np.asarray(mask, bool) & np.isfinite(gap)
    if not ok.any():
        return res
    cnt_c = np.bincount(gid_c[ok], minlength=int(gid_c.max()) + 1)
    use_c = ok & (cnt_c[gid_c] >= min_peers)
    idx = np.flatnonzero(use_c)
    if len(idx):
        c, t, z, sp = _fit_gauss(gap[idx], s2[idx], gid_c[idx], tau_min)
        res["center"][idx], res["tau2"][idx], res["z"][idx], res["support"][idx] = c, t, z, sp
        res["level"][idx] = 1
    need = ok & ~use_c
    if need.any():
        cnt_d = np.bincount(gid_d[ok], minlength=int(gid_d.max()) + 1)
        fit_rows = ok & np.isin(gid_d, np.unique(gid_d[need])) & (cnt_d[gid_d] >= min_peers)
        fidx = np.flatnonzero(fit_rows)
        if len(fidx):
            c, t, z, sp = _fit_gauss(gap[fidx], s2[fidx], gid_d[fidx], tau_min)
            take = need[fidx]
            tgt = fidx[take]
            res["center"][tgt], res["tau2"][tgt], res["z"][tgt], res["support"][tgt] = c[take], t[take], z[take], sp[take]
            res["level"][tgt] = 2
    return res


def gap_inputs(p):
    """Arrays needed to score the gap for table p (after attach)."""
    nA = p["two_party"].to_numpy(float)
    nB = (p["comp_dem"] + p["comp_rep"]).to_numpy(float)
    pA = np.divide(p["dem"].to_numpy(float), nA, out=np.full(len(p), np.nan), where=nA > 0)
    pB = np.divide(p["comp_dem"].to_numpy(float), nB, out=np.full(len(p), np.nan), where=nB > 0)
    s2 = np.clip(pA, 0.01, 0.99) * (1 - np.clip(pA, 0.01, 0.99)) / np.maximum(nA, 1) + \
        np.clip(pB, 0.01, 0.99) * (1 - np.clip(pB, 0.01, 0.99)) / np.maximum(nB, 1)
    mask = p["cross_status"].isin(["matched", "matched_by_name"]).to_numpy() & (nB >= config.MIN_DEN) & np.isfinite(pA)
    pair = p["state_po"].astype(str) + "|" + p["office"] + "|" + p["district"].astype(str) + "|" + \
        p["special"].astype(str) + "||" + p["comp_contest"].astype(str)
    gid_d = pd.factorize(pair)[0]
    gid_c = pd.factorize(pair + "|" + p["county_fips"].astype(str) + "|" + p["jurisdiction_fips"].astype(str))[0]
    return pA, pB, pA - pB, s2, mask, gid_c, gid_d


def attach(p, comp, log=print):
    """Match companion precincts to p and score the gap. Adds columns to p (returns new frame) + info dict."""
    m = match(p, comp)
    p = p.join(m)
    pA, pB, gap, s2, mask, gid_c, gid_d = gap_inputs(p)
    res = score_gap(gap, s2, mask, gid_c, gid_d)
    p["comp_share"] = pB
    p["cross_gap"] = np.where(mask, gap, np.nan)
    p["cross_peer"] = res["center"]
    p["cross_z"] = res["z"]
    p["cross_support"] = res["support"]
    p["cross_level"] = res["level"]
    pv = np.where(np.isfinite(res["z"]), 2 * stats.norm.sf(np.abs(res["z"])), np.nan)
    p["cross_p"] = pv
    st = p["cross_status"].value_counts().to_dict()
    eligible = int(sum(v for k, v in st.items() if k != "n/a"))
    matched = int(st.get("matched", 0) + st.get("matched_by_name", 0))
    info = {
        "eligible": eligible,
        "matched": matched,
        "matched_by_name": int(st.get("matched_by_name", 0)),
        "split": int(st.get("split", 0)),
        "unmatched": int(st.get("unmatched", 0)),
        "assessed": int(np.isfinite(res["z"]).sum()),
        "match_rate": round(matched / eligible, 4) if eligible else None,
    }
    log(f"    cross-race: {matched:,} of {eligible:,} eligible precincts matched "
        f"({info['split']:,} split, {info['unmatched']:,} unmatched); {info['assessed']:,} assessed")
    return p, info, {"gap": gap, "s2": s2, "mask": mask, "gid_c": gid_c, "gid_d": gid_d, "fit": res}
