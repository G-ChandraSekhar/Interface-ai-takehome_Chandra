# Computer-Use Automation System (interface.ai take-home)

An LLM discovers a workflow against a live legacy-style banking UI, distills the
run into a typed capability artifact, and replays that artifact deterministically
with no model in the decision loop. See `REPORT.md` for the design write-up
(added at the end of the build).

## Status

- [x] Phase 0 — mock target app (two tenants, chaos injection)
- [x] Phase 1 — guardrails / policy engine
- [x] Phase 2 — discovery agent loop (live LLM run required — see below)
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

## Repository guide (grows each phase)

- `mock_app/` — the fictional legacy target surface (Flask), tenants, seed data, chaos injection
- `src/discovery/` — LLM-driven observe→decide→act loop (Phase 2)
- `src/artifact/` — capability schema + distiller (Phase 3)
- `src/replay/` — deterministic, model-free executor (Phase 4)
- `src/capability_api/` — agent-facing capability catalog/invoke API (Phase 5, stretch goal)
- `src/escalation/` — control lease + operator console for human handoff (Phase 6)
- `src/guardrails/` — allowlist/policy engine + redaction (Phase 1)
- `config/` — policy configuration
- `evidence/` — committed discovery/replay run logs (Phase 8)
