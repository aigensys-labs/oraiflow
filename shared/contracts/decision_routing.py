"""Validated, human-editable ORAIflow decision-routing policy.

This policy configures route ordering and task handling. It deliberately does
not duplicate OCCF capability schemas or grant execution/financial authority.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_POLICY_FILE = Path(__file__).with_name("decision_routing.json")


class PreAssessmentRoute(BaseModel):
    """Declarative route considered before model-backed task interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    priority: int = Field(ge=0, le=1000)
    enabled: bool = True
    handler: Literal[
        "academy_personal_read",
        "workspace_navigation",
        "workspace_knowledge",
        "academy_education",
        "connector_failure",
        "canonical_read",
        "assistant_capabilities",
        "simple_social",
    ]
    purpose: str = Field(min_length=1, max_length=400)


class TaskDispatchRoute(BaseModel):
    """Declarative route from a resolved task class to its registered execution owner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_class: Literal[
        "resource", "mcp_read", "presentation", "workflow",
        "frontend_action", "clarification", "unsupported",
    ]
    handler: Literal[
        "registered_resource", "occf_read", "registered_presentation",
        "registered_workflow", "registered_frontend_action",
        "preserve_and_clarify", "explain_supported_scope",
    ]
    applicable_confidence_dimensions: list[Literal["intent", "slot", "entity"]] = Field(default_factory=lambda: ["intent"])
    low_confidence_behavior: Literal[
        "clarify_resource_or_topic", "clarify_missing_read_scope",
        "clarify_missing_presentation_inputs", "preserve_workflow_and_clarify",
        "clarify_action_target", "preserve_task_and_clarify", "offer_supported_scope",
    ]
    purpose: str = Field(min_length=1, max_length=500)


class InterpreterPolicy(BaseModel):
    """Policy governing when and how model interpretation may be used."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment_calls_per_new_turn: Literal[1] = 1
    format_recovery_calls: int = Field(default=1, ge=0, le=1)
    may_select_capabilities: Literal[False] = False
    may_select_tools: Literal[False] = False
    may_authorize_actions: Literal[False] = False
    must_receive_decision_map: Literal[True] = True
    must_preserve_all_supported_tasks: Literal[True] = True
    must_not_treat_empty_candidate_shortlist_as_out_of_scope: Literal[True] = True


class ConfidencePolicy(BaseModel):
    """Thresholds and fallback rules for interpretation confidence assessment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    configuration_source: Literal["ORAI_CONFIDENCE_GATE_MODE"]
    default_mode: Literal["observe"] = "observe"
    observe_behavior: str = Field(min_length=1, max_length=300)
    enforce_behavior: str = Field(min_length=1, max_length=300)
    low_confidence_behavior: Literal["clarify_targeted_missing_information"]
    confidence_grants_authority: Literal[False] = False


class DecisionRoutingPolicy(BaseModel):
    """Validated configuration for deterministic pre-assessment and task dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"]
    policy_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=500)
    pre_assessment_routes: list[PreAssessmentRoute] = Field(min_length=1, max_length=20)
    task_dispatch: list[TaskDispatchRoute] = Field(min_length=7, max_length=7)
    interpreter: InterpreterPolicy
    confidence: ConfidencePolicy

    @model_validator(mode="after")
    def validate_route_integrity(self) -> "DecisionRoutingPolicy":
        """Validate route identifiers, ordering and referenced task classes."""
        route_ids = [route.id for route in self.pre_assessment_routes]
        priorities = [route.priority for route in self.pre_assessment_routes]
        handlers = [route.handler for route in self.pre_assessment_routes]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("pre_assessment route IDs must be unique")
        if len(priorities) != len(set(priorities)):
            raise ValueError("pre_assessment priorities must be unique")
        if len(handlers) != len(set(handlers)):
            raise ValueError("pre_assessment handlers must be unique")
        if priorities != sorted(priorities, reverse=True):
            raise ValueError("pre_assessment_routes must be ordered by descending priority")

        task_classes = [route.task_class for route in self.task_dispatch]
        if len(task_classes) != len(set(task_classes)):
            raise ValueError("task_dispatch must define each task class exactly once")
        for route in self.task_dispatch:
            dimensions = route.applicable_confidence_dimensions
            if len(dimensions) != len(set(dimensions)):
                raise ValueError(f"{route.task_class} confidence dimensions must be unique")
            if "intent" not in dimensions and route.task_class not in {"clarification", "unsupported"}:
                raise ValueError(f"{route.task_class} must assess intent confidence")
        expected = {
            "resource", "mcp_read", "presentation", "workflow",
            "frontend_action", "clarification", "unsupported",
        }
        if set(task_classes) != expected:
            raise ValueError("task_dispatch must cover every supported task class")
        expected_handlers = {
            "resource": "registered_resource",
            "mcp_read": "occf_read",
            "presentation": "registered_presentation",
            "workflow": "registered_workflow",
            "frontend_action": "registered_frontend_action",
            "clarification": "preserve_and_clarify",
            "unsupported": "explain_supported_scope",
        }
        for route in self.task_dispatch:
            if route.handler != expected_handlers[route.task_class]:
                raise ValueError(f"{route.task_class} must use its registered runtime owner {expected_handlers[route.task_class]}")
        return self


@lru_cache(maxsize=1)
def load_decision_routing_policy() -> DecisionRoutingPolicy:
    """Load and strictly validate the human-editable routing policy."""
    with _POLICY_FILE.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    return DecisionRoutingPolicy.model_validate(payload)


def ordered_pre_assessment_routes(policy: DecisionRoutingPolicy | None = None) -> tuple[PreAssessmentRoute, ...]:
    """Return enabled pre-assessment routes in their declared evaluation order."""
    policy = policy or load_decision_routing_policy()
    return tuple(route for route in policy.pre_assessment_routes if route.enabled)


def task_dispatch_for_class(
    task_class: str, policy: DecisionRoutingPolicy | None = None,
) -> TaskDispatchRoute | None:
    """Return the registered dispatch route for a resolved task class."""
    policy = policy or load_decision_routing_policy()
    return next((item for item in policy.task_dispatch if item.task_class == task_class), None)
