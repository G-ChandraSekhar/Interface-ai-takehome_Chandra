# Computer-Use Automation System (interface.ai take-home)

An LLM discovers a workflow against a live legacy-style banking UI, distills the
run into a typed capability artifact, and replays that artifact deterministically
with no model in the decision loop. See `REPORT.md` for the design write-up
(added at the end of the build).

## Status

- [x] Phase 0 — mock target app (two tenants, chaos injection)
- [x] Phase 1 — guardrails / policy engine
- [x] Phase 2 — discovery agent loop (live LLM run required — see below)
- [x] Phase 3 — artifact schema + distiller
- [x] Phase 4 — deterministic replay (locator fallback, 3-way result, output extraction)
- [x] Phase 5 — agent-facing capability API (stretch goal)
- [x] Phase 6 — human escalation & handoff (discovery-stuck path)
- [x] Phase 7 — tenant overlay (multi-tenant reuse)
- [ ] Phase 2 — discovery agent loop
- [ ] Phase 3 — artifact schema + distiller
- [ ] Phase 4 — deterministic replay engine
- [ ] Phase 5 — agent-facing capability API (stretch goal)
- [ ] Phase 6 — escalation & handoff
- [ ] Phase 7 — tenant overlay demo
- [ ] Phase 8 — evidence + REPORT.md

## Requirements

- Python 3.11+
- An OpenAI API key (only needed for live discovery runs; replay needs no key)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env             # then fill in OPENAI_API_KEY
```

## Phase 0 — run the mock target app

The mock app simulates a legacy core-banking teller console with two tenants
sharing the same underlying product but different branding/routes/labels, plus
deterministic fault injection for testing error handling later.

Start Tenant A (terminal 1):
```bash
TENANT=a PORT=4478 python3 mock_app/app.py
```
Start Tenant B (terminal 2, optional):
```bash
TENANT=b PORT=4479 python3 mock_app/app.py
```

Open in a browser: <http://localhost:4478/desk/login>
Login: `teller1` / `training-only`

Try it manually:
1. Log in → Member Search → enter Member ID `4521` → Search → View record
2. You should see **Dana Whitfield**, balance **2,410.55**
3. Click "Open Sub-Account" → fill the form → Continue → Confirm & Open Account

Try the deterministic fault injection (append `?chaos=<mode>` to a request):
- `?chaos=session_timeout` on a member-detail page → recoverable session-expired interstitial
- `?chaos=error500` on the sub-account confirm step → hard application error (500)
- `?chaos=supervisor` on the sub-account confirm step → requires supervisor code `2468`
- `?chaos=slow` on search/detail → simulated slow load

Business-outcome checks (no chaos needed):
- Member ID `9999` → not found (404)
- Member ID `6600` → restricted / permission denied (403)

Tenant B uses a different field label (`Acct Holder No.`), a different route
prefix (`/operations`), and a different column order on the detail page —
try member `1002` there to see the same person's record rendered differently.

## Phase 1 — guardrails / policy engine

The policy engine (`src/guardrails/engine.py`) enforces an explicit,
configurable allowlist (`config/allowlist.yaml`): which origins the agent
may touch, which action types are permitted, and a three-tier risk
classification (safe / mutating / irreversible) per route pattern. Both the
discovery loop and the replay engine will call `check_action()` before every
single action -- this is the one place the allowlist is enforced, so neither
path can talk its way around it.

Run the test suite to see it in action:

```bash
python3 -m pytest tests/test_guardrails.py -v
```

Expected: 14 tests pass, covering:
- off-allowlist origins are denied outright
- disallowed action types are denied
- safe/read routes are allowed unconditionally
- mutating routes (e.g. opening a sub-account) require either an approved
  artifact or explicit confirmation
- irreversible routes (the final confirm step) require live confirmation
  *even if* the artifact is approved -- approval of the capability doesn't
  waive confirmation on its most consequential step
- step-count and duration budget limits are enforced

`src/guardrails/redact.py` also has a first pass at field-name-based
redaction (mask by known sensitive field name, not by trying to detect
sensitive values) -- this will be wired into logging/evidence in Phase 6-8.

## Phase 2 — discovery agent loop

The discovery loop (`src/discovery/loop.py`) runs an observe -> decide ->
policy-check -> act cycle: it shows the model a semantic digest of the page
(interactive elements by reference, plus the visible page text), the model
picks a tool call, the guardrails engine checks it, and if allowed it
executes against a real Playwright-controlled browser. Login is treated as
an authenticated precondition (handled directly, not by the LLM) so the
LLM-driven trace stays focused on the actual capability being discovered.

### What's tested here without needing your OpenAI key

```bash
python3 -m pytest tests/ -v
```

Expected: **24 passed**. This covers:
- `tests/test_digest_and_tools.py` -- the perception (digest) and action
  (tools) layers against the *real* mock app in a real headless browser, no
  LLM involved: finds form fields correctly, completes the full
  login -> search -> member-detail flow, and enforces guardrails (an
  off-allowlist navigate is denied; a mutating link click is blocked without
  confirmation and allowed with it).
- `tests/test_loop_stub.py` -- the loop's own control flow (marking
  outputs, validating `finish` only completes when every required output
  was marked, detecting a "stuck" model, budget limits) using a scripted
  stub in place of the real OpenAI client, so this doesn't need a key either.
- `tests/test_guardrails.py` -- the Phase 1 policy engine suite, still green.

Two real bugs were caught and fixed while building this phase (both visible
in git history): the mock app's templates used relative form
actions/links, which resolve incorrectly without a trailing slash on the
current URL (`/desk` + relative `search` -> `/search`, silently dropping the
tenant prefix) -- this is the exact bug you kept hitting manually earlier.
The second: the policy engine was checking a link click against the
*current* page instead of the link's *destination*, which would have let a
click into a mutating route slip through from a safe page. Both are fixed
and covered by tests now.

### Running a live discovery capture (needs your OPENAI_API_KEY)

Terminal 1 (leave the mock app running):
```bash
TENANT=a PORT=4478 python3 mock_app/app.py
```

Terminal 2:
```bash
source .venv/bin/activate
python3 -m src.cli discover \
  --goal "Look up member 4521 and read their name and regular savings balance." \
  --tenant a \
  --param member_id=4521 \
  --output member_name \
  --output savings_balance
```

The browser runs headed by default so you can watch it (add `--headless` to
run without a visible window). On success it prints the discovered outputs
and the evidence directory path (`evidence/discovery_<run_id>/`), which
contains `log.jsonl` (structured step-by-step trace), `result.json`, and
`screenshots/`.

## Phase 3 — artifact schema + distiller

The artifact schema (`src/artifact/schema.py`) is a strict, versioned
capability contract (Pydantic, `extra="forbid"`): target scope, typed input
params, typed outputs, an ordered list of steps each carrying a *ranked
locator ladder* (not a single selector), and a URL-pattern checkpoint. The
distiller (`src/artifact/distill.py`) reads a discovery run's `log.jsonl`
and produces one of these — nothing from the raw transcript survives except
the ordered steps, their locator ladders, and which values were
parameters vs fixed.

This phase also extended the discovery log itself: each `click`/`type`/
`select` step now records the acted-on element's accessible name and its
full locator candidate ladder (not just the bare ref like `"e3"`, which
means nothing outside that one run). Without this, the distiller would have
nothing real to freeze into the artifact.

### Tests (no browser or API key needed)

```bash
python3 -m pytest tests/test_artifact.py -v
```

Expected: **8 passed**. Covers: distilling a valid artifact from a
synthetic-but-realistic log, correctly parameterizing the typed member ID
(`input_ref="member_id"`, not frozen as a literal), preserving the locator
ladder per step, building a parameterized checkpoint URL
(`/desk/member/{member_id}`), refusing to distill a run that didn't
succeed, refusing to distill when a required output was never marked, the
strict schema rejecting unknown fields, and a full save/load round-trip
through storage.

### Distilling your real discovery run

Because the log format changed this phase (added `target_name` /
`target_candidates`), your existing evidence run from Phase 2 predates this
and can't be distilled as-is. Run discovery once more to get a compatible
log, then distill it:

```bash
python3 -m src.cli discover \
  --goal "Look up member 4521 and read their name and regular savings balance." \
  --tenant a \
  --param member_id=4521 \
  --output member_name \
  --output savings_balance
```

Note the `Evidence written to: evidence/discovery_<run_id>` path it prints,
then:

```bash
python3 -m src.cli distill \
  --run-dir evidence/discovery_<run_id> \
  --artifact-id lookup_member_savings_balance \
  --name "Look up member savings balance" \
  --param member_id=4521 \
  --output member_name \
  --output savings_balance
```

This writes `artifacts/lookup_member_savings_balance@1.json`. Open it and
read it — it should be understandable as a capability contract without
needing to see the discovery run that produced it. Every `type`/`click`
step should show a locator ladder with 1-2+ real candidates (role/name,
CSS, text), not just a `"positional"` fallback.

## Safety fix — redacting financial output values, not just credentials

Caught a real gap: the guardrails redactor only masked *input* credential
fields (password, supervisor code) by field name. It did **not** mask the
actual banking data a capability returns -- member name, savings balance --
before persisting to `evidence/*/log.jsonl` and `evidence/*/result.json`,
both of which get committed to this public repo. Since every record here is
fictional, nothing real was ever exposed, but the brief explicitly lists
"redaction of regulated financial data" as a safety evaluation criterion,
and the system wasn't actually demonstrating that for output *values* --
only for login credentials.

Fixed by adding `sensitive_output_fields` to `config/allowlist.yaml`
(`savings_balance`, `member_name`, `account_number`). The rule: the
in-memory result returned to whoever invoked the capability (the CLI, or a
calling agent in production) still carries the real value -- that's the
entire point of the capability existing. What gets **persisted to disk**
(the per-step log and the final `result.json`) is masked the same way
credentials already were.

**Documented limitation, not fixed**: the raw balance still appears in the
full page-text observation captured mid-run (the model has to see the value
to report it) and in screenshots (no pixel-level redaction). This mirrors a
limitation the reference architecture also called out explicitly rather
than silently accepting -- worth stating plainly in `REPORT.md`'s Cuts
section rather than pretending redaction is complete everywhere.

### Test (needs a browser -- run on your machine, not verifiable in the sandbox)

```bash
python3 -m pytest tests/test_loop_stub.py::test_sensitive_outputs_are_redacted_on_disk_but_not_in_returned_result -v
```

Expected: passes, proving the returned result keeps the real values while
`result.json` and the `log.jsonl` `output_marked` lines are both masked.

## Phase 4 — deterministic replay

`src/replay/engine.py` executes a saved artifact with **zero LLM
involvement**: resolve each step's locator ladder against whatever page
replay is actually on (falling back down the ladder if the top candidate no
longer resolves, and reporting which tier it needed), check the same
guardrails policy discovery uses, act, then classify the result into one of
three genuinely distinct shapes -- `success`, `business_outcome` (a known,
expected non-success state like "member not found" -- not a crash), or
`failure` (with a class, the step, what was expected, and what was
observed).

Two real design gaps got closed building this phase, both worth knowing
about when defending the design:

1. **Output extraction.** The artifact previously only remembered the
   *value* seen at discovery time (Dana Whitfield's balance), with no rule
   for re-finding a *different* value on a different page. Fixed by
   capturing, at `mark_output` time during discovery, which label sat next
   to the value (e.g. "Regular Savings"), and storing that as an
   `output_extraction` rule in the artifact. Replay re-applies that same
   label against whatever page it actually lands on -- so replaying for
   member `8832` correctly returns *that* member's real balance, not
   Dana Whitfield's frozen one.
2. **Mid-flow business outcomes.** Replaying the exact same click sequence
   for a nonexistent member naturally lands on the "not found" page instead
   of the balance page. Replay checks for known business-outcome/hard-failure/
   recoverable markers *after every action*, not just at the end, so it
   short-circuits cleanly instead of failing while hunting for a checkpoint
   that will never appear.

The artifact schema changed again this phase (`output_extraction`,
`target_url` per step) -- your Phase 3 artifact predates this and needs to
be regenerated. See below.

### Tests (no browser needed)

```bash
python3 -m pytest tests/test_replay_engine.py tests/test_extract.py -v
```

Expected: **15 passed**. `test_replay_engine.py` uses a lightweight fake
Playwright page (not a real browser -- this sandbox can't reach one) to
exercise the engine's actual decision logic end to end: success, a
**different member than discovery used returning that member's own real
data** (the concrete proof of parameterization), both business outcomes
(not-found, permission-denied), a hard application-error failure, a bounded
session-timeout recovery that still reaches success, a locator-ladder
exhaustion failure, a missing-required-param failure, a checkpoint mismatch
failure, and an off-allowlist origin denial.

### Regenerate the artifact, then run real replays

```bash
python3 -m src.cli discover \
  --goal "Look up member 4521 and read their name and regular savings balance." \
  --tenant a --param member_id=4521 --output member_name --output savings_balance
```

Note the `evidence/discovery_<run_id>` path, then:

```bash
python3 -m src.cli distill \
  --run-dir evidence/discovery_<run_id> \
  --artifact-id lookup_member_savings_balance \
  --name "Look up member savings balance" \
  --param member_id=4521 --output member_name --output savings_balance
```

Now the real test -- replay with the **same** member used at discovery:

```bash
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=4521
```

Then the test that actually matters -- replay with a **different** member
(no LLM, no re-discovery, just the artifact):

```bash
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=8832
```

Expected: `status: success`, outputs showing **Marcus Ojo / 918.20** -- a
real, different, correctly-extracted value, not Dana Whitfield's.

Then the two business outcomes -- neither member was ever seen at
discovery, proving the artifact generalizes beyond exactly what discovery saw:

```bash
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=9999   # expect: business_outcome / MEMBER_NOT_FOUND

python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=6600   # expect: business_outcome / PERMISSION_DENIED
```

And a real injected exceptional state -- the mock app supports deterministic
fault injection via a `chaos` flag set at login, and `replay` exposes it
directly:

```bash
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=4521 --chaos session_timeout
```

Expected: `status: success` still (the session-expired interstitial is a
*recoverable* condition), but the step telemetry shows `recovery_applied:
True` on the step that hit it, and `evidence/replay_<run_id>/log.jsonl`
contains a `recovery_applied` event -- this is the "replay that hits an
error or exceptional state" the brief's evidence deliverable specifically
asks for.

Each replay writes its own `evidence/replay_<run_id>/` folder (log,
result.json, screenshot on any non-success outcome) -- **commit a few of
these** (success, the different-member success, and both business outcomes
at minimum) as the required replay evidence.

## Phase 5 — agent-facing capability API (stretch goal)

`src/capability_api/server.py` is a small FastAPI service that makes the
brief's own thesis literal: `GET /capabilities` lists every saved artifact
as an OpenAI-style tool schema (auto-derived from the artifact's own typed
`input_params`/`output_schema` -- never hand-duplicated, so it can't drift),
and `POST /capabilities/{artifact_id}/invoke` runs it through the **exact
same** `replay_artifact()` the CLI uses. Same three-way result, same
guardrails, same evidence trail -- this isn't a second execution path, it's
a second front door onto the one that already exists.

**Deliberate safety boundary**: this API never lets a caller supply
`mutate_confirmed`/`irreversible_confirmed` -- both are hardcoded `False`.
A mutating-tier artifact can only run unattended through this API if it's
itself marked `approved` (a human reviewer's decision baked into the
artifact file, not something a calling agent can set). An irreversible-tier
artifact can **never** run through this API at all -- by construction it
always comes back denied, forcing that class of action through the
human-supervised CLI/escalation path instead.

### Tests (no browser needed for these)

```bash
python3 -m pytest tests/test_capability_api.py -v
```

Expected: **7 passed**. These are real HTTP round-trips through FastAPI's
TestClient hitting the actual `replay_artifact()` engine -- not mocks. The
missing-param and off-allowlist tests work without a browser because
`replay_artifact()` itself returns before ever launching Playwright for
those cases (see Phase 4). Schema generation, single-capability lookup,
404 handling, and an empty catalog are also covered.

### Run it for real and prove the loop closes

Terminal 1 (mock app, as always):
```bash
TENANT=a PORT=4478 python3 mock_app/app.py
```

Terminal 2 (the capability API):
```bash
uvicorn src.capability_api.server:app --port 8000
```

Terminal 3 (an "AI agent" that knows nothing about Playwright, the mock
app, or the replay engine -- only the HTTP API):
```bash
python3 -m src.capability_api.demo_agent --member-id 8832
```

Expected output: the agent discovers `lookup_member_savings_balance` from
`GET /capabilities`, picks it, invokes it with `member_id=8832`, and prints
back `member_name: Marcus Ojo`, `savings_balance: 918.20` -- the same real
data Phase 4's replay CLI produced, now reachable purely through the
capability catalog. Try `curl http://127.0.0.1:8000/capabilities | python3 -m json.tool`
directly too, to see the raw tool schema an agent would actually receive.

## Phase 6 — human escalation & handoff

`src/escalation/` implements Section 3.6's requirement end to end: detect a
stuck/blocked run, route an intervention with context to a human operator,
let them take control of the **same live session** (not a fresh one),
record what they did, and resume.

Four pieces, deliberately small and each independently testable:

- **`lease.py`** — a strict state machine (`AGENT_RUNNING` → `PAUSED` →
  `HUMAN_CONTROL` → `RESUMING` → `AGENT_RUNNING`) that decides who may act
  on the page right now. Illegal transitions raise.
- **`intervention.py`** — the context record routed to the operator: which
  run, which goal/capability, current step, why it stopped, the page URL, a
  screenshot.
- **`console.py`** — a minimal local FastAPI page (loopback-only, no auth
  -- documented scope, not an oversight) with **Take control** / **Hand
  back** buttons. Deliberately a *signaling plane, not a remote desktop*:
  the human drives the real, visible browser window directly with their
  own mouse and keyboard. The console never proxies a single click.
- **`controller.py`** + **`human_recorder.py`** — coordinates the pause/
  resume, and records what the human did (navigation-level, via a
  Playwright event listener attached only while they hold control).

**A real concurrency detail worth knowing if asked about it**: Playwright's
sync API is not thread-safe, but the console runs in a background thread
(uvicorn) while the paused run blocks on the main thread that owns the
page. So the console's HTTP handlers only ever flip the lease's state
(protected by a lock) — they never touch Playwright directly. All the
actual page-touching work (attaching/detaching the recorder) happens inside
`wait_for_handback()`, on the same thread that owns the page, by polling
the lease and reacting to state changes itself.

**Sequencing note**: the richest demo of this (a human entering a
supervisor code on an irreversible confirm step) needs the sub-account
capability, which doesn't exist yet — that's Phase 7. For now, this is
demonstrated via **discovery's stuck path**, which is exercisable with the
existing artifact.

### Tests

```bash
python3 -m pytest tests/test_escalation.py -v
```

Expected: **12 passed**, with no browser needed — including a real
multi-threaded test (`test_wait_for_handback_blocks_until_operator_acts...`)
that starts an actual background thread standing in for the operator,
proves the main thread genuinely blocks until it acts, and confirms the
navigation recorder captured what it did.

One more test needs a real browser — `test_stuck_with_handoff_escalates_and_a_human_can_resolve_it`
in `tests/test_loop_stub.py`. It spins up the *real* console server, uses a
background thread making *real* HTTP calls to it (not mocked) to act as the
operator, while `run_discovery` genuinely blocks on the main thread:

```bash
python3 -m pytest tests/test_loop_stub.py::test_stuck_with_handoff_escalates_and_a_human_can_resolve_it -v
```

### See it happen live (headed browser, real console, you as the operator)

Give the model an ambiguous/impossible goal so it genuinely gets stuck:

```bash
python3 -m src.cli discover \
  --goal "Look up the transaction history for member 4521." \
  --tenant a --param member_id=4521 --output transaction_history \
  --handoff
```

(There's no transaction-history page in this mock app, so the model should
reach a dead end and call this out in plain text rather than a tool call.)

When it gets stuck, the terminal prints a console URL
(`http://127.0.0.1:4590` by default) and pauses. Open that URL in a
browser, click **Take control**, then interact with the *actual* Chromium
window the agent was driving (e.g. navigate to the member's page manually),
and click **Hand back**. The agent resumes and gets one more shot at the
goal with your navigation as new context.

## Phase 7 — tenant overlay (multi-tenant reuse)

Directly answers Section 3.7 and the Section 8 stretch goal: reuse an
artifact across tenants running the same underlying app, rather than
re-recording per tenant. `src/artifact/overlay.py` is a small, reviewable
JSON patch mechanism -- `artifacts/overrides/lookup_member_savings_balance@b.json`
is the actual committed overlay adapting the Tenant A artifact to Tenant B.

What it patches, and why that's *all* it needs to patch: Tenant B's mock
app runs the exact same shared HTML templates as Tenant A, just configured
differently (`mock_app/tenants.py`) -- different origin/route prefix,
different field label ("Acct Holder No." vs "Member ID"), and critically a
different underlying HTML `name=` attribute on that one input
(`acct_holder_no` vs `member_id`). The **"Search" button and "View record"
link steps need zero changes** -- both tenants render identical accessible
names for them, since they come from the same template. So the overlay is
three small edits (`target`, `checkpoint`, and one step's locator), not a
re-recording:

```json
{
  "target": {"tenant": "b", "base_url": "http://localhost:4479", "route_prefix": "/operations"},
  "checkpoint": {"url_pattern": "/operations/member/{member_id}"},
  "step_overrides": {
    "s1": {"target": [{"strategy": "css_name_attr", "value": "input[name='acct_holder_no']"}]},
    "s2": {"target_url": "http://localhost:4479/operations/search"},
    "s3": {"target_url": "http://localhost:4479/operations/member/1002"}
  }
}
```

Output extraction needs **no override at all** -- both tenants render
"Member Name" and "Regular Savings" as the literal label text, so the same
`table_row_label` rules resolve correctly on either tenant's page. This is
the concrete drift signal a production version of this would watch: if a
tenant ever *did* rename those labels, extraction would start failing
loudly (a missing label = `EXTRACTION_FAILED`, not a silent wrong value) --
a natural trigger for "this tenant needs its own overlay reviewed."

### Tests (no browser needed)

```bash
python3 -m pytest tests/test_overlay.py -v
```

Expected: **8 passed**, including the real proof
(`test_overlaid_artifact_actually_replays_successfully_against_tenant_b`):
the Tenant A artifact, patched with the overlay above, correctly replays
against a *simulated* Tenant B page and extracts **Priya Nandakumar /
5,002.00** -- a different real value than any Tenant A member -- reusing
Phase 4's fake-page test harness rather than a live browser.

### Run it for real: the same artifact against two different tenants

Terminal 1 (Tenant A, as always):
```bash
TENANT=a PORT=4478 python3 mock_app/app.py
```

Terminal 2 (Tenant B -- yes, at the same time, different port):
```bash
TENANT=b PORT=4479 python3 mock_app/app.py
```

Terminal 3:
```bash
# Tenant A, unmodified -- same as Phase 4
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 --param member_id=4521

# The SAME artifact, patched, against Tenant B -- no re-recording
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --overlay artifacts/overrides/lookup_member_savings_balance@b.json \
  --param member_id=1002
```

Expected on the second command: `status: success`, outputs showing
**Priya Nandakumar / 5,002.00** -- Tenant B's own real member data, reached
via Tenant B's differently-branded UI, different route prefix, and
different underlying form field, using an artifact that was never once run
against Tenant B during discovery.

## Repository guide (grows each phase)

- `mock_app/` — the fictional legacy target surface (Flask), tenants, seed data, chaos injection
- `src/discovery/` — LLM-driven observe→decide→act loop (Phase 2)
- `src/artifact/` — capability schema, distiller, extraction, and JSON storage (Phase 3-4)
- `src/replay/` — model-free replay engine, locator resolver, detectors, checkpoint matching (Phase 4)
- `src/capability_api/` — agent-facing capability catalog + invoke API, stretch goal (Phase 5)
- `src/escalation/` — control lease, intervention model, operator console, handoff coordination (Phase 6)
- `artifacts/overrides/` — tenant overlays, small JSON patches for cross-tenant reuse (Phase 7)
- `src/replay/` — deterministic, model-free executor (Phase 4)
- `src/capability_api/` — agent-facing capability catalog/invoke API (Phase 5, stretch goal)
- `src/escalation/` — control lease + operator console for human handoff (Phase 6)
- `src/guardrails/` — allowlist/policy engine + redaction (Phase 1)
- `config/` — policy configuration
- `evidence/` — committed discovery/replay run logs (Phase 8)
