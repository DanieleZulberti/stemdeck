from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diagnostics import Diagnostic, DiagnosticSeverity, DiagnosticSource
from .registry import ModelRegistry
from .schema import FlowDefinition, SemanticValidationMode
from .semantic_types import is_semantically_compatible, resolve_semantic_type


class ValidationIssue(Diagnostic):
    """Validation diagnostic with the legacy constructor kept for PoC callers."""

    def __init__(
        self,
        level: str,
        code: str,
        message: str,
        *,
        node_id: str | None = None,
        port_id: str | None = None,
        output_id: str | None = None,
        parameter_id: str | None = None,
    ) -> None:
        super().__init__(
            source=DiagnosticSource.VALIDATOR,
            severity=DiagnosticSeverity(level),
            code=code,
            message=message,
            node_id=node_id,
            port_id=port_id,
            output_id=output_id,
            parameter_id=parameter_id,
        )


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def valid(self) -> bool:
        return not self.errors


class FlowValidator:
    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def validate(self, flow: FlowDefinition) -> ValidationResult:
        result = ValidationResult()
        self._validate_declared_semantic_types(flow, result)
        self._validate_models(flow, result)
        self._validate_node_inputs(flow, result)
        self._validate_parameters(flow, result)
        self._validate_flow_outputs(flow, result)
        self._validate_cycles(flow, result)
        return result

    def _validate_declared_semantic_types(
        self,
        flow: FlowDefinition,
        result: ValidationResult,
    ) -> None:
        for input_name, definition in flow.inputs.items():
            try:
                resolve_semantic_type(definition.semantic_type)
            except ValueError as exc:
                result.issues.append(
                    ValidationIssue(
                        "error",
                        "unknown_semantic_type",
                        f"Input '{input_name}': {exc}",
                        port_id=input_name,
                    )
                )

        for node_id, node in flow.nodes.items():
            if node.model not in self.registry:
                continue
            model = self.registry.get(node.model)
            for port_name, port in {**model.inputs, **model.outputs}.items():
                try:
                    resolve_semantic_type(port.semantic_type)
                except ValueError as exc:
                    result.issues.append(
                        ValidationIssue(
                            "error",
                            "unknown_semantic_type",
                            f"Model '{node.model}' port '{port_name}': {exc}",
                            node_id=node_id,
                            port_id=port_name,
                        )
                    )

    def _validate_models(self, flow: FlowDefinition, result: ValidationResult) -> None:
        for node_name, node in flow.nodes.items():
            if node.model not in self.registry:
                result.issues.append(
                    ValidationIssue(
                        "error",
                        "unknown_model",
                        f"Node '{node_name}' references unknown model '{node.model}'",
                        node_id=node_name,
                    )
                )

    def _resolve_source(self, flow: FlowDefinition, ref: str) -> tuple[str, str] | None:
        parts = ref.split(".")
        if len(parts) != 2:
            return None
        owner, port = parts
        if owner == "inputs":
            if port not in flow.inputs:
                return None
            return flow.inputs[port].type, flow.inputs[port].semantic_type
        if owner not in flow.nodes:
            return None
        node = flow.nodes[owner]
        if node.model not in self.registry:
            return None
        model = self.registry.get(node.model)
        if port not in model.outputs:
            return None
        output = model.outputs[port]
        return output.type, output.semantic_type

    def _validate_node_inputs(self, flow: FlowDefinition, result: ValidationResult) -> None:
        for node_name, node in flow.nodes.items():
            if node.model not in self.registry:
                continue
            model = self.registry.get(node.model)

            for expected_port, port_def in model.inputs.items():
                if port_def.required and expected_port not in node.inputs:
                    result.issues.append(
                        ValidationIssue(
                            "error",
                            "missing_input",
                            f"Node '{node_name}' is missing required input '{expected_port}'",
                            node_id=node_name,
                            port_id=expected_port,
                        )
                    )

            for input_port, source_ref in node.inputs.items():
                if input_port not in model.inputs:
                    result.issues.append(
                        ValidationIssue(
                            "error",
                            "unknown_input_port",
                            f"Node '{node_name}' has unknown input port '{input_port}'",
                            node_id=node_name,
                            port_id=input_port,
                        )
                    )
                    continue
                resolved = self._resolve_source(flow, source_ref)
                if not resolved:
                    result.issues.append(
                        ValidationIssue(
                            "error",
                            "invalid_source",
                            f"Node '{node_name}' input '{input_port}' references "
                            f"invalid source '{source_ref}'",
                            node_id=node_name,
                            port_id=input_port,
                        )
                    )
                    continue

                actual_type, actual_semantic = resolved
                expected = model.inputs[input_port]
                if actual_type != expected.type:
                    result.issues.append(
                        ValidationIssue(
                            "error",
                            "technical_type_mismatch",
                            f"Node '{node_name}' input '{input_port}': got {actual_type}, "
                            f"expected {expected.type}",
                            node_id=node_name,
                            port_id=input_port,
                        )
                    )
                    continue

                mode = flow.validation.semantic
                compatible = is_semantically_compatible(
                    actual_semantic,
                    expected.semantic_type,
                )
                if mode != SemanticValidationMode.OFF and not compatible:
                    level = "error" if mode == SemanticValidationMode.STRICT else "warning"
                    result.issues.append(
                        ValidationIssue(
                            level,
                            "semantic_type_mismatch",
                            f"Node '{node_name}' input '{input_port}': '{actual_semantic}' "
                            f"is not compatible with '{expected.semantic_type}'",
                            node_id=node_name,
                            port_id=input_port,
                        )
                    )

    def _validate_parameters(self, flow: FlowDefinition, result: ValidationResult) -> None:
        for node_name, node in flow.nodes.items():
            if node.model not in self.registry:
                continue
            model = self.registry.get(node.model)
            for param_name in node.params:
                if param_name not in model.parameters:
                    result.issues.append(
                        ValidationIssue(
                            "error",
                            "unknown_parameter",
                            f"Node '{node_name}' has unknown parameter '{param_name}'",
                            node_id=node_name,
                            parameter_id=param_name,
                        )
                    )

            for param_name, definition in model.parameters.items():
                value = node.params.get(param_name, definition.default)
                if value is None:
                    if definition.required:
                        result.issues.append(
                            ValidationIssue(
                                "error",
                                "missing_parameter",
                                f"Node '{node_name}' is missing required parameter "
                                f"'{param_name}'",
                                node_id=node_name,
                                parameter_id=param_name,
                            )
                        )
                    continue
                error = _validate_parameter_value(param_name, value, definition)
                if error:
                    result.issues.append(
                        ValidationIssue(
                            "error",
                            "invalid_parameter",
                            f"Node '{node_name}': {error}",
                            node_id=node_name,
                            parameter_id=param_name,
                        )
                    )

    def _validate_flow_outputs(self, flow: FlowDefinition, result: ValidationResult) -> None:
        for output_name, definition in flow.outputs.items():
            if not self._resolve_source(flow, definition.source):
                result.issues.append(
                    ValidationIssue(
                        "error",
                        "invalid_flow_output",
                        f"Flow output '{output_name}' references invalid source "
                        f"'{definition.source}'",
                        output_id=output_name,
                    )
                )

    def _dependencies(self, flow: FlowDefinition) -> dict[str, set[str]]:
        deps = {name: set() for name in flow.nodes}
        for node_name, node in flow.nodes.items():
            for source in node.inputs.values():
                owner = source.split(".", 1)[0]
                if owner in flow.nodes:
                    deps[node_name].add(owner)
        return deps

    def _validate_cycles(self, flow: FlowDefinition, result: ValidationResult) -> None:
        deps = self._dependencies(flow)
        remaining = {name: set(values) for name, values in deps.items()}
        resolved: set[str] = set()
        while remaining:
            ready = {name for name, values in remaining.items() if values <= resolved}
            if not ready:
                result.issues.append(
                    ValidationIssue(
                        "error",
                        "cycle",
                        f"Flow contains a cycle involving: {sorted(remaining)}",
                    )
                )
                return
            resolved |= ready
            for name in ready:
                remaining.pop(name)


def _validate_parameter_value(name: str, value: Any, definition: Any) -> str | None:
    expected = definition.type
    if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"parameter '{name}' must be an integer"
    if expected == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        return f"parameter '{name}' must be a number"
    if expected == "boolean" and not isinstance(value, bool):
        return f"parameter '{name}' must be a boolean"
    if expected == "string" and not isinstance(value, str):
        return f"parameter '{name}' must be a string"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if definition.minimum is not None and value < definition.minimum:
            return f"parameter '{name}' must be >= {definition.minimum}"
        if definition.maximum is not None and value > definition.maximum:
            return f"parameter '{name}' must be <= {definition.maximum}"
    if definition.choices is not None and value not in definition.choices:
        return f"parameter '{name}' must be one of {definition.choices}"
    return None
