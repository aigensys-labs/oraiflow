from services.agent_runtime.intent_contracts import (
    classify_canonical_read_intent,
)


def test_equivalent_recent_transaction_requests_resolve_to_same_canonical_intent():
    requests = [
        "What are my recent transactions?",
        "Show me my recent transactions",
        "Show my latest transactions",
        "List the last 5 transactions",
        "Can I see my transaction history?",
        "Show me recent activity",
    ]

    assert [classify_canonical_read_intent(text) for text in requests] == [
        "RECENT_TRANSACTIONS"
    ] * len(requests)


def test_explanatory_navigation_and_payment_exception_requests_are_not_misclassified():
    requests = [
        "Why was my last transaction declined?",
        "How do I dispute a transaction?",
        "Explain what pending transactions mean",
        "Open the Activity workspace",
        "Take me to recent transactions",
        "Prepare a transfer to John",
        "What can you tell me about transaction reference ABC123?",
        "How much have I spent this month?",
    ]

    assert [classify_canonical_read_intent(text) for text in requests] == [
        None
    ] * len(requests)


def test_canonical_intent_classification_is_repeatable():
    query = "What are my recent transactions?"

    assert {classify_canonical_read_intent(query) for _ in range(100)} == {
        "RECENT_TRANSACTIONS"
    }


def test_empty_and_non_text_inputs_are_not_classified():
    assert classify_canonical_read_intent("") is None
    assert classify_canonical_read_intent("   ") is None
    assert classify_canonical_read_intent(None) is None


def test_account_summary_balance_and_total_balance_have_canonical_intents():
    requests = {
        "account summary": "ACCOUNT_OVERVIEW",
        "Show me my account summary": "ACCOUNT_OVERVIEW",
        "Show all my accounts": "ACCOUNT_OVERVIEW",
        "What is my total balance?": "ACCOUNT_TOTAL_BALANCE",
        "Total available balance across all accounts": "ACCOUNT_TOTAL_BALANCE",
        "How much money do I have in total?": "ACCOUNT_TOTAL_BALANCE",
        "What is my account balance?": "ACCOUNT_BALANCE",
        "What's my balance?": "ACCOUNT_BALANCE",
        "How much money do I have?": "ACCOUNT_BALANCE",
        "Show my available balance": "ACCOUNT_BALANCE",
    }
    assert {request: classify_canonical_read_intent(request) for request in requests} == requests


def test_singular_latest_transaction_requests_use_detail_intent_not_recent_list():
    requests = [
        "What was my last transaction?",
        "Show my last transaction",
        "Show me the latest transaction details",
        "What are the details of my last transaction?",
        "Tell me about my latest transaction",
        "Details for my last transaction",
    ]
    assert [classify_canonical_read_intent(text) for text in requests] == ["LATEST_TRANSACTION_DETAIL"] * len(requests)
    assert classify_canonical_read_intent("What are my last transactions?") == "RECENT_TRANSACTIONS"


def test_transaction_explanation_is_not_collapsed_into_latest_transaction_detail():
    requests = [
        "Why was my last transaction declined?",
        "Explain my latest transaction",
        "Can I dispute the last transaction?",
        "Why is my latest transaction pending?",
    ]
    assert [classify_canonical_read_intent(text) for text in requests] == [None] * len(requests)


def test_common_total_sum_phrasing_uses_registered_total_balance_fast_path():
    assert classify_canonical_read_intent("what's my sum?") == "ACCOUNT_TOTAL_BALANCE"
    assert classify_canonical_read_intent("what's the total sum?") == "ACCOUNT_TOTAL_BALANCE"
    assert classify_canonical_read_intent("what is the total sum?") == "ACCOUNT_TOTAL_BALANCE"


def test_common_abbreviated_transaction_request_uses_recent_transactions_fast_path():
    assert classify_canonical_read_intent("latest trans?") == "RECENT_TRANSACTIONS"
    assert classify_canonical_read_intent("recent trans") == "RECENT_TRANSACTIONS"


def test_ambiguous_total_amount_and_remaining_amount_stay_agent_assisted():
    assert classify_canonical_read_intent("what is total amount?") is None
    assert classify_canonical_read_intent("what's my remaining amount?") is None


def test_semantic_variants_share_registered_canonical_read_intents():
    assert classify_canonical_read_intent("my transactions") == "RECENT_TRANSACTIONS"
    assert classify_canonical_read_intent("my last transactions") == "RECENT_TRANSACTIONS"
    assert classify_canonical_read_intent("what's my overall portfolio?") == "ACCOUNT_TOTAL_BALANCE"
    assert classify_canonical_read_intent("do I have any loans?") == "LOAN_INFORMATION"


def test_loan_information_declares_grounded_read_not_a_ui_presentation():
    from shared.contracts.registry import get_registry

    plan = get_registry()["intents"]["LOAN_INFORMATION"]["execution_plan"]
    capability = get_registry()["capabilities"][plan["capability"]]
    assert plan["response_mode"] == "grounded_explanation"
    assert plan.get("presentation") is None
    assert capability["kind"] == "mcp"
    assert capability["authorization"]["agent_invocable"] is True
    assert capability["execution"]["effect"] == "read_only"
