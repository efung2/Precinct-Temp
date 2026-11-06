#!/usr/bin/env python3
"""Add (or refresh) a dataset folder's entry in docs/data/datasets.json without re-running the pipeline.

Use this when a dataset folder was copied in from elsewhere, e.g.
    python3 pipeline/register_dataset.py docs/data/senate-2020-vtd --short-label "Senate 2020 (census districts)"
"""
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="dataset folder containing summary.json")
    ap.add_argument("--short-label", default=None)
    a = ap.parse_args(argv)
    folder = Path(a.folder)
    s = json.loads((folder / "summary.json").read_text())
    index_path = folder.parent / "datasets.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"datasets": []}
    entry = {"id": s["id"], "label": s["label"], "short": a.short_label or s["label"], "path": f"data/{s['id']}",
             "generated": s["generated"], "precincts": s["totals"]["precincts"], "states": s["totals"]["states"],
             "has_cross": s.get("has_cross", False)}
    index["datasets"] = [d for d in index["datasets"] if d["id"] != s["id"]] + [entry]
    index["datasets"].sort(key=lambda d: d["label"])
    index_path.write_text(json.dumps(index, separators=(",", ":")))
    print(f"registered {s['id']} in {index_path} ({len(index['datasets'])} datasets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
