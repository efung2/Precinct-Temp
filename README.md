# Precinct Review

Screens precinct-level election results for results that are unusual **compared with comparable precincts**, and publishes them as a static website for human review. A flag is a lead to check against official records, never a finding of fraud.

```
raw/                 your MEDSL CSVs (not committed)
pipeline/            Python: clean -> score -> calibrate -> export JSON
docs/                the website (served by GitHub Pages)
docs/data/           pipeline output; the site reads only this
tests/               tests (run: python tests/test_pipeline.py)
```

## 1. One-time setup

Needs Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python tests/test_pipeline.py      # should print three PASS lines
```

## 2. Run it on the 2018 House file

Put the full MEDSL file in `raw/` (any file name), then:

```bash
python pipeline/run_pipeline.py \
  --input raw/HOUSE_precinct_general.csv \
  --dataset-id house-2018 \
  --label "U.S. House of Representatives - Precinct Level 2018" \
  --short-label "House 2018"
```

Expect about 1 to 2 minutes and a few GB of RAM for ~150 MB. Add `--only-states AL,TX` for a quick trial. Output goes to `docs/data/house-2018/` (about 10 to 20 MB of JSON in many small files; the site loads one state at a time).

The pipeline drops blank comma-only padding lines, so a spreadsheet-exported file works as is.

## 3. Look at it locally

Browsers block `fetch` from `file://`, so serve the folder:

```bash
python -m http.server 8000 --directory docs
```

Open http://localhost:8000.

## 4. Publish on GitHub Pages (free)

1. Remove the demo data: `rm -rf docs/data/demo-synthetic`, then delete its entry in `docs/data/datasets.json`. (Re-running the pipeline for `house-2018` after deleting `docs/data` also does it.)
2. Create a GitHub repository, then:
   ```bash
   git init && git add . && git commit -m "Precinct Review"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. In the repo: **Settings -> Pages -> Build and deployment -> Source: Deploy from a branch -> Branch `main`, folder `/docs`**.
4. Your site appears at `https://<you>.github.io/<repo>/` in a minute or two.

`raw/` is git-ignored, so the big CSV is never uploaded (GitHub rejects files over 100 MB). Only the small JSON output is.

Alternatives: Netlify, Cloudflare Pages or Vercel, each pointed at the `docs` folder, also free.

## 5. Adding more datasets later

Run the pipeline again with a new `--dataset-id` and `--label`. Each run adds a tab; existing tabs are kept.

## Settings that change what gets flagged

All in `pipeline/config.py`: peer-group minimum (`MIN_PEERS`), flag threshold (`Z_FLAG`), the isolation rule (`SUPPORT_MAX`), and small-precinct cutoffs. After changing one, re-run; the Reliability page shows its new false-alarm rate.

## Known limits (also on the site)

- Contests with fewer than 50 comparable precincts are not assessed at all. This is deliberate: simulations showed spread estimates from small peer groups are too unreliable. Small or heavily split districts will therefore show "not assessed".
- No turnout metric: the MEDSL file has no registered-voter counts.
- The Reliability simulation uses the detector's own model, so it tests calibration, not realism.
- Precinct names are not stable across years, so there is no year-over-year comparison.
- Only the 2018 House file's layout has been tested on real rows (the 135 Alabama rows); other states' mode and naming quirks are covered by synthetic tests only. Skim the "data quality" counts on the Method page after each run.
