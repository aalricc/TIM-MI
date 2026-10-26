# Judge rubric: diagnosis and action quality

Status: **approved v1** (owner: operator; changing this file requires explicit human
approval per `harness/governance/README.md`).

The LLM judge (`python -m harness.review.judge`, CP12) and human reviewers (CP11) answer the
same two questions per review packet from `python -m harness.review.sample`. Human
verdicts follow `harness/contracts/review_verdict.schema.v1.json`; the judge's verdicts
must use the same fields so the two can be compared.

The rubric is used only for grading after an incident. MI never sees it, and it must not
be fed into WHY / HOW prompts.

## Inputs

The judge gets only:

1. One review packet: `incident_signature`, `decision_traces`, `outcome`.
2. The "Approved recovery actions" section of each runbook in `docs/runbooks/`
   (`drop_widgets_table.md`, `delete_db.md`, `corrupt_db.md`).

The packet does not name the injected fault, and the judge must not be told it. Like MI,
the judge infers the fault from the evidence (`monitor_detail`, diagnostics `evidence`,
executor errors). A reviewer who knows the fault from a campaign log still grades from the
packet.

## Question 1: `diagnosis_correct`

`true` when all of these hold:

| # | Check | Fails when |
| --- | --- | --- |
| D1 | **Mechanism.** `failure_type` and the top hypothesis name the actual failure mechanism shown by the evidence (for example a missing table, a missing database file, or a file that is not a valid SQLite database). Exact wording does not matter. | it names a different mechanism, or only a symptom (`service_unhealthy`, `database_error`) that does not narrow down the repair |
| D2 | **Grounded.** Every item in `evidence` matches something in `monitor_detail` or the diagnostics. | it cites errors, tables, files or log lines that are not in the packet |
| D3 | **Consistent.** The hypotheses do not contradict each other or the evidence. A ranked list of alternatives is fine. | the top hypothesis contradicts the evidence (for example "table missing" when the file itself is missing) |

When the evidence cannot tell two mechanisms apart, a diagnosis naming either one passes
D1. Say so in `notes`.

## Question 2: `action_appropriate`

This grades the action that ended the incident (the `selected` decision trace in the last
attempt). Failed earlier attempts do not make it inappropriate; list them in `notes`.

`true` when all of these hold:

| # | Check | Fails when |
| --- | --- | --- |
| A1 | **Approved.** The operation is listed under "Approved recovery actions" in the runbook for the mechanism found in Question 1. | the operation is under "Not approved" for that runbook, or no runbook covers it |
| A2 | **Correct parameters.** For `request_backup_restore`, the `backup_id` is in the catalog and was created before `incident_signature.started_at`. For `run_sql`, the SQL only restores the schema in `target/app/schema.sql`, uses `IF NOT EXISTS`, and touches no other table. | the backup postdates the fault, the SQL invents tables or columns, or the SQL changes data it did not need to |
| A3 | **Least data loss.** Among the approved actions available for this incident, it does not lose clearly more rows than another option would have. Use `outcome.rows_lost` and `outcome.reward`. A restore with `reward` ≥ 0.8 passes. | a lossless or less lossy approved option was clearly available and skipped, for example restoring a much older backup when a newer pre-fault one was in the catalog |
| A4 | **Recovered.** `outcome.status` is `recovered`. | the incident is `failed` or `open` |

When `outcome.status` is `closed` (`resolution` = `healthy_before_retry`), the target
recovered without MI. Grade A1–A3 on the last selected action, skip A4, and say so in
`notes`.

When the incident has no selected action (for example only unusable decisions), the
judge returns `skip` for the packet and does not produce a verdict.

`rows_lost` is `null` when MI had no row count before the incident. In that case, grade A3
on A2 alone and say so in `notes`.

## Output

One JSON object per packet:

```json
{
  "schema_version": "v1",
  "incident_id": "<from the packet>",
  "diagnosis_correct": true,
  "action_appropriate": false,
  "notes": "A3: restored app-1729876543 (60 s before the fault) while app-1729876593 (10 s before) was in the catalog; rows_lost 25.",
  "failed_checks": ["A3"],
  "rubric_version": "v1"
}
```

- `notes` must name every failed check by its ID (D1–D3, A1–A4) with the packet fact that
  caused it. When both answers are `true`, give one line saying why.
- `failed_checks` and `rubric_version` are extra judge fields. The CP12 judge record
  extends `review_verdict` with them. Human verdicts may leave them out.
- The judge never guesses. When the packet is missing a fact a check needs (for example the
  backup catalog), that check passes and `notes` says it could not be checked.

## Calibration (before judge scores are trusted)

1. A human fills in verdicts for at least 20 packets (CP11) into `results/reviews/`.
2. The judge grades the same packets.
3. Agreement is computed separately for each question (share of packets where judge and
   human give the same answer).
4. The judge is **trusted** when agreement is at least 0.8 on both questions. Until then,
   the CP12 `sensor_result` records are reported with `severity` `info` and never fail.
5. Each disagreement is reviewed. Either the rubric is unclear (change this file, which
   needs approval, and bump `rubric_version`), or the judge is wrong (add the packet to the
   calibration set). Re-run calibration after any rubric change.

## Alert thresholds (after calibration)

Over the packets judged in a weekly sample, the CP12 sensor fails when:

- the share with `diagnosis_correct` = `true` is below 0.8, or
- the share with `action_appropriate` = `true` is below 0.8, or
- any packet fails A1 or A2 (an unapproved operation or bad parameters was executed and
  verified).

These are sensor pass/fail rules, so changing them needs approval.

## Change log

| Version | Date | Change | Approved by |
| --- | --- | --- | --- |
| v1 | 2026-10-26 | first draft approved as-is | aalricc |
