"""Step 4: write the files the website reads.

docs/data/
    datasets.json                  index of datasets (one tab per entry)
    <dataset-id>/
        summary.json               state table, review queue, data-quality counts
        calibration.json           false-positive rates + detection power
        states/<ST>.json           every precinct of every analysable contest in that state

Per-state files use a compact "fields + rows" layout so large states stay small.
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
from clean import CONTEST

FIELDS = [
    "precinct", "county", "dem", "rep", "other", "writein", "over", "under",
    "share", "share_peer", "share_z",
    "under_rate", "under_peer", "under_z",
    "mode_share", "mode_peer", "mode_z",
    "flags", "dq",
]
METRICS = ("share", "under", "mode")


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    raise TypeError(f"not serialisable: {type(o)}")


def _dump(obj, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), default=_json_default, allow_nan=False)
    return path.stat().st_size


def _rows(g):
    df = pd.DataFrame(
        {
            "precinct": g["precinct"].to_numpy(),
            "county": g["county_name"].to_numpy(),
            "dem": g["dem"].to_numpy(),
            "rep": g["rep"].to_numpy(),
            "other": g["other"].to_numpy(),
            "writein": g["writein"].to_numpy(),
            "over": g["over"].to_numpy(),
            "under": g["under"].to_numpy(),
            "share": g["share_p"].round(4).to_numpy(),
            "share_peer": g["share_peer"].round(4).to_numpy(),
            "share_z": g["share_z"].round(2).to_numpy(),
            "under_rate": g["under_p"].round(4).to_numpy(),
            "under_peer": g["under_peer"].round(4).to_numpy(),
            "under_z": g["under_z"].round(2).to_numpy(),
            "mode_share": g["mode_p"].round(4).to_numpy(),
            "mode_peer": g["mode_peer"].round(4).to_numpy(),
            "mode_z": g["mode_z"].round(2).to_numpy(),
            "flags": g["flags"].to_numpy(),
            "dq": g["dq"].to_numpy(),
        }
    )[FIELDS]
    return df.astype(object).where(df.notna(), None).values.tolist()


def _district_label(d, office, multi_office):
    base = "At-large" if d == "0" else f"District {d}"
    if multi_office:
        return f"{office.title()} - {base}"
    return base


def _contest_key(office, d, special, multi_office):
    key = f"{d}{'-special' if special else ''}"
    if multi_office:
        key = f"{office.lower().replace(' ', '_')}-{key}"
    return key


def _sort_key(row):
    d = row["district"]
    return (row["office"], int(d) if str(d).isdigit() else 10**6, row["special"])


def export_dataset(p, contests, state_names, calib, stats, out_root, dataset_id, label,
                   short_label, source_name, params, log=print):
    out = Path(out_root) / dataset_id
    if (out / "states").exists():
        shutil.rmtree(out / "states")
    out.mkdir(parents=True, exist_ok=True)

    multi_office = p["office"].nunique() > 1
    expected = calib.get("expected_by_state", {}) if calib else {}
    groups = {k: g for k, g in p.groupby(CONTEST, sort=False)}

    state_rows = []
    queue_parts = []
    biggest = (0, "")
    for st in sorted(contests["state_po"].unique()):
        sc = contests[contests["state_po"] == st].copy()
        order = sorted(sc.to_dict("records"), key=_sort_key)
        contests_json = {}
        agg = {
            "precincts": 0,
            "assessed": dict.fromkeys(METRICS, 0),
            "flagged": {**dict.fromkeys(METRICS, 0), "any": 0},
            "skipped": {},
            "analyzed_contests": 0,
        }
        for c in order:
            key = _contest_key(c["office"], c["district"], c["special"], multi_office)
            g = groups[(st, c["office"], c["district"], c["special"])]
            meta = {
                "label": _district_label(c["district"], c["office"], multi_office)
                + (" (special)" if c["special"] else ""),
                "status": c["status"],
                "dem": c["dem_name"],
                "rep": c["rep_name"],
                "n_precincts": int(len(g)),
            }
            agg["precincts"] += len(g)
            if c["status"] != "two_party":
                meta["reason"] = {
                    "single_major_party": "Only one major party is on the ballot, so there is nothing to compare.",
                    "multi_major": "More than one candidate from the same major party (top-two or runoff style race); two-party comparison does not apply.",
                }[c["status"]]
                agg["skipped"][c["status"]] = agg["skipped"].get(c["status"], 0) + 1
                contests_json[key] = meta
                continue

            agg["analyzed_contests"] += 1
            assessed = {m: int(g[f"{m}_z"].notna().sum()) for m in METRICS}
            flagged = {m: int(g[f"flag_{m}"].sum()) for m in METRICS}
            flagged["any"] = int((g["n_flags"] > 0).sum())
            for m in METRICS:
                agg["assessed"][m] += assessed[m]
                agg["flagged"][m] += flagged[m]
            agg["flagged"]["any"] += flagged["any"]
            meta.update(
                assessed=assessed,
                flagged=flagged,
                levels={
                    "county": int((g["share_level"] == 1).sum()),
                    "district": int((g["share_level"] == 2).sum()),
                },
                under_reported=bool(g["under_reported"].iloc[0]),
                mode_split=int(g["mode_split"].sum()),
                rows=_rows(g),
            )
            contests_json[key] = meta

            hit = g[g["n_flags"] > 0]
            if len(hit):
                q = pd.DataFrame(
                    {
                        "state": st,
                        "contest": key,
                        "contest_label": meta["label"],
                        "precinct": hit["precinct"].to_numpy(),
                        "county": hit["county_name"].to_numpy(),
                        "flags": hit["flags"].to_numpy(),
                        "n_flags": hit["n_flags"].to_numpy(),
                        "max_abs_z": hit["max_abs_z"].round(2).to_numpy(),
                        "two_party": hit["two_party"].to_numpy(),
                        "share": hit["share_p"].round(4).to_numpy(),
                        "share_peer": hit["share_peer"].round(4).to_numpy(),
                    }
                )
                queue_parts.append(q)

        payload = {
            "state_po": st,
            "state": state_names.get(st, st),
            "dataset": dataset_id,
            "fields": FIELDS,
            "contests": contests_json,
        }
        size = _dump(payload, out / "states" / f"{st}.json")
        if size > biggest[0]:
            biggest = (size, st)

        state_rows.append(
            {
                "code": st,
                "name": state_names.get(st, st),
                "contests": len(order),
                "analyzed_contests": agg["analyzed_contests"],
                "skipped": agg["skipped"],
                "precincts": agg["precincts"],
                "assessed": agg["assessed"],
                "flagged": agg["flagged"],
                "expected": {m: round(expected.get(m, {}).get(st, 0.0), 2) for m in METRICS},
                "file_kb": round(size / 1024, 1),
            }
        )

    queue = []
    if queue_parts:
        q = pd.concat(queue_parts, ignore_index=True)
        q = q.sort_values(["n_flags", "max_abs_z"], ascending=[False, False]).head(config.QUEUE_SIZE)
        queue = q.astype(object).where(q.notna(), None).to_dict("records")

    tot = lambda f: int(sum(f(s) for s in state_rows))  # noqa: E731
    summary = {
        "id": dataset_id,
        "label": label,
        "source_file": source_name,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": params,
        "totals": {
            "states": len(state_rows),
            "contests": tot(lambda s: s["contests"]),
            "analyzed_contests": tot(lambda s: s["analyzed_contests"]),
            "precincts": tot(lambda s: s["precincts"]),
            "assessed": {m: tot(lambda s, m=m: s["assessed"][m]) for m in METRICS},
            "flagged": {m: tot(lambda s, m=m: s["flagged"][m]) for m in (*METRICS, "any")},
            "expected": {m: round(float(sum(s["expected"][m] for s in state_rows)), 1) for m in METRICS},
        },
        "data_quality": {k: int(v) for k, v in stats.items()},
        "states": state_rows,
        "queue": queue,
    }
    _dump(summary, out / "summary.json")
    _dump(calib or {}, out / "calibration.json")

    # ---- dataset index (one tab per dataset)
    index_path = Path(out_root) / "datasets.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"datasets": []}
    entry = {
        "id": dataset_id,
        "label": label,
        "short": short_label or label,
        "path": f"data/{dataset_id}",
        "generated": summary["generated"],
        "precincts": summary["totals"]["precincts"],
        "states": summary["totals"]["states"],
    }
    index["datasets"] = [d for d in index["datasets"] if d["id"] != dataset_id] + [entry]
    index["datasets"].sort(key=lambda d: d["label"])
    _dump(index, index_path)

    log(f"    wrote {len(state_rows)} state file(s); largest is {biggest[1]} at {biggest[0] / 1e6:.2f} MB")
    return summary
