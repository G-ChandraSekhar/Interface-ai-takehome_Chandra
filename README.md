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

## Repository guide (grows each phase)

- `mock_app/` — the fictional legacy target surface (Flask), tenants, seed data, chaos injection
- `src/discovery/` — LLM-driven observe→decide→act loop (Phase 2)
- `src/artifact/` — capability schema, distiller, extraction, and JSON storage (Phase 3-4)
- `src/replay/` — model-free replay engine, locator resolver, detectors, checkpoint matching (Phase 4)
- `src/replay/` — deterministic, model-free executor (Phase 4)
- `src/capability_api/` — agent-facing capability catalog/invoke API (Phase 5, stretch goal)
- `src/escalation/` — control lease + operator console for human handoff (Phase 6)
- `src/guardrails/` — allowlist/policy engine + redaction (Phase 1)
- `config/` — policy configuration
- `evidence/` — committed discovery/replay run logs (Phase 8)
