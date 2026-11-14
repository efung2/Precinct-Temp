"""Tests for the pipeline. Run with either:

    python tests/test_pipeline.py        (no extra packages needed)
    python -m pytest tests               (if you have pytest)
"""
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import make_sample_data  # noqa: E402
import run_pipeline  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "house_2018_AL_sample.csv"


def _strict_json(path):
    def bad(x):
        raise ValueError(f"{path}: invalid JSON constant {x}")

    return json.loads(Path(path).read_text(), parse_constant=bad)


def _run(csv, out, extra=()):
    rc = run_pipeline.main(["--input", str(csv), "--dataset-id", "t", "--out", str(out), *extra])
    assert rc == 0
    return Path(out) / "t"


def _precinct_frame(base, states):
    frames = []
    for st in states:
        d = _strict_json(base / "states" / f"{st}.json")
        for key, c in d["contests"].items():
            if "rows" in c:
                df = pd.DataFrame(c["rows"], columns=d["fields"])
                df["state_po"] = st
                df["contest"] = key
                frames.append(df)
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
def test_alabama_sample_is_handled_and_not_over_flagged():
    """Real MEDSL rows: 32 precincts in one district. The peer z-score needs 50 peers, so it assesses
    nothing; the hierarchical model can assess the 30 usable precincts (it needs only 10 per race)
    and must not flag the district's ordinary partisan variety."""
    with tempfile.TemporaryDirectory() as tmp:
        base = _run(FIXTURE, tmp, ["--skip-calibration"])
        d = _strict_json(base / "states" / "AL.json")
        c = d["contests"]["1"]
        assert c["status"] == "two_party"
        assert c["n_precincts"] == 32
        assert c["assessed"] == {"share": 0, "under": 0, "mode": 0, "turnout": 0}
        assert c["hb_assessed"]["share"] == 30
        assert c["flagged"]["any"] == 0
        assert c["funnel"]["any"]["persist"] == 0
        df = pd.DataFrame(c["rows"], columns=d["fields"])
        zero = df[df["dq"].str.contains("Z")]
        assert set(zero["precinct"]) == {"BASHI METHODIST CHURCH", "BOYS & GIRLS CLUB #2"}
        assert (df["dem"] + df["rep"]).sum() > 0
        s = _strict_json(base / "summary.json")
        assert s["totals"]["precincts"] == 32
        assert s["totals"]["flagged"]["any"] == 0


def test_synthetic_end_to_end():
    raw, planted, truth = make_sample_data.generate(seed=7)
    comp = make_sample_data.generate_companion(truth, seed=8)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        padded, plain, senate = tmp / "padded.csv", tmp / "plain.csv", tmp / "senate.csv"
        make_sample_data.write_csv(raw, padded, pad_blank=500)
        make_sample_data.write_csv(raw, plain, pad_blank=0)
        make_sample_data.write_csv(comp, senate)
        base = _run(padded, tmp / "out_padded", ["--companion", str(senate)])
        base_plain = _run(plain, tmp / "out_plain", ["--companion", str(senate)])

        summary = _strict_json(base / "summary.json")
        calib = _strict_json(base / "calibration.json")
        _strict_json(tmp / "out_padded" / "datasets.json")

        # blank padding is dropped and does not change results
        assert summary["data_quality"]["blank_rows"] == 500
        s2 = _strict_json(base_plain / "summary.json")
        assert s2["totals"] == summary["totals"]

        states = [s["code"] for s in summary["states"]]
        allp = _precinct_frame(base, states)

        # ---- planted anomalies
        def find(r):
            m = allp[
                (allp["state_po"] == r.state_po)
                & (allp["contest"] == r.district)
                & (allp["precinct"] == r.precinct)
                & (allp["county"].str.endswith(f"COUNTY {int(r.county_idx) + 1}"))
            ]
            assert len(m) == 1, (r.kind, r.precinct, len(m))
            return m.iloc[0]

        for _, r in planted.iterrows():
            row = find(r)
            if r.kind == "share_shift":
                assert "S" in row["flags"] or "C" in row["dq"], (r.precinct, row["share_z"])
            elif r.kind == "undervote_spike":
                assert "U" in row["flags"]
            elif r.kind == "mode_shift":
                assert "M" in row["flags"]
            elif r.kind == "duplicate":
                assert "D" in row["dq"]
            elif r.kind == "zero_votes":
                assert "Z" in row["dq"]
            elif r.kind == "nongeo":
                assert "N" in row["dq"] and row["flags"] == "" and row["screened"] == ""
            elif r.kind == "special_pop":
                # unusual in BOTH races: screened on vote share, then explained by the other race
                assert row["share_v"] == "L", (r.precinct, row["share_v"], row["cross_z"])
                assert row["remain"] == ""
        shifts = planted[planted["kind"] == "share_shift"]
        n_found = sum("S" in find(r)["flags"] for _, r in shifts.iterrows())
        assert n_found >= 2, "at least 2 of the 3 planted share shifts must be flagged"

        # ---- precision: almost nothing else is flagged
        n_planted_flag = len(planted[planted["kind"].isin(["share_shift", "undervote_spike", "mode_shift", "special_pop"])])
        n_flagged = int((allp["flags"] != "").sum())
        assert n_flagged <= n_planted_flag + 2, f"{n_flagged} flagged, only {n_planted_flag} planted"

        # ---- after every check, EXACTLY the planted single-race problems remain
        keep = planted[planted["kind"].isin(["share_shift", "undervote_spike", "mode_shift"])]
        want = {(r.state_po, r.district, r.precinct, int(r.county_idx) + 1) for r in keep.itertuples()}
        left = allp[allp["remain"] != ""]
        got = {(r.state_po, r.contest, r.precinct, int(r.county.rsplit(" ", 1)[1])) for r in left.itertuples()}
        assert got == want, (got ^ want)
        for _, r in planted[planted["kind"] == "share_shift"].iterrows():
            row = find(r)
            assert "S" in row["remain"] and "X" in row["remain"], (r.precinct, row["remain"])

        # ---- cross-race matching and funnel bookkeeping
        assert summary["has_cross"] and summary["cross"]["match_rate"] == 1.0
        f = summary["totals"]["funnel"]["share"]
        assert f["screened"] >= f["after_T"] >= f["after_E"] >= f["after_C"] >= f["after_L"] == f["persist"]
        assert f["screened"] - f["after_C"] + f["after_C"] - f["after_L"] >= 2  # special_pop explained

        # ---- the bimodal county must not produce flags (heterogeneity is not an anomaly)
        bimodal = allp[(allp["state_po"] == "AB") & (allp["contest"] == "2")]
        assert len(bimodal) > 0 and (bimodal["flags"] == "").all() and (bimodal["remain"] == "").all()

        # ---- contests that cannot be compared are skipped with a reason
        by_state = {s["code"]: s for s in summary["states"]}
        assert by_state["AA"]["skipped"].get("single_major_party") == 1  # uncontested district 3
        assert by_state["AC"]["skipped"].get("multi_major") == 1  # two Democrats, no Republican
        aa = _strict_json(base / "states" / "AA.json")
        assert "rows" not in aa["contests"]["3"] and aa["contests"]["3"]["reason"]

        # ---- vote totals match ground truth: modes are never double counted (state AD reports
        #      both TOTAL and per-mode rows for every precinct)
        for st in states:
            for key in allp[allp["state_po"] == st]["contest"].unique():
                got = allp[(allp["state_po"] == st) & (allp["contest"] == key)]
                exp = truth[(truth["state_po"] == st) & (truth["district"] == key)]
                assert got["dem"].sum() == exp["dem"].sum(), (st, key)
                assert got["rep"].sum() == exp["rep"].sum(), (st, key)

        # ---- calibration output is sane
        assert calib["null"]["share"]["fpr"]["4"] < 0.002
        assert calib["power"]["shifts"] and len(calib["power"]["detect_rate"]) == len(calib["power"]["shifts"])
        for m in ("share", "under", "mode"):
            assert calib["chain"]["signals"][m]["expected_persist"] <= 1.0


def test_fdr_tools():
    import fdr
    import numpy as np
    from scipy import stats

    # Benjamini-Hochberg against a hand-computed example
    q = fdr.bh_qvalues(np.array([0.01, 0.04, 0.03, 0.005, np.nan]))
    assert np.allclose(q[:4], [0.02, 0.04, 0.04, 0.02]) and np.isnan(q[4])

    # empirical null: a clean but over-dispersed set of z-scores yields (almost) no discoveries,
    # while plain BH on textbook p-values yields hundreds; rare strong signals are kept
    rng = np.random.default_rng(1)
    z = np.r_[rng.normal(0.1, 1.4, 50000), rng.normal(7, 0.3, 30)]
    qe, info = fdr.empirical_null_q(z)
    assert 1.2 < info["sigma0"] < 1.6
    assert np.sum(qe[:50000] < 0.05) <= 10
    assert np.mean(qe[50000:] < 0.05) >= 0.9
    assert np.sum(fdr.bh_qvalues(2 * stats.norm.sf(np.abs(z[:50000]))) < 0.05) > 200


def test_hierarchical_calibration():
    import hierarchical as hb
    import numpy as np

    rng = np.random.default_rng(2)
    k, n, gc, gd, cid = [], [], [], [], 0
    for d in range(30):
        dm = rng.uniform(0.2, 0.8)
        for _ in range(rng.integers(3, 15)):
            m = int(rng.choice([2, 4, 10, 40]))
            cm = np.clip(dm + rng.normal(0, 0.06), 0.05, 0.95)
            nu = cm * (1 - cm) / 0.06**2 - 1
            th = rng.beta(cm * nu, (1 - cm) * nu, m)
            nn = rng.integers(50, 2000, m)
            k += list(rng.binomial(nn, th)); n += list(nn); gc += [cid] * m; gd += [d] * m; cid += 1
    k, n, gc, gd = map(np.array, (k, n, gc, gd))
    f = hb.fit(k, n, np.ones(len(k), bool), gc, gd, 0.03)
    p, _ = hb.predictive_p(k, n, f["mu"], f["v"])
    big = np.bincount(gd)[gd] >= 10
    assert np.isfinite(p[big]).all()  # every race with 10+ precincts is assessed, small counties included
    assert np.isnan(p[~big]).all()  # races with fewer than 10 are left out, not guessed at
    assert np.isfinite(p[big & (np.bincount(gc)[gc] <= 2)]).any()
    assert np.mean(p[big] < 0.01) < 0.02


def test_missing_columns_fail_clearly():
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.csv"
        bad.write_text("a,b\n1,2\n")
        try:
            run_pipeline.main(["--input", str(bad), "--out", str(Path(tmp) / "o")])
        except SystemExit as e:
            assert "missing required column" in str(e)
        else:
            raise AssertionError("expected SystemExit")


def test_pa_convert_merges_split_precincts():
    """A precinct split by a district line: votes sit under one piece, registration under both."""
    import pa_convert

    def ret(county, prec, office, party, last, votes, name):
        r = [""] * 37
        r[0], r[1], r[2], r[3], r[8], r[9], r[11], r[12], r[15] = "2024", "G", county, prec, office, party, last, "X", votes
        r[21], r[22], r[28], r[29], r[30] = "4", name, "5", "001", str(prec).zfill(4)
        return r

    def reg(county, prec, dem, rep):
        r = ["2024", "G", county, prec, 1, "DEM", dem, 2, "REP", rep, 3, "GR", 0, 4, "LIB", 0, 5, "OTH", 0, 6, "", 0] + [0] * 16
        return r

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rows = []
        for prec, name, d, r_ in ((10, "ALPHA", 100, 200), (20, "BETA P 1 (USC 4)", 0, 0), (21, "BETA P 1 (USC 9)", 300, 250),
                                  (30, "GAMMA", 50, 60)):
            for office in ("USP", "USS"):
                rows += [ret(1, prec, office, "DEM", "HARRIS" if office == "USP" else "CASEY", d, name),
                         ret(1, prec, office, "REP", "TRUMP" if office == "USP" else "MCCORMICK", r_, name)]
        pd.DataFrame(rows).to_csv(tmp / "ret.txt", header=False, index=False)
        pd.DataFrame([reg(0, 99999, 0, 0), reg(1, 10, 200, 250), reg(1, 20, 150, 150), reg(1, 21, 200, 200),
                      reg(1, 30, 80, 90)]).to_csv(tmp / "reg.txt", header=False, index=False)
        assert pa_convert.main(["--returns", str(tmp / "ret.txt"), "--registration", str(tmp / "reg.txt"), "--out", str(tmp)]) == 0
        u = pd.read_csv(tmp / "pa-2024-precincts.csv")
        rep = json.loads((tmp / "pa-2024-build-report.json").read_text())
        assert len(u) == 3 and rep["merged_split_precincts"] == 1 and rep["source_rows_more_votes_than_registered"] == 1
        beta = u[u["precinct"] == "BETA P 1"].iloc[0]
        assert beta["registered"] == 700 and beta["pres_dem"] == 300 and beta["pres_rep"] == 250 and beta["pieces"] == 2
        assert u["registered"].sum() == 1320 and rep["units_more_votes_than_registered"] == 0
        m = pd.read_csv(tmp / "pa-2024-president.csv")
        assert m["votes"].sum() == 960 and set(m["registered_voters"]) == {450, 700, 170}


def test_turnout_signal():
    """With registered voters in the file, a precinct with far lower turnout than its peers is listed."""
    import numpy as np

    rng = np.random.default_rng(11)
    rows = []
    for c in range(4):
        for i in range(120):
            reg_n = int(rng.integers(600, 1800))
            t = 0.25 if (c, i) == (0, 0) else float(np.clip(rng.normal(0.76, 0.03), 0.5, 0.95))
            v = rng.binomial(reg_n, t)
            d = rng.binomial(v, float(np.clip(rng.normal(0.48, 0.05), 0.05, 0.95)))
            for party, n in (("DEMOCRAT", d), ("REPUBLICAN", v - d)):
                rows.append({"precinct": f"P{c}-{i}", "office": "US PRESIDENT", "party_simplified": party, "party_detailed": party,
                             "mode": "TOTAL", "votes": n, "county_name": f"C{c}", "county_fips": f"4200{c}",
                             "jurisdiction_name": f"C{c}", "jurisdiction_fips": f"4200{c}", "candidate": party[:3],
                             "district": "statewide", "dataverse": "PRESIDENT", "year": 2024, "stage": "GEN",
                             "state": "PENNSYLVANIA", "special": "FALSE", "writein": "FALSE", "state_po": "PA",
                             "registered_voters": reg_n})
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "pres.csv"
        pd.DataFrame(rows).to_csv(src, index=False)
        base = _run(src, Path(tmp) / "out", ["--skip-calibration"])
        s = _strict_json(base / "summary.json")
        assert s["has_turnout"] and s["totals"]["hb_assessed"]["turnout"] == 480
        f = _precinct_frame(base, ["PA"])
        hit = f[f["precinct"] == "P0-0"].iloc[0]
        assert hit["turnout_v"] == "P" and "R" in hit["remain"] and abs(hit["turnout"] - 0.25) < 0.06
        assert (f["turnout_v"] == "P").sum() <= 2


def test_eforensics_smoke():
    """The eforensics re-implementation runs end to end and returns valid probabilities (skipped without NumPyro)."""
    try:
        import numpyro  # noqa: F401
    except ImportError:
        print("      (skipped: numpyro not installed)")
        return
    import numpy as np
    import eforensics as ef

    rng = np.random.default_rng(4)
    N = rng.integers(300, 2000, 150)
    mu = dict(nu=0.2, tau=0.4, iM=-1.0, iS=-1.0, uM=0.0, uS=0.0)
    sd = dict(nu=0.4, tau=0.3, iM=0.3, iS=0.3, uM=0.3, uS=0.3)
    A, W, Z = ef.simulate(rng, N, np.array([0.9, 0.08, 0.02]), mu, sd)
    s, post = ef.run_units(N, A, W, warmup=100, samples=50, chains=2, log=lambda *_: None)
    assert np.all((post["pfraud"] >= 0) & (post["pfraud"] <= 1))
    assert abs(sum(s["pi_mean"]) - 1) < 1e-6 and len(s["flag_05_by_chain"]) == 2


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
