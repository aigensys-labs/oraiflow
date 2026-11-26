from services.agent_runtime.workspace_knowledge import (
    workspace_catalogue_overview_message,
    workspace_explanation_for_request,
    workspace_help_for_request,
)


def test_academy_about_question_is_answered_from_workspace_catalogue():
    answer = workspace_help_for_request("what is academy about?")
    assert answer
    assert "**Academy**" in answer
    assert "guided financial-literacy workspace" in answer
    assert "credit reports and leverage" in answer
    assert "Credit-score systems" in answer


def test_all_tabs_guide_is_assembled_from_registry_workspaces():
    answer = workspace_help_for_request("Can you guide me through all the tabs?")
    assert answer == workspace_catalogue_overview_message()
    for name in (
        "Overview",
        "Accounts",
        "Payments",
        "Loans",
        "Cards",
        "Contacts",
        "Activity",
        "Academy",
        "Controls & audit",
    ):
        assert f"**{name}**" in answer


def test_workspace_explanation_does_not_capture_personal_account_data_question():
    assert workspace_explanation_for_request("what are my accounts?") is None
    assert workspace_explanation_for_request("what are my cards?") is None


def test_workspace_explanation_is_not_a_navigation_action():
    answer = workspace_help_for_request("tell me about the Cards tab")
    assert answer and "**Cards**" in answer
    assert "A view of the customer’s demo card" in answer
    assert "Card controls" in answer


def test_academy_purpose_grammar_variants_resolve_to_same_workspace_explanation():
    requests = ["what is Academy about?", "what is Academy for?", "Academy is for what?"]
    responses = [workspace_explanation_for_request(item) for item in requests]
    assert all(responses)
    assert len(set(responses)) == 1


def test_workspace_purpose_followups_use_registered_generic_cues():
    from services.agent_runtime.workspace_knowledge import workspace_help_for_request

    for question in (
        "What is Academy about?",
        "Academy is for?",
        "Academy for what?",
        "What is the AI assistant for?",
    ):
        assert workspace_help_for_request(question), question

    # A data question must not be mistaken for a workspace-purpose request.
    assert workspace_help_for_request("What are my accounts?") is None
