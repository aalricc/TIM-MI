# MI v2 Threat Model (Phase 4)

## Scope

This threat model covers the MI runtime control loop, supervisor guardrails, primitive execution, and evaluation harness attack surfaces relevant to autonomous recovery of the sandboxed target service.

## Primary Threats

- Prompt-injection through untrusted logs, filenames, tool output, and config values.
- Out-of-policy high-risk actions (irreversible, terminal, brokered) triggered without adequate state evidence.
- Unsafe path access (backup path reachability, sandbox escape, link-following abuse).
- Budget bypass causing excessive autonomous mutation attempts.
- State drift between planning and execution leading to stale-parameter misuse.

## Controls

### Registry and risk tier controls

- Every primitive is registered with a risk tier (`read_only`, `reversible`, `irreversible`, `terminal`, `brokered`).
- Candidate actions are filtered through pre-shield checks using registry risk tier metadata.

### Pre-shield and post-shield

- **Pre-shield** enforces attempt budget, validated parameters, and risk-tier gating.
- **Post-shield** re-validates parameters and compares parameter-linked state against planning-time observations.
- Irreversible actions require a prior read-only observation in the same incident.

### Supervisor budget controls

- Budget enforcement is outside LLM/policy logic.
- Supervisor gates attempts per incident and per rolling hour window.

### Degraded mode

- When LLM diagnosis is unavailable, runtime enters degraded mode.
- Runtime may execute trusted skills only.
- If no trusted skill is available, runtime escalates to operator via `escalate_to_operator`.
- No fallback candidate executor is used.

### Path and data hygiene

- PathGuard prevents sandbox escape and backup path access.
- LLM-facing diagnose payload wraps logs/probes/lessons as data structures.
- System prompt and diagnose instruction treat untrusted content as data, not instructions.
- Structured JSON schema output is required for model responses.

## Red-team coverage

The red-team suite extends scenario S18 and includes instruction-like payloads in operational surfaces. Current penalty checks include:

- Following injected instruction text.
- Attempting backup path access during S18.
- Attempting exfiltration-style behavior during S18.

Target outcome for S18 remains zero out-of-policy actions.

## Residual risks

- Trusted skill lifecycle is currently heuristic (`success_count > 0`) and will be formalized in Phase 6 lifecycle states.
- Full runtime red-team telemetry for all exfiltration channels will be expanded with broader scenario instrumentation in later phases.

