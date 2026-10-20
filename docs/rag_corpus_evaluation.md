# RAG Corpus Evaluation

## Scope

This note summarizes corpus ingestion behavior and evaluation outcomes for:

- `sqlite-docs` (current live namespace)
- `sqlite-docs-v2` (candidate namespace)

## 1) Cleaning and chunking steps (`src/mi_agent/rag_ingest.py`)

The ingestion pipeline applies the following steps:

1. **Line cleaning / normalization**
   - collapse whitespace
   - remove print headers/footers and URL page markers
   - remove navigation/site chrome and source-browser chrome labels
   - apply small text artifact fixes (e.g., glued words)

2. **Heading detection and sectioning**
   - detect section-like headings with rule-based filters
   - reject noisy heading candidates (overlong, date/hex/noise-like patterns)
   - keep cleaned lines as body text when heading criteria are not met

3. **Chunking**
   - PDF path: section-aware, sentence-based chunking with target size and overlap
   - text/markdown path: chunk by blank lines and entry/heading patterns

4. **Record shaping and dedupe**
   - dedupe normalized repeated content
   - deterministic IDs (`source_file:chunk_index`)
   - embed field text prefixed with `source_file — section`

## 2) Record counts

- `sqlite-docs`: **852**
- `sqlite-docs-v2`: **684**

## 3) Evaluation results

| Metric | sqlite-docs | sqlite-docs-v2 |
|---|---:|---:|
| Required pass | 6 / 6 | 6 / 6 |
| Observational pass | 21 / 21 | 20 / 21 |
| Strict coverage | 41 / 54 | 41 / 54 |

## 4) Notable result deltas

- The single `sqlite-docs-v2` observational miss is the **integrity-check + no-such-table composite** case.
- `sqlite-docs-v2` improved the **dropped-table** case: **The Schema Table** now appears in top 3.

## 5) Decision rule and decision

### Decision rule

Keep the current live namespace unless the candidate namespace is strictly better overall on the tracked evaluation metrics.

### Decision

**Stay on `sqlite-docs`. Keep `sqlite-docs-v2` available.**

Reason: required performance is tied (6/6 each), strict coverage is tied (41/54 each), and observational performance is lower on `sqlite-docs-v2` (20/21 vs 21/21).

## 6) How to rerun evaluation

Run both namespaces with the same script and top-k:

```bash
PYTHONPATH=src .venv/bin/python scripts/rag_eval.py \
  --namespace sqlite-docs \
  --top-k 3 \
  --json-out results/rag_eval_sqlite-docs.json
```

```bash
PYTHONPATH=src .venv/bin/python scripts/rag_eval.py \
  --namespace sqlite-docs-v2 \
  --top-k 3 \
  --json-out results/rag_eval_sqlite-docs-v2.json
```

