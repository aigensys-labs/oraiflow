"""Registry-backed Academy topic semantics and customer education.

This module owns topic matching and educational copy assembly. It does not
make authorization decisions or perform financial-data reads; personal topics
only declare which registered intent the Agent Runtime must execute.
"""
from __future__ import annotations

import re
from typing import Any

from shared.contracts.registry import get_registry
from services.agent_runtime.intent_contracts import discover_intent_candidates


def academy_topic_catalogue() -> dict[str, dict[str, Any]]:
    """Return the registry-backed Academy topic catalogue keyed by stable ID."""
    topics = get_registry().get("application_policy", {}).get("academy_topics", [])
    catalogue = {str(topic.get("topic_id")): topic for topic in topics if topic.get("topic_id")}
    # Stable aliases preserve older model responses while the shared catalogue
    # uses the current Academy topic IDs.
    if "SAVINGS_WITH_PURPOSE" in catalogue:
        catalogue["SAVINGS_ACCOUNT_BASICS"] = catalogue["SAVINGS_WITH_PURPOSE"]
    if "LOAN_REPAYMENTS_AND_APR" in catalogue:
        catalogue["LOAN_ACCOUNT_BASICS"] = catalogue["LOAN_REPAYMENTS_AND_APR"]
        catalogue["LOAN_REPAYMENT"] = catalogue["LOAN_REPAYMENTS_AND_APR"]
    catalogue["SAVED_BENEFICIARIES"] = {
        "topic_id": "SAVED_BENEFICIARIES",
        "title": "Saved beneficiaries",
        "description": "Saved contacts that can be selected as transfer recipients.",
    }
    return catalogue


def _normalised_topic_text(value: str) -> str:
    """Normalize ordinary plural variants without broad stemming or fuzzy guessing."""
    tokens = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split()
    normalized: list[str] = []
    for token in tokens:
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
            token = token[:-1]
        normalized.append(token)
    return " ".join(normalized)


def academy_topic_matches(text: str, *, limit: int = 3) -> list[tuple[dict[str, Any], int]]:
    """Rank Academy topics using the shared catalogue's semantic vocabulary.

    Exact catalogue phrases carry more weight than isolated word overlap. The
    result is a topic hint, never an authorization or financial-data decision.
    """
    query = _normalised_topic_text(text)
    query_tokens = set(query.split())
    if not query_tokens:
        return []
    scored: list[tuple[dict[str, Any], int]] = []
    for topic in get_registry().get("application_policy", {}).get("academy_topics", []):
        if not isinstance(topic, dict) or not topic.get("topic_id"):
            continue
        score = 0
        seen_terms: set[str] = set()
        for keyword in topic.get("keywords", []) if isinstance(topic.get("keywords", []), list) else []:
            phrase = _normalised_topic_text(str(keyword))
            if not phrase or phrase in seen_terms:
                continue
            seen_terms.add(phrase)
            words = set(phrase.split())
            if f" {phrase} " in f" {query} ":
                score += 4 if len(words) > 1 else 2
            elif words and words.issubset(query_tokens):
                score += 1
        title = _normalised_topic_text(str(topic.get("title", "")))
        if title and title in query:
            score += 2
        if score >= 2:
            scored.append((topic, score))
    # Python's stable sort preserves the explicit catalogue order on equal scores.
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:max(1, int(limit))]


def _topic_query_relevant(text: str, topic: dict[str, Any]) -> bool:
    topic_id = str(topic.get("topic_id", ""))
    return any(str(item.get("topic_id", "")) == topic_id for item, _score in academy_topic_matches(text, limit=6))


def _topic_has_local_personal_reference(text: str, topic: dict[str, Any]) -> bool:
    """Require an ownership marker close to a personal-data topic reference.

    A generic ``my`` elsewhere in a budgeting question must not trigger a
    Financial Health or loan-account read. The marker must be locally connected
    to a keyword/title belonging to the topic that declares the read.
    """
    words = _normalised_topic_text(text).split()
    owner_terms = get_registry().get("application_policy", {}).get(
        "academy_personal_reference_terms", ["my", "mine", "our", "ours", "current", "recorded", "personal"],
    )
    owner_positions = {
        index for index, word in enumerate(words)
        if word in {token for term in owner_terms for token in _normalised_topic_text(str(term)).split()}
    }
    if not owner_positions:
        return False

    topic_terms = [str(topic.get("title", "")), *[str(item) for item in topic.get("keywords", [])]]
    for term in topic_terms:
        term_words = _normalised_topic_text(term).split()
        if not term_words:
            continue
        for start in range(0, len(words) - len(term_words) + 1):
            if words[start:start + len(term_words)] != term_words:
                continue
            end = start + len(term_words) - 1
            if any(min(abs(position - start), abs(position - end)) <= 4 for position in owner_positions):
                return True
    return False


def academy_personal_snapshot_intent(text: str, topic_id: str | None = None) -> str | None:
    """Resolve personal-data needs only when ownership refers to that topic.

    Comparative requests can mention several topics (for example a bureau
    score and the ORAIflow badge). All relevant personal-data topics are
    considered, but a possessive pronoun by itself is never enough.
    """
    catalogue = academy_topic_catalogue()
    if topic_id:
        candidates = [catalogue.get(str(topic_id))]
    else:
        ranked = academy_topic_matches(text, limit=8)
        candidates = [topic for topic, _score in ranked]

    registry_intents = get_registry().get("intents", {})
    for topic in candidates:
        if not isinstance(topic, dict) or not _topic_has_local_personal_reference(text, topic):
            continue
        intent_id = str(topic.get("personal_data_intent_id") or "").strip()
        if intent_id in registry_intents:
            return intent_id
    return None


def academy_direct_guidance_for_request(text: str) -> str | None:
    """Return maintained general education without an unnecessary model call.

    When a relevant topic references personal records, the Agent must first run
    its registered read and then compose the contextual explanation. Otherwise,
    all closely related topics explicitly marked catalogue_direct may be shown.
    """
    # A strong registered financial-analysis phrase takes precedence over a
    # weak Academy keyword overlap. Otherwise, a question such as "do I have
    # any spendings on transport?" can be swallowed by generic cash-flow
    # education before the transaction interpreter sees it.
    candidates = discover_intent_candidates(text, limit=8)
    if any(
        item.get("intent_id") == "TRANSACTION_ANALYSIS"
        and item.get("match_kind") == "semantic_phrase"
        and int(item.get("score", 0)) >= 1000
        for item in candidates
    ):
        return None

    ranked = academy_topic_matches(text, limit=8)
    if not ranked or academy_personal_snapshot_intent(text):
        return None
    best_score = ranked[0][1]
    selected = [
        topic for topic, score in ranked
        if score >= max(2, best_score - 2)
        and str(topic.get("response_policy", "")) == "catalogue_direct"
        and bool(str(topic.get("customer_explanation") or topic.get("description") or "").strip())
        and bool(topic.get("customer_explanation") or topic.get("description") or topic.get("learning_points"))
    ][:3]
    if not selected:
        return None
    return academy_catalogue_explanation(
        text,
        topic_ids=[str(topic.get("topic_id", "")) for topic in selected],
        require_customer_explanation=False,
    )


def academy_catalogue_explanation(
    text: str, *, topic_ids: list[str] | None = None, require_customer_explanation: bool = False,
) -> str | None:
    """Build a safe fallback explanation from registered Academy content."""
    catalogue = academy_topic_catalogue()
    selected: list[dict[str, Any]] = []
    if topic_ids:
        selected = [catalogue[item] for item in topic_ids if item in catalogue]
    else:
        ranked = academy_topic_matches(text, limit=3)
        if ranked:
            best = ranked[0][1]
            # Include a close second topic for comparative questions, but avoid
            # combining loosely related subjects into an unfocused response.
            selected = [topic for topic, score in ranked if score >= max(2, best - 2)][:3]
    if require_customer_explanation:
        selected = [topic for topic in selected if str(topic.get("customer_explanation") or "").strip()]
    if not selected:
        return None

    parts: list[str] = []
    for topic in selected:
        title = str(topic.get("title") or "Financial learning")
        explanation = str(topic.get("customer_explanation") or topic.get("description") or "")
        parts.append(f"**{title}**\n{explanation}")
        habits = topic.get("practical_habits", [])
        points = habits if isinstance(habits, list) and habits else topic.get("learning_points", [])
        if isinstance(points, list) and points:
            heading = "Habits that can help" if habits else "Key points"
            parts.append(f"**{heading}**\n" + "\n".join(f"• {item}" for item in points[:5]))
        limitations = topic.get("limitations", [])
        if isinstance(limitations, list) and limitations:
            parts.append("**A useful boundary**\n" + " ".join(str(item) for item in limitations[:2]))
    return "\n\n".join(parts)




def _format_ratio_bps(value: Any) -> str:
    from decimal import Decimal

    if isinstance(value, bool) or not isinstance(value, int):
        return "not calculable from the recorded snapshot"
    return f"{Decimal(value) / Decimal(100):.2f}%"


def _format_minor_for_health(value: Any, currency: str) -> str:
    from decimal import Decimal

    if isinstance(value, bool) or not isinstance(value, int):
        return "unavailable"
    return f"{currency} {Decimal(value) / Decimal(100):,.2f}"


def render_financial_health_explanation(data: dict[str, Any], customer_text: str) -> str:
    """Explain Academy concepts alongside a validated recorded-health snapshot.

    The input must be the data member of a validated banking.financial_health
    result envelope. The renderer does not derive or combine currencies.
    """
    ranked = academy_topic_matches(customer_text, limit=8)
    topic_ids: list[str] = []
    if ranked:
        best_score = ranked[0][1]
        topic_ids = [
            str(topic.get("topic_id", ""))
            for topic, score in ranked
            if score >= max(2, best_score - 2)
        ][:3]
    if "FINANCIAL_HEALTH_BADGE" not in topic_ids:
        topic_ids.insert(0, "FINANCIAL_HEALTH_BADGE")
    education = academy_catalogue_explanation(
        customer_text, topic_ids=topic_ids, require_customer_explanation=False,
    ) or "The ORAIflow Financial Health badge is an educational view of recorded leverage, not a credit score."

    positions = data.get("positions") if isinstance(data, dict) else None
    snapshot_lines: list[str] = []
    if isinstance(positions, list) and positions:
        for position in positions:
            if not isinstance(position, dict):
                continue
            currency = str(position.get("currency", "")).upper()
            if len(currency) != 3 or not currency.isalpha():
                continue
            equity = position.get("recorded_equity_minor")
            assets = position.get("recorded_deposit_assets_minor")
            liabilities = position.get("total_recorded_liabilities_minor")
            loans = position.get("loan_liabilities_minor")
            status = str(position.get("status") or "INSUFFICIENT_DATA")
            status_labels = {
                "LOW_LEVERAGE": "Excellent",
                "MODERATE_LEVERAGE": "Good",
                "ELEVATED_LEVERAGE": "Needs attention",
                "HIGH_LEVERAGE": "Warning",
                "LIABILITIES_MATCH_ASSETS": "Warning — liabilities match recorded assets",
                "LIABILITIES_EXCEED_ASSETS": "Warning — liabilities exceed recorded assets",
                "INSUFFICIENT_DATA": "Not enough recorded data",
            }
            snapshot_lines.append(
                f"**{currency} recorded position — {status_labels.get(status, 'Recorded status unavailable')}**\n"
                f"- Loan liabilities: **{_format_minor_for_health(loans, currency)}**\n"
                f"- Total recorded liabilities: **{_format_minor_for_health(liabilities, currency)}**\n"
                f"- Recorded deposit assets: **{_format_minor_for_health(assets, currency)}**\n"
                f"- Recorded equity: **{_format_minor_for_health(equity, currency)}**\n"
                f"- Loans / recorded equity: **{_format_ratio_bps(position.get('loan_to_equity_bps'))}**\n"
                f"- Total liabilities / recorded deposit assets: **{_format_ratio_bps(position.get('debt_to_assets_bps'))}**"
            )
    if snapshot_lines:
        snapshot = "**Your current recorded snapshot**\n" + "\n\n".join(snapshot_lines)
    else:
        snapshot = (
            "**Your current recorded snapshot**\n"
            "There is not enough usable same-currency account and loan data to calculate a ratio. "
            "I won't substitute an estimate or combine different currencies."
        )

    explanation = (
        "**How to read the two ratios**\n"
        "Loans / recorded equity compares loan principal with assets remaining after recorded liabilities. "
        "It is not the same as the badge's leverage band. The badge status is based on total recorded "
        "liabilities divided by recorded deposit assets. If recorded equity is zero or negative, "
        "loan-to-equity is not meaningfully calculable and is shown as unavailable."
    )
    impact = (
        "**What can improve or worsen the recorded picture**\n"
        "- Paying down debt from genuine surplus funds generally reduces liabilities, but using cash also "
        "reduces recorded assets. The ratios must be recalculated from both updated sides.\n"
        "- A new loan increases liabilities. If the proceeds remain as recorded cash, assets also increase; "
        "if the proceeds are spent or held outside connected accounts, the recorded ratios can move differently.\n"
        "- Keeping a sensible accessible buffer matters. Do not drain emergency money solely to improve a badge.\n"
        "- A borrowing decision should also consider the instalment, interest, fees and reliable income; "
        "this badge does not measure repayment affordability."
    )
    coverage = (
        "**Coverage and limits**\n"
        "This snapshot includes active ORAIflow deposit accounts and registered loan principal only. "
        "It does not include external accounts, property, investments, pensions, unrecorded debts or income. "
        "The badge is not a bureau credit score or a complete household balance sheet. "
        "This is a single recorded snapshot, so it cannot establish whether your position is improving or worsening."
    )
    return "\n\n".join((education, snapshot, explanation, impact, coverage))


def _money_from_minor(value: Any, currency: str) -> str:
    """Format a validated integer minor-unit amount without float arithmetic."""
    from decimal import Decimal

    if isinstance(value, bool) or not isinstance(value, int):
        return "Unavailable"
    amount = Decimal(value) / Decimal(100)
    return f"{currency} {amount:,.2f}"


def render_loan_schedule_explanation(data: dict[str, Any]) -> str:
    """Explain the authenticated illustrative loan snapshot with clear limits.

    This is deliberately deterministic. It uses only the validated MCP result,
    distinguishes the stored annual interest rate from an APR calculation, and
    never labels synthetic schedule dates as actual due/paid payment events.
    """
    if not isinstance(data, dict) or not data:
        return (
            "I couldn't find an illustrative loan record in your ORAIflow profile. "
            "I can still explain principal, interest and APR generally, but I won't invent loan terms or dates."
        )
    currency = str(data.get("currency") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        return "I retrieved a loan record, but its currency could not be validated, so I won't display calculated amounts."

    name = str(data.get("product_name") or "Illustrative loan")
    principal = _money_from_minor(data.get("principal_minor"), currency)
    outstanding = _money_from_minor(data.get("outstanding_principal_minor"), currency)
    monthly = _money_from_minor(data.get("monthly_payment_minor"), currency)
    total_interest = _money_from_minor(data.get("total_interest_minor"), currency)
    total_repayment = _money_from_minor(data.get("total_repayment_minor"), currency)
    term = data.get("term_months") if isinstance(data.get("term_months"), int) else None
    rate_bps = data.get("annual_interest_rate_bps")
    if isinstance(rate_bps, int) and not isinstance(rate_bps, bool):
        from decimal import Decimal

        annual_rate = f"{Decimal(rate_bps) / Decimal(100):.2f}%"
    else:
        annual_rate = "not provided"

    installments = data.get("installments") if isinstance(data.get("installments"), list) else []
    sample_rows = []
    for item in installments[:4]:
        if not isinstance(item, dict):
            continue
        number = item.get("installment_number")
        date = str(item.get("due_date") or "date unavailable")
        payment = _money_from_minor(item.get("payment_minor"), currency)
        principal_part = _money_from_minor(item.get("principal_minor"), currency)
        interest_part = _money_from_minor(item.get("interest_minor"), currency)
        sample_rows.append(
            f"| {number if isinstance(number, int) else '—'} | {date} | {payment} | {principal_part} | {interest_part} |"
        )
    schedule = ""
    if sample_rows:
        schedule = (
            "\n\n**First illustrative instalments**\n\n"
            "| # | Illustrative date | Payment | Principal repaid | Interest |\n"
            "|---:|---|---:|---:|---:|\n"
            + "\n".join(sample_rows)
        )

    term_text = f"{term} months" if term else "term not provided"
    first_due = str(data.get("first_due_date") or "not provided")
    loan_summary = (
        f"**{name} — illustrative terms**\n"
        f"- Original principal: **{principal}**\n"
        f"- Recorded outstanding principal: **{outstanding}**\n"
        f"- Annual interest rate in the loan record: **{annual_rate}**\n"
        f"- Term: **{term_text}**\n"
        f"- Scheduled monthly instalment: **{monthly}**\n"
        f"- Scheduled total interest: **{total_interest}**\n"
        f"- Scheduled total repayment: **{total_repayment}**\n"
        f"- First illustrative schedule date: **{first_due}**"
    )
    concepts = (
        "**How each part works**\n"
        "- **Principal** is the amount borrowed. Each principal portion reduces the outstanding amount owed.\n"
        "- **Interest** is the borrowing cost charged for the period. In a typical amortising loan, the interest portion is calculated on the remaining principal, so it often falls as the balance is repaid.\n"
        "- **APR (Annual Percentage Rate)** expresses the annualised total cost of credit, including interest and relevant mandatory fees/charges. It is useful when comparing offers."
    )
    rate_caveat = (
        f"The returned record contains an annual interest rate of {annual_rate}, but does not provide a separate APR or a fee breakdown. "
        "I therefore won't present that interest rate as the loan's APR."
    )
    tracking = (
        "**Important:** this is a synthetic illustrative schedule. Payment tracking is not available, "
        "so these dates do not establish that an instalment is currently due, has been paid or is overdue. "
        "No repayment or payment action has been taken."
    )
    if data.get("demo_only") is not True or data.get("payment_tracking") != "NOT_TRACKED":
        tracking = "The retrieved schedule is presented as provided by the loan record; check the Loans workspace for its stated tracking status."
    return "\n\n".join((
        loan_summary + schedule,
        concepts,
        rate_caveat,
        tracking,
    ))
