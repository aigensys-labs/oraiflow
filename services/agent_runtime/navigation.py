"""Single-use confirmation state for conversational workspace navigation."""

from __future__ import annotations

import re
import hashlib
import uuid
from typing import Any

from shared.contracts.registry import get_registry
from shared.pending_task import PendingTaskContext, attach_pending_task, latest_pending_task
from services.agent_runtime.message_content import message_content_to_text

PENDING_NAVIGATION_KEY = "oraiflow_pending_navigation"

_AFFIRMATIVE = re.compile(
    r"^(?:yes|yeah|yep|ok|okay|sure|certainly|please\s+do|do\s+it|go\s+ahead|open\s+it|take\s+me\s+there|that(?:'s|\s+is)\s+right)[.! ]*$",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"^(?:no|no\s+thanks|not\s+now|cancel|leave\s+it|don't|do\s+not)[.! ]*$",
    re.IGNORECASE,
)


# Resource-view requests are deterministic canonical workspace routes. This is
# intentionally narrower than general language interpretation: the customer
# must use an explicit view/list/navigation verb and name one registered page
# (or an application-policy alias). Questions such as "what is my balance?"
# and "where is audit?" do not match.
_VIEW_PREFIX = re.compile(
    r"^(?:please\s+)?(?:(?:can\s+you|could\s+you)\s+)?"
    r"(?:show\s+me|show|list|display|view|open|go\s+to|let(?:\'s|s)\s+go\s+to|navigate\s+to|"
    r"take\s+me\s+to|bring\s+me\s+to|switch\s+to|let\s+me\s+see)\s+"
    r"(?P<target>.+?)\s*[.!?]*$",
    re.IGNORECASE,
)


def _normalise_page_alias(value: str) -> str:
    value = str(value or "").casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def resolve_workspace_target(value: str) -> str | None:
    """Resolve a destination name/alias against the registered page catalogue."""
    target = _normalise_page_alias(value)
    # Discourse markers often follow a perfectly complete navigation target
    # ("take me to Academy then"). They are not part of the registered page
    # name and should not force a redundant clarification.
    target = re.sub(r"\s+(?:then|now|please|thanks|thank you)$", "", target).strip()
    target = " ".join(
        word for word in target.split() if word not in {"my", "the", "tab", "workspace", "page"}
    )
    if not target:
        return None
    navigation = get_registry().get("application_policy", {}).get("navigation", {})
    pages = set(navigation.get("pages", []))
    candidates = {
        _normalise_page_alias(str(alias)): str(destination)
        for alias, destination in navigation.get("aliases", {}).items()
    }
    candidates.update({_normalise_page_alias(page): page for page in pages})
    destination = candidates.get(target)
    return destination if destination in pages else None


def is_incomplete_navigation_request(text: str) -> bool:
    """Identify a navigation objective whose destination is not yet specified."""
    value = re.sub(r"\s+", " ", str(text or "")).casefold().strip(" .!?")
    return bool(
        re.fullmatch(
            r"(?:please\s+)?(?:let(?:'s|s)\s+)?(?:go|go\s+to|open|navigate(?:\s+to)?|switch(?:\s+to)?|take\s+me\s+there|bring\s+me\s+there|take\s+me\s+to)",
            value,
        )
    )


def attach_navigation_task(message: Any, stage: str, state: dict[str, Any] | None = None) -> Any:
    """Attach navigation clarification/confirmation to the shared typed task envelope."""
    values = dict(state or {})
    task_id = str(values.pop("task_id", "") or f"nav-{uuid.uuid4().hex}")
    task = PendingTaskContext(
        task_id=task_id,
        domain="navigation",
        intent_id="WORKSPACE_NAVIGATION",
        stage=stage,
        status=(
            "ACTIVE" if stage in {"AWAITING_DESTINATION", "AWAITING_CONFIRMATION"} else "COMPLETED"
        ),
        state=values,
    )
    return attach_pending_task(message, task)


def attach_navigation_clarification_from_plan(
    message: Any,
    planned_tasks: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    customer_text: str,
) -> Any:
    """Persist a planner-owned missing destination as typed navigation state.

    The plan remains authoritative for the clarification. This only lets the
    existing navigation handler consume the customer's next short destination
    reply instead of treating it as an unrelated new request.
    """
    for plan in planned_tasks or ():
        if not isinstance(plan, dict):
            continue
        if plan.get("intent_id") != "WORKSPACE_NAVIGATION":
            continue
        if plan.get("status") != "CLARIFICATION_REQUIRED":
            continue
        missing = {str(value) for value in plan.get("missing_fields", []) or []}
        if missing and not (missing & {"workspace", "workspace_name", "destination"}):
            continue
        return attach_navigation_task(
            message,
            "AWAITING_DESTINATION",
            {"destination_query": str(customer_text or "")[:500]},
        )
    return message


def _conversation_workspace_reference(messages: list[Any] | tuple[Any, ...] | None) -> str | None:
    """Resolve a pronoun from the latest substantive assistant answer.

    An immediately preceding navigation clarification is skipped: it belongs
    to the unresolved navigation task and is not the referent of "it". The
    prior answer must still name exactly one registered workspace.
    """
    items = list(messages or [])
    latest_human = max(
        (index for index, message in enumerate(items) if getattr(message, "type", "") == "human"),
        default=len(items),
    )

    def message_text(message: Any) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, list):
            content = message_content_to_text(content)
        return str(content or "")

    previous_assistant = None
    for message in reversed(items[:latest_human]):
        if getattr(message, "type", "") != "ai":
            continue
        content = message_text(message)
        if not content.strip():
            continue
        is_navigation_clarification = bool(
            re.search(
                r"\bwhich\s+(?:orai\s*flow\s+)?workspace\s+(?:would\s+you\s+like\s+me\s+to\s+open|did\s+you\s+mean)\b",
                content,
                re.IGNORECASE,
            )
            or re.search(r"\bwhich\s+one\s+should\s+i\s+open\b", content, re.IGNORECASE)
        )
        if is_navigation_clarification:
            continue
        previous_assistant = message
        break
    if previous_assistant is None:
        return None
    content = message_text(previous_assistant)

    navigation = get_registry().get("application_policy", {}).get("navigation", {})
    pages = [str(page) for page in navigation.get("pages", [])]
    normalized_content = _normalise_page_alias(content)
    mentioned: set[str] = set()
    for page in pages:
        normalized_page = _normalise_page_alias(page)
        # Match complete page names while tolerating whitespace and punctuation
        # differences in assistant prose (for example, "Controls & audit").
        page_pattern = r"\s+".join(re.escape(part) for part in normalized_page.split())
        if page_pattern and re.search(
            rf"(?<![a-z0-9]){page_pattern}(?![a-z0-9])",
            normalized_content,
        ):
            mentioned.add(page)
    return next(iter(mentioned)) if len(mentioned) == 1 else None


def resolve_workspace_request(
    text: str,
    *,
    messages: list[Any] | tuple[Any, ...] | None = None,
) -> str | None:
    """Resolve an explicit resource-view request to one registered workspace.

    Examples: "show me my contacts", "list saved beneficiaries", "take me to
    audit", "show my recent transactions". A simple pronoun such as "its
    page" is resolved only from the immediately preceding assistant response,
    and only when that response names one registered workspace. Concept
    questions and balance questions remain non-navigation requests.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return None

    normalized_value = value.casefold().strip(" .!?\t")
    direct_patterns = (
        (
            r"^(?:who are|show me)\s+(?:my\s+)?(?:saved\s+)?(?:beneficiaries|payees|recipients|contacts)\??$",
            "Contacts",
        ),
        (
            r"^(?:list|show|view)\s+(?:all\s+)?(?:my\s+)?(?:saved\s+)?(?:beneficiaries|payees|recipients|contacts)\??$",
            "Contacts",
        ),
        # A question asking what contacts the customer has is a request to view
        # the registered Contacts workspace, not a capabilities-help question.
        (r"^what\s+are\s+(?:my\s+)?(?:saved\s+)?contacts\??$", "Contacts"),
        (r"^what\s+contacts\s+do\s+i\s+have\??$", "Contacts"),
        (r"^who\s+are\s+my\s+saved\s+contacts\??$", "Contacts"),
    )
    for pattern, destination in direct_patterns:
        if re.fullmatch(pattern, normalized_value, re.IGNORECASE):
            return destination

    match = _VIEW_PREFIX.fullmatch(value)
    if not match:
        return None
    target = _normalise_page_alias(match.group("target"))
    target = re.sub(r"\s+(?:then|now|please|thanks|thank you)$", "", target).strip()
    # These are information queries, not page-view commands.
    if re.search(r"\b(?:balance|balances|how much|total|sum|available|remaining)\b", target):
        return None
    target = re.sub(r"\b(?:my|the|all|saved|recent|latest|workspace|page)\b", " ", target)
    target = " ".join(target.split())

    resolved = resolve_workspace_target(target)
    if resolved:
        return resolved
    if target in {"it", "its", "that", "there", "same", "for it", "for that"}:
        return _conversation_workspace_reference(messages)
    return None


def is_explicit_workspace_view_request(text: str) -> bool:
    """True when the registered parser recognises a direct resource-view command."""
    return resolve_workspace_request(text) is not None


def navigation_action_id(messages: list[Any], workspace: str, thread_id: str = "") -> str:
    """Return a stable id for the same navigation request within one turn.

    AG-UI context refreshes can re-enter the graph with an unchanged latest
    customer message. A random ID would make the browser treat each replay as a
    new navigation command and pull the user back to the original workspace.
    The stable ID lets the frontend's one-shot receipt cache consume replays.
    A later customer message has a different history index/identity and gets a
    new action ID, even when its wording is identical.
    """
    items = list(messages or [])
    latest_human = max(
        (index for index, message in enumerate(items) if getattr(message, "type", "") == "human"),
        default=-1,
    )
    if latest_human < 0:
        basis = f"{thread_id}|no-human|{workspace}"
    else:
        message = items[latest_human]
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        message_id = str(getattr(message, "id", "") or "")
        basis = f"{thread_id}|{latest_human}|{message_id}|{content}|{workspace}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    return f"nav-{digest}"


def navigation_action_already_emitted(messages: list[Any], workspace: str) -> bool:
    """Return True when this customer turn already emitted a navigation action.

    The check is scoped strictly to messages after the latest human turn. It
    prevents a replayed AG-UI/LangGraph state from issuing a second navigation
    while allowing the customer to request the same destination again in a
    later turn.
    """
    items = list(messages or [])
    latest_human = max(
        (index for index, message in enumerate(items) if getattr(message, "type", "") == "human"),
        default=-1,
    )
    if latest_human < 0:
        return False

    expected = str(workspace or "")
    for message in items[latest_human + 1 :]:
        message_type = getattr(message, "type", "")
        if message_type == "ai":
            for call in getattr(message, "tool_calls", None) or []:
                if not isinstance(call, dict) or call.get("name") != "navigate_workspace":
                    continue
                arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
                if str(arguments.get("workspace", "")) == expected:
                    return True
            additional = getattr(message, "additional_kwargs", {}) or {}
            receipt = additional.get("oraiflow_frontend_action_receipt")
            if (
                isinstance(receipt, dict)
                and receipt.get("capability_id") == "frontend.workspace.navigate"
                and receipt.get("status") in {"opened", "SUCCESS"}
            ):
                return True
        elif message_type == "tool" and getattr(message, "name", "") == "navigate_workspace":
            # Any receipt for the same turn consumes the action, including a
            # rejected one. Retrying requires a new customer instruction.
            return True
    return False


def navigation_confirmation_metadata(workspace: str, navigation_epoch: int = 0) -> dict[str, Any]:
    """Metadata attached only after a validated, allowlisted navigation proposal."""
    return {
        "version": 1,
        "action": "frontend.workspace.navigate",
        "workspace": workspace,
        "status": "AWAITING_CONFIRMATION",
        "action_id": f"nav-{uuid.uuid4().hex}",
        "navigation_epoch": max(0, int(navigation_epoch)),
    }


def _latest_pending_navigation(messages: list[Any]) -> dict[str, Any] | None:
    task = latest_pending_task(messages, domain="navigation")
    if task:
        return {
            **dict(task.state),
            "stage": task.stage,
            "status": task.status,
            "task_id": task.task_id,
        }
    # Once the current customer turn has any assistant response, older legacy
    # confirmation metadata is stale. In particular, a completed typed
    # NAVIGATION_REQUESTED task must consume the old confirmation, not revive it
    # on a subsequent AG-UI tool-result re-entry.
    items = list(messages or [])
    latest_human = max(
        (index for index, message in enumerate(items) if getattr(message, "type", "") == "human"),
        default=len(items),
    )
    if any(getattr(message, "type", "") == "ai" for message in items[latest_human + 1 :]):
        return None
    # Read-only compatibility for navigation confirmations saved before the
    # shared PendingTaskContext envelope was introduced.
    for message in reversed(items[:latest_human]):
        if getattr(message, "type", "") != "ai":
            continue
        additional = getattr(message, "additional_kwargs", {}) or {}
        pending = additional.get(PENDING_NAVIGATION_KEY)
        if not isinstance(pending, dict):
            return None
        if pending.get("status") != "AWAITING_CONFIRMATION":
            return None
        return pending
    return None


def resolve_pending_navigation_confirmation(
    messages: list[Any],
    current_text: str,
    available_actions: set[str],
) -> dict[str, Any] | None:
    """Resolve yes/no only against the immediately preceding pending UI action.

    Returns an EXECUTE, CANCELLED or UNAVAILABLE decision. Unrelated replies
    return None and proceed through normal LLM interpretation.
    """
    pending = _latest_pending_navigation(messages or [])
    if not pending or pending.get("stage", "AWAITING_CONFIRMATION") != "AWAITING_CONFIRMATION":
        return None
    text = re.sub(r"\s+", " ", str(current_text or "")).strip()
    if _NEGATIVE.fullmatch(text):
        return {
            "kind": "CANCELLED",
            "workspace": pending.get("workspace", ""),
            "action_id": pending.get("action_id", ""),
            "navigation_epoch": pending.get("navigation_epoch", 0),
        }
    if not _AFFIRMATIVE.fullmatch(text):
        return None

    workspace = str(pending.get("workspace", ""))
    pages = set(get_registry().get("application_policy", {}).get("navigation", {}).get("pages", []))
    registered = get_registry().get("capabilities", {}).get("frontend.workspace.navigate", {})
    action_name = str(registered.get("name", ""))
    if (
        pending.get("action") != "frontend.workspace.navigate"
        or workspace not in pages
        or not action_name
        or action_name not in available_actions
    ):
        return {
            "kind": "UNAVAILABLE",
            "workspace": workspace,
            "action_id": pending.get("action_id", ""),
            "navigation_epoch": pending.get("navigation_epoch", 0),
        }
    return {
        "kind": "EXECUTE",
        "workspace": workspace,
        "action_name": action_name,
        "action_id": pending.get("action_id") or f"nav-{uuid.uuid4().hex}",
        "navigation_epoch": pending.get("navigation_epoch", 0),
    }
