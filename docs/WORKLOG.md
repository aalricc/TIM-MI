# Rewrite Worklog

## Completed

- Replaced legacy phase-based architecture with workflow-first recovery loop.
- Removed stale eval/skills/policy/supervisor layers that obscured dynamic-tool flow.
- Demoted built-ins to diagnostics-first behavior.
- Added strict startup config gate for RAG/Pinecone.
- Rebuilt docs and env contract to match actual runtime behavior.
- Replaced sprawling test matrix with focused workflow-compliance tests.

## Current focus

- Keep dynamic tool lifecycle explicit and auditable.
- Preserve only meaningful persisted state (`events`, `policy`, `tool_catalog`).
