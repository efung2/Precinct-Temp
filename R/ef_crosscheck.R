# Cross-check the Python eforensics re-implementation with the ORIGINAL R package (JAGS).
#
# Usage (from the election-anomaly folder, after alarm_convert.py has made raw/alarm-units-2020-vtd.csv):
#     Rscript R/ef_crosscheck.R DE            # one state; small states (DE, AK, RI, NH) are quickest
#
# Writes raw/eforensics-R/<STATE>.csv with each district's posterior probability of a fraud class
# and prints a summary to compare with the Python results on the website (eforensics tab, By state).
#
# Notes
#  * The package's argument conventions (counts vs. proportions for w and a, parameter names) are
#    taken from its README and may differ between versions. If eforensics() complains, run
#    help(eforensics) and adjust the lines marked  # <- check
#  * JAGS runs are slow: expect minutes for a few hundred districts and much longer for big states.

suppressPackageStartupMessages({
  library(eforensics)
  library(dplyr)
})

args  <- commandArgs(trailingOnly = TRUE)
state <- if (length(args) >= 1) toupper(args[1]) else "DE"
units_file <- "raw/alarm-units-2020-vtd.csv"
if (!file.exists(units_file)) stop("Run pipeline/alarm_convert.py first (see README).")

u <- read.csv(units_file, colClasses = c(county_fips = "character", geoid = "character")) %>%
  filter(state_po == state)
if (nrow(u) == 0) stop(paste("No districts for", state))

# leader = statewide winner (same rule as the Python version)
leader_dem <- sum(u$sen_dem) >= sum(u$sen_rep)
u <- u %>% mutate(
  N = vap,
  W = if (leader_dem) sen_dem else sen_rep,
  V = sen_dem + sen_rep,
  A = N - V,
  w = W / N,          # leader votes as a share of eligible voters  # <- check
  a = A / N           # abstentions as a share of eligible voters   # <- check
)
cat(sprintf("%s: %d districts, leader = %s\n", state, nrow(u), ifelse(leader_dem, "Democrat", "Republican")))

mcmc <- list(burn.in = 2000, n.adapt = 1000, n.iter = 2000, n.chains = 2)

fit <- eforensics(
  w ~ 1,
  a ~ 1,
  mu.iota.m ~ 1,
  mu.iota.s ~ 1,
  mu.chi.m  ~ 1,
  mu.chi.s  ~ 1,
  data = u,
  elegible.voters = "N",            # <- check (the package spells it this way)
  model = "qbl",
  mcmc = mcmc,
  get.dic = 0,
  parameters = "all"
)

print(summary(fit))

# unit-level fraud class: average over draws of Z (1 = no fraud, 2 = incremental, 3 = extreme)
draws <- as.matrix(do.call(rbind, lapply(fit, as.matrix)))
zcols <- grep("^Z\\[", colnames(draws))
if (length(zcols) == 0) {
  warning("No Z columns in the output; check help(eforensics) for the parameter name of the fraud class.")
} else {
  z <- draws[, zcols, drop = FALSE]
  out <- data.frame(precinct = u$precinct, county = u$county_name,
                    p_incremental = colMeans(z == 2), p_extreme = colMeans(z == 3))
  out$p_fraud <- out$p_incremental + out$p_extreme
  dir.create("raw/eforensics-R", showWarnings = FALSE)
  write.csv(out, file.path("raw/eforensics-R", paste0(state, ".csv")), row.names = FALSE)
  cat(sprintf("R eforensics: P(fraud) > 0.5 in %d of %d districts (%.1f%%); > 0.9 in %d\n",
              sum(out$p_fraud > 0.5), nrow(out), 100 * mean(out$p_fraud > 0.5), sum(out$p_fraud > 0.9)))
  cat("Compare with the website: eforensics tab -> By state -> eforensics > 0.5 for", state, "\n")
}
