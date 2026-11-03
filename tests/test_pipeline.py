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
    """Real MEDSL rows: 32 precincts in one district. With only 30 assessable precincts there
    are too few peers (MIN_PEERS = 50), so the pipeline must assess nothing rather than guess."""
    with tempfile.TemporaryDirectory() as tmp:
        base = _run(FIXTURE, tmp, ["--skip-calibration"])
        d = _strict_json(base / "states" / "AL.json")
        c = d["contests"]["1"]
        assert c["status"] == "two_party"
        assert c["n_precincts"] == 32
        assert c["assessed"] == {"share": 0, "under": 0, "mode": 0}
        assert c["flagged"]["any"] == 0
        df = pd.DataFrame(c["rows"], columns=d["fields"])
        zero = df[df["dq"].str.contains("Z")]
        assert set(zero["precinct"]) == {"BASHI METHODIST CHURCH", "BOYS & GIRLS CLUB #2"}
        assert (df["dem"] + df["rep"]).sum() > 0
        s = _strict_json(base / "summary.json")
        assert s["totals"]["precincts"] == 32
        assert s["totals"]["flagged"]["any"] == 0


def test_synthetic_end_to_end():
    raw, planted, truth = make_sample_data.generate(seed=7)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        padded, plain = tmp / "padded.csv", tmp / "plain.csv"
        make_sample_data.write_csv(raw, padded, pad_blank=500)
        make_sample_data.write_csv(raw, plain, pad_blank=0)
        base = _run(padded, tmp / "out_padded")
        base_plain = _run(plain, tmp / "out_plain")

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
                assert "N" in row["dq"] and row["flags"] == ""
        shifts = planted[planted["kind"] == "share_shift"]
        n_found = sum("S" in find(r)["flags"] for _, r in shifts.iterrows())
        assert n_found >= 2, "at least 2 of the 3 planted share shifts must be flagged"

        # ---- precision: almost nothing else is flagged
        n_planted_flag = len(planted[planted["kind"].isin(["share_shift", "undervote_spike", "mode_shift"])])
        n_flagged = int((allp["flags"] != "").sum())
        assert n_flagged <= n_planted_flag + 2, f"{n_flagged} flagged, only {n_planted_flag} planted"

        # ---- the bimodal county must not produce flags (heterogeneity is not an anomaly)
        bimodal = allp[(allp["state_po"] == "AB") & (allp["contest"] == "2")]
        assert len(bimodal) > 0 and (bimodal["flags"] == "").all()

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
