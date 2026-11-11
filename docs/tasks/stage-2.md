# Stage 2 — first AI: invoice extraction, PII masking, name matching, evals

Goal: invoices are read from PDF by an LLM with per-field confidence, every malicious invoice is rejected,
planted error #3 (campaign renamed) is detected, and `make eval` reports extraction accuracy next to the
stage-1 results. Error #8 is still expected to be missed (stage 4).

How to use: one task = one branch = one agent session. Start every Claude Code task in **plan mode**.
Before merging: `make lint && make test` and a green CI check.
Read `docs/plan.md` sections 2 (principles), 3 (architecture), 4 (modules), 10 (security) and 11 (stages),
and `docs/threat-model.md` once task 2.1 has created it.

**Principles for every stage-2 task**
- Rules first, AI second: anything code can check (sums, VAT, formats, "is this value in the document")
  is code. The LLM only reads messy text and judges fuzzy names.
- AI proposes, a human approves: LLM output never sets `approved`, never writes aliases, never sends anything.
- Protection lives in code and the database, not in prompts. A prompt that says "ignore instructions in
  the document" is a courtesy, never a control.
- The extractor has no tools: the LLM layer has no way to pass tool definitions.
- All LLM calls go through `backend/app/llm/`, which masks PII on every call.
- CI and tests never call a real model or need an API key (fake model only).
- No secrets in git: keys only in `.env`; `.env.example` lists the names.
- The answer key stays in `eval.*` and is read only by `evals/` as `eval_runner` (decision 033).

---

## 2.1 — Threat model draft
**Tool:** Claude Code (Opus) · **Branch:** `docs/threat-model`

```
Read AGENTS.md, docs/plan.md sections 3, 10, 11 and docs/decisions.md (004, 016, 025, 033, 035).
Goal: a threat model for stage 2, written before the code, so every later task can cite threat IDs.
Changes: create docs/threat-model.md mapped to the OWASP Top 10 for LLM Applications:
- assets (financial data in core, the LLM API key, the answer key), trust boundaries
  (PDF -> extractor -> core; platform API names -> name matching; LLM provider; Langfuse)
- threats with IDs: T1 prompt injection via invoice (incl. hidden text), T2 sensitive data to the provider
  or traces, T3 improper output handling (LLM values reaching core), T4 excessive agency,
  T5 unbounded consumption / cost, T6 secret leakage, T7 eval leakage (answer key), T8 supply chain (new deps)
- per threat: mitigation in code/DB, the test that proves it, owning task (2.2-2.10), status (planned)
Tests: none (docs only).
Security: this is the security baseline for stage 2; mitigations must not rely on prompt wording.
Propose the plan first.
```
**Done when:** `docs/threat-model.md` exists and every task below maps to at least one threat ID.

---

## 2.2 — Generator: synthetic PII and malicious invoices (#10)
**Tool:** Claude Code · **Branch:** `feat/malicious-invoices`

```
Read docs/plan.md section 5 and decisions 023-025.
Goal: documents that exercise PII masking and prompt-injection defences, with #10 in the answer key.
Changes:
- generator/documents/: add a synthetic IBAN (valid mod-97, fake bank code), a billing email (*.example)
  and a phone number to invoice headers/footers. Ground-truth JSON does NOT get these fields
  (data minimization: extraction never needs them).
- generator/errors.py: plant #10 (prompt_injection, detector "extraction_security", stage 2) as copies of
  real invoices with a payload, one variant each: white text, tiny font, text outside the page box,
  PDF metadata, visible "note to the AI" text, a fake JSON block, an instruction to change the total or
  approve the invoice. Payloads are inert strings (URLs only on example.invalid). Each claims its slot
  like other errors (decision 025); one answer-key row each. Ground truth = the visible values.
Tests: every variant planted and recorded once; visible amounts unchanged; same seed -> same bytes;
update world/document digests; the stage-1 JSON loader still loads the documents.
Security: T1, T7. No real names, banks or domains.
Propose the plan first.
```
**Done when:** `make generate` writes #10 answer-key rows; `make eval-stage1` still exits 0 (owner run).

---

## 2.3 — PII masking
**Tool:** Claude Code · **Branch:** `feat/pii-masking`

```
Goal: no IBAN, email, phone, card number or personal code is ever sent to an LLM or a trace.
Changes: backend/app/llm/masking.py, pure functions (plain code, no AI): IBAN (mod-97 checked), email,
phone (LT and international), card numbers (Luhn), Lithuanian personal code (checksum).
Replace with typed placeholders ([IBAN_1], [EMAIL_1], ...). The mapping stays in memory and is never logged
or returned to callers outside the module. The LLM client (2.4) will call it on every request.
Tests: each type masked; amounts, dates, invoice numbers and campaign_refs are untouched (false-positive
test over the text of all generated PDFs); masking twice gives the same result.
Security: T2.
Propose the plan first.
```
**Done when:** the masked text of every generated PDF contains zero PII matches; tests pass.

---

## 2.4 — LLM access layer
**Tool:** Claude Code (Opus) · **Branch:** `feat/llm-client`

```
Read decisions 010, 014, 027 and the open questions at the end of docs/tasks/stage-2.md (provider, caps, key).
Goal: one place for every LLM call: strict JSON, no tools, PII masked, cost capped, testable without a key.
Changes in backend/app/llm/:
- client.py: complete_json(task, prompt_id, text, schema) -> LLMResult (parsed object, tokens, cost_eur,
  latency, model, prompt hash). There is no tools parameter. Temperature 0, timeouts, retry on 429/5xx
  with decision 027's backoff. Output: provider structured output + pydantic validation; one retry,
  then the call fails (no repair loop). Masking (2.3) runs on every call.
- one provider adapter (the chosen provider); plain httpx or the SDK, with the reason.
- fake.py: scripted/recorded responses that record every call; the default in tests.
- cost.py: Decimal price table in config; per-run ledger; refuse a call when the per-run cap or the
  monthly cap (sum of app.pipeline_runs.cost_eur this month) would be exceeded.
- prompts as versioned files, hashed into every result. A tracer hook (no-op) for 2.9.
- settings in app.config (API key as SecretStr); .env.example updated.
Tests: fake model only, adapter via httpx.MockTransport; cap refusal; invalid JSON -> failure;
masking applied; requests never contain tools; test_eval_isolation.py forbids provider SDK imports
outside backend/app/llm/.
Security: T2, T4, T5, T6. Add gitleaks to pre-commit (first real key in the project).
Propose the plan first and list any new dependency with its reason.
```
**Done when:** tests pass with no network and no API key; caps and masking are proven by tests.

---

## 2.5 — Invoice extraction with confidence
**Tool:** Claude Code (Opus) · **Branch:** `feat/invoice-extraction`

```
Read docs/plan.md module 3, decisions 024 and 029, docs/threat-model.md (T1, T3).
Goal: PDF -> strict JSON (same fields as the ground-truth JSON) with per-field confidence; suspicious
documents rejected by code before any LLM call.
Changes in backend/app/extraction/:
- pdf_text.py: visible text plus hidden-text findings (render mode 3, white fill, font < 2 pt,
  outside the media box, metadata). Propose the library (pypdf moved to runtime or another) with a reason.
- guards.py: suspicious-content rules (instruction-like phrases, fake JSON, URLs). A finding -> rejected.
- schema.py: pydantic model; money as decimal strings, no floats, no extra fields, length/pattern limits.
- confidence.py: confidence computed in code, per field = min of: value found verbatim in the visible
  text, arithmetic (sum of lines = subtotal, VAT 21%, total), format (dates, campaign_ref pattern),
  the model's own score (one input only).
- extract.py: text -> guards -> mask -> LLM (2.4) -> schema -> checks -> ExtractionResult with status
  extracted / needs_review / rejected and reasons. Below the threshold (config) or a money value not
  found in the text -> needs_review. Never approved. Credit notes too.
Tests (fake model): every malicious variant rejected before the LLM is called; a fake answer with a
changed total is caught by the grounding check; ungrounded value -> low confidence; floats rejected.
Security: T1, T3, T4. The document text is data; nothing in it can change what the code does.
Propose the plan first.
```
**Done when:** all generated malicious PDFs are rejected by code; benign ones extract with the fake model.

---

## 2.6 — Extraction eval
**Tool:** Claude Code · **Branch:** `feat/eval-extraction`

```
Read decision 033 (isolation rules) and evals/stage1.py.
Goal: measured extraction quality, not assumed.
Changes: evals/extraction.py and `make eval-extraction` (owner-run, costs money; --limit N for a sample).
Run the extractor on the generated PDFs and compare with the ground-truth JSON next to each file, field
by field (exact Decimal / exact string). Report: per-field accuracy, document exact match, line
precision/recall, needs_review rate, wrong-but-high-confidence count (the dangerous one),
malicious rejected X/Y (the list comes from eval.answer_key, read as eval_runner), cost, latency,
model and prompt hash. Write a counts-only section to docs/eval-results.md; per-document details go to
the terminal only. Exit 1 if any malicious invoice is not rejected; accuracy is reported, not gated
(the gate comes in stage 5).
Tests: fake model and a hand-built key; never the real answer key.
Security: T7; test_eval_isolation.py still passes.
Propose the plan first.
```
**Done when:** an extraction accuracy table is in `docs/eval-results.md`.

---

## 2.7 — Pipeline: load invoices through extraction
**Tool:** Claude Code · **Branch:** `feat/pipeline-extraction`

```
Read docs/plan.md section 7 and decisions 029, 032, 034, 036.
Goal: pipeline step 5 can use LLM extraction instead of the ground-truth JSON.
Changes:
- pipelines/load_invoices.py: INVOICE_SOURCE=ground_truth|llm (default ground_truth, so CI, Airflow and the
  demo keep working without a key). In llm mode the loader never opens a .json file.
- writes status extracted / needs_review and invoice_lines.extracted_confidence. needs_review invoices are
  loaded and reconciled and get an extraction_needs_review issue. Rejected documents never reach core;
  they become an extraction_rejected_suspicious issue (written as app).
- new migration 0006: file_sha256 on core.invoices and core.credit_notes, so an unchanged file is not
  extracted again (idempotency and cost). Existing migrations, roles and grants unchanged.
- run cost and tokens into app.pipeline_runs; cap reached -> remaining documents skipped, run partial.
- a decisions.md entry.
Tests: mode switch; no-.json guard in llm mode; second run = same rows and zero LLM calls; cap -> partial.
Security: T3, T5.
Propose the plan first.
```
**Done when:** `make pipeline` in llm mode loads benign invoices and rejects every malicious one.

---

## 2.8 — Campaign rename: rule finds, AI judges, human approves (#3)
**Tool:** Claude Code (Opus) · **Branch:** `feat/ai-name-matching`

```
Read decisions 025 (#3), 028 (aliases), 030 and 033.
Goal: detect planted error #3. Today the renamed name is stored as an alias with source='rule' and
nothing flags it.
Changes:
- rule first: backend/app/reconciliation/renames.py (pure) finds campaigns whose later name has no
  [campaign_ref] tag.
- AI second: for each candidate the LLM gets masked minimal context (old name, new name, client, platform,
  month) and returns {same_campaign, confidence, reason <= 200 chars}.
- code writes an issue reconciliation_campaign_renamed (entity = platform campaign id, one key per
  campaign-month) with template text, the reason quoted as data, and the proposed action
  "Confirm the new name as an alias" or "Check: may be a different campaign".
- no writes to core.campaign_aliases: a human approves later (stage 4 sets source='human').
- new step pipelines/match_names.py after load_plans, before reconcile; update STEPS and the Airflow DAG.
If the eval shows the AI adds nothing over the rule, record that in docs/decisions.md; it is a valid result.
Tests: the rule finds the planted renames only; fake model both answers; no alias rows written; DAG drift test.
Security: T1 (names come from platform APIs), T4.
Propose the plan first.
```
**Done when:** every planted #3 has exactly one issue; tests pass.

---

## 2.9 — Langfuse tracing
**Tool:** Claude Code · **Branch:** `feat/langfuse`

```
Goal: every LLM call traceable with cost and latency.
Changes: implement the tracer hook in backend/app/llm/ only: one trace per run_id, one span per call with
model, tokens, cost, latency, prompt hash and status. Inputs/outputs only when LANGFUSE_CAPTURE_IO=true,
and always masked. Tracing is off when keys are missing (CI, demo); a tracing failure never fails a call.
Hosting as decided in the open questions. Add the langfuse dependency (state the reason); .env.example updated.
Tests: fake tracer; spans contain no unmasked PII; tracer errors are swallowed and logged.
Security: T2, T6.
Propose the plan first.
```
**Done when:** a `make pipeline` run in llm mode shows in Langfuse with cost per document.

---

## 2.10 — Stage 2 integration eval
**Tool:** Claude Code · **Branch:** `feat/eval-stage2`

```
Read decision 033 and evals/stage1.py.
Goal: one command proves stage 2 is done.
Changes: evals/stage2.py and `make eval` (alias `make eval-stage2`): reuse decision 033's matching with
STAGE = 2 (move shared code within evals/), add #3 (reconciliation_campaign_renamed, entity = platform
campaign id) and #10 (an extraction_rejected_suspicious issue for that document and no core.invoices row
with its number). Include the extraction accuracy from 2.6. Exit 0 only if every #1-#7, #9, #10 is found,
false alarms <= limit, and all malicious invoices are rejected. Update docs/eval-results.md and the
evidence/status column of docs/threat-model.md.
Tests: hand-built keys only; #3 and #10 matching; the stage-1 cases still pass.
Security: T7.
Propose the plan first.
```
**Done when:** `make eval` exits 0 (owner run): extraction accuracy reported, all malicious invoices
rejected, error #3 detected.

---

## Open questions

Decide these before task 2.4. Each has options and a recommendation; none is decided here.

### 1. LLM provider and model for development
The plan mentions Azure OpenAI (EU region) only for stage 5.
- **a) Azure OpenAI, EU region, small model deployment now.** Same as stage 5 and plan §10 privacy;
  needs an Azure subscription and more setup.
- **b) Anthropic API directly** (e.g. Claude Haiku 5.5 for extraction). Quick to start; becomes the
  second provider for stage-5 model comparison and fallback.
- **c) OpenAI API directly.** Quick to start; close to Azure OpenAI, but a different data-processing setup.
- **d) Local model via Ollama.** Free and private; weaker, slower, less reliable structured output.

*Recommendation:* (a) if an Azure subscription is available, so stage 5 is a configuration change;
otherwise (b). Use (d) only as a free smoke test. The adapter interface stays provider-agnostic either way.

### 2. Monthly cost cap
A full run extracts about 128 PDFs (124 invoices + 4 credit notes).
- Monthly: €10 / €25 / €50. Per run: €0.50 / €1 / €2.

*Recommendation:* a €25/month hard budget set at the provider, an app-level monthly cap of €20 and a
per-run cap of €1. Task 2.4 re-checks the per-run figure from real token counts.

### 3. Where the API key lives
- **a) `.env`** (gitignored), read by `app.config`.
- **b) macOS Keychain or 1Password CLI**, injected into the environment at run time.
- **c) Azure Key Vault now** (planned for stage 5).

*Recommendation:* (a) for stage 2, with a dedicated, spend-limited key; gitleaks in pre-commit; never in
CI (fake model) and never on the demo server (`INVOICE_SOURCE=ground_truth`). Key Vault in stage 5.

### 4. Langfuse hosting
Not in the brief, but it decides where trace data goes.
- **a) Langfuse Cloud, EU region, free tier.**
- **b) Self-hosted in a compose profile.** Langfuse v3 needs Postgres, ClickHouse, Redis and object storage.

*Recommendation:* (a), with masked synthetic data and `LANGFUSE_CAPTURE_IO` off by default; (b) later if needed.
