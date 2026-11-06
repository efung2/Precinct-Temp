"""eforensics "qbl" model (Mebane), re-implemented in Python with NumPyro.

Specification follows W. R. Mebane Jr., "eforensics: A Bayesian Implementation of A Positive
Empirical Model of Election Frauds" (Political Science 485 lecture, Oct 21 2019), and the R
package eforensics (Ferrari, McAlister & Mebane 2018; Mebane 2019):

  observed per unit i:  N_i eligible voters, W_i votes for the leader, A_i = N_i - V_i abstentions
  Z_i in {1 no fraud, 2 incremental fraud, 3 extreme fraud},  Z_i ~ Cat(pi)
  pi~1 ~ U(0,1), pi~2 ~ U(0, pi~1), pi~3 ~ U(0, pi~1), pi_j = pi~j / sum      (pi_1 the largest)
  nu_i  = logistic(beta + kappa_nu_i)          true leader share of valid votes
  tau_i = logistic(gamma + kappa_tau_i)        true turnout
  iota_i^l  = k * logistic(rho_l + kappa_i^l)              l in {M, S}, k = 0.7   (incremental)
  upsilon_i^l = k + (1 - k) * logistic(delta_l + kappa_i^l)                         (extreme)
  kappa ~ Normal(mu_kappa, sigma_kappa),  mu_kappa ~ N(0,1),  sigma_kappa ~ Exp(5)
  E(a_i) = 1 - tau | (1-tau)(1-iota^M) | (1-tau)(1-upsilon^M)
  E(w_i) = nu tau  | nu tau + iota^M (1-tau) + iota^S tau (1-nu) | same with upsilon
  A_i ~ Binomial(N_i, E(a_i)),  W_i ~ Binomial(N_i, E(w_i))

Differences from the R/JAGS package, stated plainly:
  * Intercept-only (no covariates), as in the default qbl call.
  * The constant term and the random-effect mean are one parameter here (they are not
    separately identified when there are no covariates).
  * Z_i is summed out of the likelihood (exact) so that the No-U-Turn sampler can be used;
    the posterior probability of each class is recovered afterwards for every draw.
  * Convergence is judged with split R-hat and effective sample size rather than MCMCSE
    stopping rules. States that do not converge are reported as such.

Per-unit outputs: P(Z=2), P(Z=3), P(fraud) = P(Z>1); fraudulent votes for the leader
F_wi = N_i * sum_j P(Z=j) * (w_ij - nu_i tau_i) (posterior mean, Mebane's "approach two").
"""
import os
import time

# let JAX run several sampling chains in parallel on a multi-core CPU (must be set before JAX loads)
os.environ.setdefault("XLA_FLAGS", "--xla_force_host_platform_device_count=4")

import numpy as np

K = 0.7
COMPONENTS = ("nu", "tau", "iM", "iS", "uM", "uS")


def _lazy():
    import jax
    import jax.numpy as jnp
    import numpyro
    import numpyro.distributions as dist
    from numpyro.infer import MCMC, NUTS, init_to_value
    return jax, jnp, numpyro, dist, MCMC, NUTS, init_to_value


def _probs(jnp, jax, lin):
    nu = jax.nn.sigmoid(lin["nu"])
    tau = jax.nn.sigmoid(lin["tau"])
    iM = K * jax.nn.sigmoid(lin["iM"])
    iS = K * jax.nn.sigmoid(lin["iS"])
    uM = K + (1 - K) * jax.nn.sigmoid(lin["uM"])
    uS = K + (1 - K) * jax.nn.sigmoid(lin["uS"])
    a = jnp.stack([1 - tau, (1 - tau) * (1 - iM), (1 - tau) * (1 - uM)], -1)
    w = jnp.stack([nu * tau,
                   nu * tau + iM * (1 - tau) + iS * tau * (1 - nu),
                   nu * tau + uM * (1 - tau) + uS * tau * (1 - nu)], -1)
    return nu, tau, jnp.clip(a, 1e-7, 1 - 1e-7), jnp.clip(w, 1e-7, 1 - 1e-7)


def _model(N, A, W):
    jax, jnp, numpyro, dist, *_ = _lazy()
    n = N.shape[0]
    t1 = numpyro.sample("pt1", dist.Uniform(0, 1))
    t2 = numpyro.sample("pt2u", dist.Uniform(0, 1)) * t1
    t3 = numpyro.sample("pt3u", dist.Uniform(0, 1)) * t1
    pi = jnp.stack([t1, t2, t3]) / (t1 + t2 + t3)
    numpyro.deterministic("pi", pi)
    lin = {}
    for c in COMPONENTS:
        mu = numpyro.sample(f"mu_{c}", dist.Normal(0, 1))
        sd = numpyro.sample(f"sd_{c}", dist.Exponential(5.0))
        with numpyro.plate(f"units_{c}", n):
            e = numpyro.sample(f"e_{c}", dist.Normal(0, 1))
        lin[c] = mu + sd * e
    _, _, a, w = _probs(jnp, jax, lin)
    ll = dist.Binomial(N[:, None], probs=a).log_prob(A[:, None]) + \
        dist.Binomial(N[:, None], probs=w).log_prob(W[:, None])
    numpyro.factor("lik", jax.scipy.special.logsumexp(jnp.log(pi) + ll, axis=-1).sum())


def fit(N, A, W, seed=0, warmup=400, samples=250, chains=4, max_tree_depth=7):
    """Run NUTS. Returns dict with posterior draws needed downstream and diagnostics."""
    jax, jnp, numpyro, dist, MCMC, NUTS, init_to_value = _lazy()
    from numpyro.diagnostics import summary
    n = len(N)
    # start every chain in the "mostly no fraud" region (pi_1 ~ 0.9); the posterior decides from there
    init = {"pt1": 0.9, "pt2u": 0.06, "pt3u": 0.03}
    for c in COMPONENTS:
        init[f"mu_{c}"] = 0.0
        init[f"sd_{c}"] = 0.3
        init[f"e_{c}"] = np.zeros(n)
    t = time.time()
    mcmc = MCMC(NUTS(_model, target_accept_prob=0.85, max_tree_depth=max_tree_depth,
                     init_strategy=init_to_value(values=init)),
                num_warmup=warmup, num_samples=samples, num_chains=chains,
                chain_method="parallel" if chains <= len(jax.devices()) else "sequential", progress_bar=False)
    mcmc.run(jax.random.PRNGKey(seed), jnp.asarray(N, float), jnp.asarray(A, float), jnp.asarray(W, float))
    s = mcmc.get_samples()
    by_chain = mcmc.get_samples(group_by_chain=True)
    keep = ["pi"] + [f"mu_{c}" for c in COMPONENTS] + [f"sd_{c}" for c in COMPONENTS]
    sm = summary({k: by_chain[k] for k in keep})
    rhat = {k: np.atleast_1d(np.asarray(v["r_hat"])).tolist() for k, v in sm.items()}
    ess = {k: np.atleast_1d(np.asarray(v["n_eff"])).tolist() for k, v in sm.items()}
    div = int(np.asarray(mcmc.get_extra_fields()["diverging"]).sum())
    return {"samples": {k: np.asarray(v) for k, v in s.items()}, "rhat": rhat, "ess": ess,
            "divergences": div, "seconds": round(time.time() - t, 1), "n": n,
            "settings": {"warmup": warmup, "samples": samples, "chains": chains}}


def unit_posteriors(res, N, A, W, chunk=200):
    """Per-unit class probabilities and fraudulent-vote estimates, averaged over posterior draws."""
    jax, jnp, numpyro, dist, *_ = _lazy()
    s = res["samples"]
    D = s["pi"].shape[0]
    n = len(N)
    Nj, Aj, Wj = (jnp.asarray(x, float) for x in (N, A, W))
    p2 = np.zeros(n)
    p3 = np.zeros(n)
    fw = np.zeros(n)
    ft = np.zeros(n)
    fw_tot = np.zeros(D)
    for lo in range(0, D, chunk):
        hi = min(D, lo + chunk)
        lin = {c: s[f"mu_{c}"][lo:hi, None] + s[f"sd_{c}"][lo:hi, None] * s[f"e_{c}"][lo:hi] for c in COMPONENTS}
        lin = {c: jnp.asarray(v) for c, v in lin.items()}
        nu, tau, a, w = _probs(jnp, jax, lin)
        ll = dist.Binomial(Nj[None, :, None], probs=a).log_prob(Aj[None, :, None]) + \
            dist.Binomial(Nj[None, :, None], probs=w).log_prob(Wj[None, :, None])
        lp = jnp.log(jnp.asarray(s["pi"][lo:hi]))[:, None, :] + ll
        r = jnp.exp(lp - jax.scipy.special.logsumexp(lp, axis=-1, keepdims=True))  # draws x units x 3
        base_w = nu * tau
        base_t = tau
        t_cls = 1 - a  # turnout implied by each class
        dw = (w - base_w[..., None]) * r
        dt = (t_cls - base_t[..., None]) * r
        p2 += np.asarray(r[..., 1].sum(0))
        p3 += np.asarray(r[..., 2].sum(0))
        fw_draw = np.asarray((dw[..., 1:].sum(-1)) * Nj[None, :])
        fw += fw_draw.sum(0)
        ft += np.asarray(((dt[..., 1:].sum(-1)) * Nj[None, :]).sum(0))
        fw_tot[lo:hi] = fw_draw.sum(1)
    return {"p2": p2 / D, "p3": p3 / D, "pfraud": (p2 + p3) / D, "fraud_votes": fw / D,
            "fraud_turnout": ft / D, "fraud_votes_total_draws": fw_tot}


def converged(res, rhat_max=1.1):
    r = res["rhat"]
    vals = [x for x in r["pi"] if np.isfinite(x)]
    return bool(vals) and max(vals) <= rhat_max


def _chain_slice(res, c):
    S = res["settings"]["samples"]
    return {"samples": {k: v[c * S:(c + 1) * S] for k, v in res["samples"].items()}}


def run_units(N, A, W, seed=0, warmup=400, samples=250, chains=4, log=print, label=""):
    """Fit with several chains and report BOTH the pooled posterior and each chain separately.

    The qbl posterior is often multimodal: an "incremental fraud" class with a very small fraud
    magnitude is almost the same as "no fraud", so chains can settle in different places. When
    that happens (pi R-hat > 1.1) the state is marked not converged and the spread of results
    across chains is reported, instead of picking one chain.
    """
    res = fit(N, A, W, seed, warmup, samples, chains)
    per = [unit_posteriors(_chain_slice(res, c), N, A, W) for c in range(chains)]
    post = {k: np.mean([p_[k] for p_ in per], axis=0) for k in ("p2", "p3", "pfraud", "fraud_votes", "fraud_turnout")}
    post["fraud_votes_total_draws"] = np.concatenate([p_["fraud_votes_total_draws"] for p_ in per])
    pi = res["samples"]["pi"]
    S = res["settings"]["samples"]
    summary = {
        "n": int(len(N)),
        "pi_mean": pi.mean(0).tolist(),
        "pi_lo": np.quantile(pi, 0.025, axis=0).tolist(),
        "pi_hi": np.quantile(pi, 0.975, axis=0).tolist(),
        "pi_by_chain": [pi[c * S:(c + 1) * S].mean(0).tolist() for c in range(chains)],
        "rhat_pi": res["rhat"]["pi"],
        "ess_pi": res["ess"]["pi"],
        "converged": converged(res),
        "divergences": res["divergences"],
        "seconds": res["seconds"],
        "settings": res["settings"],
        "flag_05": int((post["pfraud"] > 0.5).sum()),
        "flag_09": int((post["pfraud"] > 0.9).sum()),
        "flag_05_by_chain": [int((p_["pfraud"] > 0.5).sum()) for p_ in per],
        "flag_09_by_chain": [int((p_["pfraud"] > 0.9).sum()) for p_ in per],
        "fraud_votes_mean": float(post["fraud_votes_total_draws"].mean()),
        "fraud_votes_lo": float(np.quantile(post["fraud_votes_total_draws"], 0.025)),
        "fraud_votes_hi": float(np.quantile(post["fraud_votes_total_draws"], 0.975)),
        "fraud_votes_by_chain": [float(p_["fraud_votes_total_draws"].mean()) for p_ in per],
        "leader_votes": int(np.sum(W)),
        "iota_M_by_chain": [float(np.median(K / (1 + np.exp(-res["samples"]["mu_iM"][c * S:(c + 1) * S]))))
                            for c in range(chains)],
    }
    log(f"      {label}: n={len(N):,} pi1={summary['pi_mean'][0]:.3f} flags>0.5={summary['flag_05']:,} "
        f"(chains {summary['flag_05_by_chain']}) R-hat={max(summary['rhat_pi']):.2f} t={res['seconds']}s")
    return summary, post


def simulate(rng, N, pi, mu, sd, corr_nu_tau=0.0, groups=None, group_sd=None):
    """Draw counts from the qbl data-generating process (used to validate the implementation).

    mu, sd: dicts of logit-scale means / unit-level sds for the six components.
    corr_nu_tau / groups / group_sd: optional REAL-WORLD-LIKE departures without any fraud:
    correlated turnout and support, and clustering by area (counties).
    """
    n = len(N)
    e = {c: rng.normal(0, 1, n) for c in COMPONENTS}
    if corr_nu_tau:
        e["tau"] = corr_nu_tau * e["nu"] + np.sqrt(1 - corr_nu_tau**2) * e["tau"]
    lin = {c: mu[c] + sd[c] * e[c] for c in COMPONENTS}
    if groups is not None and group_sd:
        g = np.unique(groups, return_inverse=True)[1]
        for c, s in group_sd.items():
            lin[c] = lin[c] + rng.normal(0, s, g.max() + 1)[g]
    sig = lambda x: 1 / (1 + np.exp(-x))  # noqa: E731
    nu, tau = sig(lin["nu"]), sig(lin["tau"])
    iM, iS = K * sig(lin["iM"]), K * sig(lin["iS"])
    uM, uS = K + (1 - K) * sig(lin["uM"]), K + (1 - K) * sig(lin["uS"])
    Z = rng.choice(3, size=n, p=pi)
    a = np.choose(Z, [1 - tau, (1 - tau) * (1 - iM), (1 - tau) * (1 - uM)])
    w = np.choose(Z, [nu * tau, nu * tau + iM * (1 - tau) + iS * tau * (1 - nu),
                      nu * tau + uM * (1 - tau) + uS * tau * (1 - nu)])
    # draw votes consistently: valid votes V ~ Bin(N, 1-a); leader votes W ~ Bin(V, w/(1-a))
    V = rng.binomial(N, np.clip(1 - a, 1e-9, 1))
    W = rng.binomial(V, np.clip(w / np.clip(1 - a, 1e-9, 1), 0, 1))
    return N - V, W, Z
