# MERIDIAN Adaptation — Write-up

**Branch:** `adaptation/meridian` · **Target:** web-sample.interface-hiring.com · 20 Aug 2026

Depth on any point below is in [`ADAPTATION-LOG.md`](ADAPTATION-LOG.md) — the
development log, including every defect and the reasoning behind each decision.

---

## What adapting took

**Twenty core modules are byte-identical to `main`,** including every module
carrying a safety or correctness guarantee. The load-bearing fact:
`src/guardrails/engine.py` has **zero changes**. MERIDIAN's origin, its three
risk tiers and its irreversible posting endpoints are all expressed in
`config/allowlist.yaml`. The policy engine was pointed at a new bank without
being edited.

Six modules changed, all additively, and each change was forced by the target
rather than chosen:

| Change | Why the target forced it |
|---|---|
| **Target config seam** (`src/targets.py`, `config/targets/*.yaml`) | Sign-on was `_mock_login()`, duplicated verbatim in discovery and replay. Two auth paths would make "adaptation is configuration" false by construction. |
| **Extraction rewrite** | MERIDIAN renders two label/value pairs per row (`Member No.:⇥100234⇥Name:⇥Lovelace, Ada`). The old parser split on the first tab, so `Name` and `Phone` were **unreachable** — a real bug in existing code, exposed by a new surface. Also added a grid strategy: "the Balance where Share ID is X" had no expression in the schema. |
| **Declarative detectors + recovery** | Six business outcomes, two recoverable conditions, one hard failure, declared per target. Recovery actions carry a `resume` flag because both MERIDIAN recoveries land you *elsewhere* — recovering without re-navigating would continue against the wrong page while reporting success. |
| **Human-performed irreversible steps** | `open_sub_account@1` stopped at review with no confirm step. On MERIDIAN that affects three of seven capabilities. |
| **Select binding** | Selects froze as literals, and MERIDIAN's option labels contain live balances — freezing one binds a capability to a *balance*: correct once, then silently wrong. Now reads back the stable option value. |
| **Content assertions on checkpoints** | See below. |

**The headline difficulty turned out not to be one.** The brief flags the
per-transaction hidden token. Reconnaissance killed the plan to build a
read-and-bind primitive: because we drive the real browser and click the real
submit button, the browser serialises that hidden field natively. It required
no special handling **because we never leave the UI** — reconstructing requests
would have needed it; driving the actual form does not.

---

## The capability API contract

A capability is a typed artifact: `input_params` with types and required-ness,
`output_schema`, an ordered step list, and a checkpoint. `artifact_to_tool_schema()`
emits OpenAI-shaped function schemas straight from that, so **the catalog *is*
the tool list** — the chatbot writes no tool definitions, and a capability
recorded tomorrow is callable with no code change.

```
POST /capabilities/{id}/invoke?version=N   →  {status, outputs, outcome_code, failure, run_dir}
```

Same replay engine as the CLI, same guardrails, same evidence trail — a second
front door, not a second execution path. Two constraints are enforced at
distill time, both learned from real defects: every output must be readable
from the single page the capability ends on, and **every declared input must be
used by some step** — an artifact advertising a parameter nothing consumes would
accept a caller's value and silently ignore it, which on Place Account Hold
means freezing the wrong share and reporting success.

---

## Driving the UI, and its runtime states

Replay resolves each step through the existing locator ladder — unchanged from
the take-home, and it works on MERIDIAN's transaction forms without
modification.

**Classification is marker-driven, not status-driven**, forced by evidence: a
natural "no member records matched" returns **HTTP 200**, while an injected
validation fault and a legitimate insufficient-funds outcome both return 400.
Status-first would miss the most common business outcome and conflate two
others. Status is recorded for evidence, never used to classify.

Two recovery budgets, because they stop different things. Per-step stops one
step retrying forever; it cannot stop a condition returning on *every* page,
since each step starts with a fresh allowance. Without a run-wide ceiling the
run walks on until it meets a page whose controls are missing and blames a
selector.

Fault injection is driven through the host's own System Settings, armed after
sign-on and cleared in `finally` — the host is shared and in-memory, so a
forced fault left set would break the next person's run with nothing in *their*
evidence to explain it.

---

## How the guarantees survive the new surface

- **Irreversible actions never run unattended.** The HTTP invoke path and the
  chatbot both call replay with `irreversible_confirmed=False` hardcoded, with
  no parameter, token or phrasing that changes it. Asked to move money, the
  chatbot calls the capability and the **policy engine** refuses it, producing
  an evidence bundle. Defended twice: against a bug that would have tiered a
  posting step SAFE, and against building a supervisor-credential bypass.
- **The three tiers are visible in the chatbot.** Safe runs; **mutating** returns
  an HMAC-signed pending action the person confirms with a click; irreversible
  is refused. Confirmation is signed over the exact parameters rather than the
  model noticing agreement — altering one digit invalidates it.
- **Escalation is unchanged.** A human takes control of the live session, and
  replay **verifies the resulting state rather than trusting them**. See
  `evidence/replay_20260820T182817Z_457fa0/` — a run reporting
  `checkpoint_not_met` on a transfer that *had* posted, which is how the
  driver-resync bug was found.
- **Redaction unchanged.** Sensitive outputs are masked at the write boundary,
  so `member_name` is `***REDACTED***` in every log and artifact on disk.

---

## What I cut, and what I'd do next

**Cut:** sign-on as a *recorded* artifact — recording it means the model types a
password, and redaction correctly masks it, so the distiller would bind
`[REDACTED]` as a literal. Weakening redaction is the wrong trade; sign-on is
covered better as a configured precondition, exercised on every run of every
capability. **DOM snapshots** — §3.4 lists them; the core never emitted them,
and screenshots plus per-candidate locator diagnostics cover the need.
**`update_member_information` covers phone only**, not email or address.

**Next, in order:** the two missing update fields; structural checks on what an
artifact *claims* about itself (a version number asserts supersession, and
nothing enforces it — a variant recorded as a version silently removed
by-number lookup from every caller asking the catalog what's current);
recalibrating the locator ladder's confidence priors from accumulated telemetry.

**What should make you uneasy:** most defects were found by *reading output*,
not by tests failing — and the two most dangerous would both have shipped
looking green. The distiller's contract checks are the first structural answer
to that, and the right direction for the next round.

**229 tests passing** (126 at the start), seven capabilities recorded and
replayed against different members with different values.
