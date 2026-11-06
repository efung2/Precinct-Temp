# Precinct Review

Screens precinct-level election results for precincts that are unusual **compared with comparable precincts**, then runs every screened precinct through independent cross-checks. Only precincts that survive every check are listed for human review. A listed precinct is a lead to check against official records, never a finding of fraud.

```
raw/                 your MEDSL CSVs (not committed to GitHub)
pipeline/            Python: clean -> score -> cross-check -> calibrate -> export JSON
docs/                the website (served by GitHub Pages)
docs/data/           pipeline output; the site reads only this
tests/               tests (run: python3 tests/test_pipeline.py)
```

## What the pipeline does

| Step | Method | Raises or rules out |
|---|---|---|
| Peer z-score | precinct vs. comparable precincts in the same race, county and local election authority | raises (screen) |
| Hierarchical Bayesian model | beta-binomial with partial pooling county -> district -> dataset; posterior predictive p-values, calibrated by simulation | raises (screen); works for small counties |
| Multiple testing | Benjamini-Hochberg q-values on the hierarchical p-values | rules out (`T`) |
| Empirical null | Efron's central-matching empirical null, tail-area FDR | rules out (`E`) |
| Clusters | more similar precincts nearby than chance allows | rules out (`C`) |
| Cross-race | same precinct, another race on the same ballot (needs a companion file) | rules out (`L`) when both races show the same pattern; raises a gap signal (`X`) when they differ |

Signals: vote share (`S`), under-votes (`U`), voting mode (`M`), cross-race gap (`X`). Settings are in `pipeline/config.py`.

## 1. One-time setup

Needs Python 3.10+. Use the command `python3` (plain `python` is Python 2 on some machines).

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # numpy, pandas, scipy
python3 tests/test_pipeline.py     # should print five PASS lines
```

If you already had a `.venv` from before, run the `pip install` line again: SciPy is new.

## 2. Process the data

Put the MEDSL files in `raw/`. Each run makes one tab on the site.

**House 2018** (add the 2018 Senate file as `--companion` once you have it):

```bash
python3 pipeline/run_pipeline.py \
  --input raw/HOUSE_precinct_general.csv \
  --companion raw/SENATE_precinct_general_2018.csv \
  --dataset-id house-2018 \
  --label "U.S. House of Representatives - Precinct Level 2018" \
  --short-label "House 2018"
```

**Senate 2020** (companion: the 2020 House file):

```bash
python3 pipeline/run_pipeline.py \
  --input raw/2020-SENATE-precinct-general.csv \
  --companion raw/2020-HOUSE-precinct-general.csv \
  --dataset-id senate-2020 \
  --label "U.S. Senate - Precinct Level 2020" \
  --short-label "Senate 2020"
```

Use your actual file names. Leave out the `--companion` line if you don't have that file yet: everything except the cross-race check still runs. The companion must be **the same election year** (it compares two races on the same ballot).

Each run takes about 1-2 minutes and writes `docs/data/<dataset-id>/`. Add `--only-states AL,GA` for a quick trial; `--skip-calibration` skips the simulations (faster, but the Reliability page is then empty).

Where to get the companion files: [MIT Election Lab data page](https://electionlab.mit.edu/data) -> precinct-level returns for U.S. Senate 2018 and U.S. House 2020.

## 3. Look at it locally

```bash
python3 -m http.server 8000 --directory docs
```

Open http://localhost:8000. Stop the server with Ctrl+C (not Ctrl+Z).

## 4. Update the live site on GitHub

From the project folder, after copying in the new files:

```bash
git status                      # check: no raw/ CSVs or .venv/ listed
git add -A
git commit -m "Add hierarchical model, FDR checks, cross-race comparison, Senate 2020"
git push
```

GitHub Pages refreshes within a couple of minutes. Re-run the pipeline and repeat these three commands whenever the data or settings change.

## Known limits (also on the site)

- Clean elections for calibration are simulated from the pipeline's own model: this checks each step is calibrated, not that real precincts behave like the model.
- Single-precinct shifts are hard to detect (about 20% for a 30-point shift). A short list is not proof that nothing happened.
- Races with fewer than 10 assessable precincts are not assessed. Contests with only one major party, or two candidates from the same party, are skipped.
- Precinct names must match across the two files for the cross-race check; the site reports the match rate. Precincts split between districts are skipped.
- No turnout metric (no registration counts in MEDSL files). eforensics, census covariates and spatial checks are not included yet.
