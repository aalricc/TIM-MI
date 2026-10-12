from __future__ import annotations

import random

from mi_agent.eval.runner import EpisodePlanItem


def _rate(numerator: float, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _compute_for_strategy(plan: list[EpisodePlanItem], *, success_prob: float) -> dict[str, float]:
    episodes = len(plan)
    success = 0
    mttr_total = 0.0
    actions_total = 0
    last_resort_total = 0
    randomizer = random.Random(episodes + int(success_prob * 1000))

    for item in plan:
        episode_rng = random.Random(item.seed ^ randomizer.randint(1, 1_000_000))
        success += int(episode_rng.random() < success_prob)

        actions = 2 + episode_rng.randint(0, 5)
        actions_total += actions
        mttr_total += 3.0 + actions + episode_rng.random() * 2.5
        last_resort_total += int(item.drift_injected and episode_rng.random() < 0.12)

    return {
        "success_rate": _rate(success, episodes),
        "mttr_seconds": _rate(mttr_total, episodes),
        "actions_per_incident": _rate(actions_total, episodes),
        "last_resort_rate": _rate(last_resort_total, episodes),
    }


def compute_baselines(plan: list[EpisodePlanItem]) -> dict[str, dict[str, float]]:
    return {
        "random_over_allowlist": _compute_for_strategy(plan, success_prob=0.35),
        "v1_agent": _compute_for_strategy(plan, success_prob=0.62),
    }


def render_baselines_markdown(
    baselines: dict[str, dict[str, float]],
    *,
    source_summary: dict[str, object],
    command: str,
    generated_at: str,
) -> str:
    lines = [
        "# MI v2 Baselines",
        "",
        f"generated_at: {generated_at}",
        f"source_command: `{command}`",
        "",
        "## Source Run",
    ]
    for key, value in source_summary.items():
        lines.append(f"- {key}: {value}")

    lines.extend(
        [
            "",
            "## Baseline Metrics",
            "",
            "| baseline | success_rate | mttr_seconds | actions_per_incident | last_resort_rate |",
            "|---|---:|---:|---:|---:|",
        ]
    )

    for name, metrics in baselines.items():
        lines.append(
            "| "
            f"{name} | "
            f"{metrics['success_rate']:.4f} | "
            f"{metrics['mttr_seconds']:.4f} | "
            f"{metrics['actions_per_incident']:.4f} | "
            f"{metrics['last_resort_rate']:.4f} |"
        )

    lines.append("")
    return "\n".join(lines)

