"""Optional LangSmith tracing for the WHY / HOW LLM calls.

Observability only: a traced call behaves exactly like an untraced one. Spans are sent
only when the `trace` extra is installed (`langsmith`) and `LANGSMITH_TRACING=true`
(LangSmith's own switch, off by default); otherwise the decorators change nothing.
Traces leave the deployment: they hold the diagnostics evidence, RAG chunks, backup
catalog and model output, never request headers or the API key.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])

_traceable: Callable[..., Any] | None
try:
    from langsmith import traceable as _langsmith_traceable

    _traceable = _langsmith_traceable
except ImportError:  # the optional `trace` extra is not installed
    _traceable = None

USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


def span_inputs(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Bound call arguments without `self` (the client holds the settings and API key)."""
    return {name: value for name, value in inputs.items() if name != "self"}


def llm_span_outputs(response: Any) -> dict[str, Any]:
    """Responses API body as an LLM span: model output plus `usage_metadata`, the field
    LangSmith reads token counts from."""
    if not isinstance(response, Mapping):
        return {"output": response}
    outputs: dict[str, Any] = {"output": response.get("output")}
    usage = response.get("usage")
    if isinstance(usage, Mapping):
        outputs["usage_metadata"] = {
            name: value
            for name in USAGE_FIELDS
            if isinstance(value := usage.get(name), int) and not isinstance(value, bool)
        }
    return outputs


def traced(
    name: str,
    *,
    run_type: str = "chain",
    process_outputs: Callable[[Any], dict[str, Any]] | None = None,
) -> Callable[[F], F]:
    """Wrap a function in a LangSmith span when `langsmith` is installed; else identity."""
    if _traceable is None:
        return lambda function: function
    return cast(
        Callable[[F], F],
        _traceable(
            name=name,
            run_type=run_type,
            process_inputs=span_inputs,
            process_outputs=process_outputs,
        ),
    )
