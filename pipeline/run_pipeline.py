#!/usr/bin/env python3
"""Run the whole pipeline: raw MEDSL precinct CSV  ->  JSON files for the website.

Example:
    python pipeline/run_pipeline.py \
        --input raw/HOUSE_precinct_general.csv \
        --dataset-id house-2018 \
        --label "U.S. House of Representatives - Precinct Level 2018" \
        --short-label "House 2018"
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import calibrate  # noqa: E402
import clean  # noqa: E402
import config  # noqa: E402
import export  # noqa: E402
import metrics  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="path to the raw MEDSL precinct CSV")
    ap.add_argument("--dataset-id", default="house-2018", help="folder name under docs/data (letters, digits, dashes)")
    ap.add_argument("--label", default="U.S. House of Representatives - Precinct Level 2018", help="full name shown on the site")
    ap.add_argument("--short-label", default=None, help="short tab name shown on the site")
    ap.add_argument("--out", default=str(HERE.parent / "docs" / "data"), help="output folder (default: docs/data)")
    ap.add_argument("--z-flag", type=float, default=config.Z_FLAG, help=f"|z| threshold for a flag (default {config.Z_FLAG})")
    ap.add_argument("--chunksize", type=int, default=250_000, help="rows per read chunk")
    ap.add_argument("--only-states", default=None, help="comma-separated state codes for a quick test run, e.g. AL,TX")
    ap.add_argument("--skip-calibration", action="store_true", help="skip the simulation step (faster, but the site loses its reliability panel)")
    ap.add_argument("--seed", type=int, default=config.SEED)
    args = ap.parse_args(argv)

    config.Z_FLAG = args.z_flag
    calibrate.config.Z_FLAG = args.z_flag
    only = {s.strip().upper() for s in args.only_states.split(",")} if args.only_states else None
    t0 = time.time()

    def log(msg):
        print(f"[{time.time() - t0:6.1f}s] {msg}", flush=True)

    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"Input file not found: {src}")

    log(f"1/4 reading {src.name} ({src.stat().st_size / 1e6:.0f} MB)")
    df, stats = clean.read_raw(src, chunksize=args.chunksize, only_states=only)
    log(f"    {stats['raw_rows']:,} raw rows -> {stats['rows_kept']:,} kept "
        f"({stats['blank_rows']:,} blank padding rows dropped)")
    state_names = df.groupby("state_po")["state"].first().to_dict()

    log("2/4 cleaning and building the precinct table")
    df, cand_tab = clean.classify(df, stats)
    p, contests = clean.build_precinct_table(df, cand_tab, stats)
    del df
    log(f"    {stats['precincts']:,} precincts in {stats['contests']:,} contests; "
        f"{stats.get('contests_two_party', 0):,} are two-party contests that can be analysed")

    log("3/4 scoring precincts against their peers")
    p, fits = metrics.analyze(p, z_flag=args.z_flag)
    for m, f in fits.items():
        n_assessed = int((~np.isnan(f["z"])).sum())
        n_flag = int(metrics.is_flagged(f["z"], f["support"], args.z_flag).sum())
        log(f"    {m:6s}: {n_assessed:,} assessed, {n_flag:,} flagged at |z| >= {args.z_flag:g}")

    calib = None
    if not args.skip_calibration:
        log("    calibration (simulating clean elections and injected anomalies)")
        rng = np.random.default_rng(args.seed)
        calib = calibrate.run_calibration(fits, p["state_po"].to_numpy(), rng, log=log)

    log("4/4 writing website data")
    params = {
        "z_flag": args.z_flag,
        "min_peers": config.MIN_PEERS,
        "min_den": config.MIN_DEN,
        "tau_min": config.TAU_MIN,
        "stage": config.STAGE,
        "seed": args.seed,
        "only_states": sorted(only) if only else None,
    }
    export.export_dataset(
        p, contests, state_names, calib, stats, args.out, args.dataset_id, args.label,
        args.short_label, src.name, params, log=log,
    )
    log(f"done. Output is in {Path(args.out) / args.dataset_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
