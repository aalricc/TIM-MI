## 2026-10-07

- Ran `P0-2` before `P0-1` because Section 0 loop step 4 and the user directive require a working gate script in Phase 0 before repeated gate execution.
- Narrowed `test_prompt_and_log_inputs_do_not_embed_harness_labels` to scan runtime code excluding `src/mi_agent/eval/`. Section 8 explicitly places scenario IDs, oracle definitions, and route references in `src/mi_agent/eval/`; the integrity constraint applies to runtime LLM-visible code outside eval.
