from types import SimpleNamespace

from shared.contact_lookup_policy import is_standalone_saved_contact_lookup
from shared.pending_task import PendingTaskContext, attach_pending_task, latest_pending_task
from shared.transfer_workflow import latest_pending_workflow, with_workflow

CONTACT_CANDIDATE = [{"intent_id": "SAVED_CONTACT_LOOKUP"}]


def _transfer_workflow(stage="AWAITING_CONTACT"):
    return {
        "workflow_type": "TRANSFER_PREPARATION",
        "workflow_id": "trf-contact-owner-test",
        "action_id": "act-contact-owner-test",
        "stage": stage,
        "last_action_status": "draft_opened" if stage == "DRAFT_OPENED" else "",
        "beneficiary_query": "John",
        "contact_candidates": [
            {"contact_id": "ct-john-carter", "full_name": "John Carter"},
            {"contact_id": "ct-john-mitchell", "full_name": "John Mitchell"},
        ],
    }


def _assistant_with_transfer_state(stage="AWAITING_CONTACT"):
    message = SimpleNamespace(
        type="ai",
        role="assistant",
        content=(
            "I found more than one saved contact matching John. Which person did you mean? "
            "Reply with the full name, a unique surname, bank name or option number. "
            "No transfer draft has been opened."
        ),
        additional_kwargs={},
    )
    with_workflow(message, _transfer_workflow(stage))
    return message


def test_serialized_agui_metadata_preserves_active_transfer_owner_for_latter_reply():
    assistant = _assistant_with_transfer_state()
    serialized = {
        "role": "assistant",
        "content": assistant.content,
        "metadata": assistant.additional_kwargs,
    }
    messages = [serialized, {"role": "user", "content": "latter", "id": "turn-latter"}]

    task = latest_pending_task(messages, domain="transfer")
    assert task is not None
    assert task.stage == "AWAITING_CONTACT"
    assert latest_pending_workflow(messages)["stage"] == "AWAITING_CONTACT"
    assert is_standalone_saved_contact_lookup(messages, CONTACT_CANDIDATE) is False


def test_transfer_contact_disambiguation_is_not_rendered_as_standalone_lookup_without_metadata():
    messages = [
        {
            "role": "assistant",
            "content": (
                "I found more than one saved contact matching John. Which person did you mean? "
                "Reply with the full name, a unique surname or option number. "
                "No transfer draft has been opened."
            ),
        },
        {"role": "user", "content": "latter"},
    ]
    assert is_standalone_saved_contact_lookup(messages, CONTACT_CANDIDATE) is False


def test_successful_draft_ack_is_terminal_for_its_turn_but_not_for_a_later_new_request():
    ack = _assistant_with_transfer_state("DRAFT_OPENED")
    current_turn = [
        {"role": "user", "content": "everyday"},
        {"role": "tool", "name": "prepare_transfer_draft", "content": "{}"},
        {"role": "assistant", "content": ack.content, "metadata": ack.additional_kwargs},
    ]
    assert is_standalone_saved_contact_lookup(current_turn, CONTACT_CANDIDATE) is False

    next_turn = current_turn + [{"role": "user", "content": "Find John in my saved contacts."}]
    assert is_standalone_saved_contact_lookup(next_turn, CONTACT_CANDIDATE) is True


def test_transfer_task_in_current_plan_takes_precedence_over_contact_candidate():
    messages = [
        {"role": "user", "content": "latter"},
        {
            "role": "assistant",
            "content": "",
            "metadata": {
                "oraiflow_task_plan": {
                    "tasks": [{"intent_id": "TRANSFER_PREPARATION"}],
                },
            },
        },
    ]
    assert is_standalone_saved_contact_lookup(messages, CONTACT_CANDIDATE) is False


def test_explicit_standalone_saved_contact_search_remains_supported():
    messages = [{"role": "user", "content": "Find John in my saved contacts."}]
    assert is_standalone_saved_contact_lookup(messages, CONTACT_CANDIDATE) is True
