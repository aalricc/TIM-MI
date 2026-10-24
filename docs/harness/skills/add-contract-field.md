# Skill: add or change a harness contract field

Contracts in `harness/contracts/` describe records derived from runtime output. Records
are rebuilt from logs and state files at any time, including logs written by older
runtime versions, so compatibility is judged against old logs, not just new code.

## Decide: additive or breaking

| Change | Kind |
| --- | --- |
| New optional property | additive (stay in v1) |
| New required property that every log version can supply, or nullable (`["string", "null"]`) with the mapping setting `null` when absent | additive |
| New enum value | additive |
| Loosening a type or bound | additive |
| Removing or renaming a property, removing an enum value, tightening a type or bound, new required property old logs cannot supply | **breaking**: new `*.schema.v2.json`; keep v1 until no consumer reads it (risky change) |

## Steps (additive)

1. Edit the schema in `harness/contracts/`. With `"additionalProperties": false`, a new
   property must be listed under `properties` or records carrying it fail validation.
2. Populate it in the matching builder in `harness/mappings/runtime.py` (for example
   `harness/mappings/runtime.py::decision_traces`). Pass runtime values through unchanged;
   never coerce or invent values. Missing source data becomes `null`.
3. Document the derivation in `docs/harness/HARNESS_RUNTIME_MAPPING.md` and, if the record
   list changed, in `harness/contracts/README.md`.
4. Test: `tests/test_harness_mappings.py` already validates every record from every
   scenario in `tests/harness_scenarios.py` against the contract; add an assertion on the
   new field's value for at least one scenario, and a contract-level test in
   `tests/test_harness_contracts.py` if the field has conditional rules (`if`/`then`).

## Steps (breaking)

1. Copy the schema to `<name>.schema.v2.json` and change `$id` and the
   `schema_version` const to `v2`.
2. Load it with `harness/contracts/__init__.py::load_schema` (`version="v2"`); update
   builders and consumers.
3. Extend the contract tests to v2: the checks in `tests/test_harness_contracts.py`
   (for example `tests/test_harness_contracts.py::test_contract_schema_is_valid_draft_2020_12`
   and `tests/test_harness_contracts.py::test_contract_set_is_exactly_the_documented_contracts`)
   only look at `*.schema.v1.json`, so a v2 file is untested until you add it. Update the
   contracts README.

## Done when

```bash
make harness
make check
```
