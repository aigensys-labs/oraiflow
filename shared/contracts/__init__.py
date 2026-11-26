"""ORAIflow's canonical, versioned capability-contract framework."""

from .registry import (
    ContractViolation,
    capability_by_name,
    create_result_envelope,
    get_capability,
    get_kernel_operation,
    get_presentation,
    get_registry,
    parse_result_envelope,
    validate_capability_input,
    validate_capability_output,
    validate_kernel_input,
    validate_kernel_output,
    validate_presentation_props,
)

__all__ = [
    "ContractViolation",
    "capability_by_name",
    "create_result_envelope",
    "get_capability",
    "get_kernel_operation",
    "get_presentation",
    "get_registry",
    "parse_result_envelope",
    "validate_capability_input",
    "validate_capability_output",
    "validate_kernel_input",
    "validate_kernel_output",
    "validate_presentation_props",
]
