# Public Benchmark Protocol — Official Judging Arm (English one-pager)

> Companion to `docs/benchmark.md` §2.3 (Chinese full protocol). This page exists so
> non-Chinese readers can audit **how the official scores were produced and how to re-run them**.
> Source of truth for the numbers: the results document (数字正本, cited in `docs/release-sync.md`).

## 1. What is measured

Two public long-context memory benchmarks, run **offline retrieval first, official judging opt-in**:

| Benchmark | Data | Metric | Judge |
|---|---|---|---|
| LongMemEval-oracle | `xiaowu0162/longmemeval-cleaned` @`98d7416` (oracle slice, `longmemeval_oracle.json`, sha256 pinned in report) | accuracy | official per-question-type judge prompts (`get_anscheck_prompt`, abstention template for `_abs`) |
| LoCoMo-10 | `snap-research/locomo` @`3eb6f2c` (`locomo10.json`) | token F1 (Porter stemming) | official `task_eval/evaluation.py` (port verified against official output question-by-question) |

## 2. The only input difference (footnote every comparison)

The official pipelines feed the **full history**; we feed **the memory layer's injected context
(top-8 memories chosen by retrieval)** — that is exactly what is being tested (does the memory
system pick the right context?). Any table comparing our numbers with published full-context
baselines must carry this footnote per row.

## 3. Opt-in arm and guards

```bash
# endpoint + credential come from environment only (no literals in code/repo):
#   HIPPOCAMPUS_BASE_URL / HIPPOCAMPUS_API_KEY / HIPPOCAMPUS_MODEL
HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-en-v1.5" \
  hippocampus --home D:/tmp/hc-bench/run-lme-official bench longmemeval \
  --data D:/tmp/hc-bench/longmemeval_oracle.json --limit 200 --model-arm \
  --json D:/tmp/hc-bench/lme_official_full.json
```

- Explicit switch only: offline default, no outbound requests unless `--model-arm` (or `--online`) is passed.
- Guards (hard-coded in `hippocampus.eval.model_arm`): concurrency 16, retries 2, per-call timeout
  120 s, **budget hard-stop ¥30** (estimated from usage), balance checked before and after each run
  (the balance API lags; report both estimate and observed deduction).
- Model arm: `deepseek-chat`, temperature 0. Answer input = injected context (top-8), not full text.
- LoCoMo judging runs locally (official script port); LongMemEval judging calls the LLM
  (per-question-type official judge prompts).

## 4. Measured numbers (2026-09-18, run on Windows / AMD 7500F)

| Benchmark | Official score (95% CI) | Same-run retrieval (evidence / answer in context) |
|---|---|---|
| LongMemEval-oracle (**n=500**, full) | **74.2% (70.2%–77.8%, Wilson)**, 0 failures / 0 skipped | 99.6% / 40.0% |
| LongMemEval-oracle (n=200, earlier sample — kept as history) | 70.5% (63.8%–76.4%, Wilson) | 100.0% / 48.5% |
| LoCoMo-10 (n=1986) | **F1 32.55% (30.8%–34.4%, bootstrap)** | 45.5% / 17.4% |
| LoCoMo-10 + `--neighbors` (n=1986) | **F1 38.68% (36.8%–40.5%, bootstrap)** | 63.7% / 22.7% |

Each row's retrieval figures belong to **that** batch only — do not mix the n=500 and n=200
LongMemEval rows when citing.

Cost: ≈¥4.76 estimated total (three batches, 4386 calls); budget guard ¥30.
Decision gate to publish: LME accuracy ≥50% **and** LoCoMo F1 ≥15% — both pass, publish with the
top-8-injection footnote and the deepseek-chat footnote.

## 5. Honest limits

- Official numbers are **single-model × single-run** measurements (model-sensitivity unknown).
- Judge model is `deepseek-chat`, not the papers' GPT-4 family (footnoted).
- LME official slice is the oracle (easy) split: 3 sessions per question. The hard split
  (`longmemeval_s_cleaned`, ≈53 sessions) is not yet run (blocked by per-account memory until the
  LRU cache landed; see `docs/roadmap.md`).
- Full details, category breakdowns, and repro commands: `docs/benchmark.md`.