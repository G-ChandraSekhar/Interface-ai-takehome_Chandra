"""
The conversational front door.

Thin by construction: it writes no tool definitions, reading the catalog
instead, and it cannot authorise anything the HTTP invoke endpoint could not.
Both properties are asserted here because both are the kind of thing that
erodes quietly -- a hand-written tool list drifts from the artifacts it
describes, and a "just for the demo" confirmation flag is exactly how a
guardrail gets relaxed.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from src.capability_api import chat as chat_module
from src.capability_api.chat import capability_tools

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


def test_the_catalog_is_the_tool_list():
    """No hand-written schemas: record a capability and the chatbot can call it.

    The moment these are maintained separately they disagree, and the
    disagreement shows up as a model calling a capability with the wrong
    arguments.
    """
    tools = capability_tools(ARTIFACTS, target=None)
    assert tools, "no capabilities discovered"

    for tool in tools:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        # Required arguments come from the artifact's own ParamSpec, so a
        # model is told what it must supply rather than guessing.
        assert isinstance(fn["parameters"]["required"], list)


def test_only_the_latest_version_of_each_capability_is_offered():
    """Offering a model both @1 and @2 invites it to pick the superseded one,
    and nothing in the schema says which is current. A superseded version stays
    invocable over HTTP -- it is just not handed to a model unprompted."""
    tools = capability_tools(ARTIFACTS, target=None)
    names = [t["function"]["name"] for t in tools]
    assert len(names) == len(set(names))


def test_an_irreversible_action_can_never_be_confirmed_from_here():
    """The property that matters most, and the one that must not soften.

    Mutating actions became confirmable so that a teller changing a phone
    number is not treated like someone moving money -- that is the middle tier
    doing its job. Irreversible did NOT become confirmable, and there must be
    no parameter, no token, and no phrasing that makes it so: that tier needs a
    human at the live session, which a chat box is not.
    """
    source = inspect.getsource(chat_module._invoke)
    assert "irreversible_confirmed=False" in source

    full = inspect.getsource(chat_module)
    assert "irreversible_confirmed=True" not in full

    # Nothing a caller sends can reach it.
    for entry in (chat_module.chat, chat_module.confirm):
        assert "irreversible_confirmed" not in inspect.signature(entry).parameters


def test_a_mutating_action_stops_for_confirmation_before_it_runs():
    """It does not run and then ask. It asks, and runs only when told."""
    source = inspect.getsource(chat_module._invoke)
    assert "needs_confirmation" in source
    # The check precedes the replay call, not the other way round.
    assert source.index("needs_confirmation") < source.index("replay_artifact")


def test_confirmation_is_a_signed_token_not_the_model_reading_yes():
    """Letting the model notice agreement makes confirmation a model
    judgement, and a model can be talked into judging almost anything."""
    import time

    token = chat_module._sign(
        "update_member_information", {"member_id": "100234", "phone": "555-0199"},
        int(time.time()) + 300,
    )

    capability, params, problem = chat_module.verify_token(token)
    assert problem is None
    assert capability == "update_member_information"
    assert params["phone"] == "555-0199"

    # Change what is being confirmed and the signature fails: a confirmation
    # for one phone number cannot be replayed to set another.
    _, _, problem = chat_module.verify_token(token.replace("555-0199", "555-0200"))
    assert problem and "did not match" in problem

    # And it does not last.
    _, _, problem = chat_module.verify_token(chat_module._sign("x", {}, 1))
    assert problem and "expired" in problem


def test_the_model_never_sees_the_token():
    """A token in the transcript is a token the model could repeat back as
    though the person had confirmed."""
    source = inspect.getsource(chat_module.chat)
    assert 'k != "confirm_token"' in source


def test_confirming_runs_what_was_shown_not_what_is_sent_alongside():
    """Parameters come out of the verified token."""
    source = inspect.getsource(chat_module.confirm)
    assert "verify_token(token)" in source
    assert "confirmed=True" in source
    assert inspect.signature(chat_module.confirm).parameters.keys() <= {"token", "artifacts_dir"}


def test_a_refusal_is_explained_rather_than_reported_bare():
    """A model given only 'failure' has nothing to tell the person, and a model
    with nothing to say tends to invent something."""
    from src.replay.result import FailureClass, FailureDetail, ReplayResult, ReplayStatus

    denied = ReplayResult(
        status=ReplayStatus.FAILURE,
        failure=FailureDetail(
            step_class=FailureClass.POLICY_DENIED,
            step_id="s11",
            expected="policy allows this action",
            observed="Irreversible action requires a human.",
        ),
        run_dir="/tmp/replay_x",
    )
    described = chat_module._describe(denied, "funds_transfer", {"amount": "50.00"})

    assert described["failure_class"] == "policy_denied"
    assert "human" in described["note"]
    assert "do not retry" in described["note"].lower()


def test_a_business_outcome_is_flagged_as_an_answer_not_an_error():
    """'No such member' is a result the caller asked for."""
    from src.replay.result import ReplayResult, ReplayStatus

    outcome = ReplayResult(
        status=ReplayStatus.BUSINESS_OUTCOME,
        outcome_code="MEMBER_NOT_FOUND",
        outcome_message="No member record matched the search criteria.",
        run_dir="/tmp/replay_y",
    )
    described = chat_module._describe(outcome, "member_inquiry", {"query": "Nobody"})

    assert described["outcome_code"] == "MEMBER_NOT_FOUND"
    assert "not an error" in described["note"]


def test_every_invocation_carries_its_evidence_id():
    """So a claim in the chat can be checked against the run that produced it."""
    from src.replay.result import ReplayResult, ReplayStatus

    ok = ReplayResult(
        status=ReplayStatus.SUCCESS,
        outputs={"share_balance": "$208.54"},
        run_dir="/x/evidence/replay_20260820T210054Z_bd74c5",
    )
    described = chat_module._describe(ok, "check_member_balance", {})
    assert described["evidence"] == "replay_20260820T210054Z_bd74c5"
    assert described["outputs"]["share_balance"] == "$208.54"


def test_a_missing_api_key_degrades_with_a_reason(monkeypatch):
    """The dashboard still works; the chat says why it does not."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = chat_module.chat([{"role": "user", "content": "hello"}], artifacts_dir=ARTIFACTS)
    assert result["invocations"] == []
    assert "OPENAI_API_KEY" in result["reply"]


def test_a_mutating_action_is_not_explained_as_if_it_were_irreversible():
    """Observed live: asked to change a phone number, the model replied that
    the action 'cannot be completed in this conversation' and to use the
    operator console -- while a working confirm button sat underneath it.

    That is the irreversible tier's justification borrowed for a mutating
    action. The mechanism was right and the words were wrong, which is worse
    than either alone: the person is told to go somewhere else by the same
    interface that is offering to do it.
    """
    from src.replay.result import ReplayStatus  # noqa: F401

    note = None
    source = inspect.getsource(chat_module._invoke)
    assert "CONFIRMABLE HERE" in source
    assert "Do NOT tell them to use the" in source

    prompt = chat_module.SYSTEM_PROMPT
    assert "Never tell them to go\n    to the operator console for one of these" in prompt
    # ...and the console IS still the answer for the tier that needs it.
    assert "MOVES MONEY" in prompt and "operator console" in prompt


def test_the_model_is_told_to_call_rather_than_ask_first():
    """Otherwise it pre-empts, politely, and nothing real happens.

    Observed live, twice. Asked to update a phone number, the model replied
    "do you confirm?" without calling the capability -- no invocation, no
    signed token, and a person looking at a question with no control to answer
    it. Earlier, asked to move money, it declined on its own manners without
    the policy engine ever being consulted.

    Both looked correct and neither ran the mechanism. Confirmation and refusal
    have to be produced by the system -- bound to exact parameters, recorded in
    evidence, checkable afterwards -- not by the model's sense of what is
    appropriate.
    """
    prompt = chat_module.SYSTEM_PROMPT
    assert "ALWAYS call the capability" in prompt
    assert "never decide for yourself" in prompt
    assert "the system decides that and tells you" in prompt
    # Asking for an ARGUMENT is still correct, and must not be confused with
    # asking for permission.
    assert "Asking for an ARGUMENT is different from asking" in prompt


# ---------------------------------------------------------------------------
# Scope: this is a teller tool, not a general assistant
# ---------------------------------------------------------------------------
#
# Observed live: asked "what is interface.ai", it produced a paragraph about
# the company. Nothing was wrong with the answer -- the problem is that a
# banking console answered it at all. A general-purpose model behind a narrow
# prompt is still a general-purpose model; the prompt is a request, not a
# constraint, and someone else's tokens pay for whatever it decides to be
# helpful about.


def test_every_turn_must_call_a_tool():
    """Free text is the drift channel. Removing it is structural, not wording."""
    source = inspect.getsource(chat_module.chat)
    assert 'tool_choice="required"' in source


def test_prose_is_not_passed_through_even_if_the_model_returns_some():
    """Belt and braces: tool_choice should prevent it, but an unscoped answer
    is exactly what this exists to stop, so it is not forwarded."""
    source = inspect.getsource(chat_module.chat)
    no_calls = source.split("if not choice.tool_calls:")[1].split("\n\n")[0]
    assert "OUT_OF_SCOPE_REPLY" in no_calls
    assert "choice.content" not in no_calls


def test_there_is_a_way_to_decline_and_a_way_to_ask():
    """The model needs somewhere to go for both, or it will improvise one."""
    names = [t["function"]["name"] for t in chat_module.CONTROL_TOOLS]
    assert "decline_out_of_scope" in names
    assert "ask_for_missing_argument" in names

    asker = next(t for t in chat_module.CONTROL_TOOLS
                 if t["function"]["name"] == "ask_for_missing_argument")
    # The distinction that produced a real defect earlier tonight.
    assert "PERMISSION" in asker["function"]["description"]


def test_a_control_only_turn_costs_no_second_completion():
    """A refusal that costs an extra model call is a refusal someone can bill
    you for by repeating it. When there is no work to do, the system answers
    from fixed text and returns before the summarising call."""
    source = inspect.getsource(chat_module.chat)
    no_work = source.split("if not capability_calls:")[1].split("# There IS work")[0]
    assert "OUT_OF_SCOPE_REPLY" in no_work
    assert "return" in no_work
    assert "client.chat.completions.create" not in no_work


def test_real_work_survives_an_out_of_scope_aside():
    """Observed live: "look up member 100234, and also explain how HTTPS works"
    refused BOTH halves.

    Two causes. The prompt never said a mixed request should be partly served,
    and the dispatch returned on the first decline it found -- discarding a
    capability call sitting later in the same list. Refusing the whole thing is
    safe and costs the person the half that was legitimate.
    """
    source = inspect.getsource(chat_module.chat)

    # Calls are sorted before any of them is acted on.
    assert "capability_calls = [" in source
    assert source.index("capability_calls = [") < source.index("if not capability_calls:")

    # And a decline alongside real work is recorded, not answered.
    assert "Out of scope. Do NOT answer it" in source

    # Assert on the prompt's words without depending on where it wraps.
    prompt = " ".join(chat_module.SYSTEM_PROMPT.split())
    assert "do the work and leave the rest alone" in prompt
    assert "Only decline outright when NOTHING in the request" in prompt


def test_input_and_output_are_both_bounded():
    """A long paste is not a teller request, and an unbounded history is a
    context window someone else is paying for."""
    assert chat_module.MAX_MESSAGE_CHARS <= 1000
    assert chat_module.MAX_HISTORY_MESSAGES <= 20
    assert chat_module.MAX_REPLY_TOKENS <= 500

    source = inspect.getsource(chat_module.chat)
    assert "MAX_HISTORY_MESSAGES" in source
    assert source.count("max_tokens=MAX_REPLY_TOKENS") >= 2


def test_an_oversized_message_is_refused_before_any_model_call(monkeypatch):
    """Trim before spending anything."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    result = chat_module.chat(
        [{"role": "user", "content": "x" * (chat_module.MAX_MESSAGE_CHARS + 1)}],
        artifacts_dir=ARTIFACTS,
    )
    assert result["invocations"] == []
    assert "longer than this console accepts" in result["reply"]


def test_the_prompt_refuses_scope_changes_however_they_are_framed():
    prompt = chat_module.SYSTEM_PROMPT
    assert "not a general assistant" in prompt
    assert "as a test" in prompt and "as a joke" in prompt
    assert "claim to replace these" in prompt


def test_an_action_that_will_be_refused_is_still_called():
    """Observed live: a transfer request came back as OUT OF SCOPE.

    A transfer is squarely in scope -- it is one of the six capabilities. It
    should be refused by the POLICY ENGINE, which records an evidence bundle
    and gives the person a real answer about their own request. Declining it
    as out-of-scope leaves them with nothing and no record that they asked.

    The cause was two prompt sections colliding: the tier section said money
    movement "cannot be done here at all", the scope section said decline
    anything this console does not do, and the model read them together.

    Same shape as three earlier defects -- the model imitating a guarantee
    instead of invoking it. It is right about the outcome and wrong to be the
    one deciding it.
    """
    prompt = " ".join(chat_module.SYSTEM_PROMPT.split())

    assert 'decline_out_of_scope means "no capability covers this"' in prompt
    assert 'It does NOT mean "this will probably be refused"' in prompt
    assert "including transfers, new shares and holds, which you know will be refused" in prompt
    assert "Refusing is the system's job, not yours" in prompt

    # And the tier section no longer reads as a scope exclusion.
    assert "still yours to call" in prompt
    assert "in scope and refused, which is not the same as out of scope" in prompt


def test_a_failed_run_is_never_reported_as_a_protected_value():
    """Observed live, and the most misleading defect of the night.

    Asked for a member's name, the capability failed with checkpoint_not_met.
    The reply said the name "is protected and cannot be provided" -- false. The
    model held an instruction about redacted values, found itself with no
    output, and reached for the nearest plausible explanation.

    A teller would walk away believing the name is private. The evidence panel
    said checkpoint_not_met three inches away. A wrong explanation is worse
    than no answer, because it ends the enquiry.
    """
    from src.replay.result import FailureClass, FailureDetail, ReplayResult, ReplayStatus

    failed = ReplayResult(
        status=ReplayStatus.FAILURE,
        failure=FailureDetail(
            step_class=FailureClass.CHECKPOINT_NOT_MET,
            step_id=None,
            expected="outputs visible on this page",
            observed="extracted member_name='Lovelace, Ada' does not contain '100234'",
        ),
        run_dir="/tmp/replay_z",
    )
    note = chat_module._describe(failed, "member_inquiry", {})["note"]

    assert "FAILED" in note
    for wrong_word in ("protected", "redacted", "private"):
        assert wrong_word in note  # named explicitly so they are ruled out
    assert "did not complete" in note

    prompt = " ".join(chat_module.SYSTEM_PROMPT.split())
    assert "This applies ONLY when a capability succeeded" in prompt
    assert "never describe it as protected" in prompt


# ---------------------------------------------------------------------------
# Being useful, not just being narrow
# ---------------------------------------------------------------------------


def test_asking_what_the_console_does_is_in_scope():
    """Refusing everything that is not a transaction makes a front desk
    useless. A question ABOUT this system is a question this system can
    answer."""
    names = [t["function"]["name"] for t in chat_module.CONTROL_TOOLS]
    assert "describe_this_console" in names

    prompt = " ".join(chat_module.SYSTEM_PROMPT.split())
    assert "call describe_this_console" in prompt
    assert "That is a question ABOUT this system, which is in scope" in prompt


def test_the_description_is_read_from_the_catalog_not_from_the_model():
    """The obvious alternative -- letting the model describe the system --
    means a teller is told about capabilities by something that has never read
    the artifact list. Confidently, and wrongly the moment one is added."""
    description = chat_module._console_description(ARTIFACTS, None)

    tools = chat_module.capability_tools(ARTIFACTS, target=None)
    for tool in tools:
        readable = tool["function"]["name"].replace("_", " ")
        assert readable in description, readable + " missing from the description"

    # And it explains the tiers, which is what a person actually needs to know.
    assert "confirmation" in description
    assert "operator console" in description

    prompt = " ".join(chat_module.SYSTEM_PROMPT.split())
    assert "do not answer it from memory" in prompt


def test_the_description_costs_no_second_completion():
    """Like the other control paths: the system answers and returns."""
    source = inspect.getsource(chat_module.chat)
    # The no-work branch, which is where all three control tools are handled.
    block = source.split("if not capability_calls:")[1].split("# There IS work")[0]
    assert "_console_description" in block
    assert "client.chat.completions.create" not in block


def test_a_refusal_points_somewhere_useful():
    """A dead end teaches the person to stop asking."""
    assert "what can you do" in chat_module.OUT_OF_SCOPE_REPLY
