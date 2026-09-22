# 🧠 Hippocampus

[![ci](https://github.com/Chestnuts-Sisyphus/hippocampus/actions/workflows/ci.yml/badge.svg)](https://github.com/Chestnuts-Sisyphus/hippocampus/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/Chestnuts-Sisyphus/hippocampus)](https://github.com/Chestnuts-Sisyphus/hippocampus/releases)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](pyproject.toml)
[![runtime deps](https://img.shields.io/badge/runtime%20deps-4-brightgreen.svg)](pyproject.toml)

**Cross-session memory for agents — one memory core (`MemoryCore`), two consumption forms,
measured on public benchmarks with official judging.**

[中文说明](README.zh-CN.md) · [Mechanism](docs/memory-core-v1.md) · [Architecture](#-architecture) · [Running it](#-quick-start) · [Security](docs/security.md) · [Contributing](CONTRIBUTING.md)

---

Hippocampus treats memory as the **core capability of an agent**, not a bolt-on retrieval library:
conversations land in a structured store, and every later request is answered against the *right*
memories — retrieved, layered, and only then handed to the model.

In-repo docs are written in Chinese (the project is target-language-agnostic; the core UI strings
and docs are CN). Repo map and protocol details live under [`docs/`](#-documentation).

---

## ✨ What it does

| Capability | What you get |
|---|---|
| **Memory core** | 4-channel retrieval (semantic / keyword / graph / episode) + relevance-cliff truncation + budget packing; stable/fluid layered injection; **dual-track** (confirmed / non-confirmed) + **observation track** isolation — model outputs are *never* silently injected; conflicts are suspended for human confirmation; lifecycle & offline consolidation; safety and missed-extraction guards. Every memory is viewable, editable, deletable, and source-traceable. |
| **Proxy form** | Accepts **three inbound formats** — OpenAI Chat Completions, OpenAI Responses, Anthropic Messages (sniffed from the request shape). Point your client's `base_url` at it and it gains memory: injection before requests, consolidation after responses, confirm blocks returned inside responses, replies in the *client's own protocol*. Zero client changes. Details & honest boundaries: [`docs/proxy.md`](docs/proxy.md). |
| **Agent form** | LangGraph orchestration (think / act / answer) + LangChain tools. Memory is woven into reasoning: retrieval decides context → execution → consolidation gated on exit and evidence. Three exits (done / cannot / needs-human); replayable traces. |

Plug-ins that exist today: **model endpoints** (OpenAI-compatible, swappable upstream). Memory
backend (SQLite + Chroma) and tool sources are on the roadmap — **no MCP / third-party tool
integration** is claimed or shipped.

---

## 🏆 Public benchmark results (measured, 2026-09-18)

Official judging implemented as an explicit opt-in arm (`bench --model-arm`): the model answers
from the memory layer's **injected context (top-8)** — not the full history — and judging follows
the official repositories (pinned revisions, see `docs/benchmark.md` §二·三).

| Benchmark | Official metric | Result (95% CI) | n |
|---|---|---|---|
| LongMemEval-oracle | LLM-judged accuracy (official judge prompts) | **74.2% (70.2%–77.8%, Wilson)** | 500 (full) |
| LongMemEval-oracle (earlier sampled run) | same official judging | 70.5% (63.8%–76.4%, Wilson) | 200 (sampled; kept as history, statistically compatible) |
| LoCoMo-10 | Official F1 (Porter-stemmed token F1, official eval script) | **32.55% (30.8%–34.4%, bootstrap)** | 1986 (full) |
| LoCoMo-10 + `--neighbors` (±1-turn expansion) | same official F1 | **38.68% (36.8%–40.5%, bootstrap)** | 1986 (full) |

Same-run retrieval metrics (evidence-in-context / answer-in-context): LongMemEval **99.6%** /
40.0% on the full 500-question batch (0 failures, 0 skipped) and 100.0% / 48.5% on the earlier
200-question sample (different batches, do not mix); LoCoMo **45.5%** / 17.4% baseline,
**63.7%** / 22.7% with the neighbor-expansion arm
(`--neighbors`; with neural embedding `bge-small-en-v1.5`; default zero-download lexical tier: 36.7%).

> **Honest footnotes (do not skip when citing):** the model arm uses `deepseek-chat` at
> temperature 0, and its input is the memory layer's top-8 injected context, so these numbers are
> **not directly comparable to full-context baselines** — published baselines are shown with
> per-row footnotes in the results document. Official runs cost API tokens (¥30 budget guard
> built in; every run records balance before/after).

Performance (real-scale synthetic store: 1,154 memories / 2,406 entities / 6,035 relations /
497 episodes): 4-channel retrieval p50 **48 ms**; injection assembly p50 109 ms; single write
p50 **42 ms** (incremental index sync, 2026-09-18; was 632.8 ms with full re-sync — same batch **批 D**, batch pairs in `docs/roadmap.md` §五);
multi-account memory bounded by an LRU session cache: LME neural benchmark (200 accounts) peak
RSS **~1.1 GB** (was ~4 GB), evidence recall still 100%.

> **Number source-of-truth (F2):** every figure above is kept in sync with the local results
> document (a private working file, not part of this repository; latest re-measurement
> 2026-09-18). If a number changes anywhere, update both places the same day — see
> `docs/release-sync.md` for the exact sync checklist. The **in-repo checkable part** of the official
> LongMemEval run is [`results/lme_official_500b_by_type.json`](results/lme_official_500b_by_type.json):
> the per-question-type split (six types, including the 78-question knowledge-update subset), Wilson
> CIs, the source report's sha256, and a row-by-row cross-check against the report — derived offline,
> with no model calls. Per-question rows (questions / gold / predictions) are deliberately **not** in
> the repo; the aggregate is what a reviewer can re-verify.

Full numbers, category breakdowns, CI methods, costs, and repro commands:
`docs/benchmark.md` + `docs/roadmap.md`.

---

## 🚀 Quick start

```bash
# Install (three options; not on PyPI yet, so these are the live paths)
pip install "hippocampus-agent[vector,proxy] @ git+https://github.com/Chestnuts-Sisyphus/hippocampus"
#   or: copy the source tree and  pip install -e "/path/to/hippocampus[vector,proxy]"
#   or (no install, just run): PYTHONPATH=/path/to/hippocampus/src python -m hippocampus.cli doctor

hippocampus doctor                     # health check: data root / port / locks / index / embedding tier
hippocampus seed                       # load sample data (known ground truth: facts, conflicts, stale items)
hippocampus demo --memories            # one-command eval + memory on/off comparison
python scripts/demo_flow.py            # cross-session long-task demo: 3 forms + memory-across-sessions asserts
```

> **Naming note.** The project, CLI and import package are all `hippocampus`; only the **PyPI
> distribution name** is `hippocampus-agent` — the bare name `hippocampus` on PyPI belongs to a
> third-party memoization package, and installing by the bare name would fetch the wrong thing.

Measured demo output (2026-09-17, offline tier, no credentials, synthetic data):

```
[memory on]  10/10   (100%, avg 2.0 steps)
[memory off]  3/10   (30%,  avg 2.0 steps)
      failures: retrieval miss 7
[dual-source labels] 100% agreement (9 comparable)
Boundary: minimal evalset — 10 questions (7 QA / 3 actions), single endpoint, single round;
          offline answerer is a rule-based one (not model reasoning); synthetic sample data —
          reproducible method, no general claims of quality.
```

**Real captures, not mock-ups.** Both images below are rendered straight from the commands shown in
them (offline tier, zero credentials, synthetic data): the only display-side edits are colouring and
soft-wrapping, and the filtering each image applied is printed inside the image itself, so you can
re-run the same pipeline and get the same lines. Regenerate with
`python scripts/gen_demo_screenshot.py`.

![Terminal capture: hippocampus doctor, seed, and the memory-on/off demo in the offline tier](docs/demo-offline-eval.png)

`scripts/demo_flow.py` — the same memory reaching a second session through **all three forms**
(core / agent / proxy), asserted end to end (A1–A4, exit code 0):

![Terminal capture: cross-session demo across the three forms, A1-A4 asserts passing](docs/demo-cross-session.png)

Proxy form:

```bash
hippocampus proxy --port 8765
# first start generates an instance token (<data root>/instance_token; doctor shows first 8 chars)
#   clients send  Authorization: Bearer <full token>  (401 without)
# point your client's base_url at http://127.0.0.1:8765
#   OpenAI Chat: /v1/chat/completions   Responses: /v1/responses   Anthropic: /v1/messages
# stream:true is forwarded SSE line-by-line (true streaming); upstream non-2xx passes through verbatim
```

Agent form:

```bash
hippocampus chat "挑 5 个适合我的岗位"      # requires a configured model endpoint
hippocampus chat --offline "记住：我不看外包"  # memory discipline works without a key / offline
```

---

## 🏗️ Architecture

![Hippocampus architecture: one memory core, two consumption forms, dual collections, three orthogonal state axes, dual tracks plus an observation track](docs/architecture.svg)

Every box carries the code anchor it stands for (`file:line`, in the box corner). The diagram is
**generated, not drawn**: `python scripts/gen_architecture_svg.py` re-verifies all 33 line-level
anchors, 5 file-level anchors, plus two absence assertions (`no physical delete of memories`, `no namespace`) against the
source tree and **refuses to emit the SVG** if any of them moved — a diagram that drifts from the
code is worse than no diagram. The write side (three orthogonal state axes, the three write tracks,
and the conflict-confirmation loop) has its own figure:
[`docs/memory-lifecycle.svg`](docs/memory-lifecycle.svg).

| Layer | Form | Entry point |
|---|---|---|
| Memory core | `MemoryCore` (SQLite + Chroma + BM25, 4-channel retrieval) | `hippocampus.core` |
| Proxy | OpenAI-compatible proxy (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`) | `hippocampus proxy` |
| Agent | LangGraph graph (think / act / answer) | `hippocampus chat` |

Guards are wired at every layer: outbound URLs are validated (**https-only** by default, no
localhost/private/reserved; plaintext public egress needs an explicit opt-in),
credentials come from environment or keyring only (zero literals in source/tests),
memory writes are safe-ident/safe-DDL checked, and offline mode means *no outbound requests, period*
— benchmarks default to offline unless `--model-arm` is explicitly passed.

---

## 🧪 Tests & CI

- **678 pytest tests** (3 xfailed) — memory core, both forms, guards, embedding tiers, public-bench
  adapters, official judging arm; all offline-runnable (`pytest --basetemp=D:/tmp/pt`).
- Demo eval runs in CI with **threshold assertions** (memory on ≥9/10, memory off ≤6/10) — score
  regressions turn the pipeline red.
- Static checks: `check_interface` (v1 contract append-only), `audit_deps`, `scan_credentials`
  (zero credential literals across the tracked tree), `scan_personal_data`, `ruff`.
- `main` is protected: the six CI checks (`full` over the OS × Python matrix, `core-only`, `lint`) are
  **required**, with **no** required reviewer (single maintainer) —
  `gh api repos/Chestnuts-Sisyphus/hippocampus/branches/main/protection`.
- **CD**: pushing a `v*` tag runs [`.github/workflows/release.yml`](.github/workflows/release.yml) —
  build sdist/wheel, install the built wheel into a clean venv and smoke-test it offline, attach the
  artifacts to the GitHub Release. **PyPI publishing is deliberately off** (it is an outward-facing
  action); the trusted-publishing switch is documented in that workflow's comments.
- Contributing rules (offline-first, zero credential literals, no out-of-repo paths, numbers must be
  reproducible): [`CONTRIBUTING.md`](CONTRIBUTING.md). Vulnerability reports:
  [`.github/SECURITY.md`](.github/SECURITY.md).

---

## 📚 Documentation

| Doc | What it covers |
|---|---|
| [`docs/benchmark.md`](docs/benchmark.md) | Public-benchmark protocol: pinned dataset revisions (sha256), retrieval/official judging arms, repro commands |
| [`docs/benchmark.en.md`](docs/benchmark.en.md) | English one-pager: what the official judging arm measures, guards, and measured results |
| [`docs/release-sync.md`](docs/release-sync.md) | GitHub sync discipline: push/number-sync/tag checklist so the repo never lags the local results |
| [`docs/roadmap.md`](docs/roadmap.md) | Known limitations, honest boundaries, improvement roadmap |
| [`docs/proxy.md`](docs/proxy.md) | Proxy form: format matrix, auth, streaming, honest boundaries |
| [`docs/deployment.md`](docs/deployment.md) | Deployment modes: loopback-only default, multi-instance, exposing behind TLS |
| [`docs/memory-core-v1.md`](docs/memory-core-v1.md) | `MemoryCore` v1 interface contract (append-only) |
| [`docs/security.md`](docs/security.md) | Threat model, outbound URL rules, credential handling |
| [`docs/embedding.md`](docs/embedding.md) | Embedding tiers, which to choose (with measurements), pooling per model |
| [`docs/offline.md`](docs/offline.md) | Offline tier: what works without a network/credentials |
| [`docs/naming.md`](docs/naming.md) | Naming decisions (PyPI name, terminology) |
| [`docs/forms-parity.md`](docs/forms-parity.md) | Feature parity across the two forms |
| [`docs/verification-design.md`](docs/verification-design.md) | Verification methodology for eval topics |
| [`results/lme_official_500b_by_type.json`](results/lme_official_500b_by_type.json) | In-repo aggregate of the official LongMemEval run: per-question-type accuracy + Wilson CI + source sha256 + cross-check rows (no per-question rows) |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to run tests/lint, commit conventions, the offline-first and privacy rules |
| [`.github/SECURITY.md`](.github/SECURITY.md) | How to report a vulnerability (private advisory), supported versions, design boundaries that are *not* vulnerabilities |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | Behaviour baseline for issues/PRs |
| [`CITATION.cff`](CITATION.cff) | Citation metadata |

---

## 📜 Status & limitations (honest)

What works today: two production-shaped consumption forms, real retrieval pipeline, public
benchmarks with official judging, performance at real-store scale, and CI-enforced regression
thresholds. What is *not* there — so nobody reads more into the repo than it delivers:

- **No MCP / third-party tool integration** (by design; the design doc says so — this is the
  *consuming* direction; the sibling GitTok project *provides* an MCP server, which is unrelated).
- **Management endpoints `/run` `/trace` `/health` exist but bind loopback only** (round-7 T3:
  `hippocampus serve`; instance-token auth; same port as the proxy form so you run one at a time).
- **Session cache is LRU-capped** (`HIPPOCAMPUS_SESSION_CACHE_MAX`, default 16): bounded RSS
  (~1.1 GB at 200 accounts) but evicted accounts reopen their session on next access.
- **Single `write` uses incremental index sync** — p50 **42 ms** at 1,154-memory scale (was
  632.8 ms with full re-sync, 批 D 同批配对); residual risk is index/library divergence under concurrent writers,
  covered by `index_health` + `hippocampus index rebuild`.
- **English corpus × CN-tuned tokenizer/thresholds** — absolute retrieval scores on English benchmarks are lower than an EN-tuned system would score (documented per-benchmark).
- **Official judging costs API tokens** and is an explicit opt-in flag; every run prints call counts, estimated cost, and balance before/after (¥30 budget hard-stop).
- **Naming**: PyPI distribution name is `hippocampus-agent` (see above).

---

## 📝 License

MIT