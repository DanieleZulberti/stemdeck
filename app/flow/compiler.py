from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from .diagnostics import Diagnostic, DiagnosticSeverity, DiagnosticSource
from .schema import FlowDefinition, FlowDocument


@dataclass(frozen=True)
class CompileResult:
    definition: FlowDefinition | None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def compiled(self) -> bool:
        return self.definition is not None and not self.errors

    @property
    def errors(self) -> list[Diagnostic]:
        return [
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.severity == DiagnosticSeverity.ERROR
        ]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.severity == DiagnosticSeverity.WARNING
        ]


class FlowCompiler:
    """Compile a permissive FlowDocument into the strict runtime model."""

    def compile(self, document: FlowDocument) -> CompileResult:
        diagnostics: list[Diagnostic] = []

        runtime_inputs: dict[str, dict] = {}
        for input_id, input_document in document.inputs.items():
            if not isinstance(input_document.semantic_type, str) or not input_document.semantic_type:
                diagnostics.append(
                    self._error(
                        "missing_input_semantic_type",
                        f"Flow input '{input_id}' has no semantic type",
                        port_id=input_id,
                    )
                )
                continue
            if not isinstance(input_document.type, str) or not input_document.type:
                diagnostics.append(
                    self._error(
                        "missing_input_artifact_type",
                        f"Flow input '{input_id}' has no artifact type",
                        port_id=input_id,
                    )
                )
                continue
            runtime_inputs[input_id] = {
                "type": input_document.type,
                "semantic_type": input_document.semantic_type,
            }

        runtime_nodes: dict[str, dict] = {}
        for node_id, node_document in document.nodes.items():
            definition_id = node_document.definition_id
            if not isinstance(definition_id, str) or not definition_id:
                diagnostics.append(
                    self._error(
                        "missing_node_definition",
                        f"Node '{node_id}' has no node definition",
                        node_id=node_id,
                    )
                )
                continue

            bindings: dict[str, str] = {}
            for port_id, source in node_document.inputs.items():
                # An empty editor binding is equivalent to an omitted binding.
                # Required-port validation belongs to FlowValidator where the
                # NodeDefinition contract is available.
                if source is None or source == "":
                    continue
                if not isinstance(source, str):
                    diagnostics.append(
                        self._error(
                            "invalid_input_binding",
                            f"Node '{node_id}' input '{port_id}' has an invalid source reference",
                            node_id=node_id,
                            port_id=port_id,
                        )
                    )
                    continue
                bindings[port_id] = source

            runtime_nodes[node_id] = {
                "definition_id": definition_id,
                "inputs": bindings,
                "params": dict(node_document.params),
                "outputs": {
                    output_id: policy.model_dump(exclude_none=True)
                    for output_id, policy in node_document.outputs.items()
                },
            }

        runtime_outputs: dict[str, dict] = {}
        for output_id, output_document in document.outputs.items():
            source = output_document.source
            if not isinstance(source, str) or not source:
                diagnostics.append(
                    self._error(
                        "missing_flow_output_source",
                        f"Flow output '{output_id}' has no source",
                        output_id=output_id,
                    )
                )
                continue
            runtime_outputs[output_id] = {
                "source": source,
                "label": output_document.label if isinstance(output_document.label, str) and output_document.label.strip() else None,
                "publish": output_document.publish,
            }

        if diagnostics:
            return CompileResult(definition=None, diagnostics=diagnostics)

        payload = {
            "flow_id": document.flow_id,
            "name": document.name,
            "revision": document.revision,
            "schema_version": document.schema_version,
            "inputs": runtime_inputs,
            "nodes": runtime_nodes,
            "outputs": runtime_outputs,
            "validation": document.validation.model_dump(),
        }
        try:
            definition = FlowDefinition.model_validate(payload)
        except ValidationError as exc:
            diagnostics.extend(self._pydantic_diagnostics(exc))
            return CompileResult(definition=None, diagnostics=diagnostics)

        return CompileResult(definition=definition, diagnostics=diagnostics)

    @staticmethod
    def _error(
        code: str,
        message: str,
        *,
        node_id: str | None = None,
        port_id: str | None = None,
        output_id: str | None = None,
    ) -> Diagnostic:
        return Diagnostic(
            source=DiagnosticSource.COMPILER,
            severity=DiagnosticSeverity.ERROR,
            code=code,
            message=message,
            node_id=node_id,
            port_id=port_id,
            output_id=output_id,
        )

    def _pydantic_diagnostics(self, exc: ValidationError) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for error in exc.errors(include_url=False):
            location = tuple(str(part) for part in error.get("loc", ()))
            node_id = None
            port_id = None
            output_id = None
            if len(location) >= 2 and location[0] == "nodes":
                node_id = location[1]
                if len(location) >= 4 and location[2] == "inputs":
                    port_id = location[3]
            elif len(location) >= 2 and location[0] == "outputs":
                output_id = location[1]
            elif len(location) >= 2 and location[0] == "inputs":
                port_id = location[1]

            diagnostics.append(
                self._error(
                    "invalid_runtime_definition",
                    f"Cannot compile {'.'.join(location) or 'flow'}: {error['msg']}",
                    node_id=node_id,
                    port_id=port_id,
                    output_id=output_id,
                )
            )
        return diagnostics
