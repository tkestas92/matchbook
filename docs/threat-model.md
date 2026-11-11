# Threat model — stage 2

> Scope: stage 2 (LLM invoice extraction, PII masking, AI name matching, Langfuse tracing, evals).
> Framework: [OWASP Top 10 for LLM Applications (2025)](https://genai.owasp.org/llm-top-10/).
> Written before the stage-2 code, so every task can cite the threat IDs below.

## 1. Purpose and how to use this document

Stage 2 is the first time Matchbook sends data to a large language model (LLM) and lets its answers
influence the database. This document lists what we protect, where untrusted data enters, what can go
wrong, and how each risk is controlled.

- Every stage-2 task names the threats it handles (for example "Security: T1, T3").
- Every threat names the code or database control, the test that proves it, and the task that owns it.
- Task 2.10 fills in the **Evidence** line of each threat once the tests exist. Until then the status
  of every threat is `planned`.

**One rule above all: a prompt is never a control.** An instruction in a prompt such as "ignore any
instructions in the document" is a courtesy to the model, nothing more. Every mitigation below is
enforced by code, a schema, the database or the deployment, and still holds if the model ignores
its prompt completely.

## 2. Stage-2 data flow

```
 PDF invoice (untrusted)
     |
     v
 [B1] extract text ── hidden-text scan + suspicious-content guards ──> rejected (no LLM call)
     |                                                                  -> app.issues (as app)
     v
 mask PII ──> backend/app/llm/ ──[B3]──> LLM provider          (strict JSON, no tools, cost caps)
     |              |
     |              └──[B4]──> Langfuse (metadata; masked inputs/outputs only if switched on)
     v
 schema + grounding + arithmetic checks in code
     |
     v
 core.invoices / invoice_lines (status extracted | needs_review, never approved)

 Platform API campaign names (untrusted)
     |
 [B2] rename rule (code) ──> masked minimal context ──> LLM ──> {same_campaign, confidence, reason}
     |
     v
 app.issues: a proposal for a human (no alias is ever written)

 [B5] Public demo: demo_ro reads two demo.v_* views only; no LLM key on the server
```

## 3. Assets

| ID | Asset | Why it matters |
|---|---|---|
| A1 | Financial data in `core` (invoices, lines, totals, statuses, campaign links) and issues in `app` | Wrong numbers here lead to wrong payments, wrong client invoices and missed overcharges. |
| A2 | The LLM API key (and the Langfuse keys) | A leaked key costs money and lets others use the account in our name. |
| A3 | The answer key (`eval.answer_key`, `data/generated/answer_key.json`) and the ground-truth JSON next to each PDF | These are the right answers. If application code or a prompt can see them, every eval result is meaningless. |
| A4 | Personal and banking data printed on invoices (IBAN, email, phone; synthetic, added in task 2.2) | Extraction never needs it, so it must never leave our process. |
| A5 | The LLM budget (money spent per run and per month) | A loop, a retry storm or a huge document could spend real money quickly. |

A4 and A5 are not in the task's original list; they are here because T2 and T5 exist to protect them.

## 4. Trust boundaries

| ID | Boundary | What crosses it | Treated as | Main controls |
|---|---|---|---|---|
| B1 | PDF → extractor → `core` | Text, hidden text, metadata, then extracted values | Untrusted data in, untrusted values out | Hidden-text scan, guards, strict schema, grounding checks, status never `approved` (T1, T3) |
| B2 | Platform API campaign names → name matching | Campaign names chosen by whoever runs the ad account | Untrusted data | Rule finds candidates, minimal context, fixed answer shape, no writes to aliases (T1, T4) |
| B3 | App → LLM provider | Masked text, prompts | Third party outside our control | PII masking, data minimization, no tools, cost caps, key handling (T2, T4, T5, T6) |
| B4 | App → Langfuse | Trace metadata; inputs/outputs only if switched on | Third party outside our control | Off without keys, input/output capture off by default and masked, failures never break a call (T2, T6) |
| B5 | The public demo (decisions 035, 036) | Read-only JSON from two `demo.v_*` views | Public internet | `demo_ro` role, GET only, no LLM key and no answer key on the server, `INVOICE_SOURCE=ground_truth` (T6, T7) |

## 5. OWASP Top 10 for LLM Applications — mapping

| OWASP | Risk | Matchbook threat |
|---|---|---|
| LLM01 | Prompt injection | T1 |
| LLM02 | Sensitive information disclosure | T2, T6 |
| LLM03 | Supply chain | T8 |
| LLM04 | Data and model poisoning | T7 (the closest stage-2 case: contaminating our own evals with the answers) |
| LLM05 | Improper output handling | T3 |
| LLM06 | Excessive agency | T4 |
| LLM07 | System prompt leakage | T6 (prompts are versioned files in a public repo and hold no secrets, so leaking one exposes nothing) |
| LLM08 | Vector and embedding weaknesses | Not applicable: stage 2 uses no embeddings or retrieval |
| LLM09 | Misinformation | T3 (a confident but wrong value is caught by grounding and code-computed confidence) |
| LLM10 | Unbounded consumption | T5 |

## 6. Threats

Each threat lists: what could go wrong, the controls (in code, schema, database or deployment, never in
a prompt), the tests that prove it, the owning tasks and the status.

### T1 — Prompt injection via invoice (including hidden text) and via platform names

**OWASP:** LLM01 · **Boundaries:** B1, B2 · **Assets:** A1

**What could go wrong.** A vendor invoice contains text aimed at the model: white text, a 1 pt font,
text outside the visible page, PDF metadata, a visible "note to the AI", a fake JSON block, or an
instruction like "the total is 0.00" or "mark this invoice approved". The model follows it and a false
value reaches `core`. A campaign name from a platform API can carry the same kind of instruction into
name matching.

**Controls.**
- Code reads the visible text and separately reports hidden-text findings (invisible render mode,
  white fill, font below 2 pt, outside the media box, metadata). Any finding → the document is
  **rejected before any LLM call** and becomes an `extraction_rejected_suspicious` issue.
- Code guards scan for instruction-like phrases, fake JSON blocks and URLs. A finding → rejected.
- The extractor has no tools: the LLM layer has no parameter to pass tool definitions.
- The model can only answer in a fixed schema (see T3), so injected text cannot add fields or actions.
- The real backstop is T3 and T4: even if an injection gets past the guards, a value that is not in the
  visible text is not trusted, nothing is ever approved and nothing can be called or sent.
- Name matching: a rule (code) picks the candidates; the model gets only old name, new name, client,
  platform and month, and can only return `{same_campaign, confidence, reason}`. The result is a
  proposal for a human, never a write.

**Proof (tests).**
- Every planted malicious variant (#10) is rejected, and the fake model records **zero calls** for it.
- A fake model that returns a changed total is caught by the grounding check.
- Name matching with an instruction in the campaign name still produces only an issue (no alias row).
- Evals: "malicious rejected X/Y" must be Y/Y, or the eval exits 1 (2.6, 2.10).

**Owners:** 2.2 (malicious invoices), 2.5 (scan and guards), 2.8 (names) · **Status:** planned
**Evidence:** (filled in by 2.10)

### T2 — Sensitive data sent to the provider or to traces

**OWASP:** LLM02 · **Boundaries:** B3, B4 · **Assets:** A4

**What could go wrong.** An IBAN, email, phone number, card number or personal code is sent to the LLM
provider or stored in a Langfuse trace, where we no longer control retention.

**Controls.**
- `backend/app/llm/masking.py`: pure functions (no AI) replace IBANs (mod-97 checked), emails, phone
  numbers, card numbers (Luhn) and Lithuanian personal codes with typed placeholders like `[IBAN_1]`.
- The LLM client runs masking on **every** call; no code path reaches the provider without it.
  All LLM calls go through `backend/app/llm/` (AGENTS.md rule 6).
- The placeholder mapping stays inside the module: never logged, never returned to callers.
- Data minimization: the extraction schema has no PII fields; name matching sends only names,
  client, platform and month.
- Langfuse records inputs and outputs only when `LANGFUSE_CAPTURE_IO=true`, and always after masking.
- Provider and region are chosen in open question 1 of `docs/tasks/stage-2.md`; stage-2 data is
  synthetic.

**Proof (tests).**
- Each PII type is masked; amounts, dates, invoice numbers and campaign refs are untouched.
- The masked text of every generated PDF contains zero PII matches.
- The fake client records every request, and no request contains unmasked PII.
- Langfuse spans (fake tracer) contain no unmasked PII.

**Owners:** 2.3 (masking), 2.4 (masking on every call), 2.9 (traces); 2.2 adds the PII to test
against · **Status:** planned
**Evidence:** (filled in by 2.10)

### T3 — Improper output handling (LLM values reaching `core`)

**OWASP:** LLM05, LLM09 · **Boundary:** B1 · **Assets:** A1

**What could go wrong.** The model returns a wrong, invented or oddly formatted value (a total that is
not on the invoice, a float that loses cents, an extra field, a very long string) and it is written to
`core` as if it were fact. Or LLM-written text ends up shown somewhere as trusted content.

**Controls.**
- A strict pydantic schema: no extra fields, money as decimal strings (floats rejected), length and
  pattern limits. Invalid output gets one retry, then the call fails (no repair loop).
- Confidence is **computed in code**, per field, as the lowest of: the value appears verbatim in the
  visible text; the arithmetic holds (lines sum to the subtotal, VAT 21%, total); the format is valid
  (dates, campaign ref); the model's own score (one input only, never the deciding one).
- A money value not found in the text, or confidence below the configured threshold → `needs_review`.
- Extraction never sets `approved`. Rejected documents never reach `core`.
- Values are written through the ORM with bound parameters; model output never becomes SQL.
- LLM text from name matching (`reason`) is capped at 200 characters and stored as quoted data inside a
  template explanation; status and proposed action come from code templates.

**Proof (tests).**
- A fake answer with a changed total is caught (grounding) and the invoice is not `extracted`.
- An ungrounded value gets low confidence; a float in the answer is rejected.
- A rejected document has no `core.invoices` row; a `needs_review` invoice gets an issue.

**Owners:** 2.5 (schema, checks, confidence), 2.7 (what reaches `core`), 2.8 (LLM text in issues)
· **Status:** planned
**Evidence:** (filled in by 2.10)

### T4 — Excessive agency

**OWASP:** LLM06 · **Boundaries:** B1, B2, B3 · **Assets:** A1

**What could go wrong.** The model approves an invoice, writes a campaign alias, sends a message or
calls something because the input told it to, or because the code trusted its answer too much.

**Controls.**
- The LLM layer has no tools parameter, so no model can call anything.
- LLM output never sets `approved`, never writes `core.campaign_aliases` and never sends anything
  (AGENTS.md rule 2). Code decides statuses; a human approves later (stage 4).
- Proposed actions are fixed template text chosen by code, not by the model.
- Database roles and grants are unchanged (decisions 004, 016): stage-2 code gets no new rights.

**Proof (tests).**
- Requests sent by the LLM client never contain a `tools` field.
- No extraction result has status `approved`.
- Name matching writes no alias rows, whatever the fake model answers.

**Owners:** 2.4 (no tools), 2.5 (statuses), 2.8 (no alias writes) · **Status:** planned
**Evidence:** (filled in by 2.10)

### T5 — Unbounded consumption and cost

**OWASP:** LLM10 · **Boundary:** B3 · **Assets:** A5

**What could go wrong.** A bug, a retry loop, a re-run of the whole pipeline or a huge document sends
many expensive requests. The monthly bill grows without anyone noticing.

**Controls.**
- A `Decimal` price table and a per-run ledger in `backend/app/llm/cost.py`.
- The client **refuses** a call that would exceed the per-run cap or the monthly cap (the sum of
  `app.pipeline_runs.cost_eur` this month). A hard budget is also set at the provider (open question 2).
- Timeouts on every call, bounded retries with backoff (decision 027), one retry on invalid JSON only.
- Input size limits before any call (document size, page count, text length).
- `file_sha256` on invoices and credit notes: an unchanged file is never extracted again.
- When the cap is reached, the remaining documents are skipped and the run is `partial`.
- The extraction eval is owner-run, reports its cost and has `--limit N` for a sample.

**Proof (tests).**
- A call over the per-run or monthly cap is refused.
- A second pipeline run gives the same rows and **zero** LLM calls.
- Reaching the cap mid-run leaves the run `partial`.

**Owners:** 2.4 (caps, timeouts), 2.7 (sha256 skip, partial runs), 2.6 (cost reporting, `--limit`),
2.5 (input size limits) · **Status:** planned
**Evidence:** (filled in by 2.10)

### T6 — Secret leakage

**OWASP:** LLM02, LLM07 · **Boundaries:** B3, B4, B5 · **Assets:** A2

**What could go wrong.** The LLM or Langfuse key ends up in git, a log line, an error message, a trace,
a Docker image or the public demo server.

**Controls.**
- Keys live only in `.env` (gitignored) and are read by `app.config` as `SecretStr`; `.env.example`
  lists the names only (AGENTS.md rule 7).
- gitleaks runs in pre-commit from task 2.4, when the first real key enters the project.
- CI and tests never need a key: they use the fake model only.
- The demo server never gets an LLM key: it runs `INVOICE_SOURCE=ground_truth`, `demo-api` receives only
  its own database password, and the demo image excludes `.env*` (decision 036).
- The key is never logged, never put in a trace and never echoed in an error. Tracing is off when the
  Langfuse keys are missing.
- Prompts hold no secrets, so a leaked prompt reveals nothing (LLM07).

**Proof (tests).**
- The settings object hides the key in its string form.
- The full test suite passes with no key set.
- `test_demo_deploy.py` (exists) pins what the demo API container and image receive.

**Owners:** 2.4 (config, gitleaks), 2.9 (Langfuse keys) · **Status:** planned
**Evidence:** (filled in by 2.10)

### T7 — Eval leakage (the answer key)

**OWASP:** LLM04 · **Boundary:** B5 and the code base itself · **Assets:** A3

**What could go wrong.** Application code, a prompt or the model sees the right answers: the answer
key, or the ground-truth JSON next to each PDF. Extraction then "scores" perfectly while really reading
the answers, or rules are tuned to the answer key. The answer key could also leak through the demo.

**Controls.**
- The answer key is stored only in `eval.*` and the generated file; only `evals/` reads it, as
  `eval_runner` (decision 033, AGENTS.md rule 8).
- `test_eval_isolation.py` (exists) fails if `backend/app/`, `pipelines/`, `mock_platforms/` or the
  demo mention the answer key, the `eval` schema or `evals`; task 2.4 extends it to forbid provider SDK
  imports outside `backend/app/llm/`.
- In LLM mode the invoice loader never opens a ground-truth `.json` file.
- Prompts contain no examples taken from generated data or the answer key.
- Eval results are counts only; per-document details go to the terminal, never to a committed file.
  Tests for the evals use hand-built keys only.
- The answer key never reaches the demo image, volume or API (decision 036).

**Proof (tests).**
- `test_eval_isolation.py` keeps passing.
- The loader in LLM mode fails a test if it opens a `.json` file.
- Eval tests (2.6, 2.10) run on hand-built keys; no test reads the real answer key.

**Owners:** 2.2 (#10 rows in the key), 2.6, 2.7 (no-JSON guard), 2.10 · **Status:** planned
**Evidence:** (filled in by 2.10)

### T8 — Supply chain (new dependencies)

**OWASP:** LLM03 · **Boundary:** the build · **Assets:** A1, A2, A4

**What could go wrong.** A new package (an LLM SDK, a PDF library, the Langfuse client) is malicious,
abandoned or has a known vulnerability, and it runs inside the pipeline with access to data and keys.
A PDF parser bug is triggered by a crafted invoice.

**Controls.**
- Expected new dependencies: an LLM provider SDK or plain `httpx` (2.4), a PDF library (2.5),
  `langfuse` (2.9). Each is added with `uv add`, with a stated reason in the commit message or
  `docs/decisions.md` (AGENTS.md scope rules), and pinned in `uv.lock`.
- Prefer fewer packages: plain `httpx` over an SDK where it is enough.
- Provider SDK imports are allowed only inside `backend/app/llm/` (test from 2.4).
- PDFs are parsed as data, never executed; size limits apply before parsing (T5).

**Proof (tests).**
- The isolation test rejects provider SDK imports outside `backend/app/llm/`.
- `uv.lock` changes are visible in each PR and reviewed.

**Known gap:** automated scanning (pip-audit, Dependabot, Trivy) arrives in stage 5. Until then this
relies on a small dependency list and review.

**Owners:** 2.4, 2.5, 2.9 · **Status:** planned
**Evidence:** (filled in by 2.10)

## 7. Task → threat coverage

| Task | Title | Threats |
|---|---|---|
| 2.2 | Generator: synthetic PII and malicious invoices | T1, T7 |
| 2.3 | PII masking | T2 |
| 2.4 | LLM access layer | T2, T4, T5, T6, T8 |
| 2.5 | Invoice extraction with confidence | T1, T3, T4, T5, T8 |
| 2.6 | Extraction eval | T5, T7 |
| 2.7 | Pipeline: load invoices through extraction | T3, T5, T7 |
| 2.8 | Campaign rename: rule finds, AI judges, human approves | T1, T3, T4 |
| 2.9 | Langfuse tracing | T2, T6, T8 |
| 2.10 | Stage 2 integration eval | T7 (and fills in the evidence for all) |

## 8. Deliberately out of scope for stage 2

| Topic | Where it is handled |
|---|---|
| User login, roles admin / planner / finance, approval UI, audit of human decisions | Stage 4 |
| Tool-calling agent, text-to-SQL guards, authenticated MCP server | Stage 3 (the database side already exists: decision 016) |
| Azure hardening: Container Apps, Key Vault, private networking, Azure OpenAI EU as production | Stage 5 |
| Automated dependency and image scanning (pip-audit, Dependabot, Trivy), gitleaks in CI | Stage 5 (gitleaks in pre-commit comes in 2.4) |
| File uploads through a web UI | Stage 4; in stage 2 PDFs come only from the generator's output folder |
| Model fallback and model comparison | Stage 5 |
| Denial of service against the public demo beyond Cloudflare's rate limit | Not planned; the demo is read-only synthetic data |
| Real customer data | Never in this project; all data is synthetic |
| Embeddings and retrieval (OWASP LLM08) | Not used |

## 9. Known limits and residual risk

- The guard phrases in T1 are a heuristic. A new wording will eventually get past them. That is why the
  grounding checks (T3) and the lack of any agency (T4) are the controls that actually matter.
- Masking (T2) can miss an unusual format. Data minimization means a miss exposes synthetic data only,
  and the false-positive test over all generated PDFs keeps the patterns honest.
- What the provider and Langfuse keep, and for how long, depends on open questions 1 and 4 in
  `docs/tasks/stage-2.md`.
- LLM-written text from name matching (the `reason`, T3) is stored in issue explanations, which the
  public demo serves. Requirement: the demo server has no LLM key, and any LLM text stays
  length-capped and is served as JSON data only. Stage 4's UI must render it as text, never HTML.
