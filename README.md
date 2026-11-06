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
python3 tests/test_pipeline.py     # should print six PASS lines
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

## 5. eforensics comparison (2020 Senate)

The **eforensics** tab compares our checks with Walter Mebane's eforensics model on the same
2020 Senate data. It needs the ALARM Project's census-2020 files (folder `census-vest-2020`,
one CSV per state) in `raw/`.

```bash
pip install -r requirements-eforensics.txt          # NumPyro + JAX (one time)

# 1. census voting districts -> MEDSL-style files (Senate, President) + eforensics input
python3 pipeline/alarm_convert.py --alarm-dir raw/census-vest-2020 --out-dir raw

# 2. our checks on the same districts (President 2020 = cross-race companion)
python3 pipeline/run_pipeline.py \
  --input raw/alarm-senate-2020-vtd.csv \
  --companion raw/alarm-president-2020-vtd.csv \
  --dataset-id senate-2020-vtd \
  --label "U.S. Senate 2020 - Census voting districts (ALARM/VEST)" \
  --short-label "Senate 2020 (census districts)"

# 3. eforensics on all states + real-count check + head-to-head simulations (about 40 minutes)
python3 pipeline/run_eforensics.py --parts census real sim --resume
```

Step 3 also needs the Senate 2020 tab (`senate-2020`) to exist, for the real-count comparison.
`--resume` reuses finished states from `raw/eforensics-cache/` if a run is interrupted.

### Cross-check with the original R package (optional)

The website uses a Python re-implementation of eforensics. To check it against the authors' R/JAGS code:

1. Install R for macOS from https://cran.r-project.org/bin/macosx/ (pick the Apple-silicon or Intel build to match your Mac).
2. Install JAGS: `brew install jags` (if you use Homebrew), or the JAGS `.pkg` from https://sourceforge.net/projects/mcmc-jags/.
3. In R (open the R app, or type `R` in Terminal):
   ```r
   install.packages(c("devtools", "dplyr"))
   devtools::install_github("DiogoFerrari/eforensics")
   ```
   (The package is also published as `UMeforensics/eforensics_public`.)
4. From the project folder: `Rscript R/ef_crosscheck.R DE`. Small states (DE, AK, RI, NH) take minutes. Compare the printed share of districts with P(fraud) > 0.5 with the website's eforensics tab.

The R package's argument conventions can differ between versions; lines that may need adjusting are marked `# <- check` in the script.

## Data sources and citations

- MIT Election Data and Science Lab (MEDSL). U.S. House precinct-level returns 2018; U.S. Senate precinct-level returns 2018 and 2020. Harvard Dataverse. https://electionlab.mit.edu/data
- Kenny, Christopher T., and Cory McCartan. ALARM Project, census-2020 (2020 Census data by voting district, with election results). CC BY-SA 4.0. https://github.com/alarm-redist/census-2020
- Voting and Election Science Team (VEST). 2020 precinct-level election results. Harvard Dataverse. (Source of the election results in the ALARM files.)
- U.S. Census Bureau. 2020 Census Redistricting Data (P.L. 94-171).
- Mebane, Walter R., Jr. 2019. "eforensics: A Bayesian Implementation of A Positive Empirical Model of Election Frauds." Political Science 485 lecture, University of Michigan; Ferrari, Diogo, Kevin McAlister and Walter R. Mebane Jr. 2018. "Developments in Positive Empirical Models of Election Frauds: Dimensions and Decisions." Summer Meeting of the Political Methodology Society; Klimek, Peter, et al. 2012. "Statistical Detection of Systematic Election Irregularities." PNAS 109(41).

## Known limits (also on the site)

- Clean elections for calibration are simulated from the pipeline's own model: this checks each step is calibrated, not that real precincts behave like the model.
- Single-precinct shifts are hard to detect (about 20% for a 30-point shift). A short list is not proof that nothing happened.
- Races with fewer than 10 assessable precincts are not assessed. Contests with only one major party, or two candidates from the same party, are skipped.
- Precinct names must match across the two files for the cross-race check; the site reports the match rate. Precincts split between districts are skipped.
- No turnout metric in our checks (no registration counts in MEDSL files). Census covariates and spatial checks are not included yet.
- The eforensics comparison uses a Python re-implementation and census voting-age population as "eligible voters" (includes non-citizens). See the eforensics tab for its caveats.
