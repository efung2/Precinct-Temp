"""Every setting that affects which precincts get flagged lives here.

Change a value, re-run the pipeline, and the calibration report on the site
updates to show what that choice does to the false-positive rate.
"""

# ---- Which raw rows to keep -------------------------------------------------
STAGE = "GEN"  # MEDSL "stage" column: GEN = general election

# ---- Peer comparison --------------------------------------------------------
# A precinct is compared with the other precincts in its own county (same
# contest). If the county has fewer than MIN_PEERS assessable precincts, the
# comparison widens to the whole district. If that is still too few, the
# precinct is simply not assessed (never guessed at).
# Why 50: simulations showed that spread estimates from small peer groups are
# noisy enough to produce heavy-tailed scores (about 80x too many extreme
# scores at 10 peers, close to nominal from about 60 peers).
MIN_PEERS = 50

# "Isolated outlier" rule. A precinct that is statistically far from its peers
# is only flagged if it is NOT part of a cluster of similar precincts: at most
# SUPPORT_MAX other peers may lie within SUPPORT_RADIUS_SD local standard
# deviations of it. Clusters of similar precincts (campus precincts, a
# segregated neighbourhood, a retirement community) are real heterogeneity, not
# anomalies. Precincts that would have been flagged but fail this rule are not
# hidden: they get dq code "C" and are counted separately on the site.
SUPPORT_MAX = 2
SUPPORT_RADIUS_SD = 1.0

# A precinct needs at least this many votes in the denominator of a metric
# (two-party votes for share, ballots for undervote rate, ...) to be assessed.
MIN_DEN = 20

# |z| at or above this value puts a precinct on the review list.
Z_FLAG = 4.0
Z_THRESHOLDS_REPORTED = (3.0, 3.5, 4.0, 4.5, 5.0)

# Floor on the between-precinct spread (absolute units, 0-1 scale). Stops the
# model from becoming over-confident in very uniform peer groups.
TAU_MIN = {"share": 0.03, "under": 0.004, "mode": 0.03}

# ---- Data-quality checks ----------------------------------------------------
# Two precincts with identical vote vectors are only noted if they are at least
# this big (tiny precincts match by chance all the time).
DUP_MIN_VOTES = 50

# Precinct names that are not real places (absentee pools, county totals, ...).
# They are kept in the output but excluded from peer comparison.
NON_GEO_PATTERNS = [
    r"\b(?:absentee|provisional|uocava|overseas|county\s+totals?|central\s+count|"
    r"affidavit|unallocated|unassigned|not\s+assigned|one[\s\-]*stop|"
    r"early\s+vot(?:e|ing)|mail(?:[\s\-]*in)?\s+ballots?|total\s+votes)\b",
    r"^(?:early|mail|federal|military|statewide|totals?|in[\s\-]*person|cvr)$",
]

# ---- Calibration ------------------------------------------------------------
SHIFTS = (0.05, 0.10, 0.15, 0.20, 0.30)  # injected change in two-party share
SIZE_BUCKETS = (  # (low, high, label) on two-party votes
    (0, 100, "under 100"),
    (100, 300, "100-299"),
    (300, 1000, "300-999"),
    (1000, 10**12, "1,000+"),
)
CALIB_TARGET_DRAWS = 400_000
CALIB_MIN_REPS = 8
CALIB_MAX_REPS = 60
SEED = 20181106

# ---- Export -----------------------------------------------------------------
QUEUE_SIZE = 300  # length of the national review queue shown on the site
