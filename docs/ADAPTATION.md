# MERIDIAN Adaptation — Fidelity Audit & Development Log

**Branch:** `adaptation/meridian`
**Target:** [web-sample.interface-hiring.com](https://web-sample.interface-hiring.com/) (MERIDIAN CORE)
**Date:** 20 August 2026

This is the working record behind the adaptation. It answers three questions:

1. Did the adaptation change the core, or extend it?
2. What did the new requirements get, and why was each decision made that way?
3. What was cut, and what is still wrong.

---

## 1. The short answer

**The core was extended, not rewritten.** Twenty modules are byte-identical to
`main`, including every module carrying a safety or correctness guarantee. Six
changed, all additively — §2.3 lists every removed line and what replaced it.

The single most load-bearing fact: **`src/guardrails/engine.py` has zero
changes.** MERIDIAN's origin, its three risk tiers, and its irreversible
posting endpoints are all expressed in `config/allowlist.yaml`. The policy
engine was pointed at a new bank without being edited, and this still holds on
the new target:

```
hold/post, confirmed=True, artifact_approved=True  ->  require_confirmation
```

---

## 2. Fidelity audit

### 2.1 Untouched (20 modules)

Verified by `git diff main -- <file>` returning empty for each:

| Area | Modules |
|---|---|
| **Guardrails** | `engine.py`, `redact.py`, `result.py` |
| **Replay internals** | `locator_resolver.py`, `checkpoint.py`, `result.py`, `stability.py` |
| **Telemetry / drift** | `record.py`, `health.py` |
| **Artifact storage** | `store.py`, `overlay.py` |
| **Perception & evidence** | `digest.py`, `evidence.py`, `llm_openai.py` |
| **Escalation core** | `lease.py`, `console.py`, `intervention.py`, `human_recorder.py` |
| **Capability API** | `registry.py` (+ `server.py`, extended only additively) |

The locator ladder, the tenant overlay mechanism, the control-lease state
machine, the drift detection, the evidence writer, and the multi-run stability
scoring all work on MERIDIAN **exactly as written for the mock app**.

### 2.2 Changed, and the ratio

Overwhelmingly additive. `src/artifact/extract.py` is the only rewrite, and it
is a 40-line text parser rather than architecture.

### 2.3 Every removed line, and what replaced it

**`schema.py`** — one line:

```
- strategy: Literal["table_row_label"]
+ strategy: Literal["table_row_label", "table_grid_cell"]
```

A widened enum. New fields are all `Optional` with `None` defaults, so a
previously-distilled artifact still validates unchanged.

**`engine.py`** — four:

| Removed | Replaced by | Why |
|---|---|---|
| `_mock_login()` | `_establish_session()` → `targets.authenticate()` | It was duplicated verbatim in `loop.py`. Two auth paths would make "adaptation is configuration" false by construction. Its chaos-field injection is preserved in `config/targets/mock.yaml`. |
| `extract_by_label(...)` | `apply_extraction(rule, params)` | Dispatches on rule strategy; `table_row_label` behaves identically. |
| hardcoded `Continue` click | `_apply_recovery()` | A pattern with **no declared recovery still gets the original click-through**, pinned by `test_a_pattern_with_no_declared_recovery_keeps_the_original_behaviour`. |
| `select_option(label=...)` | `_select_option()` | Tries `value=` first, **falls back to `label=`** — the original behaviour is the fallback, not removed. |

**`distill.py`**, **`loop.py`**, **`tools.py`**, **`detectors.py`** — additive,
apart from `_mock_login()` moving to config. Each widened path retains its
previous behaviour as an explicit fallback:
`_default_detectors_for_tenant()` returns `detectors.py`'s module constants
when a target declares none; the extraction builder still accepts a bare
`extraction_label` from logs recorded before this change.

### 2.4 Backward compatibility, tested not assumed

- The **13 browser tests** drive the real mock app end to end through the new
  `authenticate()`, the new extractor, and the new recovery dispatch. All pass.
- `load_target("a")` and `load_target("b")` resolve to `mock.yaml`, so every
  artifact distilled before today still replays.
- `config/targets/mock.yaml` declares `resume: false` for the mock's
  session-expired interstitial, because that one *does* return you in place.
- `artifacts/lookup_member_savings_balance@1.json` — which has no `detectors`
  and no `stability` — still loads and replays.

---

## 3. Principle-by-principle check

| Principle | How it held |
|---|---|
| **Simplicity first / minimal impact** | 20 modules untouched. Largest change is a text parser. |
| **No laziness — root causes** | The driver-resync bug could have been a `sleep()`; the cause is named and addressed. The prompt flaw was fixed *and* enforced at distill so it cannot recur silently. |
| **Verification before done** | Every change run against the live target, not just unit-tested. Seven of eight defects found by reading real output. |
| **Demand elegance** | The token "problem" was solved by *not building* a primitive — §4.1. |
| **One allowlist, one enforcement point** | Unchanged. Discovery and replay still call the same `check_action()`. |
| **Irreversible actions never run unattended** | Preserved, and defended twice: against a bug that would have quietly tiered a post step SAFE (§4.5), and against building a supervisor-credential bypass (§4.7). |
| **Artifact is a contract, not a transcript** | `extra="forbid"` still holds. And now enforced harder — §4.11. |
| **Business outcome ≠ failure** | Extended: 6 business outcomes, 2 recoverable, 1 hard failure, declared per target. |
| **Human judgment not automated away** | No automation gate added. Escalation still requires a human at the moment the action happens. |
| **Honest documentation of limits** | §7 lists everything outstanding, including two known defects left in place. |

---

## 4. What the new requirements got

### 4.1 The per-transaction token — deliberately *not* built

The brief flags MERIDIAN's hidden `_token` as a headline difficulty, and the
plan was to extend the schema with a read-and-bind primitive.

**Reconnaissance killed that plan, correctly.** `_token` is a hidden `<input>`
inside the form. Because the core drives the real browser and clicks the real
submit button, the browser serialises that field natively. Verified live:
`smoke.py` posted a transfer without ever reading the token. It is also
session-scoped rather than per-transaction — identical across `/settings`,
`/transfer`, `/update`, and `/hold` within one session, and unchanged after a
completed POST.

**Decision: build nothing.** An unused abstraction invites "did you need
that?", a question with no good answer.

The write-up line is the inverse and it is stronger: *MERIDIAN's
per-transaction token required no special handling, because we never leave the
UI. Reconstructing requests would have required reading and re-injecting it;
driving the actual form does not.*

### 4.2 Target configuration seam

**Built:** `src/targets.py`, `config/targets/meridian.yaml`,
`config/targets/mock.yaml`. One `authenticate()` replaces two duplicated
`_mock_login()` functions. Credentials come from env vars, defaulting to the
target's own published demo operators — runs with no setup, no secrets in the
repo.

**Rejected:** leaving the mock on its old path. Two auth paths would have
invalidated the central claim.

### 4.3 Extraction: multi-pair rows and data grids

**A real bug in existing code, exposed by the new target.** MERIDIAN renders:

```
Member No.:\t100234\tName:\tLovelace, Ada
```

Two label/value pairs on one line. The parser partitioned on the first tab:

| Call | Before |
|---|---|
| `extract_by_label('Member No.')` | `'100234\tName:\tLovelace, Ada'` — wrong value, garbage appended |
| `extract_by_label('Name')` | `None` — unreachable |
| `find_label_for_value('Lovelace, Ada')` | `None` |

That third one is the blocker: `distill.py` refuses without an extraction rule,
so discovery would have completed and *then* failed at distillation with a
misleading error — one wasted LLM run per attempt.

**Also added `table_grid_cell`** for the shares table: "the Balance where Share
ID is X" is a two-dimensional lookup with no expression in the schema at all.

**Decision — disambiguate by the colon convention.** A 4-cell line is
ambiguous: `Member No.:\t100234\tName:\tLovelace, Ada` is two pairs,
`Share ID\tType\tBalance\tStatus` is a grid header. Legacy screens punctuate a
*field label* and don't punctuate a *column heading* — the same signal a human
reading the screen uses. **Rejected:** cell-count heuristics (both are 4
cells), and requiring the artifact to declare the shape (the distiller has to
infer it).

**Decision — grid row keys bind as `key_input_ref`,** mirroring
`input_ref`/`literal_value`. This is what makes `check_member_balance` work for
any share rather than one frozen one.

### 4.4 Detector taxonomy and declarative recovery

**Decision — markers primary, HTTP status advisory.** Forced by evidence:

- natural "no member records matched" → **HTTP 200**
- injected validation fault and a legitimate insufficient-funds outcome → **both 400**

Status-first classification would miss the most common business outcome on the
target and conflate two others. Status is recorded for evidence and in the
failure detail, never used to classify.

**Decision — `RecoveryAction` with a `resume` flag.** Both MERIDIAN recoveries
land you somewhere else: maintenance's `Continue` goes to `/menu`, a timeout
drops to `/signon`. Recovery without resume would continue the flow against the
wrong page *while reporting that it recovered*. The mock's interstitial does
return you in place, so it is declared `resume: false` and its behaviour is
unchanged.

`reauthenticate` calls back into `authenticate()` — only possible because
sign-on became configuration.

### 4.5 Irreversible steps performed by a human

**Gap inherited from the take-home:** `open_sub_account@1` stops at review with
no confirm step. On MERIDIAN that bites three of six capabilities.

**Built:** a human-resolved step is logged as performed, so the distiller
includes it and replay knows where to pause.

**Then caught a second defect on inspection.** The first version recorded
`page.url` rather than the click's destination, so `s11` pointed at
`/transfer/review`. Replay would have tiered it **SAFE** and posted money
**without pausing for a human.**

The artifact looked correct. Replay would have succeeded. All tests were green.

> A guarantee enforced by a URL recorded in a data file is only as strong as
> that URL being right. The tier check was never wrong — it was asked the wrong
> question.

Two tests pin it, including one that fails if anyone later demotes a posting
endpoint in the allowlist.

### 4.6 Select binding and canonical values

Selects always froze as `literal_value` — a transfer that could only ever move
money between two fixed accounts. Worse, the model picks by visible label,
which on MERIDIAN is `100234-S0070 - Share Draft (Checking) ($234.55)`.
Freezing that binds the capability to a **balance**: correct once, then
silently stops matching.

**Built:** read back the option's underlying value after selection, record
that, bind to parameters, and select by `value=` with `label=` fallback — in
both `tools.py` and `engine.py`, so capture and replay cannot diverge.

### 4.7 Supervisor gating — the decision not to build a bypass

MERIDIAN models supervisor as a separate login. The fast option was to let the
Place Hold artifact carry `super1`'s credentials and post unattended.

**Rejected.** The safety claim is structural: *an irreversible action cannot
execute without a human in the loop at the moment it happens.* An artifact
holding supervisor credentials degrades that to "the artifact had the right
credentials" — not a claim a bank's risk function would accept.

**And it costs nothing.** MERIDIAN yields two demo runs from the same decision:
a teller attempting Place Hold gets a clean 403 business outcome; a
supervisor-gated hold escalates to a human.

### 4.8 Single-page output marking

**A latent flaw in the original design**, not the adaptation. The prompt said
*"mark outputs as soon as you can see them — do not wait until the end."* The
distiller takes the last marked page as the checkpoint, so every output must be
extractable from *that* page. Marking early produces artifacts whose rules
point at pages replay has already left.

The mock never exposed it; MERIDIAN exposed it on the first run. Fixed in the
prompt **and enforced in the distiller**, so it cannot recur silently.

### 4.9 Handoff driver resync — the most interesting bug

A paused run blocks the thread Playwright uses to pump browser events. For the
entire duration of a handoff the driver is **blind to everything the human
does**: the real Chromium navigates, MERIDIAN records the transaction, and the
`Page` object learns none of it. `page.url` stays frozen and `framenavigated`
never fires.

Three transfers posted correctly and all three reported `checkpoint_not_met`
against a two-screens-stale URL.

**Discovery masked it completely** — the model's next `build_observation()`
drains the event stream incidentally. It could only surface on the replay path,
on a target where handoff was actually exercised.

**The evidence is unusually good.** The `002_failure.png` screenshot taken
*after* handback on a run that reported failure shows `TRANSFER POSTED ·
CN480030`, because `evidence.screenshot()` is itself a Playwright call and
pumped the events on its way through. Same run, same page, two different views
seconds apart.

**Fixed** by reading `location.href` from the live browser after handback,
which forces the pending events through with it.

### 4.10 A model skips any field whose default already satisfies the goal

Found on `place_account_hold@1`. The Share and Reason Code dropdowns already
showed `102777-S0001` and `FRAUD`, so the model never touched either. The run
succeeded, the hold was applied, the confirmation number was real, and the
artifact declared `share_id` and `reason` as inputs that **no step used**.

The capability could only ever freeze the first share in the dropdown, for
fraud — on the most consequential action in the system, with the two values
that most need to be caller-controlled silently frozen.

Nothing automated could see it. Discovery reported success, distillation
accepted it, every test passed. It is visible only by reading the steps.

**Fixed by re-recording** with a goal that says to set every field explicitly
even where a value already appears selected, and by switching the reason code
to `LEGAL` — so that if the model skipped the dropdown again, the artifact
would record `FRAUD` and the mismatch would be obvious rather than silent.

**The general lesson:** an LLM takes the shortest path that satisfies the
stated goal, and a pre-filled form field is a shortcut. A capability recorded
this way works perfectly on the case it was recorded against and is
unparameterised everywhere else.

### 4.11 The distiller refuses an input nothing uses

Directly out of §4.10: rather than rely on a human noticing, the contract is
now enforced.

`input_params` is published in the catalogue as the arguments a caller may set.
Replay only ever varies a value that something in the artifact references. So
an input nothing references is a promise the artifact **cannot keep** — and the
way it fails is the worst available: a caller asks to freeze share A for reason
X, the run succeeds, returns a real confirmation number, and freezes share B
for reason Y instead. Every signal reports success and the evidence bundle
agrees.

**Decision — refuse rather than warn.** The trade is asymmetric:

| | Refuse is wrong | Warn is wrong |
|---|---|---|
| Who finds out | The developer, immediately | Nobody — until production |
| How | Blocked at distill, with a clear message | A wrong account gets frozen |
| Cost | Minutes of irritation | A customer harmed, and the audit trail says "success" |

Refuse fails loudly at build time. Warn fails silently at run time. Consistent
with the three refusals the distiller already had, all of which fired correctly
during this work.

A parameter used only by a **grid extraction key** or only by the **checkpoint
pattern** counts as consumed — tested, because a rule that refuses correct
artifacts is a rule people learn to work around. The error message ends with
the fix: *"Re-record with a goal that sets every field explicitly, even where a
value already appears selected."*

### 4.12 Fault injection through the target's own controls

§2.2 of the brief calls the runtime states the load-bearing part, so
"recoverable" needed live evidence rather than only unit tests.

**Built:** the target config declares *how* a host injects faults — for
MERIDIAN, its own System Settings form. The engine arms it **after sign-on**
(the controls live behind it, and a fault armed earlier would break sign-on
rather than the capability under test) and **clears it in `finally`**.

That last part is not tidiness. The host is shared and in-memory: a forced
fault left set would silently break the next person's run, with nothing in
*their* evidence to explain it.

Two modes, deliberately different:

- `--chaos <kind>` fires on **every** request, so recovery retries within its
  budget and then fails. Proves the budget is real.
- `--error-rate` is transient, so recovery can clear it and the run completes.
  Proves recovery recovers.

### 4.13 Capability API and dashboard

**Run history reads the existing evidence bundles**, not a second store. A
separate database would mean the dashboard and the audit trail could disagree
about what happened, and the evidence bundle is what a reviewer would actually
be handed. It also avoids instrumenting the same fifteen return sites that made
telemetry a wrapper rather than an emission.

The status vocabulary shown to a person is **richer than `ReplayStatus`**:
`escalated` and `recovered` are derived from the event log, because whether a
human was pulled in and whether the run healed itself are the first two things
anyone scans for, and neither is a property of the result object.

The dashboard is a **single served page** over that API — React from a CDN, no
build step, no second toolchain in a Python repo. Its signature element is the
escalation rendered as a measured pause: *"14.8s with a person in the loop ·
9.6s before control was taken."* The human's time in the loop is what this
system is built around, and it is sitting in the evidence timestamps already.

Superseded artifact versions and mock-app runs are **filtered, not deleted** —
a version that was replaced is still part of the audit trail and stays
invocable through the API.

### 4.14 Two over-fitted detector markers, found by replaying

Both were written by pasting the sentence from the first screen that happened
to contain the words. Both misclassified as a result, and neither was
detectable without running the capability.

**The noun changes by screen.** `"The transaction could not be validated:"`
matches the funds-transfer rejection. The open-share screen says `"The
**request** could not be validated:"`. So a correct refusal by the host — a
certificate requested below its $500 minimum deposit — matched nothing, fell
through to the locator ladder, and was reported as `locator_not_found`. Now
matches the shared fragment, `"could not be validated:"`.

**A banner is not a refusal.** `"SUPERVISOR OVERRIDE REQUIRED"` is printed as a
heading on the 403 page — and *also* as a banner at the top of the Place
Account Hold **form**, for every operator, supervisors included. Replay
classified a healthy form as a permission denial and stopped at s5 before
filling anything in.

> `place_account_hold` could never have replayed. For anyone. It was recorded
> successfully, distilled successfully, passed every test, and sat in the
> catalogue as a working capability.

**Discovery structurally could not catch this.** Discovery has no detector
layer — it drives the page and asks the model what it sees. Detectors exist
only on the replay path. So the recording succeeded while every replay of it
failed, and nothing in the recording was wrong.

It was found by replaying a capability that had until then only ever been
recorded, which is exactly the gap §7 named. Now matches `"is not authorized
to perform this"` — a sentence the host produces only when refusing, naming
the operator it refuses.

**The general lesson:** a marker is a claim about a target's copy, and the
natural way to write one — paste the sentence off the screen in front of you —
over-fits to that screen. Both fixes moved from *a sentence one screen says* to
*the fragment every refusal shares*. A test now asserts that no marker fires on
any of four healthy pages, because a taxonomy that over-matches is worse than
one that under-matches: under-matching fails loudly at the locator ladder,
over-matching reports a confident wrong answer.

**`scripts/resync_detectors.py`** exists because of this. Artifacts snapshot
their detectors at record time and replay prefers them, so a corrected marker
does not reach artifacts already on disk. Re-recording six capabilities to fix
a string would be absurd; hand-editing six JSON files is worse. The script is
explicit and `--dry-run`-able, and touches nothing but the detectors block —
replay must never silently change the classification a reviewer approved.

### 4.15 A checkpoint cannot express "wherever the search landed"

`member_inquiry@1` declared `search_by` as an input that no step used, so
search-by-last-name — which §2.1 asks for — was unreachable. Re-recording with
a goal that forces the dropdown produced `member_inquiry@2`, where both
`search_by` and `query` bind to real steps.

It also produced a limitation the take-home never hit.

The checkpoint is a URL pattern rendered against the invocation's parameters.
That works whenever the destination is derivable from the inputs: search by
member number and the flow ends at `/members/{member_id}`, which is exactly the
number the caller passed. **Search by name breaks the assumption.** The caller
supplies a surname; the member number only exists once the search resolves. So
the distiller has nothing to parameterise and freezes the literal it observed:

```
checkpoint: /members/101555        # Grace Hopper, the run it was recorded from
```

Replaying `@2` with `query=Turing` extracts Turing's real name and address,
every step resolving at tier 1, and then fails `checkpoint_not_met` —
`/members/100987` is not `/members/101555`. **The checkpoint is behaving
correctly.** The artifact is asserting something it has no business asserting.

**Considered and rejected: a wildcard,** `/members/*`. It borrows notation the
system already uses — `PolicyEngine` matches route patterns with `fnmatch` —
but for the opposite purpose. Policy wildcards a **category**: any member's
confirm route is irreversible, and which member is deliberately irrelevant. A
checkpoint asks about an **instance**: did *this run* arrive where it claims?
Wildcarding the identity there deletes the only claim being made.

Measuring it made the case worse. `fnmatch`'s `*` crosses `/`, so `/members/*`
also passes `/members/100987/transfer` — a run that died halfway through a
transfer form:

| Ended on | `/members/*` | exact |
|---|---|---|
| main menu / search results / sign-on | caught | caught |
| **mid-flow, the transfer form** | **passed** | caught |
| **a different member's record** | **passed** | caught |

It does not keep a shape assertion. It asserts "somewhere under `/members/`",
which is most of the flow.

`@2` was shipped as a documented partial, then fixed properly — §4.16.

### 4.16 Content assertions on the checkpoint

`REPORT.md`'s Cuts section has said since the take-home that *"the checkpoint
is a URL pattern only... a content assertion would be stronger."* §4.15 is the
case that forced it, and it arrived from the target rather than from anyone
deciding to build it.

A checkpoint now answers its question in two parts, because on a real console
both parts are not always knowable from the caller's inputs.

**Shape** — `url_pattern` gains a `{*}` segment for a path component the
distiller cannot bind to any parameter. `{*}` matches **exactly one segment**,
deliberately not `fnmatch`'s `*`: that crosses `/`, so a checkpoint of
`/members/*` is satisfied by `/members/100987/transfer` — a run that died
halfway through a transfer form. Measured before choosing, not assumed.

**Identity** — `CheckpointAssertion` states a claim in terms the caller *did*
supply: the value extracted for `member_name` must contain the `query` they
searched for. This is the same division `ExtractionRule` already makes — assert
the shape, parameterise the identity — which is why the wildcard alone was the
wrong answer: it does the first half and abandons the second.

Both are derived automatically at distill time. Assertions are recorded **only
where they were true of the run being distilled**; an assertion the recording
itself would have failed is not a property of the capability, it is a bug being
written into the contract.

**The identifier heuristic is deliberately narrow**, and the asymmetry is the
reasoning. Mistaking a route word for a record id *silently* weakens a
checkpoint that was correct — the artifact keeps passing while asserting less,
and nothing says so. Mistaking a record id for a route word freezes it, which
fails loudly on the first replay with different inputs, exactly as `@2` did. A
false negative announces itself; a false positive does not.

**Redaction turned out to sit in the middle of this**, and the fix is the
better part of the design. `member_name` is in `sensitive_output_fields`, so
what reaches the evidence log is `***REDACTED***` — and a distiller checking
whether the caller's `query` appears in *that* finds nothing and silently emits
a weaker checkpoint. So the containment is computed at **mark-output time**,
while the raw value is still in hand, and only the **parameter names** are
logged: the fact that `member_name` contained the caller's `query`, never what
either was.

The result is a checkpoint that asserts the identity of a record whose name is
masked in every log and every artifact on disk. Verified end to end:
`member_inquiry@3` was recorded searching for `Hopper` and replays correctly
for `Turing`, while a search matching nobody still short-circuits to
`MEMBER_NOT_FOUND` before extraction — a legitimate empty result is a business
outcome, not a checkpoint failure.

### 4.17 Two recovery budgets, because they stop different things

The per-step budget stops one step retrying forever. It cannot stop a condition
that returns on every page: each step begins with a fresh allowance, so no
single step ever exhausts its own. The run keeps advancing until it reaches a
page whose controls are missing and reports `locator_not_found` — blaming a
selector for a fault.

That is not hypothetical; it is what the forced-maintenance run did (§6). The
refusal was correct and the label was wrong, which matters because a label is
where a reader starts debugging.

`max_recovery_attempts_per_run` is checked first and set **above** the per-step
budget. The ordering is deliberate: an ordinary flaky page still clears through
the per-step path exactly as before, and only a condition that keeps coming
back reaches the run ceiling — where it is named `SESSION_RECOVERY_EXHAUSTED`,
a failure class that already existed and was simply unreachable for a fault
that never cleared.

The general shape is worth naming, because it is the third time this project
has hit it: **a budget scoped to one unit cannot see a problem that spreads
across units.** Per-step recovery attempts miss a fault on every page, exactly
as per-step locator telemetry misses drift across runs — which is why
`cli.py health` exists alongside `stability`. Same lesson, different axis.

### 4.17b Auditing the dashboard's own numbers

Every figure the dashboard shows is derived, and a metric that is confidently
wrong is worse than one that is missing: nobody re-checks a number that looks
plausible. `scripts/audit_dashboard.py` recomputes each one — status, duration,
human-in-loop seconds, locator tiers, outputs, event counts — straight from
`log.jsonl` and `result.json`, by a deliberately *different* route than
`runs.py` takes, and reports disagreements. Zero across 38 MERIDIAN runs.

It immediately caught its own author: changing `_display_status()` without
updating the independent check flagged 72 rows.

Those 72 turned out to be `replay_started → replay_denied` — the guardrails
refusing before a run began, on a missing parameter or an off-allowlist
origin. They write no `result.json`, so they displayed as **`unknown`**,
burying the clearest evidence the policy engine exists in the least
informative word available. **A refusal is a decision, not an absence of one.**
They now read as `denied`, with their own colour.

Two things a reader of the dashboard should know, both judgement calls rather
than defects. `escalated` and `recovered` are display overrides on an engine
status of `success`, so the green count alone *undercounts* completed runs —
a run that finished perfectly but needed a human shows purple. And `duration`
is last-event minus first, so for a handoff run it includes the human's time;
the separate `human Ns` figure isolates it.

One run legitimately remains `unknown`: a discovery interrupted mid-escalation,
which has no outcome to report rather than an unreadable one.

### 4.18 A conversational front door that enforces all three tiers

**It writes no tool definitions.** `artifact_to_tool_schema()` has emitted
OpenAI-shaped function schemas since the take-home, so the catalog *is* the
tool list: record a capability and the chatbot can call it with no code change.
That is §3.2's claim exercised rather than asserted. Maintained separately they
would drift, and the drift would surface as a model calling a capability with
the wrong arguments.

**The first version was safe and wrong.** It refused mutating and irreversible
actions identically, collapsing three tiers into two — so a teller changing a
phone number was told to use the operator console, which is the *irreversible*
tier's justification and does not apply to a record edit. Being over-cautious
is still being inaccurate, and the middle tier exists precisely so these two
are not treated alike.

Now: safe runs immediately; **mutating** returns a pending action the person
confirms with a deliberate click; **irreversible** is refused outright, and no
confirmation exists that changes that.

**Confirmation is a signed token, not the model noticing agreement.** The
obvious build lets the model see that the person said yes. That makes
confirmation a *model judgement* — and a model can be talked into judging
almost anything ("they approved this earlier", "they said yes to the last
one") — while leaving nothing checkable afterwards. Instead the pending action
is HMAC-signed over its exact parameters. The token is never shown to the
model, the parameters are read back **out of the token** rather than from the
request, it expires, and it can never be minted for an irreversible action.
Verified live: altering one digit of the phone number invalidates the
signature and nothing runs.

**Three defects, all the same shape: the model imitating a guarantee rather
than invoking it.**

| What it did | What was wrong |
|---|---|
| Declined a transfer politely | Never called the tool; the policy engine was never consulted |
| Asked "do you confirm?" | Never called the tool; no token existed, so the person had a question they could not answer |
| Explained a phone update as a wire transfer | Told them to use the console while a working confirm button sat underneath |

All three *looked* correct. A person watching the first would have concluded
the guardrail worked — it does, but nothing in that request exercised it.

> This is the failure mode specific to putting a model in front of a system
> with real guarantees: it will produce a convincing description of a
> guarantee it never touched.

The prompt now requires calling the capability, and distinguishes asking for a
missing **argument** (correct, and it does refuse to invent a member number)
from asking for **permission** (never — the system decides that). Each defect
is pinned by a test that reads the prompt or the code path, because "it came
out right once" is not a property.

**Then it answered "what is interface.ai".** Nothing was wrong with the
answer — the problem is that a banking console produced one. A general-purpose
model behind a narrow prompt is still a general-purpose model: the prompt is a
request, not a constraint, and someone else's tokens pay for whatever it
decides to be helpful about.

**Free text is the drift channel, so it was removed.** `tool_choice="required"`
means every turn calls something, with exactly three places to go: a
capability, `ask_for_missing_argument`, or `decline_out_of_scope`. There is
nowhere left to write a paragraph. Control tools answer without a second
completion and the system owns the wording — a refusal that costs an extra
model call is one someone can bill you for by repeating it. Input is bounded
(600 characters, 12 messages of history) before the catalog even loads, and
output at 300 tokens.

**And then it became a wall**, which is its own failure. Refusing everything
that is not a transaction makes a front desk useless, so
`describe_this_console` answers "what can you do" **from the catalog**. The
alternative — letting the model describe the system — means a teller is told
about capabilities by something that has never read the artifact list:
confidently, and wrongly the moment one is added. The refusal now points at it,
because a dead end teaches people to stop asking.

**Three more defects, same shape as the first three.**

| What it did | What was wrong |
|---|---|
| Refused a mixed request whole | The dispatch returned on the first decline, discarding a capability call later in the same list — so "look up member 100234, and also explain HTTPS" refused the lookup too |
| Declined a transfer as *out of scope* | A transfer is squarely in scope and should be refused by the **policy engine**, which records evidence. Two prompt sections collided: one said money movement cannot be done here, the other said decline what this console does not do |
| Reported a failed lookup as *"the member name is protected"* | **False.** The run did not complete. The model held an instruction about redacted values and reached for the nearest plausible explanation — while the evidence panel said `checkpoint_not_met` three inches away |

The last is the worst of the six, and different in kind. The others skipped a
mechanism whose outcome the model correctly predicted; this one **invented an
explanation** for a failure it had no account of. A teller would have walked
away believing the name was private. A wrong explanation is worse than no
answer, because it ends the enquiry.

**All six argue the same thing.** Everything routed through the prompt is a
request. The guarantees that hold — `irreversible_confirmed=False`, the signed
confirmation tokens — are in code the model cannot reach, and that distinction
is now something the chatbot demonstrates rather than claims.

`scripts/probe_chat.py` exercises fourteen standards against a running
instance, including whether a refusal gets routed around, whether claimed
authority softens anything, whether page content is treated as instructions,
and whether a failed run is reported as a failure.

### 4.19 A variant is not a version

`member_inquiry@1` searches by **member number**. `@3` searches by **last
name**. They share an id, so the catalog was told `@3` superseded `@1` — and
the chatbot, which is offered the latest version of each capability, was handed
only the by-name variant. Asked to look up member 100987, it typed a member
number into a name field.

The content assertion added hours earlier (§4.16) caught it:
`extracted member_name='Lovelace, Ada' does not contain '100234'`.

> **Versioning encodes supersession.** A variant recorded as a version silently
> removes the older behaviour from every caller that asks the catalog what is
> current.

Nothing was broken. `@1` still replayed perfectly; the catalog still listed it;
`/capabilities` still served it. Only a caller asking *"what is the current
member_inquiry"* lost the ability to search by number — and every automated
caller asks exactly that, because that is what latest-only means.

It surfaced because a model, handed one tool, used it for a job it was not for.
A human operator reading the catalog would have seen both entries and picked
the right one. This is the class of defect that appears only when something
consumes the catalog *as a contract* rather than reading it as a list.

**Fixed without a new discovery run** — the by-name evidence already existed,
so it was re-distilled as `member_inquiry_by_name@1`, and the mis-versioned
artifacts moved to `artifacts/history/`: still in the repo as evidence, out of
the catalog, because `discover_artifacts` globs one level. Seven capabilities
now, each named for what it does.

**What this implies for the artifact contract:** version numbers carry a claim
the schema never states and nothing enforces. A `supersedes` field, or refusing
to distill a version whose input parameters differ from its predecessor's,
would make the claim checkable. Same shape as §4.11 — the defect is in what the
artifact asserts, not in what any code does.

---

## 5. How the work unfolded

The ordering was deliberate: target-agnostic seams first, so recording the
capabilities would be cheap. The other way round — hacking around each obstacle
at record time — would have saved an hour and lost the top-weighted evaluation
criterion.

| # | Step | Outcome |
|---|---|---|
| 1 | `recon.py` — map the surface without a browser | Routes, forms, hidden `_token`, sign-on fields |
| 2 | `recon2.py` — token lifecycle, review→post, isolated injections, supervisor gating | Token session-scoped; full error taxonomy; `super1` clears the gate |
| 3 | `smoke.py` — real browser | **Token needs no primitive.** Ladder works unmodified. Extraction bug found. |
| 4 | Extraction rewrite + tests | |
| 5 | Target config seam; allowlist extended | `PolicyEngine` unchanged; live sign-on both profiles |
| 6 | Detectors + declarative recovery + tests | |
| 7 | `member_inquiry` | Prompt flaw found on run #1; fixed; recorded |
| 8 | `check_member_balance` | Grid rule bound to a parameter, live |
| 9 | `update_member_information` | Verifies the write from the member record, not a confirmation echo |
| 10 | `funds_transfer` | Two defects found by inspection; re-recorded as `@2` |
| 11 | Transfer replay | Driver-resync bug; 3 failures; fixed; `CN480034` |
| 12 | Capability API + dashboard | Run history read off the existing evidence bundles |
| 13 | `open_new_share` | `CN480058` |
| 14 | `place_account_hold` | Extraction refused, then an unparameterised artifact caught; `@2` clean |
| 15 | Unused-input refusal; fault injection | Recovery demonstrated live |
| 16 | Replaying the two capabilities that had only been recorded | Two over-fitted detector markers found; `place_account_hold` had never been replayable |
| 17 | Re-recording `member_inquiry` to bind `search_by` | Dead parameter closed; exposed that a URL checkpoint cannot express a search-by-name destination |
| 18 | Checkpoint content assertions | Closes a limitation `REPORT.md` has carried since the take-home; `@3` recorded on one surname, replays on another |
| 19 | Run-wide recovery ceiling | A returning fault now reports the condition rather than a missing selector |
| 20 | Auditing the dashboard's derived figures | Zero disagreements; 72 guardrail refusals were mislabelled `unknown` |
| 21 | Chatbot | Three tiers enforced; six defects found, all the model imitating a guarantee rather than invoking it |
| 22 | Scope enforcement and a help path | Free text removed; the console describes itself from its own catalog |
| 23 | Splitting `member_inquiry` | A variant had been recorded as a version, silently removing by-number lookup from every caller asking for the latest |

Reconnaissance before code was the highest-leverage decision. Three scripts, no
guesses: every design choice after step 3 was made against observed behaviour
rather than the brief's description of it.

---

## 6. Verification

**229 tests passing**, up from 126 — including the 13 browser tests that
exercise the real mock app through every changed path.

Live against MERIDIAN CORE:

| What | Result |
|---|---|
| Sign-on, both profiles | `teller1` + `super1` → `/menu` |
| `member_inquiry` | discovered on 100234, replayed on **100987** → Turing, Alan |
| `check_member_balance` | replayed on a **different member and share** → `$3.25` |
| `update_member_information` | replayed on 100987, different phone |
| `funds_transfer@2` | replayed **reversed direction**, different amount → `CN480034` |
| `open_new_share` | replayed on a **different member, share type and deposit** → `CN480103` |
| `place_account_hold@2` | replayed as supervisor on a **different member, share and reason** → `CN480108` |
| Supervisor refusal | same artifact as a teller → `SUPERVISOR_REQUIRED`, from the real 403 after filling the form |
| Host rule rejection | certificate below its minimum deposit → `TRANSACTION_REJECTED`, carrying the host's own reason |
| Escalation, discovery | `CN480026` |
| Escalation, replay | `CN480034` — no LLM in the decision loop |
| Verify-don't-trust | 3 × `checkpoint_not_met` + a balance check proving the money moved |
| Business outcome, unplanned | a transfer refused for `Source share is HOLD`, reported not crashed |
| Recovery, live | maintenance interstitial detected, `Continue` clicked, position restored — `recovery_applied: True` on `s1` |
| Content checkpoint | `member_inquiry@3` recorded on `Hopper`, replays on `Turing`; a search matching nobody still returns `MEMBER_NOT_FOUND` |
| Chatbot, safe tier | balance read from a plain-language request, figure reported verbatim |
| Chatbot, mutating tier | phone update held for confirmation, confirmed by click, applied — and a tampered token refused |
| Chatbot, irreversible tier | transfer refused by the **policy engine**, with an evidence bundle |
| Chatbot, scope | "what is interface.ai" declined in one completion; "only a test" changes nothing |
| Chatbot, help | "what can you do" lists all seven capabilities, read off the catalog |
| Chatbot, mixed request | member 100987 returned; the HTTPS aside declined, never answered |
| Both lookup paths | `member_inquiry` by number and `member_inquiry_by_name` reach the same member by different routes |
| Dashboard figures | every displayed number recomputed from raw evidence — no disagreements |

**An imprecision in that recovery run, since fixed.** With a *forced* fault
(fires on every request) the run recovered on `s1`, met the interstitial again
on `s2`, and reported `locator_not_found` rather than
`session_recovery_exhausted`. The recovery budget was per-step, so a condition
returning on every page exhausted no single step's allowance — each step began
with a fresh one — and the run simply advanced until it met a page whose
controls were absent. A correct refusal with the wrong name, and the wrong name
sends a reader to the locator ladder instead of to the condition.
`max_recovery_attempts_per_run` now sits alongside the per-step budget; see
§4.17.

---

## 7. What was cut, and what is still wrong

Per §5 of the brief, stated rather than discovered:

**Cut deliberately:**

- **Sign-on as a recorded artifact.** It is on the §2.1 list, but recording it
  means the model types a password, and redaction correctly masks
  password-named fields at the write boundary — the distiller would bind
  `[REDACTED]` as the literal value. The fix would be weakening redaction,
  which is the wrong trade. Sign-on is covered better as a configured
  precondition: exercised on **every run of every capability**, working live for
  both operator profiles, and it is what the `reauthenticate` recovery calls.
- **DOM snapshots.** §3.4 lists them among the evidence a run should carry.
  The core never emitted them — evidence is `log.jsonl`, `result.json` and
  screenshots — and the screenshots plus the per-candidate locator diagnostics
  cover the debugging need they were there for.
- **A transient-fault run that recovers *and* completes.** Needs a posting
  capability with `--error-rate` plus a human at the handoff console.
  MERIDIAN's random error rate applies only to posting actions, so a read-only
  capability never rolls the dice.

**Known defects, left in place:**

- **Version numbers carry an unchecked claim.** `member_inquiry@1` and the
  by-name variant shared an id until they were split (§4.19); nothing in the
  schema states that a version supersedes its predecessor, and nothing enforces
  it. A `supersedes` field — or refusing to distill a version whose input
  parameters differ from the previous one's — would make the claim checkable.
  The same shape as §4.11: the defect is in what the artifact asserts, not in
  what any code does.

- `member_inquiry@1` could no longer be *produced* under §4.11's check — it
  predates it, and its `search_by` parameter is still unused. It replays
  correctly for search-by-number, which is what the catalog offers it for.
  `member_inquiry_by_name@1` is what compliance with both §4.11 and §4.16
  looks like. The two mis-versioned intermediates sit in `artifacts/history/`,
  out of the catalog but kept: `@2` is the one whose frozen checkpoint exposed
  the URL-pattern limitation.
- `s2`'s ladder is single-candidate (`css_name_attr` only) — no fallback if
  that attribute changes. `label_proximity` did not fire on the search form's
  `Value:` cell; worth understanding rather than leaving unremarked.
- `funds_transfer@1` and `place_account_hold@1` are the flawed artifacts, kept
  on disk deliberately as evidence of caught defects.

**What should make you uneasy:** nine of ten defects were found by *reading
output*, not by tests failing. The process worked, but coverage did not catch
them in advance — and the two most dangerous (the `target_url` tier bug and the
driver resync) would both have shipped looking green. §4.11 is the first
structural answer to that, and the right direction for the next round: check
the artifact's contract, not just the code's logic.

---

## 8. Open questions

1. **Does the target-config seam sit where it belongs?** `src/targets.py` is a
   new top-level module because both discovery and replay depend on it and
   neither should own it.
2. **Is `_looks_like_pairs`'s colon heuristic too clever?** It is
   app-observable and documented, but it *is* a heuristic in a parser. The
   stricter alternative is making the artifact declare the shape, at the cost of
   the distiller no longer being able to infer it.
3. **`resume: true` re-navigates to the pre-recovery URL.** On a forced fault
   this re-triggers it until the recovery budget is exhausted, then fails.
   Bounded retries then a clean failure is the intended behaviour.
4. **Should the recovery budget also have a per-run ceiling?** See the
   imprecision noted in §6.

---

## 9. Next steps

In priority order:

1. More structural checks on the artifact contract, in the spirit of §4.11 and
   §4.19 — the defect class that unit tests are constitutionally unable to
   catch, because the code is correct and the artifact's claim about itself is
   not. Checking that a new version's input parameters match its predecessor's
   would have caught the variant split before a model tripped over it.
2. Recalibrating the locator ladder's confidence priors from accumulated
   telemetry rather than leaving them as fixed guesses.
3. A `DesktopSurface` behind the same locator-ladder contract, and
   authenticated console access — both carried over from the take-home's own
   cut list and both still outstanding.
