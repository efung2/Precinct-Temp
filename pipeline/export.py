"""Step 4: write the files the website reads.

docs/data/
    datasets.json                  index of datasets (one tab per entry)
    <dataset-id>/
        summary.json               totals, review funnel, state table, review queue, data-quality counts
        calibration.json           false-alarm rates + detection power (peer z and full chain)
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
from checks import SIGNALS, funnel
from clean import CONTEST

FIELDS = [
    "precinct", "county", "dem", "rep", "other", "writein", "over", "under",
    "share", "share_peer", "share_z", "share_q", "share_enq", "share_v",
    "under_rate", "under_peer", "under_z", "under_q", "under_enq", "under_v",
    "mode_share", "mode_peer", "mode_z", "mode_q", "mode_enq", "mode_v",
    "comp_share", "cross_gap", "cross_peer", "cross_z", "cross_q", "cross_enq", "cross_v", "cross_status",
    "flags", "screened", "remain", "dq",
]
METRICS = ("share", "under", "mode")
CROSS_CODE = {"matched": "M", "matched_by_name": "M", "split": "S", "unmatched": "U", "n/a": ""}


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    raise TypeError(f"not serialisable: {type(o)}")


def _clean(o):
    """Replace NaN/inf anywhere in a nested structure with None (strict JSON)."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float) and not np.isfinite(o):
        return None
    if isinstance(o, np.floating) and not np.isfinite(o):
        return None
    return o


def _dump(obj, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_clean(obj), f, separators=(",", ":"), default=_json_default, allow_nan=False)
    return path.stat().st_size


def _sig(x, d=3):
    """Round small probabilities to d significant digits (keeps q = 1e-9 readable)."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    ok = np.isfinite(x) & (x > 0)
    if ok.any():
        mag = np.floor(np.log10(x[ok]))
        f = 10 ** (d - 1 - mag)
        out[ok] = np.round(x[ok] * f) / f
    out[np.isfinite(x) & (x <= 0)] = 0.0
    return out


def _rows(g):
    df = pd.DataFrame(
        {
            "precinct": g["precinct"].to_numpy(),
            "county": g["county_name"].to_numpy(),
            "dem": g["dem"].to_numpy(), "rep": g["rep"].to_numpy(), "other": g["other"].to_numpy(),
            "writein": g["writein"].to_numpy(), "over": g["over"].to_numpy(), "under": g["under"].to_numpy(),
            "share": g["share_p"].round(4).to_numpy(),
            "share_peer": g["share_peer"].round(4).to_numpy(),
            "share_z": g["share_z"].round(2).to_numpy(),
            "share_q": _sig(g["share_q"]), "share_enq": g["share_enq"].round(3).to_numpy(),
            "share_v": g["share_v"].to_numpy(),
            "under_rate": g["under_p"].round(4).to_numpy(),
            "under_peer": g["under_peer"].round(4).to_numpy(),
            "under_z": g["under_z"].round(2).to_numpy(),
            "under_q": _sig(g["under_q"]), "under_enq": g["under_enq"].round(3).to_numpy(),
            "under_v": g["under_v"].to_numpy(),
            "mode_share": g["mode_p"].round(4).to_numpy(),
            "mode_peer": g["mode_peer"].round(4).to_numpy(),
            "mode_z": g["mode_z"].round(2).to_numpy(),
            "mode_q": _sig(g["mode_q"]), "mode_enq": g["mode_enq"].round(3).to_numpy(),
            "mode_v": g["mode_v"].to_numpy(),
            "comp_share": pd.to_numeric(g["comp_share"], errors="coerce").round(4).to_numpy(),
            "cross_gap": pd.to_numeric(g["cross_gap"], errors="coerce").round(4).to_numpy(),
            "cross_peer": pd.to_numeric(g["cross_peer"], errors="coerce").round(4).to_numpy(),
            "cross_z": pd.to_numeric(g["cross_z"], errors="coerce").round(2).to_numpy(),
            "cross_q": _sig(pd.to_numeric(g["cross_q"], errors="coerce")),
            "cross_enq": pd.to_numeric(g["cross_enq"], errors="coerce").round(3).to_numpy(),
            "cross_v": g["cross_v"].to_numpy(),
            "cross_status": g["cross_status"].map(CROSS_CODE).fillna("").to_numpy(),
            "flags": g["flags"].to_numpy(),
            "screened": g["screened"].to_numpy(),
            "remain": g["remain"].to_numpy(),
            "dq": g["dq"].to_numpy(),
        }
    )[FIELDS]
    return df.astype(object).where(df.notna(), None).values.tolist()


def _district_label(d, office, multi_office):
    base = "Statewide" if d == "STATEWIDE" else ("At-large" if d == "0" else f"District {d}")
    if multi_office:
        return f"{office.title()} - {base}"
    return base


def _contest_key(office, d, special, multi_office):
    key = f"{str(d).lower()}{'-special' if special else ''}"
    if multi_office:
        key = f"{office.lower().replace(' ', '_')}-{key}"
    return key


def _sort_key(row):
    d = row["district"]
    return (row["office"], int(d) if str(d).isdigit() else 10**6, row["special"])


def _funnels(g, has_cross):
    out = {m: funnel(g[f"{m}_v"].to_numpy(dtype=object), has_cross and m == "share") for m in METRICS}
    if has_cross:
        out["cross"] = funnel(g["cross_v"].to_numpy(dtype=object), False)
    scr = g["screened"].to_numpy(dtype=object) != ""
    out["any"] = {"screened": int(scr.sum()), "persist": int((g["remain"].to_numpy(dtype=object) != "").sum())}
    return out


def _add(acc, f):
    for k, v in f.items():
        if isinstance(v, dict):
            acc.setdefault(k, {})
            _add(acc[k], v)
        elif v is not None:
            acc[k] = acc.get(k, 0) + v
    return acc


def export_dataset(p, contests, state_names, calib, stats, out_root, dataset_id, label,
                   short_label, source_name, params, cross_info=None, enull_info=None, log=print):
    out = Path(out_root) / dataset_id
    if (out / "states").exists():
        shutil.rmtree(out / "states")
    out.mkdir(parents=True, exist_ok=True)

    has_cross = cross_info is not None
    multi_office = p["office"].nunique() > 1
    expected = calib.get("expected_by_state", {}) if calib else {}
    groups = {k: g for k, g in p.groupby(CONTEST, sort=False)}

    state_rows = []
    queue_parts = []
    biggest = (0, "")
    funnel_total = {}
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
            "funnel": {},
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
            hb_assessed = {m: int(g[f"{m}_q"].notna().sum()) for m in METRICS}
            flagged = {m: int(g[f"flag_{m}"].sum()) for m in METRICS}
            flagged["any"] = int((g["n_flags"] > 0).sum())
            for m in METRICS:
                agg["assessed"][m] += assessed[m]
                agg["flagged"][m] += flagged[m]
            agg["flagged"]["any"] += flagged["any"]
            fn = _funnels(g, has_cross)
            _add(agg["funnel"], fn)
            cs = g["cross_status"].value_counts().to_dict()
            meta.update(
                assessed=assessed,
                hb_assessed=hb_assessed,
                flagged=flagged,
                funnel=fn,
                levels={
                    "county": int((g["share_level"] == 1).sum()),
                    "district": int((g["share_level"] == 2).sum()),
                },
                cross={
                    "matched": int(cs.get("matched", 0) + cs.get("matched_by_name", 0)),
                    "split": int(cs.get("split", 0)),
                    "unmatched": int(cs.get("unmatched", 0)),
                    "assessed": int(pd.to_numeric(g["cross_z"], errors="coerce").notna().sum()),
                    "companion": sorted({str(x).split("|")[0].title() + " " + str(x).split("|")[1]
                                         for x in g["comp_contest"].dropna().unique()}) if "comp_contest" in g else [],
                } if has_cross else None,
                under_reported=bool(g["under_reported"].iloc[0]),
                mode_split=int(g["mode_split"].sum()),
                rows=_rows(g),
            )
            contests_json[key] = meta

            hit = g[g["screened"] != ""]
            if len(hit):
                ev = np.nanmax(np.vstack([np.abs(pd.to_numeric(hit[c_], errors="coerce").to_numpy(float))
                                          for c_ in ("share_z", "under_z", "mode_z", "cross_z",
                                                     "share_hbz", "under_hbz", "mode_hbz")]), axis=0)
                q = pd.DataFrame(
                    {
                        "state": st,
                        "contest": key,
                        "contest_label": meta["label"],
                        "precinct": hit["precinct"].to_numpy(),
                        "county": hit["county_name"].to_numpy(),
                        "screened": hit["screened"].to_numpy(),
                        "remain": hit["remain"].to_numpy(),
                        "share_v": hit["share_v"].to_numpy(),
                        "under_v": hit["under_v"].to_numpy(),
                        "mode_v": hit["mode_v"].to_numpy(),
                        "cross_v": hit["cross_v"].to_numpy(),
                        "evidence": np.round(np.nan_to_num(ev, nan=0.0), 2),
                        "two_party": hit["two_party"].to_numpy(),
                        "share": hit["share_p"].round(4).to_numpy(),
                        "share_peer": hit["share_peer"].round(4).to_numpy(),
                        "comp_share": pd.to_numeric(hit["comp_share"], errors="coerce").round(4).to_numpy(),
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
        _add(funnel_total, agg["funnel"])

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
                "funnel": agg["funnel"],
                "expected": {m: round(expected.get(m, {}).get(st, 0.0), 2) for m in METRICS},
                "file_kb": round(size / 1024, 1),
            }
        )

    queue = []
    if queue_parts:
        q = pd.concat(queue_parts, ignore_index=True)
        q["persists"] = q["remain"] != ""
        q = q.sort_values(["persists", "evidence"], ascending=[False, False])
        n_keep = max(config.QUEUE_SIZE, int(q["persists"].sum()))
        q = q.head(min(n_keep, config.QUEUE_MAX)).drop(columns=["persists"])
        queue = q.astype(object).where(q.notna(), None).to_dict("records")

    tot = lambda f: int(sum(f(s) for s in state_rows))  # noqa: E731
    chain = (calib or {}).get("chain", {})
    summary = {
        "id": dataset_id,
        "label": label,
        "source_file": source_name,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": params,
        "has_cross": has_cross,
        "cross": cross_info,
        "enull_info": enull_info,
        "totals": {
            "states": len(state_rows),
            "contests": tot(lambda s: s["contests"]),
            "analyzed_contests": tot(lambda s: s["analyzed_contests"]),
            "precincts": tot(lambda s: s["precincts"]),
            "assessed": {m: tot(lambda s, m=m: s["assessed"][m]) for m in METRICS},
            "hb_assessed": {m: int(p[f"{m}_q"].notna().sum()) for m in METRICS},
            "flagged": {m: tot(lambda s, m=m: s["flagged"][m]) for m in (*METRICS, "any")},
            "expected": {m: round(float(sum(s["expected"][m] for s in state_rows)), 1) for m in METRICS},
            "funnel": funnel_total,
            "chain_expected": {m: {"screened": v.get("expected_screened"), "persist": v.get("expected_persist")}
                               for m, v in chain.get("signals", {}).items()},
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
        "has_cross": has_cross,
    }
    index["datasets"] = [d for d in index["datasets"] if d["id"] != dataset_id] + [entry]
    index["datasets"].sort(key=lambda d: d["label"])
    _dump(index, index_path)

    log(f"    wrote {len(state_rows)} state file(s); largest is {biggest[1]} at {biggest[0] / 1e6:.2f} MB")
    return summary
