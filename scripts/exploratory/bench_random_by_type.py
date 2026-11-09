"""
Benchmark harness for the JAX hierarchical turnover model.

Runs ``RandomByTypeModel`` (and optionally ``ConstantModel``) at several data
sizes and reports:

* Wall time for warmup and sampling (seconds)
* NUTS diagnostics: mean acceptance, divergent count, mean integration steps,
  final step size
* Per-parameter effective sample size (via ``blackjax.diagnostics``)
* Log-density at the posterior mean (proxy for MAP)

Results are dumped to JSON so they can be diffed across commits. Run this
BEFORE changing the model code to capture a baseline, then again after each
phase to attribute effects.

Usage:
    python scripts/exploratory/bench_random_by_type.py \\
        --sizes small medium [large] [real] \\
        --out ~/data/openpois/bench/baseline.json \\
        [--num-draws 250]

Size presets:
    small  — n = 10 000, K = 20
    medium — n = 1 000 000, K = 91
    large  — n = 4 200 000, K = 91  (slow; ~matches production scale)
    real   — reads real osm_observations.parquet via config.yaml
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import jax
import jax.numpy as jnp
import jax.random as jrd
import numpy as np
import pandas as pd

from blackjax.diagnostics import effective_sample_size

from openpois.models.jax_core import jax_rng
from openpois.models.model_fitter import ModelFitter
from openpois.models.osm_models import RandomByTypeModel
from openpois.models.setup import prepare_data_for_model


SIZE_PRESETS = {
    "small":  dict(n = 10_000,    k = 20,  min_per_group = 5),
    "medium": dict(n = 1_000_000, k = 91,  min_per_group = 5),
    "large":  dict(n = 4_200_000, k = 91,  min_per_group = 5),
}


def _simulate(
    key: jrd.KeyArray,
    n: int,
    k: int,
    min_per_group: int = 5,
    true_log_lambda_0: float = -5.3,
    true_log_sigma: float = 0.8,
) -> pd.DataFrame:
    """Simulate an observations DataFrame from the RandomByTypeModel likelihood."""
    k_eps, k_grp, k_dt, k_y = jrd.split(key, 4)
    # Simulate group epsilons from N(0, exp(log_sigma))
    eps = np.asarray(
        jrd.normal(k_eps, (k,)) * np.exp(true_log_sigma)
    )
    log_lam = true_log_lambda_0 + eps

    # Group assignment with a power-law-ish imbalance so we test uneven fits.
    # Weights ~ 1/(i+1); renormalised. Then enforce min_per_group per group.
    weights = 1.0 / (np.arange(k) + 1.0)
    weights = weights / weights.sum()
    # Sample (n - k*min_per_group) according to weights, then add min_per_group per group
    assert n > k * min_per_group, "n too small for requested min_per_group"
    n_weighted = n - k * min_per_group
    g_rand = np.asarray(
        jrd.categorical(k_grp, jnp.log(jnp.asarray(weights)), shape = (n_weighted,))
    )
    g = np.concatenate([
        g_rand,
        np.repeat(np.arange(k), min_per_group),
    ]).astype(np.int32)
    rng = np.random.default_rng(int(jrd.randint(k_grp, (), 0, 2**31 - 1)))
    rng.shuffle(g)

    # dt ~ Uniform(0.1, 10)
    dt = np.asarray(jrd.uniform(k_dt, (n,), minval = 0.1, maxval = 10.0))

    lam_per_obs = np.exp(log_lam[g])
    p = 1.0 - np.exp(-lam_per_obs * dt)
    y = np.asarray(jrd.bernoulli(k_y, jnp.asarray(p))).astype(np.int32)

    # Use string group labels so the category encoding exercises the real code path.
    group_names = np.array([f"grp_{i:03d}" for i in range(k)])
    return pd.DataFrame({
        "tag_years": dt,
        "changed": y,
        "shared_label": group_names[g],
    })


def _load_real_observations() -> pd.DataFrame:
    """Load the real OSM observations via config.yaml."""
    from openpois.config import load_config
    cfg = load_config()
    path = cfg.get_file_path("osm_data", "osm_observations")
    min_value_count = cfg.get(
        "osm_turnover_model", "min_value_count", fail_if_none = False
    )
    group_key = cfg.get(
        "osm_turnover_model", "group_key", fail_if_none = False
    )
    df = pd.read_parquet(path)
    prepared = prepare_data_for_model(
        data = df,
        group_key = group_key,
        group_values = None,
        min_value_count = min_value_count,
        t1_col = "last_tag_timestamp",
        t2_col = "obs_timestamp",
    )
    return prepared


def _ess_per_param(param_draws: dict[str, jnp.ndarray]) -> dict[str, float]:
    """Minimum ESS across elements of each pytree leaf."""
    out = {}
    for name, arr in param_draws.items():
        a = jnp.asarray(arr)
        if a.ndim == 1:
            ess = float(effective_sample_size(a[None, :]))
            out[name] = ess
        else:
            # Multiple elements: report min ESS (worst-case)
            flat = a.reshape(a.shape[0], -1).T            # (n_elem, n_draws)
            esss = np.asarray(
                jax.vmap(lambda row: effective_sample_size(row[None, :]))(flat)
            )
            out[f"{name}__min"] = float(esss.min())
            out[f"{name}__median"] = float(np.median(esss))
    return out


def _log_density_at_mean(
    fitter: ModelFitter,
    param_draws: dict[str, jnp.ndarray],
) -> float:
    """Evaluate log-density at the element-wise posterior mean."""
    post_mean = {
        name: jnp.mean(jnp.asarray(arr), axis = 0)
        for name, arr in param_draws.items()
    }
    return float(fitter.calculate_lp(post_mean))


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd = str(Path(__file__).resolve().parents[2]),
        ).decode().strip()
    except Exception:
        return "unknown"


def _run_one(
    tag: str,
    df: pd.DataFrame,
    num_draws: int,
    group_key: str = "shared_label",
) -> dict:
    """Build the model, fit it, collect timings and diagnostics."""
    n = len(df)
    model = RandomByTypeModel(
        dataset = df,
        metadata = {
            "dt_col": "tag_years",
            "group": group_key,
            "var_prior": (-1.0, 5.0),
        },
    )
    k = model.group_lookup.shape[0]
    print(f"[{tag}] n={n:,} k={k} — building fitter")

    fitter = ModelFitter(
        event_rate_fun = model.event_rate_fun,
        starting_params = model.starting_params,
        data = model.data,
        target = model.target,
        num_warmup = num_draws,
        num_samples = num_draws,
        param_likelihood = model.param_likelihood,
        derive_draws = model.derive_draws,
        log_likelihood_fun = model.log_likelihood_fun,
        log_1md_fun = getattr(model, "log_1md_fun", None),
        verbose = False,
    )

    t_fit_start = time.perf_counter()
    fitter.fit()
    # Ensure the draws are realised on device before we stop the clock.
    jax.tree_util.tree_map(lambda x: x.block_until_ready(), fitter.param_draws)
    t_fit_end = time.perf_counter()

    info = fitter.sampler_info
    mean_accept = float(jnp.mean(info.acceptance_rate))
    divergences = int(jnp.sum(info.is_divergent))
    mean_steps = float(jnp.mean(info.num_integration_steps))
    step_size = float(fitter.warmup_params["step_size"])

    ess = _ess_per_param(fitter.param_draws)
    log_density_at_mean = _log_density_at_mean(fitter, fitter.param_draws)

    return {
        "tag": tag,
        "n": int(n),
        "k": int(k),
        "num_draws": int(num_draws),
        "wall_fit_s": round(t_fit_end - t_fit_start, 3),
        "mean_acceptance": round(mean_accept, 4),
        "divergences": divergences,
        "mean_integration_steps": round(mean_steps, 3),
        "final_step_size": round(step_size, 6),
        "log_density_at_post_mean": round(log_density_at_mean, 3),
        "ess": {k: round(v, 2) for k, v in ess.items()},
    }


def main():
    parser = argparse.ArgumentParser(description = __doc__)
    parser.add_argument(
        "--sizes",
        nargs = "+",
        default = ["small"],
        choices = ["small", "medium", "large", "real"],
        help = "Which size presets to run.",
    )
    parser.add_argument(
        "--num-draws",
        type = int,
        default = 250,
        help = "Draws for both warmup and sampling (matches current default).",
    )
    parser.add_argument(
        "--out",
        type = str,
        default = "~/data/openpois/bench/bench_latest.json",
        help = "JSON output path.",
    )
    parser.add_argument("--seed", type = int, default = 0)
    args = parser.parse_args()

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents = True, exist_ok = True)

    rng = jrd.PRNGKey(args.seed) if args.seed else jax_rng()

    runs = []
    for size in args.sizes:
        if size == "real":
            df = _load_real_observations()
            runs.append(_run_one("real", df, num_draws = args.num_draws))
            continue
        preset = SIZE_PRESETS[size]
        key, rng = jrd.split(rng)
        df = _simulate(
            key,
            n = preset["n"],
            k = preset["k"],
            min_per_group = preset["min_per_group"],
        )
        runs.append(_run_one(size, df, num_draws = args.num_draws))

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "jax_version": jax.__version__,
        "platform": jax.default_backend(),
        "num_draws": args.num_draws,
        "seed": args.seed,
        "runs": runs,
    }

    with open(out_path, "w") as f:
        json.dump(payload, f, indent = 2)
    print(f"Wrote {out_path}")
    for r in runs:
        print(
            f"  {r['tag']:>6s}: n={r['n']:>9,} k={r['k']:>3} "
            f"fit={r['wall_fit_s']:>7.2f}s "
            f"accept={r['mean_acceptance']:.3f} "
            f"div={r['divergences']:>4} "
            f"step={r['final_step_size']:.4f}"
        )


if __name__ == "__main__":
    main()
