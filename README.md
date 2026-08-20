# Computer-Use Automation System

An LLM discovers a workflow against a live legacy banking UI, distills the run
into a typed capability artifact, and replays that artifact deterministically
with no model in the decision loop.

The system now runs against **two** targets: MERIDIAN CORE
([web-sample.interface-hiring.com](https://web-sample.interface-hiring.com/)),
the adaptation target, and the original self-built mock app it was first
developed on. Pointing it at a new console is a YAML file in `config/targets/`
plus, at most, a small adapter — see `docs/ADAPTATION.md` for the audit of what
that actually took, and `REPORT.md` for the original design write-up.

---

# Demo path — MERIDIAN CORE

**Everything below runs without an API key.** Discovery needs one; replay,
the API, and the dashboard do not.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && python3 -m playwright install chromium
python3 -m pytest tests/ -q                      # 172 passing
```

Credentials default to the demo operators MERIDIAN prints on its own sign-on
page, so nothing else is needed to run the demo. To override:

```bash
export MERIDIAN_OPERATOR=teller1 MERIDIAN_PASSWORD=password
export MERIDIAN_SUPERVISOR=super1 MERIDIAN_SUPERVISOR_PASSWORD=password
```

## 1. Start the API and dashboard

```bash
python3 -m uvicorn src.capability_api.server:app --port 4600
```

Open **<http://127.0.0.1:4600>**. One process serves both: the capability
catalog, run history read straight off the evidence bundles, each run's inputs
and structured outputs, its locator-resolution tiers, its screenshots, and its
event log. The **MERIDIAN / All** toggle switches between the current target's
working set and the full archive.

Leave it running — it re-reads evidence every few seconds, so runs from the
next steps appear live.

## 2. Replay a capability — deterministic, no LLM

```bash
python3 -m src.cli replay --artifact-id member_inquiry --version 1   --param member_id=100987 --param search_by=number --headless
```

→ `Turing, Alan` / `2 Bletchley Park, Milton Keynes`

The artifact was discovered against member **100234**. Replaying it against
**100987** returns that member's real values, because outputs are re-derived
from whatever page replay lands on rather than replayed back from discovery.

Reading one share's balance out of the member's share table:

```bash
python3 -m src.cli replay --artifact-id check_member_balance --version 1   --param member_id=100234 --param share_id=100234-S0070 --headless
```

## 3. Invoke it as an agent would

```bash
curl -s localhost:4600/capabilities | python3 -m json.tool | head -40

curl -s -X POST 'localhost:4600/capabilities/member_inquiry/invoke?version=1' \
  -H 'Content-Type: application/json' \
  -d '{"params": {"member_id": "101555", "search_by": "number"}}' \
  | python3 -m json.tool
```

Same replay engine as the CLI, same guardrails, same evidence trail — a second
front door, not a second execution path.

## 4. Exceptional states

**A business outcome — a legitimate answer, not a crash:**

```bash
python3 -m src.cli replay --artifact-id member_inquiry --version 1 \
  --param member_id=999999 --param search_by=number --headless
```

→ `business_outcome` / `MEMBER_NOT_FOUND`. Note MERIDIAN answers this with
**HTTP 200**, which is why classification is marker-driven rather than
status-driven.

**A recoverable condition, forced through MERIDIAN's own fault injection:**

```bash
python3 -m src.cli replay --artifact-id check_member_balance --version 1 \
  --param member_id=100234 --param share_id=100234-S0070 \
  --chaos maintenance --headless
```

The maintenance interstitial is detected, `Continue` is clicked, and the flow
re-navigates back to where it was — `recovery_applied: True` in the step
telemetry. A forced fault fires on *every* request and never clears, so the run
then fails within budget rather than looping. The injection is armed on the
host's own settings screen after sign-on and cleared again in `finally`.

**A hard failure:**

```bash
python3 -m src.cli replay --artifact-id check_member_balance --version 1 \
  --param member_id=100234 --param share_id=100234-S0070 \
  --chaos server --headless
```

→ `failure` / `app_error`, carrying the host's own `ERR-…` reference.

## 5. Escalation — an irreversible action, with a human

Money movement can never run unattended. Check which shares are open first, as
this host is shared and its state moves:

```bash
python3 -c "
from playwright.sync_api import sync_playwright
from src.targets import authenticate
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); pg = b.new_page()
    authenticate(pg, 'meridian')
    pg.goto('https://web-sample.interface-hiring.com/members/100234')
    for ln in pg.locator('body').inner_text().splitlines():
        if ln.count(chr(9)) == 3 and '-S' in ln: print(ln.replace(chr(9), ' | '))
    b.close()
"
```

Then, picking two shares that show `OPEN`:

```bash
python3 -m src.cli replay --artifact-id funds_transfer --version 2 \
  --param member_id=100234 --param from_share=100234-S0001-3 \
  --param to_share=100234-S0070 --param amount=1.00 --param memo="demo" \
  --handoff
```

Replay walks the flow, reaches **Post Transfer**, and stops — the policy engine
classifies that endpoint irreversible and no flag overrides it. It prints an
operator console URL (`http://127.0.0.1:4590`):

1. Open the console, click **Take control**
2. Click **Post Transfer** in the browser window it raises for you
3. Wait for the page to change, then click **Hand back**

Replay resumes, **verifies the resulting state rather than trusting you**, and
extracts the confirmation number. If the action was not performed, it reports
`checkpoint_not_met` rather than claiming success — see
`evidence/replay_20260820T182817Z_457fa0/` for exactly that.

Without `--handoff` the same replay fails closed.

**The refusal path**, no human involved — a teller attempting a supervisor-only
function gets a clean business outcome:

```bash
python3 -m src.cli replay --artifact-id place_account_hold --version 2 \
  --param member_id=102777 --param share_id=102777-S0001 \
  --param reason=LEGAL --param notes="demo" --headless
```

## 6. Record a new capability (needs `OPENAI_API_KEY`)

```bash
cp .env.example .env        # add OPENAI_API_KEY

python3 -m src.cli discover --tenant meridian \
  --goal "Using Member Inquiry, search for member number 100234 and open that member's record. From the member record page, report the member's full name and the mailing address shown there." \
  --param member_id=100234 --param search_by=number \
  --output member_name --output address

python3 -m src.cli distill --run-dir evidence/discovery_<run_id> \
  --artifact-id my_capability --name "My capability" \
  --param member_id=100234 --param search_by=number \
  --output member_name --output address
```

Two constraints the distiller enforces, both learned the hard way and both
documented in `docs/ADAPTATION.md`: every output must be readable from the
single page the capability ends on, and every declared input must be used by
some step — an artifact advertising a parameter nothing consumes would accept a
caller's value and silently ignore it.

## Capabilities

| Capability | Inputs | Outputs |
|---|---|---|
| `member_inquiry@1` | member_id, search_by | member_name, address |
| `check_member_balance@1` | member_id, share_id | member_name, share_balance |
| `update_member_information@1` | member_id, phone | member_name, phone |
| `funds_transfer@2` | member_id, from_share, to_share, amount, memo | confirmation, amount |
| `open_new_share@1` | member_id, share_type, deposit | confirmation, share_type |
| `place_account_hold@2` | member_id, share_id, reason, notes | confirmation, hold_status |

Sign-on is covered as a configured precondition rather than a recorded
artifact — it runs on every capability, for both operator profiles, and is what
the session-timeout recovery calls. `docs/ADAPTATION.md` §7 explains why
recording it would have meant weakening redaction.

## Other commands

```bash
python3 -m src.cli stability --artifact-id check_member_balance --version 1 \
  --param member_id=100234 --param share_id=100234-S0070 --runs 5

python3 -m src.cli health        # locator-ladder drift across replay history
```

---

# The original take-home

Everything below is the phase-by-phase build log for the self-built mock app
this system was first developed against. It is still fully runnable (`--tenant a`
/ `--tenant b`) and its tests are part of the 172 above — the mock is now just
another entry in `config/targets/`, which is what keeps the claim that adapting
is a configuration exercise honest rather than asserted.

## Reviewer quickstart (mock app)


```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && python3 -m playwright install chromium
python3 -m pytest tests/ -q                      # 172 passing

cp .env.example .env                             # add OPENAI_API_KEY for discovery only
```

Terminal 1 — the target app:
```bash
TENANT=a PORT=4478 python3 mock_app/app.py
```

Terminal 2 — the end-to-end thread (goal → discovery → artifact → replay):
```bash
# 1. LLM-driven discovery against the live UI (needs OPENAI_API_KEY)
python3 -m src.cli discover \
  --goal "Look up member 4521 and read their name and regular savings balance." \
  --tenant a --param member_id=4521 --output member_name --output savings_balance

# 2. Distill that run into a reusable capability (use the run id it printed)
python3 -m src.cli distill --run-dir evidence/discovery_<run_id> \
  --artifact-id lookup_member_savings_balance --name "Look up member savings balance" \
  --param member_id=4521 --output member_name --output savings_balance

# 3. Replay deterministically -- no LLM from here on.
#    A DIFFERENT member than discovery used: proves the artifact is a
#    capability, not a recording.
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=8832        # -> success: Marcus Ojo / 918.20

# 4. A business outcome, not a crash
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=9999        # -> business_outcome: MEMBER_NOT_FOUND

# 5. A hard failure, with debuggable detail
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=4521 --chaos error500    # -> failure: app_error
```

Steps 2–5 work without an API key using the committed artifact. Everything
below is a phase-by-phase build log; `REPORT.md` has the design rationale,
and the **Evidence index** at the end of this file maps every committed run
to what it demonstrates.

## Status

- [x] Phase 0 — mock target app (two tenants, chaos injection)
- [x] Phase 1 — guardrails / policy engine
- [x] Phase 2 — discovery agent loop (live LLM run required — see below)
- [x] Phase 3 — artifact schema + distiller
- [x] Phase 4 — deterministic replay (locator fallback, 3-way result, output extraction)
- [x] Phase 5 — agent-facing capability API (stretch goal)
- [x] Phase 6 — human escalation & handoff (discovery-stuck and budget-exceeded paths)
- [x] Phase 7 — tenant overlay (multi-tenant reuse)
- [x] Phase 8 — evidence + REPORT.md
- [x] Adaptation — MERIDIAN CORE: six capabilities, capability API, dashboard
      (see the demo path at the top of this file and `docs/ADAPTATION.md`)

## Requirements

- Python 3.9+ (developed and tested against 3.9; the codebase deliberately
  avoids `X | None` syntax at runtime for this reason — see REPORT.md)
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
- irreversible routes (the final confirm step) can *never* run unattended --
  no artifact approval and no confirmation flag authorizes them; they always
  route to a human taking control of the live session (see the irreversible
  section below)
- step-count and duration budget limits are enforced

`src/guardrails/redact.py` also has a first pass at field-name-based
redaction (mask by known sensitive field name, not by trying to detect
sensitive values). This was later extended to also cover financial *output*
values (member balance, member name), not just credential *inputs* -- see
the safety-fix note in `REPORT.md`.

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

This covers:
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
`mutate_confirmed` -- it's hardcoded `False`, so a mutating-tier artifact
can only run unattended here if it's itself marked `approved` (a human
reviewer's decision baked into the artifact file, not something a calling
agent can set). An irreversible-tier artifact can **never** run through
this API at all: the API passes no handoff route, and an irreversible step
without a human to route to fails closed by construction. That class of
action is reachable only through the human-supervised CLI escalation path.

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

**Scope note**: this section demonstrates escalation from discovery's stuck
and budget-exceeded paths. The *irreversible-tier* escalation — where an
agent is structurally forbidden from completing an action and a human must
perform it — is covered separately below, with its own live evidence and a
real mutating capability (`open_sub_account`).

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
    "s1": {"target": [
      {"strategy": "label_proximity", "value": "Acct Holder No.|input", "confidence": 0.85},
      {"strategy": "css_name_attr", "value": "input[name='acct_holder_no']", "confidence": 0.75}
    ]},
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

### Regenerate a genuine hard-failure replay (real browser, real evidence)

```bash
python3 -m src.cli replay --artifact-id lookup_member_savings_balance --version 1 \
  --param member_id=4521 --chaos error500
```

Expected: `status: failure`, failure class `app_error`. This writes a real
`evidence/replay_<run_id>/` folder — commit it alongside the others as the
brief's required hard-failure evidence.

## Robustness pass

Three further improvements, all adopted after the reference-repo comparison
above and each covered by new tests:

6. **Per-candidate locator diagnostics** (`src/replay/locator_resolver.py`).
   Resolution now records *why* every candidate was rejected —
   `no_match`, `not_unique` (with the element count), `not_visible`,
   `disabled`, `not_applicable`, or `error: <detail>` — rather than
   reporting a bare "nothing resolved." A failure's `observed` field now
   reads like `tier 1 role_name: not_unique (matched 3); tier 2 css_id:
   no_match (matched 0)`, which tells an operator exactly what changed
   about the page instead of requiring them to reproduce the run. This
   pass also added the visibility/enabled checks the resolver previously
   lacked entirely — an element that resolved uniquely but was hidden or
   disabled was formerly accepted and acted on.

7. **Confidence scores on locator candidates** (`src/artifact/schema.py`,
   populated by `src/artifact/distill.py`). Fixed per-strategy priors
   (`role_name` 0.9 → `css_name_attr` 0.75 → `css_id` 0.7 → `text` 0.55 →
   `positional` 0.2) reflecting how tightly each locator kind is coupled to
   things that change. Not measured probabilities — they make the drift
   signal quantitative rather than merely ordinal: a step resolving at 0.55
   is one copy edit from breaking, even while it still passes. Replay also
   now logs a `locator_rescued_by_fallback` event and records
   `rescued_from` telemetry whenever a step resolves above tier 1, so
   creeping drift is visible in evidence before it becomes a failure.

8. **Per-artifact embedded policy** (`ArtifactPolicy` in the schema).
   Each artifact carries its own `allowed_origins` / `allowed_actions`,
   derived at distill time from what the run actually did — the single
   origin it touched and only the action kinds it actually used. Replay
   enforces this **in addition to** the global `config/allowlist.yaml`,
   never instead of it; both must permit an action. The point is that a
   capability can't quietly widen its reach if the global policy is later
   loosened for some unrelated capability's sake: what this artifact's
   reviewer signed off on stays binding. Optional for backwards
   compatibility — artifacts distilled before this field existed have
   `policy: null` and are governed by the global policy alone.

```bash
python3 -m pytest tests/test_locator_diagnostics.py tests/test_replay_engine.py tests/test_artifact.py -v
```

Expected: **37 passed** — covering every rejection reason, artifact-policy
enforcement for both action-kind and origin violations, backwards
compatibility for policy-less artifacts, and confidence-score assignment.

## Irreversible actions: never unattended

The strongest safety property in the system, and the one with the most
direct live evidence. An **irreversible** step (the final "Confirm & Open
Account") cannot execute without a human taking control of the live session
at the moment it happens. This is structural, not flag-dependent:
`PolicyEngine` ignores confirmation flags entirely for that tier, there is
no CLI flag that authorizes it, and an artifact marked `approved` doesn't
help either. Replay and discovery both route such a step to the operator
console; without a handoff route configured, they fail closed.

Critically, after a human performs the action, the agent does **not**
re-perform it — replay verifies the resulting state instead, and discovery
is told explicitly not to retry. Re-clicking an irreversible control after
a human already actioned it would be exactly the double-execution this tier
exists to prevent.

### Both halves, proven live (committed evidence)

**Blocked, with no human available** —
`evidence/discovery_20260816T112803Z_1a2e4f/`:
```bash
python3 -m src.cli discover \
  --goal "For member 4521, open a new Holiday Savings sub-account with a 25.00 opening deposit, and confirm it. Report the member's name and the account type that was opened." \
  --tenant a --param member_id=4521 \
  --output member_name --output account_type --mutate
```
The model completes 9 steps, reaches the confirm step, and is refused. Its
own final message: *"it requires a human to take control for irreversible
actions."* The mock app's request log confirms it never POSTed to
`/subaccount/confirm`.

**Escalated and resolved by a human** —
`evidence/discovery_20260816T113022Z_b107e3/`, same command plus
`--handoff`. The run pauses and prints a console URL; open it, click **Take
control**, click "Confirm & Open Account" yourself in the live Chromium
window, then click **Hand back**. The agent resumes, verifies, and finishes
(`status: success`, 10 steps).

### The resulting mutating capability

`artifacts/open_sub_account@1.json` — distilled from the successful run. Note
what it contains: six steps ending at the **review** page, with the
irreversible confirm step correctly *absent*, because that step isn't
something the agent is permitted to do. Its checkpoint is the review page —
exactly where agent authority ends. Its policy is
`["click", "select", "type"]` (it needed `select` for the account-type
dropdown, which the read-only lookup capability didn't) and still no
`navigate`.

It replays deterministically against a different member than discovery used:
```bash
python3 -m src.cli replay --artifact-id open_sub_account --version 1 \
  --param member_id=8832 --mutate
```
Expected: `success`, **Marcus Ojo / Holiday Savings**
(`evidence/replay_20260816T113525Z_3d7306/`).

## Repository guide

- `mock_app/` — the fictional legacy target surface (Flask), tenants, seed data, chaos injection (Phase 0)
- `src/guardrails/` — allowlist/policy engine + redaction (Phase 1)
- `src/discovery/` — LLM-driven observe→decide→act loop (Phase 2)
- `src/artifact/` — capability schema, distiller, extraction, overlay, and JSON storage (Phase 3, 7)
- `src/replay/` — model-free replay engine, locator resolver, detectors, checkpoint matching (Phase 4)
- `src/capability_api/` — agent-facing capability catalog + invoke API, stretch goal (Phase 5)
- `src/escalation/` — control lease, intervention model, operator console, handoff coordination (Phase 6)
- `artifacts/` — saved capability artifacts; `artifacts/overrides/` holds tenant overlays (Phase 3, 7)
- `config/` — policy configuration
- `evidence/` — committed discovery/replay run logs; see the evidence index below
- `REPORT.md` — the design write-up (Phase 8)

## Evidence index

Every folder below is a real run against the live mock app in a real
browser — no synthetic or hand-written logs. Folder names are timestamps;
this maps them to what each one demonstrates.

| Scenario | Folder | Shows |
|---|---|---|
| Live LLM discovery (read-only) | `discovery_20260816T111307Z_9b5d7f/` | The genuine model-driven run the lookup artifact was distilled from |
| Replay success | `replay_20260816T111405Z_c925d9/` | Deterministic replay, zero LLM, Tenant A |
| Replay with a *different* member | `replay_20260816T090019Z_d17d60/` | Parameterization is real: member 8832 → Marcus Ojo / 918.20 |
| Business outcome — not found | `replay_20260816T090020Z_6dce12/` | `MEMBER_NOT_FOUND`, a legitimate answer, not a crash |
| Business outcome — permission denied | `replay_20260816T090021Z_5add95/` | `PERMISSION_DENIED`, distinct from the above |
| Recoverable exceptional state | `replay_20260816T090022Z_14cc0e/` | Session-expired interstitial dismissed; `recovery_applied: True`, run still succeeds |
| Hard failure | `replay_20260816T105504Z_560603/` | Injected app error → `status: failure`, class `app_error`, with screenshot |
| Human handoff (stuck/budget path) | `discovery_20260816T093504Z_5b952c/` | Real ~40s gap between `operator_took_control` and `operator_handed_back` |
| **Irreversible blocked** | `discovery_20260816T112803Z_1a2e4f/` | Agent refused the confirm step with no human available — fails closed |
| **Irreversible resolved by human** | `discovery_20260816T113022Z_b107e3/` | Escalated, human performed it, agent resumed and completed |
| Mutating capability replay | `replay_20260816T113525Z_3d7306/` | `open_sub_account` replayed for a different member (8832) |
| Tenant B via overlay | `replay_20260816T111549Z_2b9383/` | Same artifact + small patch → Priya Nandakumar / 5,002.00 on Tenant B |
| Capability API invocation | `replay_20260816T090842Z_5d4451/` | Invoked over HTTP by the demo agent, not the CLI |
| Redaction verification | `replay_20260816T105541Z_e3cd4c/` | `result.json` shows `***REDACTED***` for financial outputs |

Each folder contains `log.jsonl` (structured step-by-step trace) and
`result.json`. Runs that ended in a non-success state also carry a
`screenshots/` capture of the page at that moment; discovery runs capture
final-state screenshots as well.

The `evidence/` directory also holds several earlier runs of the same
scenarios, kept because they're genuine and show the project's actual
history rather than a curated final state — e.g. `discovery_…ea0a12`,
`…0bb3ad`, and `…6fc61e` are earlier discovery captures from before the
artifact schema gained output extraction and per-artifact policy;
`replay_…939a24`, `…2fbb47`, `…802bad`, and `…c10814` are earlier
replay/overlay runs from the corresponding stages. The table above points
to the current, most complete run for each scenario.
