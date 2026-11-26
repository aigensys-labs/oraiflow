from types import SimpleNamespace

from services.agent_runtime.navigation import (
    PENDING_NAVIGATION_KEY,
    navigation_confirmation_metadata,
    navigation_action_already_emitted,
    navigation_action_id,
    is_incomplete_navigation_request,
    is_explicit_workspace_view_request,
    resolve_workspace_target,
    attach_navigation_task,
    resolve_pending_navigation_confirmation,
    resolve_workspace_request,
)


def _ai(content, pending=None):
    return SimpleNamespace(
        type="ai",
        content=content,
        additional_kwargs={PENDING_NAVIGATION_KEY: pending} if pending else {},
    )


def _human(content):
    return SimpleNamespace(type="human", content=content, additional_kwargs={})


def test_affirmative_confirms_only_the_immediately_pending_workspace_action():
    pending = navigation_confirmation_metadata("Controls & audit")
    messages = [
        _ai("I found Controls & audit. Would you like me to open it?", pending),
        _human("yes"),
    ]
    decision = resolve_pending_navigation_confirmation(messages, "yes", {"navigate_workspace"})
    assert decision is not None
    assert decision["kind"] == "EXECUTE"
    assert decision["workspace"] == "Controls & audit"
    assert decision["action_name"] == "navigate_workspace"


def test_ok_and_okay_confirm_only_an_immediately_pending_workspace_action():
    for answer in ("ok", "okay", "OK."):
        pending = navigation_confirmation_metadata("Contacts")
        messages = [
            _ai("I found Contacts. Would you like me to open that workspace?", pending),
            _human(answer),
        ]
        decision = resolve_pending_navigation_confirmation(messages, answer, {"navigate_workspace"})
        assert decision is not None
        assert decision["kind"] == "EXECUTE"
        assert decision["workspace"] == "Contacts"


def test_negative_confirmation_cancels_navigation():
    pending = navigation_confirmation_metadata("Controls & audit")
    messages = [_ai("Would you like me to open Controls & audit?", pending), _human("no")]
    decision = resolve_pending_navigation_confirmation(messages, "no", {"navigate_workspace"})
    assert decision == {
        "kind": "CANCELLED",
        "workspace": "Controls & audit",
        "action_id": pending["action_id"],
        "navigation_epoch": pending["navigation_epoch"],
    }


def test_yes_without_pending_navigation_does_not_navigate():
    messages = [_ai("What would you like to do next?"), _human("yes")]
    assert resolve_pending_navigation_confirmation(messages, "yes", {"navigate_workspace"}) is None


def test_unrelated_turn_does_not_confirm_navigation_or_leave_stale_pending_proposal():
    pending = navigation_confirmation_metadata("Controls & audit")
    messages = [
        _ai("Would you like me to open Controls & audit?", pending),
        _human("What's my balance?"),
        _ai("I can retrieve your account balances."),
        _human("yes"),
    ]
    assert resolve_pending_navigation_confirmation(messages, "yes", {"navigate_workspace"}) is None


def test_navigation_requires_registered_action_available_in_current_view():
    pending = navigation_confirmation_metadata("Controls & audit")
    messages = [_ai("Would you like me to open it?", pending), _human("yes")]
    decision = resolve_pending_navigation_confirmation(messages, "yes", set())
    assert decision is not None
    assert decision["kind"] == "UNAVAILABLE"


def test_navigation_rejects_unallowlisted_workspace_in_stored_state():
    pending = {**navigation_confirmation_metadata("/admin"), "status": "AWAITING_CONFIRMATION"}
    messages = [_ai("Would you like me to open it?", pending), _human("yes")]
    decision = resolve_pending_navigation_confirmation(messages, "yes", {"navigate_workspace"})
    assert decision is not None
    assert decision["kind"] == "UNAVAILABLE"


def test_resource_show_and_list_requests_resolve_to_the_registered_workspace():
    cases = {
        "show me my contacts": "Contacts",
        "Show my contacts.": "Contacts",
        "what are my contacts?": "Contacts",
        "what contacts do I have?": "Contacts",
        "who are my saved contacts?": "Contacts",
        "list my contacts": "Contacts",
        "show me contacts": "Contacts",
        "list saved beneficiaries": "Contacts",
        "take me to contacts": "Contacts",
        "show me audit": "Controls & audit",
        "take me to audit": "Controls & audit",
        "show my recent transactions": "Activity",
        "show me my accounts": "Accounts",
    }
    for utterance, expected in cases.items():
        assert resolve_workspace_request(utterance) == expected, utterance


def test_direct_resource_view_verbs_are_explicit_without_a_confirmation_turn():
    for utterance in (
        "Show my contacts",
        "List my saved beneficiaries",
        "Show me my accounts",
        "Show my recent transactions",
    ):
        assert is_explicit_workspace_view_request(utterance) is True, utterance
    for utterance in (
        "what is my balance?",
        "where are my contacts?",
        "what is the Contacts workspace for?",
    ):
        assert is_explicit_workspace_view_request(utterance) is False, utterance


def test_information_and_concept_questions_are_not_misrouted_to_workspace_pages():
    for utterance in (
        "what is my balance?",
        "what are saved beneficiaries?",
        "where is audit?",
        "what's my total sum?",
    ):
        assert resolve_workspace_request(utterance) is None, utterance


def _tool_call(workspace, action_id="nav-1234567890abcdef"):
    return SimpleNamespace(
        type="ai",
        content="",
        additional_kwargs={},
        tool_calls=[
            {
                "name": "navigate_workspace",
                "args": {"workspace": workspace, "action_id": action_id},
                "id": action_id,
            }
        ],
    )


def test_navigation_dispatch_is_one_shot_within_the_same_customer_turn():
    messages = [_human("show me my contacts"), _tool_call("Contacts")]
    assert navigation_action_already_emitted(messages, "Contacts") is True


def test_navigation_receipt_consumes_the_action_for_the_current_turn():
    tool = SimpleNamespace(type="tool", name="navigate_workspace", content='{"outcome":"SUCCESS"}')
    messages = [_human("show me my contacts"), _tool_call("Contacts"), tool]
    assert navigation_action_already_emitted(messages, "Contacts") is True


def test_navigation_acknowledgement_consumes_the_action_for_the_current_turn():
    ack = SimpleNamespace(
        type="ai",
        content="I've opened Contacts for you.",
        tool_calls=[],
        additional_kwargs={
            "oraiflow_frontend_action_receipt": {
                "capability_id": "frontend.workspace.navigate",
                "status": "opened",
            }
        },
    )
    messages = [_human("show me my contacts"), _tool_call("Contacts"), ack]
    assert navigation_action_already_emitted(messages, "Contacts") is True


def test_manual_later_request_can_navigate_to_the_same_workspace_again():
    messages = [
        _human("show me my contacts"),
        _tool_call("Contacts"),
        _human("show me my accounts"),
    ]
    assert navigation_action_already_emitted(messages, "Accounts") is False
    assert navigation_action_already_emitted(messages, "Contacts") is False


def test_navigation_action_from_an_earlier_turn_does_not_block_a_new_turn():
    messages = [
        _human("show me my contacts"),
        _tool_call("Contacts"),
        _human("take me to contacts"),
    ]
    assert navigation_action_already_emitted(messages, "Contacts") is False


def test_navigation_action_id_is_stable_when_same_turn_is_replayed_after_context_refresh():
    messages = [_human("show me my contacts")]
    first = navigation_action_id(messages, "Contacts", "thread-abc")
    replay = navigation_action_id(messages, "Contacts", "thread-abc")
    assert first == replay
    assert first.startswith("nav-")


def test_new_customer_turn_gets_a_new_navigation_action_id_even_for_same_destination():
    first_turn = [_human("show me my contacts")]
    second_turn = [_human("show me my contacts"), _human("show me my contacts")]
    first = navigation_action_id(first_turn, "Contacts", "thread-abc")
    second = navigation_action_id(second_turn, "Contacts", "thread-abc")
    assert first != second


def test_lets_go_to_registered_workspace_is_a_canonical_navigation_request():
    assert resolve_workspace_request("let's go to Contacts") == "Contacts"
    assert resolve_workspace_request("let's go to audit") == "Controls & audit"


def test_incomplete_navigation_request_is_preserved_as_a_typed_task():
    from shared.pending_task import latest_pending_task

    assistant = _ai("Which ORAIflow workspace would you like me to open?")
    attach_navigation_task(
        assistant, "AWAITING_DESTINATION", {"destination_query": "take me there"}
    )
    messages = [assistant, _human("Contacts")]
    pending = latest_pending_task(messages, domain="navigation")
    assert pending is not None
    assert pending.stage == "AWAITING_DESTINATION"
    assert resolve_workspace_target("Contacts") == "Contacts"


def test_navigation_verbs_without_destination_are_recognized_without_phrase_catalogue_growth():
    for utterance in ("let's go", "go to", "take me there", "open", "navigate to"):
        assert is_incomplete_navigation_request(utterance), utterance
    assert not is_incomplete_navigation_request("what is the Contacts workspace?")


def test_take_me_to_academy_then_resolves_registered_destination():
    assert resolve_workspace_request("take me to academy then") == "Academy"
    assert resolve_workspace_target("academy") == "Academy"


def test_navigation_destination_reply_does_not_treat_discourse_marker_as_workspace_name():
    assert resolve_workspace_target("Academy then") == "Academy"
