# MI Threat Model

## Scope

This document covers runtime control-loop safety for autonomous recovery:

- monitoring and incident entry
- LLM diagnosis/planning
- guarded tool execution
- verification and learning persistence

## Primary threats

- Prompt injection through logs, filenames, probe output, and config values.
- Unsafe or out-of-policy mutating actions.
- Sandbox or backup-path escape via path traversal/symlink abuse.
- Repeated high-risk actions without fresh state evidence.
- Secret leakage through telemetry.

## Controls

### Tool and action controls

- Single tool catalog from `src/mi_agent/tools/executor.py`.
- Only registered primitives are executable.
- Strict parameter schema validation before execution.
- Each primitive carries a risk tier used during candidate screening.
- Unknown actions are rejected and replanned.

### Guardrails and rollback

- Pre-shield precheck enforces risk-tier policy and evidence requirements.
- Post-shield validation rechecks parameter-dependent state before execution.
- Mutating actions snapshot state before execution.
- Failed verification triggers rollback when supported.

### Recovery pacing budget policy

- MI uses retry pacing backoff with jitter and cancellation support.
- MI does not enforce hard incident attempt/time budget cutoffs.

### LLM reliability controls

- Live model is fixed to `gpt-5.3-codex`.
- Endpoint is configured via `MI_LLM_ENDPOINT`.
- Preflight runs before live scenarios/tests.
- Retries use exponential backoff + jitter for transient errors.
- Permanent auth/permission/config errors stop incident handling cleanly.

### Degraded mode behavior

- Degraded mode never escalates to a human operator.
- When LLM calls are transiently unavailable, the runtime keeps retrying with backoff.
- When LLM errors are permanent (auth/permission/config), the incident stops with explicit diagnostics.

### Data and secret hygiene

- Runtime treats external text/log output as untrusted data.
- Event logging excludes API keys/auth headers.
- Verifier remains the only success gate.

## Residual risks

- Recovery quality depends on available registered primitives.
- Live LLM latency and provider outages can delay recovery.
- Scenario coverage in eval harness is finite and does not guarantee production completeness.

## Red-team coverage

- Red-team scenario testing remains part of evaluation to check prompt-injection resistance and policy bypass attempts.
