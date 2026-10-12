from __future__ import annotations

import math
from collections.abc import Mapping
from statistics import NormalDist


def evaluate_promotion(
    incumbent: Mapping[str, float | int],
    candidate: Mapping[str, float | int],
    *,
    metric: str,
    confidence: float = 0.95,
) -> dict[str, float | bool | int | str]:
    if metric.strip() == "":
        raise ValueError("metric must not be empty")
    if confidence <= 0 or confidence >= 1:
        raise ValueError("confidence must be between 0 and 1")

    incumbent_estimate = _required_float(incumbent, metric)
    candidate_estimate = _required_float(candidate, metric)
    incumbent_standard_error = _optional_float(incumbent, f"{metric}_se")
    candidate_standard_error = _optional_float(candidate, f"{metric}_se")

    incumbent_count = _optional_int(incumbent, "count", default=1)
    candidate_count = _optional_int(candidate, "count", default=1)
    has_samples = incumbent_count > 0 and candidate_count > 0

    difference = candidate_estimate - incumbent_estimate
    combined_standard_error = math.sqrt(
        (incumbent_standard_error**2) + (candidate_standard_error**2)
    )
    z_value = NormalDist().inv_cdf(confidence)
    margin = z_value * combined_standard_error
    lower_bound = difference - margin

    promote = has_samples and lower_bound >= 0.0
    return {
        "promote": promote,
        "metric": metric,
        "confidence": confidence,
        "z": z_value,
        "difference": difference,
        "combined_se": combined_standard_error,
        "margin": margin,
        "lower_bound": lower_bound,
        "incumbent": incumbent_estimate,
        "candidate": candidate_estimate,
        "incumbent_count": incumbent_count,
        "candidate_count": candidate_count,
    }


def _required_float(payload: Mapping[str, float | int], key: str) -> float:
    if key not in payload:
        raise ValueError(f"missing required key: {key}")
    return float(payload[key])


def _optional_float(payload: Mapping[str, float | int], key: str) -> float:
    if key not in payload:
        return 0.0
    return float(payload[key])


def _optional_int(payload: Mapping[str, float | int], key: str, *, default: int) -> int:
    if key not in payload:
        return default
    return int(payload[key])
